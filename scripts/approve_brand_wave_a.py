"""Wave A brand approvals + backfill on mumzworld_v3_sample.

Approves only:  star babies, party centre.
Then runs a brand backfill: one ProductAttribute(brand) row per product
whose events resolve into the active AAV set. Idempotent.

No force=true. No merges. No recommendations. No other approvals.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
)
from app.models.workspace import Workspace
from app.services.proposed_attribute_value_service import promotion_readiness

WS_SLUG = "mumzworld_v3_sample"
ATTR = "brand"
APPROVE_TARGETS = ["star babies", "party centre"]
client = TestClient(app)


def status_counts(db, ws_id, attribute):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == attribute
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_counts(db, ws_id, attribute):
    active = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == attribute,
        AttributeAllowedValue.is_active == True).count()
    total = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == attribute).count()
    return active, total


def coverage(db, ws_id, attribute):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_attr = (db.query(ProductAttribute.product_id)
                 .join(Product, ProductAttribute.product_id == Product.id)
                 .filter(Product.workspace_id == ws_id,
                         ProductAttribute.attribute_id == attribute)
                 .distinct().count())
    return total, with_attr


def run_brand_backfill(db, ws_id) -> tuple[int, Counter]:
    """One PA(brand) row per product whose highest-confidence event
    resolves into the active brand AAV set. Skip products with an
    existing brand PA. Idempotent."""
    active_aav = {
        v for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.is_active == True,
        ).all()
    }
    active_lower = {v.lower() for v in active_aav}
    cluster_to_canonical: dict[str, str] = {}
    for agg in db.query(A).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        if agg.status == PROPOSAL_STATUS_APPROVED:
            canonical = agg.canonical_value
        elif agg.status == PROPOSAL_STATUS_MERGED:
            canonical = agg.promoted_to_allowed_value
        else:
            continue
        if canonical and canonical.lower() in active_lower:
            cluster_to_canonical[agg.cluster_key] = next(
                (v for v in active_aav if v.lower() == canonical.lower()),
                canonical,
            )

    products = db.query(Product).filter(Product.workspace_id == ws_id).all()
    already = {
        pid for (pid,) in db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == ws_id,
                ProductAttribute.attribute_id == ATTR)
        .distinct().all()
    }
    events_by_pid: dict[str, list] = defaultdict(list)
    for ev in db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR
    ).all():
        events_by_pid[ev.product_id].append(ev)

    inserted = 0
    by_canonical: Counter = Counter()
    for prod in products:
        if prod.id in already:
            continue
        evs = events_by_pid.get(prod.product_id, [])
        if not evs:
            continue
        assignable = []
        for ev in evs:
            canonical = cluster_to_canonical.get(ev.normalized_value)
            if canonical is None:
                continue
            assignable.append((ev, canonical))
        if not assignable:
            continue
        ev, canonical = max(
            assignable,
            key=lambda x: (float(x[0].confidence or 0), x[0].created_at),
        )
        db.add(ProductAttribute(
            product_id=prod.id, attribute_id=ATTR, attribute_value=canonical,
        ))
        inserted += 1
        by_canonical[canonical] += 1
    if inserted:
        db.commit()
    return inserted, by_canonical


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id
        before = {
            "agg_brand_status": status_counts(db, ws_id, ATTR),
            "agg_pt_status": status_counts(db, ws_id, "product_type"),
            "agg_age_status": status_counts(db, ws_id, "age_group"),
            "aav_brand_active": aav_counts(db, ws_id, ATTR)[0],
            "aav_pt_active": aav_counts(db, ws_id, "product_type")[0],
            "aav_age_active": aav_counts(db, ws_id, "age_group")[0],
            "events_brand": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "events_total": db.query(E).filter(E.workspace_id == ws_id).count(),
            "pa_brand": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "pa_pt": (db.query(ProductAttribute)
                      .join(Product, ProductAttribute.product_id == Product.id)
                      .filter(Product.workspace_id == ws_id,
                              ProductAttribute.attribute_id == "product_type").count()),
            "pa_age": (db.query(ProductAttribute)
                       .join(Product, ProductAttribute.product_id == Product.id)
                       .filter(Product.workspace_id == ws_id,
                               ProductAttribute.attribute_id == "age_group").count()),
        }
        # Locate the two aggregates.
        agg_by_cluster: dict[str, A] = {}
        for ck in APPROVE_TARGETS:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg is None:
                raise SystemExit(f"aggregate not found: {ck!r}")
            check = promotion_readiness(agg)
            if not check.ready:
                raise SystemExit(
                    f"{ck!r} not ready ({check.reasons}); aborting"
                )
            if agg.status != "pending":
                raise SystemExit(
                    f"{ck!r} not pending (status={agg.status}); aborting"
                )
            agg_by_cluster[ck] = agg
            print(f"  pre-check  {ck:<14} agg.id={agg.id} count={agg.proposal_count} "
                  f"distinct={agg.distinct_product_count} avg_conf={agg.avg_confidence:.3f}")
    finally:
        db.close()

    print()
    print("=" * 72)
    print(f"PRE-APPROVAL  (ws_id={ws_id}, attribute={ATTR})")
    print("=" * 72)
    print(f"  brand aggregates by status   : {before['agg_brand_status']}")
    print(f"  brand AAV active             : {before['aav_brand_active']}")
    print(f"  brand events                 : {before['events_brand']}")
    print(f"  brand PA rows                : {before['pa_brand']}")

    # ----- Approvals -----
    print()
    print("=" * 72)
    print("APPROVALS")
    print("=" * 72)
    for ck, agg in agg_by_cluster.items():
        r = client.post(
            f"/admin/taxonomy/api/aggregates/{agg.id}/approve",
            params={"workspace_id": ws_id, "attribute": ATTR},
        )
        if r.status_code != 200:
            print(f"  {ck:<14} HTTP {r.status_code}  body={r.text[:160]}")
            raise SystemExit(f"approval for {ck} failed")
        body = r.json()
        print(f"  {ck:<14} agg.id={agg.id}  HTTP 200  -> "
              f"status={body['status']} promoted_to={body['promoted_to_allowed_value']!r}")

    # ----- Backfill -----
    print()
    print("=" * 72)
    print("BACKFILL")
    print("=" * 72)
    db = SessionLocal()
    try:
        inserted, by_canonical = run_brand_backfill(db, ws_id)
    finally:
        db.close()
    print(f"  PA(brand) rows inserted : {inserted}")
    for v, n in sorted(by_canonical.items(), key=lambda x: -x[1]):
        print(f"    {v:<22} {n}")

    # ----- Sample products with brand assigned -----
    print()
    print("=" * 72)
    print("5 SAMPLE PRODUCTS WITH BRAND ASSIGNED")
    print("=" * 72)
    db = SessionLocal()
    try:
        sample_rows = (db.query(ProductAttribute, Product)
                       .join(Product, ProductAttribute.product_id == Product.id)
                       .filter(Product.workspace_id == ws_id,
                               ProductAttribute.attribute_id == ATTR)
                       .order_by(Product.id)
                       .limit(5).all())
        for pa, prod in sample_rows:
            name = (prod.name or "")[:64]
            print(f"  {prod.product_id:<28} brand={pa.attribute_value!r:<18} name={name!r}")

        # Per-product max (must be 1).
        per_product = Counter(
            pa.product_id for pa in db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )
        max_per_product = max(per_product.values()) if per_product else 0
    finally:
        db.close()

    # ----- Coverage + final state -----
    db = SessionLocal()
    try:
        total, with_brand = coverage(db, ws_id, ATTR)
        after = {
            "agg_brand_status": status_counts(db, ws_id, ATTR),
            "agg_pt_status": status_counts(db, ws_id, "product_type"),
            "agg_age_status": status_counts(db, ws_id, "age_group"),
            "aav_brand_active": aav_counts(db, ws_id, ATTR)[0],
            "aav_pt_active": aav_counts(db, ws_id, "product_type")[0],
            "aav_age_active": aav_counts(db, ws_id, "age_group")[0],
            "events_brand": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "events_total": db.query(E).filter(E.workspace_id == ws_id).count(),
            "pa_brand": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "pa_pt": (db.query(ProductAttribute)
                      .join(Product, ProductAttribute.product_id == Product.id)
                      .filter(Product.workspace_id == ws_id,
                              ProductAttribute.attribute_id == "product_type").count()),
            "pa_age": (db.query(ProductAttribute)
                       .join(Product, ProductAttribute.product_id == Product.id)
                       .filter(Product.workspace_id == ws_id,
                               ProductAttribute.attribute_id == "age_group").count()),
        }
    finally:
        db.close()

    print()
    print("=" * 72)
    print("BRAND COVERAGE AFTER BACKFILL")
    print("=" * 72)
    print(f"  products with brand          : {with_brand}/{total} = "
          f"{100*with_brand/max(total,1):.2f}%")
    print(f"  max brand rows per product   : {max_per_product}  (must be 1)")

    print()
    print("=" * 72)
    print("DELTAS")
    print("=" * 72)
    print(f"  brand pending -> approved    : "
          f"{before['agg_brand_status'].get('pending',0)} -> {after['agg_brand_status'].get('pending',0)} pending; "
          f"{before['agg_brand_status'].get('approved',0)} -> {after['agg_brand_status'].get('approved',0)} approved")
    print(f"  brand AAV active             : {before['aav_brand_active']} -> {after['aav_brand_active']}  "
          f"(diff=+{after['aav_brand_active'] - before['aav_brand_active']})")
    print(f"  brand PA rows                : {before['pa_brand']} -> {after['pa_brand']}  "
          f"(diff=+{after['pa_brand'] - before['pa_brand']})")
    print(f"  brand events                 : {before['events_brand']} -> {after['events_brand']}  "
          f"(unchanged: {before['events_brand'] == after['events_brand']})")

    # ----- Confirmations -----
    print()
    print("=" * 72)
    print("CONFIRMATIONS  (no other data modified)")
    print("=" * 72)
    def cmp(label, b, a):
        ok = b == a
        marker = "OK  " if ok else "FAIL"
        print(f"  {marker}  {label:<32} before={b}  after={a}")
    cmp("product_type aggregates",       before["agg_pt_status"],   after["agg_pt_status"])
    cmp("age_group   aggregates",        before["agg_age_status"],  after["agg_age_status"])
    cmp("AAV active product_type",       before["aav_pt_active"],   after["aav_pt_active"])
    cmp("AAV active age_group",          before["aav_age_active"],  after["aav_age_active"])
    cmp("PA rows product_type",          before["pa_pt"],           after["pa_pt"])
    cmp("PA rows age_group",             before["pa_age"],          after["pa_age"])
    cmp("brand events (preserved)",      before["events_brand"],    after["events_brand"])
    cmp("total events (preserved)",      before["events_total"],    after["events_total"])
    print()
    print("  no merges executed.")
    print("  no recommendations re-run.")
    print("  no force=true used.")
    print("  no scoring weights modified.")
    print("  no engine code modified.")
    print("  one brand per product enforced (max-per-product check above).")


if __name__ == "__main__":
    main()
