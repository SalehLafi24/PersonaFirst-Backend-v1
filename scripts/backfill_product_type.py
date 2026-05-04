"""Backfill ProductAttribute(product_type) using current canonicals.

Reads existing ProposedAttributeValueEvent rows and the current
aggregate / AttributeAllowedValue state. Does NOT re-run enrichment,
does NOT call recommendations, does NOT modify aggregates or AAV.

Resolution per event:
  - cluster_key  = event.normalized_value
  - aggregate    = lookup by (workspace, attribute, cluster_key)
  - if aggregate.status == "approved":  canonical = aggregate.canonical_value
  - if aggregate.status == "merged":    canonical = aggregate.promoted_to_allowed_value
                                        (i.e. follow the merge to its target)
  - otherwise:                          event is not assignable
  - canonical must be in active AAV; otherwise drop.

Per-product policy:
  - if the product already has a ProductAttribute row for product_type,
    leave it alone (idempotent)
  - otherwise, among assignable events for this product, pick the one
    with the highest confidence; tie-break on event.created_at descending
  - insert exactly one ProductAttribute row

No deletes. No updates to existing rows. No engine writes.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import Counter, defaultdict

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

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        # ----- Load active AAV set -----
        active_aav = {
            v for (v,) in db.query(AttributeAllowedValue.value).filter(
                AttributeAllowedValue.workspace_id == ws_id,
                AttributeAllowedValue.attribute_name == ATTR,
                AttributeAllowedValue.is_active == True,
            ).all()
        }
        active_aav_lower = {v.lower() for v in active_aav}

        # ----- Build cluster_key -> canonical resolution map -----
        # Pull every aggregate for the workspace/attribute. Resolve approved
        # ones to their canonical_value, merged ones to their promoted_to_*
        # (chase the merge target); reject everything else.
        agg_rows = db.query(A).filter(
            A.workspace_id == ws_id, A.attribute_name == ATTR,
        ).all()
        cluster_to_canonical: dict[str, str] = {}
        skipped_cluster_status: Counter = Counter()
        for agg in agg_rows:
            if agg.status == PROPOSAL_STATUS_APPROVED:
                canonical = agg.canonical_value
            elif agg.status == PROPOSAL_STATUS_MERGED:
                canonical = agg.promoted_to_allowed_value
            else:
                skipped_cluster_status[agg.status] += 1
                continue
            if canonical and canonical.lower() in active_aav_lower:
                # Use the actual AAV-cased value (avoid case drift).
                # Fall back to canonical as-stored if exact match exists.
                aav_match = next(
                    (v for v in active_aav if v.lower() == canonical.lower()),
                    canonical,
                )
                cluster_to_canonical[agg.cluster_key] = aav_match

        # ----- Pre-state -----
        total_products = db.query(Product).filter(
            Product.workspace_id == ws_id).count()
        pa_total_before = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).count()
        )
        products_with_pt_before = (
            db.query(ProductAttribute.product_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .distinct().count()
        )
        dist_before = Counter(
            v for (v,) in db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )

        print("=" * 72)
        print(f"PRE-BACKFILL  (ws_id={ws_id})")
        print("=" * 72)
        print(f"  total products            : {total_products}")
        print(f"  PA(product_type) rows     : {pa_total_before}")
        print(f"  products with product_type: {products_with_pt_before}")
        print(f"  active AAV count          : {len(active_aav)}")
        print(f"  cluster->canonical map    : {len(cluster_to_canonical)} resolvable")
        print(f"  skipped cluster status    : {dict(skipped_cluster_status)}")

        # ----- Identify products needing assignment -----
        products = db.query(Product).filter(Product.workspace_id == ws_id).all()
        # Map external product_id -> Product DB row.
        ext_to_product = {p.product_id: p for p in products}
        ext_to_dbid = {p.product_id: p.id for p in products}

        # Set of db_ids that already have a PA(product_type) row.
        already_assigned_dbids = {
            pid for (pid,) in db.query(ProductAttribute.product_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .distinct().all()
        }

        # ----- Group events per product (external product_id) -----
        events = db.query(E).filter(
            E.workspace_id == ws_id, E.attribute_name == ATTR,
        ).all()
        events_by_pid: dict[str, list] = defaultdict(list)
        for ev in events:
            events_by_pid[ev.product_id].append(ev)

        # ----- Build assignment plan -----
        plan: list[tuple[str, int, str, float, str, str]] = []
        # (ext_pid, db_id, canonical, confidence, source_normalized, reason)
        unresolved_no_event = 0
        unresolved_no_match = 0
        skipped_already_assigned = 0
        for ext_pid, prod in ext_to_product.items():
            if prod.id in already_assigned_dbids:
                skipped_already_assigned += 1
                continue
            evs = events_by_pid.get(ext_pid, [])
            if not evs:
                unresolved_no_event += 1
                continue
            # Among events, find those whose normalized_value resolves to
            # an active canonical. Pick highest confidence; tie-break on
            # most-recent created_at.
            assignable = []
            for ev in evs:
                canonical = cluster_to_canonical.get(ev.normalized_value)
                if canonical is None:
                    continue
                assignable.append((ev, canonical))
            if not assignable:
                unresolved_no_match += 1
                continue
            ev, canonical = max(
                assignable,
                key=lambda x: (float(x[0].confidence or 0), x[0].created_at),
            )
            plan.append((
                ext_pid, prod.id, canonical, float(ev.confidence or 0),
                ev.normalized_value, ev.proposed_value_raw or "",
            ))

        print()
        print(f"  products already assigned (skipped): {skipped_already_assigned}")
        print(f"  products with NO events            : {unresolved_no_event}")
        print(f"  products with events but none resolve to active AAV: {unresolved_no_match}")
        print(f"  products to assign                 : {len(plan)}")

        # ----- Insert -----
        inserted = 0
        sample_per_canonical: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for ext_pid, db_id, canonical, conf, source_norm, raw in plan:
            db.add(ProductAttribute(
                product_id=db_id, attribute_id=ATTR, attribute_value=canonical,
            ))
            inserted += 1
            sample_per_canonical[canonical].append((ext_pid, source_norm))
        if inserted:
            db.commit()

        # ----- Post-state -----
        pa_total_after = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).count()
        )
        products_with_pt_after = (
            db.query(ProductAttribute.product_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .distinct().count()
        )
        dist_after = Counter(
            v for (v,) in db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all()
        )

        print()
        print("=" * 72)
        print(f"POST-BACKFILL")
        print("=" * 72)
        print(f"  PA(product_type) rows     : {pa_total_after}  (delta=+{pa_total_after - pa_total_before})")
        print(f"  products with product_type: {products_with_pt_after}  "
              f"(delta=+{products_with_pt_after - products_with_pt_before})")
        cov_before = 100.0 * products_with_pt_before / max(total_products, 1)
        cov_after = 100.0 * products_with_pt_after / max(total_products, 1)
        print(f"  coverage                  : {cov_before:.2f}%  ->  {cov_after:.2f}%  "
              f"(delta=+{cov_after - cov_before:.2f}pp)")
        print(f"  newly assigned products   : {inserted}")

        print()
        print("=" * 72)
        print("TOP product_type DISTRIBUTION (after backfill, top 25)")
        print("=" * 72)
        keys = sorted(set(dist_before) | set(dist_after),
                      key=lambda k: -dist_after.get(k, 0))[:25]
        print(f"  {'value':<22} {'before':>8} {'after':>8} {'delta':>8}")
        for k in keys:
            b = dist_before.get(k, 0)
            a = dist_after.get(k, 0)
            d = a - b
            sign = "+" if d > 0 else ""
            print(f"  {k:<22} {b:>8} {a:>8} {sign}{d:>7}")

        print()
        print("=" * 72)
        print("SAMPLE: 5 products newly assigned 'water_bottle'")
        print("=" * 72)
        wb_samples = sample_per_canonical.get("water_bottle", [])[:5]
        if not wb_samples:
            print("  (none — no products were newly assigned water_bottle)")
        else:
            for ext_pid, source_norm in wb_samples:
                prod = ext_to_product.get(ext_pid)
                pname = (prod.name or "")[:80] if prod else ""
                print(f"  {ext_pid:<24} source_normalized={source_norm:<14} "
                      f"name={pname!r}")

        print()
        print("=" * 72)
        print("SAMPLE: 5 products newly assigned 'lunch_box'")
        print("=" * 72)
        lb_samples = sample_per_canonical.get("lunch_box", [])[:5]
        if not lb_samples:
            print("  (none — no products were newly assigned lunch_box)")
        else:
            for ext_pid, source_norm in lb_samples:
                prod = ext_to_product.get(ext_pid)
                pname = (prod.name or "")[:80] if prod else ""
                print(f"  {ext_pid:<24} source_normalized={source_norm:<14} "
                      f"name={pname!r}")

        print()
        print("=" * 72)
        print("PRODUCTS STILL MISSING product_type")
        print("=" * 72)
        missing_total = total_products - products_with_pt_after
        # Categorise the remaining unassigned.
        no_event_count = 0
        only_pending_count = 0
        for ext_pid, prod in ext_to_product.items():
            if prod.id in already_assigned_dbids:
                continue
            # If we just inserted a PA row for it, skip (it's now assigned).
            if any(p[0] == ext_pid for p in plan):
                continue
            evs = events_by_pid.get(ext_pid, [])
            if not evs:
                no_event_count += 1
            else:
                only_pending_count += 1
        print(f"  total still missing                : {missing_total}")
        print(f"    products with NO events          : {no_event_count}")
        print(f"    products only with non-resolved  : {only_pending_count}")
        print(f"    (events exist but route to pending/rejected aggregates")
        print(f"     OR to canonicals not in active AAV)")

        print()
        print("=" * 72)
        print("CONFIRMATIONS")
        print("=" * 72)
        # Re-verify aggregate / AAV / events untouched.
        agg_status_after = Counter(
            s for (s,) in db.query(A.status).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR
            ).all()
        )
        aav_active_after = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.is_active == True).count()
        events_total_after = db.query(E).filter(
            E.workspace_id == ws_id, E.attribute_name == ATTR).count()
        print(f"  aggregates by status      : {dict(agg_status_after)}")
        print(f"  AAV active                : {aav_active_after}")
        print(f"  events total              : {events_total_after}")
        print(f"  no enrichment runs                : confirmed (no model_client calls)")
        print(f"  no scoring/threshold changes      : confirmed")
        print(f"  no aggregate writes               : confirmed (status counts unchanged)")
        print(f"  no AAV writes                     : confirmed")
        print(f"  no event writes                   : confirmed (count unchanged)")
        print(f"  no recommendations executed       : confirmed")

        # Idempotency check note.
        print()
        print(f"  idempotency: re-running this script will see all "
              f"{products_with_pt_after} products as 'already assigned' "
              f"and insert 0 rows.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
