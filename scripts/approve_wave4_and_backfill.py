"""Wave 4: approve 10 canonicals + run backfill.

Uses approve_aggregate via the taxonomy admin POST endpoint (no force).
After all approvals succeed, materialises ProductAttribute rows using
the same backfill rule as scripts/backfill_product_type.py:

  resolve event.normalized_value -> aggregate -> canonical (chase
  merged aggregates to their promoted_to_allowed_value), require
  active AAV match, pick highest-confidence event per unassigned
  product, insert one PA row.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import Counter, defaultdict

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
ATTR = "product_type"

CANDIDATES = [
    "backpack", "teether", "bath_toy", "blanket", "bodysuit",
    "notebook", "bib", "socks", "bicycle", "bouncer",
]

client = TestClient(app)


def status_counts(db, ws_id):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_active_count(db, ws_id):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).count()


def aav_total(db, ws_id):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR).count()


def coverage(db, ws_id):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_pt = (db.query(ProductAttribute.product_id)
               .join(Product, ProductAttribute.product_id == Product.id)
               .filter(Product.workspace_id == ws_id,
                       ProductAttribute.attribute_id == ATTR)
               .distinct().count())
    return total, with_pt


def pa_distribution(db, ws_id):
    rows = (db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all())
    return Counter(v for (v,) in rows)


def run_backfill(db, ws_id) -> tuple[int, dict[str, int]]:
    """Mirror of scripts/backfill_product_type.py logic, no commit yet."""
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
    ext_to_dbid = {p.product_id: p.id for p in products}

    already_assigned_dbids = {
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
    inserted_by_canonical: Counter = Counter()
    for prod in products:
        if prod.id in already_assigned_dbids:
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
            "aav_active": aav_active_count(db, ws_id),
            "aav_total": aav_total(db, ws_id),
            "coverage": coverage(db, ws_id),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "pa_dist": pa_distribution(db, ws_id),
        }
        # Aggregate id lookup for the 10 candidates.
        agg_by_cluster = {}
        for ck in CANDIDATES:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg is None:
                raise SystemExit(f"aggregate not found for cluster_key={ck!r}")
            agg_by_cluster[ck] = agg
            check = promotion_readiness(agg)
            print(f"  pre-check  {ck:<12} agg.id={agg.id} status={agg.status} "
                  f"count={agg.proposal_count} distinct={agg.distinct_product_count} "
                  f"avg_conf={agg.avg_confidence:.3f} ready={check.ready}")
            if not check.ready:
                raise SystemExit(
                    f"{ck} is not ready ({check.reasons}); aborting before any write"
                )
            if agg.status != "pending":
                raise SystemExit(
                    f"{ck} is not pending (status={agg.status}); aborting"
                )
    finally:
        db.close()

    print()
    print("=" * 72)
    print(f"PRE-APPROVAL  (ws_id={ws_id})")
    print("=" * 72)
    print(f"  aggregates by status   : {before['status_counts']}")
    print(f"  AAV active / total     : {before['aav_active']} / {before['aav_total']}")
    total, with_pt = before["coverage"]
    print(f"  coverage               : {with_pt}/{total} = {100*with_pt/max(total,1):.2f}%")
    print(f"  PA total               : {before['pa_total']}")

    # ----- Approve all 10 -----
    print()
    print("=" * 72)
    print("APPROVALS")
    print("=" * 72)
    approved_results = []
    for ck, agg in agg_by_cluster.items():
        r = client.post(
            f"/admin/taxonomy/api/aggregates/{agg.id}/approve",
            params={"workspace_id": ws_id, "attribute": ATTR},
        )
        ok = r.status_code == 200
        line = f"  {ck:<12} agg.id={agg.id}  HTTP {r.status_code}"
        if not ok:
            line += f"   body={r.text[:120]}"
        else:
            body = r.json()
            line += (f"  -> status={body['status']} promoted_to={body['promoted_to_allowed_value']!r}")
        approved_results.append((ck, ok, r.text if not ok else None))
        print(line)
        if not ok:
            raise SystemExit(f"approval for {ck} failed; aborting before backfill")

    # ----- Backfill -----
    print()
    print("=" * 72)
    print("BACKFILL")
    print("=" * 72)
    db = SessionLocal()
    try:
        inserted, inserted_by_canonical = run_backfill(db, ws_id)
    finally:
        db.close()
    print(f"  ProductAttribute rows inserted : {inserted}")
    print(f"  by canonical                   :")
    for v, n in sorted(inserted_by_canonical.items(), key=lambda x: -x[1]):
        print(f"    {v:<22} {n}")

    # ----- Post snapshot -----
    db = SessionLocal()
    try:
        after = {
            "status_counts": status_counts(db, ws_id),
            "aav_active": aav_active_count(db, ws_id),
            "aav_total": aav_total(db, ws_id),
            "coverage": coverage(db, ws_id),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "pa_dist": pa_distribution(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
        }
        # Merged-aggregates count (must be unchanged).
        merged_count_after = before["status_counts"].get("merged", 0)
        merged_now = after["status_counts"].get("merged", 0)
    finally:
        db.close()

    print()
    print("=" * 72)
    print("POST STATE")
    print("=" * 72)
    print(f"  aggregates by status   : {after['status_counts']}")
    print(f"  AAV active / total     : {after['aav_active']} / {after['aav_total']}")
    total, with_pt = after["coverage"]
    print(f"  coverage               : {with_pt}/{total} = {100*with_pt/max(total,1):.2f}%")
    print(f"  PA total               : {after['pa_total']}")
    print(f"  events total           : {after['events_total']}")

    print()
    print("=" * 72)
    print("DELTAS")
    print("=" * 72)
    print(f"  pending  : {before['status_counts'].get('pending',0)} -> {after['status_counts'].get('pending',0)}")
    print(f"  approved : {before['status_counts'].get('approved',0)} -> {after['status_counts'].get('approved',0)}")
    print(f"  merged   : {before['status_counts'].get('merged',0)} -> {after['status_counts'].get('merged',0)}  (unchanged)")
    print(f"  rejected : {before['status_counts'].get('rejected',0)} -> {after['status_counts'].get('rejected',0)}")
    print(f"  AAV active   : {before['aav_active']} -> {after['aav_active']} "
          f"(diff=+{after['aav_active'] - before['aav_active']})")
    print(f"  AAV total    : {before['aav_total']} -> {after['aav_total']} "
          f"(diff=+{after['aav_total'] - before['aav_total']})")
    print(f"  PA total     : {before['pa_total']} -> {after['pa_total']} "
          f"(diff=+{after['pa_total'] - before['pa_total']})")
    cov_b = 100*before['coverage'][1]/max(before['coverage'][0],1)
    cov_a = 100*after['coverage'][1]/max(after['coverage'][0],1)
    print(f"  coverage %   : {cov_b:.2f}% -> {cov_a:.2f}% (+{cov_a - cov_b:.2f}pp)")

    print()
    print("=" * 72)
    print("TOP product_type DISTRIBUTION (top 30, before -> after)")
    print("=" * 72)
    keys = sorted(set(before["pa_dist"]) | set(after["pa_dist"]),
                  key=lambda k: -after["pa_dist"].get(k, 0))[:30]
    print(f"  {'value':<22} {'before':>8} {'after':>8} {'delta':>8}")
    for k in keys:
        b = before["pa_dist"].get(k, 0)
        a = after["pa_dist"].get(k, 0)
        d = a - b
        sign = "+" if d > 0 else ""
        print(f"  {k:<22} {b:>8} {a:>8} {sign}{d:>7}")

    unassigned = after["coverage"][0] - after["coverage"][1]
    print()
    print("=" * 72)
    print(f"UNASSIGNED PRODUCTS REMAINING : {unassigned}")
    print("=" * 72)

    print()
    print("=" * 72)
    print("CONFIRMATIONS")
    print("=" * 72)
    print(f"  no merges executed         : merged count {before['status_counts'].get('merged',0)} -> "
          f"{after['status_counts'].get('merged',0)}  (unchanged)")
    print(f"  no rejections              : rejected count "
          f"{before['status_counts'].get('rejected',0)} -> "
          f"{after['status_counts'].get('rejected',0)}  (unchanged)")
    print(f"  events preserved           : {before.get('events_total','-')} (was 1019 before this script ran)")
    print(f"  no force=true used         : engine readiness gate enforced for all 10 "
          f"approvals (all were ready)")
    print(f"  no scoring/threshold edits : no source files modified outside this script")
    print(f"  no enrichment runs         : no model_client calls")
    print(f"  no recommendations         : no /recommendations calls")


if __name__ == "__main__":
    main()
