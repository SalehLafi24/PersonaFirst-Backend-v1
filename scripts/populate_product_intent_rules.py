"""Populate Product.repurchase_behavior / repurchase_window_days /
recommendation_role from seed_data/product_type_intent_rules.json.

Idempotent: re-running yields the same state. Reports:
    - rule coverage (matched / unmatched product_types)
    - rows updated vs unchanged
    - per-rule counts (e.g. how many products got repurchase_behavior=one_time)

The mapping is taxonomy-level (keyed only by product_type). No customer
preference, no LLM, no per-product judgement.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace


_RULES_PATH = ROOT / "seed_data" / "product_type_intent_rules.json"


def _load_rules() -> dict[str, dict]:
    if not _RULES_PATH.exists():
        raise SystemExit(f"rules file not found: {_RULES_PATH}")
    raw = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    return raw.get("product_type") or {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="mumzworld_v3_sample")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rules = _load_rules()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")
        ws_id = ws.id

        products = db.query(Product).filter(Product.workspace_id == ws_id).all()

        # Per-product product_type lookup.
        pt_rows = (
            db.query(ProductAttribute.product_id, ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == "product_type")
            .all()
        )
        type_by_db_id: dict[int, str] = {db_id: val for db_id, val in pt_rows}

        seen_types: Counter = Counter()
        matched_types: Counter = Counter()
        unmatched_types: Counter = Counter()
        no_type: int = 0
        updated = 0
        unchanged = 0
        per_behavior: Counter = Counter()
        per_role: Counter = Counter()

        for prod in products:
            pt = type_by_db_id.get(prod.id)
            if pt is None:
                no_type += 1
                continue
            seen_types[pt] += 1
            rule = rules.get(pt)
            if rule is None:
                unmatched_types[pt] += 1
                continue
            matched_types[pt] += 1

            new_behavior = rule.get("repurchase_behavior")
            new_window = rule.get("repurchase_window_days")
            new_role = rule.get("recommendation_role") or "same_use_case"

            changed = (
                prod.repurchase_behavior != new_behavior
                or prod.repurchase_window_days != new_window
                or prod.recommendation_role != new_role
            )
            if not changed:
                unchanged += 1
                continue
            if not args.dry_run:
                prod.repurchase_behavior = new_behavior
                prod.repurchase_window_days = new_window
                prod.recommendation_role = new_role
            updated += 1
            per_behavior[new_behavior or "(unset)"] += 1
            per_role[new_role] += 1

        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    print("=" * 80)
    print(f"populate_product_intent_rules  workspace={args.workspace}"
          + ("  (dry-run)" if args.dry_run else ""))
    print("=" * 80)
    print(f"  products total              : {len(products)}")
    print(f"  products with no product_type: {no_type}")
    print(f"  rule coverage               : "
          f"{sum(matched_types.values())} matched  /  "
          f"{sum(unmatched_types.values())} unmatched")
    print(f"  rows updated                : {updated}")
    print(f"  rows unchanged              : {unchanged}")
    if per_behavior:
        print(f"  by repurchase_behavior      : {dict(per_behavior)}")
    if per_role:
        print(f"  by recommendation_role      : {dict(per_role)}")
    if unmatched_types:
        print()
        print(f"  unmatched product_types (need a rule):")
        for t, n in unmatched_types.most_common():
            print(f"    {t:<22} {n}")


if __name__ == "__main__":
    main()
