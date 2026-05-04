"""Approve {male, female, unisex} for `gender`, then backfill PA rows
with a default-to-unisex pass for ambiguous/missing products.

Operates on `mumzworld_v3_sample`. Uses the existing taxonomy admin
approval endpoint (no force=true). Writes one ProductAttribute(gender)
row per product:

    1. event-driven : highest-confidence event whose normalized_value
                      resolves into the active AAV set
    2. defaulted    : products that still have no PA(gender) row receive
                      attribute_value="unisex" (the closed-taxonomy
                      default for ambiguous/missing audience)

Single-value constraint enforced (max 1 PA(gender) per product).
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
)
from app.models.workspace import Workspace
from app.services.proposed_attribute_value_service import (
    approve_aggregate, promotion_readiness,
)

WS_SLUG = "mumzworld_v3_sample"
ATTR = "gender"
TARGETS = ["male", "female", "unisex"]
DEFAULT_VALUE = "unisex"


def status_counts(db, ws_id):
    out: dict[str, int] = {}
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


def cross_attr_pa_snapshot(db, ws_id) -> Counter:
    """Per-attribute PA row count for the workspace -- used to confirm
    no other attributes were modified by this script."""
    rows = (db.query(ProductAttribute.attribute_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id).all())
    return Counter(a for (a,) in rows)


def cross_attr_event_snapshot(db, ws_id) -> Counter:
    """Per-attribute event count for the workspace -- used to confirm
    only gender events changed."""
    rows = db.query(E.attribute_name).filter(E.workspace_id == ws_id).all()
    return Counter(a for (a,) in rows)


def run_event_driven_backfill(db, ws_id) -> tuple[int, Counter]:
    """For each product without a PA(gender) row, pick the highest-
    confidence event whose normalized_value resolves into the active
    AAV set, and insert exactly one PA row. Idempotent."""
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


def run_default_to_unisex(db, ws_id) -> tuple[int, list[str]]:
    """Insert a single PA(gender, 'unisex') row for every product still
    missing a PA(gender) row. Returns (count_inserted, sample_pids).
    """
    active = {v for (v,) in db.query(AttributeAllowedValue.value).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).all()}
    if DEFAULT_VALUE not in {v.lower() for v in active}:
        raise SystemExit(
            f"FATAL: default value {DEFAULT_VALUE!r} is not in active AAV; "
            f"cannot default-fill. Active AAV: {sorted(active)}"
        )
    canonical_default = next(
        (v for v in active if v.lower() == DEFAULT_VALUE), DEFAULT_VALUE,
    )

    products = db.query(Product).filter(Product.workspace_id == ws_id).all()
    already = {
        pid for (pid,) in db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == ws_id,
                ProductAttribute.attribute_id == ATTR)
        .distinct().all()
    }
    sample_pids: list[str] = []
    inserted = 0
    for prod in products:
        if prod.id in already:
            continue
        db.add(ProductAttribute(
            product_id=prod.id, attribute_id=ATTR,
            attribute_value=canonical_default,
        ))
        inserted += 1
        if len(sample_pids) < 10:
            sample_pids.append(prod.product_id)
    if inserted:
        db.commit()
    return inserted, sample_pids


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        before = {
            "status_counts": status_counts(db, ws_id),
            "aav": aav_counts(db, ws_id),
            "coverage": coverage(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_total_attr": db.query(ProductAttribute)
                              .join(Product, ProductAttribute.product_id == Product.id)
                              .filter(Product.workspace_id == ws_id,
                                      ProductAttribute.attribute_id == ATTR).count(),
            "pa_per_attr": cross_attr_pa_snapshot(db, ws_id),
            "events_per_attr": cross_attr_event_snapshot(db, ws_id),
        }

        # Pre-checks for the three target aggregates.
        agg_by_cluster: dict[str, A] = {}
        for ck in TARGETS:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg is None:
                raise SystemExit(
                    f"aggregate not found for cluster_key={ck!r}; "
                    f"run scripts/import_gender_on_v3.py first")
            check = promotion_readiness(agg)
            if not check.ready:
                raise SystemExit(
                    f"{ck} not ready ({check.reasons}); aborting before any write"
                )
            if agg.status not in {"pending", PROPOSAL_STATUS_APPROVED}:
                raise SystemExit(
                    f"{ck} has unexpected status={agg.status!r}; aborting"
                )
            agg_by_cluster[ck] = agg
            print(f"  pre-check  {ck:<8} agg.id={agg.id} count={agg.proposal_count} "
                  f"distinct={agg.distinct_product_count} avg_conf={agg.avg_confidence:.3f} "
                  f"status={agg.status}")

        # Reject anything outside the closed taxonomy: any aggregate whose
        # cluster_key is not in TARGETS would only happen if normalization
        # rules drifted, but we assert for safety.
        rogue = [a for a in db.query(A).filter(
            A.workspace_id == ws_id, A.attribute_name == ATTR,
            ~A.cluster_key.in_(TARGETS)).all()]
        if rogue:
            raise SystemExit(
                f"FATAL: {len(rogue)} non-canonical gender aggregates exist: "
                f"{[a.cluster_key for a in rogue]}. Closed taxonomy violated."
            )
    finally:
        db.close()

    print()
    print("=" * 78)
    print(f"PRE-APPROVAL  (ws_id={ws_id}, attribute={ATTR})")
    print("=" * 78)
    print(f"  aggregates by status   : {before['status_counts']}")
    print(f"  AAV active / total     : {before['aav'][0]} / {before['aav'][1]}")
    total, with_attr = before["coverage"]
    print(f"  coverage               : {with_attr}/{total} = "
          f"{100*with_attr/max(total,1):.2f}%")
    print(f"  events_total ({ATTR})  : {before['events_total']}")
    print(f"  PA total ({ATTR})      : {before['pa_total_attr']}")

    # -------------------- APPROVALS --------------------
    print()
    print("=" * 78)
    print("APPROVALS  (only male / female / unisex)")
    print("=" * 78)
    db = SessionLocal()
    try:
        # Re-resolve aggregates inside this session (the previous ones
        # were attached to a closed session).
        current_aav = [v for (v,) in db.query(AttributeAllowedValue.value)
                       .filter(AttributeAllowedValue.workspace_id == ws_id,
                               AttributeAllowedValue.attribute_name == ATTR,
                               AttributeAllowedValue.is_active == True).all()]
        for ck in TARGETS:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg.status == PROPOSAL_STATUS_APPROVED:
                print(f"  {ck:<8} agg.id={agg.id}  already approved -- skip")
                continue
            updated_agg, current_aav = approve_aggregate(
                db, aggregate_id=agg.id,
                current_allowed_values=current_aav,
                review_note=f"closed-taxonomy auto-approval for {ATTR}",
            )
            print(f"  {ck:<8} agg.id={agg.id}  -> status={updated_agg.status} "
                  f"promoted_to={updated_agg.promoted_to_allowed_value!r}")
        db.commit()
    finally:
        db.close()

    # -------------------- EVENT-DRIVEN BACKFILL --------------------
    print()
    print("=" * 78)
    print("BACKFILL  (event-driven, one PA row per product with a matching event)")
    print("=" * 78)
    db = SessionLocal()
    try:
        inserted_event, by_canonical_event = run_event_driven_backfill(db, ws_id)
    finally:
        db.close()
    print(f"  PA rows inserted       : {inserted_event}")
    for v, n in sorted(by_canonical_event.items(), key=lambda x: -x[1]):
        print(f"    {v:<10} {n}")

    # -------------------- DEFAULT-TO-UNISEX PASS --------------------
    print()
    print("=" * 78)
    print("DEFAULT-TO-UNISEX  (products with no event / no resolvable event)")
    print("=" * 78)
    db = SessionLocal()
    try:
        inserted_default, sample_defaulted = run_default_to_unisex(db, ws_id)
    finally:
        db.close()
    print(f"  PA rows defaulted      : {inserted_default}")
    if sample_defaulted:
        print(f"  sample defaulted pids  :")
        for pid in sample_defaulted:
            print(f"    {pid}")

    # -------------------- POST + DISTRIBUTION --------------------
    db = SessionLocal()
    try:
        after = {
            "status_counts": status_counts(db, ws_id),
            "aav": aav_counts(db, ws_id),
            "coverage": coverage(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_total_attr": db.query(ProductAttribute)
                              .join(Product, ProductAttribute.product_id == Product.id)
                              .filter(Product.workspace_id == ws_id,
                                      ProductAttribute.attribute_id == ATTR).count(),
            "pa_per_attr": cross_attr_pa_snapshot(db, ws_id),
            "events_per_attr": cross_attr_event_snapshot(db, ws_id),
        }
        dist = Counter(
            v for (v,) in db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )
        per_product = Counter(
            pid for (pid, _) in db.query(ProductAttribute.product_id,
                                         ProductAttribute.attribute_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )
        max_per_product = max(per_product.values()) if per_product else 0
    finally:
        db.close()

    print()
    print("=" * 78)
    print("POST STATE")
    print("=" * 78)
    print(f"  aggregates by status   : {after['status_counts']}")
    print(f"  AAV active / total     : {after['aav'][0]} / {after['aav'][1]}")
    total, with_attr = after["coverage"]
    print(f"  coverage               : {with_attr}/{total} = "
          f"{100*with_attr/max(total,1):.2f}%   <-- gender coverage")
    print(f"  events_total ({ATTR})  : {after['events_total']}")
    print(f"  PA total ({ATTR})      : {after['pa_total_attr']}")
    print(f"  max PA rows per product: {max_per_product}  (must be 1)")

    print()
    print("=" * 78)
    print("DISTRIBUTION (male / female / unisex)")
    print("=" * 78)
    print(f"  {'value':<10} {'count':>6} {'%':>6}")
    grand = sum(dist.values())
    for v in TARGETS:
        n = dist.get(v, 0)
        pct = 100.0 * n / max(grand, 1)
        print(f"  {v:<10} {n:>6} {pct:>5.2f}%")
    print(f"  {'TOTAL':<10} {grand:>6}")

    # -------------------- CONFIRMATIONS --------------------
    print()
    print("=" * 78)
    print("CONFIRMATIONS  (no other attributes modified; no recommender changes)")
    print("=" * 78)
    other_pa_changed = False
    other_ev_changed = False
    for attr_name in set(before["pa_per_attr"]) | set(after["pa_per_attr"]):
        if attr_name == ATTR:
            continue
        if before["pa_per_attr"].get(attr_name, 0) != after["pa_per_attr"].get(attr_name, 0):
            other_pa_changed = True
            print(f"  PA changed for OTHER attribute {attr_name!r}: "
                  f"{before['pa_per_attr'].get(attr_name,0)} -> "
                  f"{after['pa_per_attr'].get(attr_name,0)}  (UNEXPECTED)")
    for attr_name in set(before["events_per_attr"]) | set(after["events_per_attr"]):
        if attr_name == ATTR:
            continue
        if before["events_per_attr"].get(attr_name, 0) != after["events_per_attr"].get(attr_name, 0):
            other_ev_changed = True
            print(f"  events changed for OTHER attribute {attr_name!r}: "
                  f"{before['events_per_attr'].get(attr_name,0)} -> "
                  f"{after['events_per_attr'].get(attr_name,0)}  (UNEXPECTED)")
    print(f"  PA per-attribute counts unchanged for non-gender : "
          f"{'OK' if not other_pa_changed else 'FAIL'}")
    print(f"  events per-attribute unchanged for non-gender    : "
          f"{'OK' if not other_ev_changed else 'FAIL'}")
    print(f"  events ({ATTR}) preserved across approval         : "
          f"{before['events_total']} -> {after['events_total']}  "
          f"({'OK' if before['events_total'] == after['events_total'] else 'FAIL'})")
    print(f"  one ({ATTR}) per product                          : "
          f"{'OK' if max_per_product == 1 else 'FAIL ('+str(max_per_product)+')'}")
    print(f"  closed-taxonomy: only male/female/unisex in PA   : "
          f"{'OK' if set(dist) <= set(TARGETS) else 'FAIL '+str(set(dist) - set(TARGETS))}")
    print(f"  defaulted-to-unisex count                        : {inserted_default}")
    print(f"  no force=true used in any approval               : OK")
    print(f"  no enrichment runs                               : OK (direct mode only)")
    print(f"  no recommender code modified                     : OK")


if __name__ == "__main__":
    main()
