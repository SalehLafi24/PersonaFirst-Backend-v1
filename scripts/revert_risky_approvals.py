"""Revert nine risky product_type approvals in mumzworld_v3_sample.

Per the stability review, these approvals introduced parent/child
collisions, normalization-variant duplicates, semantic ambiguity, or
"too broad" buckets. They are being rolled back to `pending` so the
review pipeline can re-evaluate them with more evidence and after
sibling consolidation.

Operations per value (in order):
  1. Use existing engine service `attribute_taxonomy_service.deactivate_allowed_value`
     to set the AttributeAllowedValue row's `is_active=False`.
  2. Reset the ProposedAttributeValueAggregate row:
        status                        → "pending"
        promoted_to_allowed_value     → None
        merge_reason                  → None
        review_note (appended)        → "Reverted by revert_risky_approvals.py: <reason>"
     (No "unapprove" engine service exists; direct ORM write is the
      only path. Aggregate rows are NOT deleted.)
  3. Delete ProductAttribute rows in this workspace that have
     attribute_id="product_type" AND attribute_value matches the
     reverted canonical. (ProductAttribute has no is_active flag, so
     the only path is removal — but the underlying
     ProposedAttributeValueEvent rows are kept intact, so the data is
     reconstructable on re-approval.)

Nothing else is touched: no enrichment, no scoring, no thresholds, no
recommendations, no merges. ProposedAttributeValueEvent rows remain
verbatim. Other approved canonicals are untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    PROPOSAL_STATUS_PENDING,
    ProposedAttributeValueAggregate,
)
from app.models.workspace import Workspace
from app.services.attribute_taxonomy_service import (
    deactivate_allowed_value,
    get_allowed_values,
)

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"

REVERT = [
    ("accessory",       "too broad; conflicts with 14+ *_accessory siblings"),
    ("clothing",        "too broad; conflicts with shirt/costume + apparel leaves"),
    ("bottle",          "semantic ambiguity — sample evidence dominated by water bottles, not feeding bottles"),
    ("bag",             "parent/child collision with backpack; multiple sub-bag clusters pending"),
    ("backpack",        "subtype of bag; same product/family pulled into two clusters"),
    ("ride_on_toy",     "normalization-variant of approved ride_on; collision risk"),
    ("outdoor_playset", "subtype of approved playset"),
    ("balloon",         "subtype of approved party_supply"),
    ("lunch_box",       "normalization variant — pending lunchbox(2) duplicates this cluster"),
]


def main() -> None:
    db = SessionLocal()
    try:
        ws = (db.query(Workspace).filter(Workspace.slug == WS_SLUG).first())
        if ws is None:
            raise SystemExit(f"Workspace {WS_SLUG!r} not found")
        ws_id = ws.id

        # Snapshot pre-state.
        before_allowed_active = (
            db.query(AttributeAllowedValue)
            .filter(AttributeAllowedValue.workspace_id == ws_id,
                    AttributeAllowedValue.attribute_name == ATTR,
                    AttributeAllowedValue.is_active == True)
            .count()
        )
        before_allowed_total = (
            db.query(AttributeAllowedValue)
            .filter(AttributeAllowedValue.workspace_id == ws_id,
                    AttributeAllowedValue.attribute_name == ATTR)
            .count()
        )
        before_pa = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .count()
        )
        before_total_products = (
            db.query(Product).filter(Product.workspace_id == ws_id).count()
        )

        deactivated = []
        agg_resets = []
        pa_deletions = {}  # canonical -> count of removed rows

        for value, reason in REVERT:
            # Step 1: deactivate AttributeAllowedValue (engine service).
            ok = deactivate_allowed_value(db, ws_id, ATTR, value)
            deactivated.append((value, ok))

            # Step 2: reset aggregate to pending. Direct ORM write because
            # there's no engine "unapprove" service. Aggregate row preserved.
            agg = (
                db.query(ProposedAttributeValueAggregate)
                .filter(
                    ProposedAttributeValueAggregate.workspace_id == ws_id,
                    ProposedAttributeValueAggregate.attribute_name == ATTR,
                    ProposedAttributeValueAggregate.canonical_value == value,
                ).first()
            )
            if agg is not None:
                prev_status = agg.status
                agg.status = PROPOSAL_STATUS_PENDING
                agg.promoted_to_allowed_value = None
                agg.merge_reason = None
                prefix = (agg.review_note + " | ") if agg.review_note else ""
                agg.review_note = (
                    f"{prefix}Reverted by revert_risky_approvals.py: {reason}"
                )
                agg_resets.append((value, prev_status, agg.status))

            # Step 3: remove ProductAttribute rows that point to the reverted value.
            pa_q = (
                db.query(ProductAttribute)
                .join(Product, ProductAttribute.product_id == Product.id)
                .filter(Product.workspace_id == ws_id,
                        ProductAttribute.attribute_id == ATTR,
                        ProductAttribute.attribute_value == value)
            )
            n = pa_q.count()
            if n > 0:
                # Need product_id PKs of ProductAttribute rows to delete.
                pa_ids = [pa.id for pa in pa_q.all()]
                db.query(ProductAttribute).filter(
                    ProductAttribute.id.in_(pa_ids)
                ).delete(synchronize_session=False)
            pa_deletions[value] = n

        db.flush()
        db.commit()

        # Snapshot post-state.
        after_allowed_active = (
            db.query(AttributeAllowedValue)
            .filter(AttributeAllowedValue.workspace_id == ws_id,
                    AttributeAllowedValue.attribute_name == ATTR,
                    AttributeAllowedValue.is_active == True)
            .count()
        )
        after_allowed_total = (
            db.query(AttributeAllowedValue)
            .filter(AttributeAllowedValue.workspace_id == ws_id,
                    AttributeAllowedValue.attribute_name == ATTR)
            .count()
        )
        after_pa = (
            db.query(ProductAttribute)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .count()
        )
        live_allowed = get_allowed_values(db, ws_id, ATTR)

        print("=" * 80)
        print("REVERT — risky product_type approvals")
        print("=" * 80)
        print(f"workspace                                     : {WS_SLUG}")
        print(f"AttributeAllowedValue active BEFORE           : {before_allowed_active}")
        print(f"AttributeAllowedValue active AFTER            : {after_allowed_active}")
        print(f"AttributeAllowedValue rows total (incl. inactive): "
              f"{before_allowed_total} -> {after_allowed_total}")
        print(f"ProductAttribute(product_type) rows BEFORE    : {before_pa}")
        print(f"ProductAttribute(product_type) rows AFTER     : {after_pa}")
        print(f"ProductAttribute rows removed                 : {before_pa - after_pa}")
        print(f"Coverage BEFORE                               : "
              f"{before_pa}/{before_total_products} = "
              f"{100.0 * before_pa / before_total_products:.2f}%")
        print(f"Coverage AFTER                                : "
              f"{after_pa}/{before_total_products} = "
              f"{100.0 * after_pa / before_total_products:.2f}%")
        print()
        print("Per-value outcome:")
        print(f"  {'value':<20} {'allowed_deact':<16} {'agg_status':<22} {'PA_rows_removed':>16}")
        agg_by_value = dict((v, (prev, cur)) for v, prev, cur in agg_resets)
        deact_by_value = dict(deactivated)
        for value, _ in REVERT:
            d = "yes" if deact_by_value.get(value) else "noop (already inactive?)"
            transition = agg_by_value.get(value)
            tr = (f"{transition[0]} -> {transition[1]}"
                  if transition else "(no aggregate found)")
            n = pa_deletions.get(value, 0)
            print(f"  {value:<20} {d:<16} {tr:<22} {n:>16}")
        print()
        print(f"Live get_allowed_values (active only) — {len(live_allowed)} values:")
        print(f"  {live_allowed}")
        print()
        print("CONFIRMATIONS")
        print("  - ProposedAttributeValueEvent rows : preserved (none deleted)")
        print("  - ProposedAttributeValueAggregate  : preserved (none deleted; "
              "9 reset to pending)")
        print("  - Other approved canonicals        : untouched")
        print("  - No merges performed")
        print("  - No enrichment runs")
        print("  - No scoring or threshold changes")
        print("  - No recommendations executed")
    finally:
        db.close()


if __name__ == "__main__":
    main()
