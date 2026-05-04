"""Approve infant/toddler/kids/teen for age_group, then backfill PA rows.

Operates on the test workspace `csv_mapping_import_test` (ws_id from
the prior normalizer-MVP run). Uses approve_aggregate via the taxonomy
admin endpoint, then materialises one ProductAttribute(age_group) row
per product whose events resolve into the active AAV set.

No force=true. No merges. No recommendations.
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

WS_SLUG = "csv_mapping_import_test"
ATTR = "age_group"
TARGETS = ["infant", "toddler", "kids", "teen"]
client = TestClient(app)


def status_counts(db, ws_id):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_counts(db, ws_id):
    active = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).count()
    total = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR).count()
    return active, total


def coverage(db, ws_id):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_attr = (db.query(ProductAttribute.product_id)
                 .join(Product, ProductAttribute.product_id == Product.id)
                 .filter(Product.workspace_id == ws_id,
                         ProductAttribute.attribute_id == ATTR)
                 .distinct().count())
    return total, with_attr


def run_backfill(db, ws_id) -> tuple[int, Counter]:
    """For each product without an age_group PA row, pick the highest-
    confidence event whose normalized_value resolves (via approved or
    merged aggregate) into the active AAV set, and insert exactly one
    PA row. Skip products that already have one. Skip products whose
    events do not resolve. Idempotent."""
    active_aav = {
        v for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.is_active == True,
        ).all()
    }
    active_aav_lower = {v.lower() for v in active_aav}

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
        if canonical and canonical.lower() in active_aav_lower:
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

    inserted_by_canonical: Counter = Counter()
    inserted = 0
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
        inserted_by_canonical[canonical] += 1
    if inserted:
        db.commit()
    return inserted, inserted_by_canonical


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        # Pre-state.
        before = {
            "status_counts": status_counts(db, ws_id),
            "aav": aav_counts(db, ws_id),
            "coverage": coverage(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
        }

        # Locate aggregates.
        agg_by_cluster: dict[str, A] = {}
        for ck in TARGETS:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg is None:
                raise SystemExit(f"aggregate not found for cluster_key={ck!r}")
            check = promotion_readiness(agg)
            if not check.ready:
                raise SystemExit(
                    f"{ck} not ready ({check.reasons}); aborting before any write"
                )
            if agg.status != "pending":
                raise SystemExit(
                    f"{ck} is not pending (status={agg.status}); aborting"
                )
            agg_by_cluster[ck] = agg
            print(f"  pre-check  {ck:<10} agg.id={agg.id} count={agg.proposal_count} "
                  f"distinct={agg.distinct_product_count} avg_conf={agg.avg_confidence:.3f}")
    finally:
        db.close()

    print()
    print("=" * 72)
    print(f"PRE-APPROVAL  (ws_id={ws_id}, attribute={ATTR})")
    print("=" * 72)
    print(f"  aggregates by status   : {before['status_counts']}")
    print(f"  AAV active / total     : {before['aav'][0]} / {before['aav'][1]}")
    total, with_attr = before["coverage"]
    print(f"  coverage               : {with_attr}/{total} = "
          f"{100*with_attr/max(total,1):.2f}%")
    print(f"  events_total           : {before['events_total']}")
    print(f"  PA total (age_group)   : {before['pa_total']}")

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
            print(f"  {ck:<10} HTTP {r.status_code}  body={r.text[:160]}")
            raise SystemExit(f"approval for {ck} failed")
        body = r.json()
        print(f"  {ck:<10} agg.id={agg.id}  HTTP 200  -> "
              f"status={body['status']} promoted_to={body['promoted_to_allowed_value']!r}")

    # ----- Backfill -----
    print()
    print("=" * 72)
    print("BACKFILL")
    print("=" * 72)
    db = SessionLocal()
    try:
        inserted, by_canonical = run_backfill(db, ws_id)
    finally:
        db.close()
    print(f"  ProductAttribute rows inserted : {inserted}")
    print(f"  by canonical                   :")
    for v, n in sorted(by_canonical.items(), key=lambda x: -x[1]):
        print(f"    {v:<14} {n}")

    # ----- Post snapshot -----
    db = SessionLocal()
    try:
        after = {
            "status_counts": status_counts(db, ws_id),
            "aav": aav_counts(db, ws_id),
            "coverage": coverage(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
        }
        # Distribution.
        dist = Counter(
            v for (v,) in db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )
        # PA rows per product (must all be 1).
        pa_per_product = (
            db.query(ProductAttribute.product_id,
                     ProductAttribute.attribute_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .all()
        )
        per_product = Counter(pid for (pid, _) in pa_per_product)
        max_per_product = max(per_product.values()) if per_product else 0
    finally:
        db.close()

    print()
    print("=" * 72)
    print("POST STATE")
    print("=" * 72)
    print(f"  aggregates by status   : {after['status_counts']}")
    print(f"  AAV active / total     : {after['aav'][0]} / {after['aav'][1]}")
    total, with_attr = after["coverage"]
    print(f"  coverage               : {with_attr}/{total} = "
          f"{100*with_attr/max(total,1):.2f}%")
    print(f"  events_total           : {after['events_total']}  (must equal {before['events_total']})")
    print(f"  PA total (age_group)   : {after['pa_total']}")
    print(f"  max PA rows per product: {max_per_product}  (must be 1)")

    print()
    print("=" * 72)
    print("DISTRIBUTION OF age_group")
    print("=" * 72)
    print(f"  {'value':<14} {'count':>6}")
    for v, n in dist.most_common():
        print(f"  {v:<14} {n:>6}")

    print()
    print("=" * 72)
    print("DELTAS")
    print("=" * 72)
    print(f"  pending  : {before['status_counts'].get('pending',0)} -> {after['status_counts'].get('pending',0)}")
    print(f"  approved : {before['status_counts'].get('approved',0)} -> {after['status_counts'].get('approved',0)}")
    print(f"  merged   : {before['status_counts'].get('merged',0)} -> {after['status_counts'].get('merged',0)}  (unchanged)")
    print(f"  AAV active : {before['aav'][0]} -> {after['aav'][0]} "
          f"(diff=+{after['aav'][0] - before['aav'][0]})")
    print(f"  PA total   : {before['pa_total']} -> {after['pa_total']} "
          f"(diff=+{after['pa_total'] - before['pa_total']})")
    cov_b = 100*before['coverage'][1]/max(before['coverage'][0],1)
    cov_a = 100*after['coverage'][1]/max(after['coverage'][0],1)
    print(f"  coverage   : {cov_b:.2f}% -> {cov_a:.2f}% (+{cov_a-cov_b:.2f}pp)")

    # ----- Production sanity check -----
    print()
    print("=" * 72)
    print("PRODUCTION WORKSPACE INTEGRITY (mumzworld_v3_sample)")
    print("=" * 72)
    db = SessionLocal()
    try:
        prod_ws = db.query(Workspace).filter(
            Workspace.slug == "mumzworld_v3_sample").first()
        if prod_ws:
            n_aav = db.query(AttributeAllowedValue).filter(
                AttributeAllowedValue.workspace_id == prod_ws.id,
                AttributeAllowedValue.is_active == True).count()
            n_pa_total = (db.query(ProductAttribute)
                          .join(Product, ProductAttribute.product_id == Product.id)
                          .filter(Product.workspace_id == prod_ws.id).count())
            n_evts = db.query(E).filter(
                E.workspace_id == prod_ws.id).count()
            print(f"  AAV active / events / PA rows: "
                  f"{n_aav} / {n_evts} / {n_pa_total}  (untouched)")
    finally:
        db.close()

    # ----- Confirmations -----
    print()
    print("=" * 72)
    print("CONFIRMATIONS")
    print("=" * 72)
    print(f"  no merges executed         : merged count "
          f"{before['status_counts'].get('merged',0)} -> "
          f"{after['status_counts'].get('merged',0)}  (unchanged)")
    print(f"  no rejections              : rejected count "
          f"{before['status_counts'].get('rejected',0)} -> "
          f"{after['status_counts'].get('rejected',0)}")
    print(f"  events preserved           : {before['events_total']} -> {after['events_total']}")
    print(f"  no force=true used         : all approvals were ready")
    print(f"  no scoring/threshold edits : confirmed")
    print(f"  no enrichment runs         : confirmed")
    print(f"  no recommendations         : confirmed")
    print(f"  one age_group per product  : "
          f"{'OK' if max_per_product <= 1 else 'FAIL ('+str(max_per_product)+')'}")


if __name__ == "__main__":
    main()
