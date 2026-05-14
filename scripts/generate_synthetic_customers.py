"""Generate deterministic synthetic customers from real catalog products.

Five customer profiles, each defined ONLY by attribute filters over the
real catalog:

    1. baby_essentials  -- product_type in baby-care set, age_group=infant
    2. toy_focused      -- product_type in toy set
    3. apparel_focused  -- product_type in apparel set
    4. book_heavy       -- product_type=book
    5. mixed_category   -- one product per top product_type, broad

For each profile:
    - Pick the first 5..10 products (lowest Product.id) matching the filter
      from the real catalog.
    - Insert one Customer row + one CustomerInteraction(purchase) per
      product, occurred_at evenly spaced backward from today.

Idempotent: if interactions for a customer already exist they are NOT
duplicated (the generator is keyed by (workspace_id, customer_id)).

No fictional names, biographies, or manually-assigned preferences. The
"profile" is purely the attribute filter applied to the real catalog --
the persona that comes out is whatever the real interactions imply.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.customer import (
    INTERACTION_PURCHASE,
    Customer,
    CustomerInteraction,
)
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace


# Profile definitions. Filters reference real attribute values found in
# mumzworld_v3_sample (verified via the validation harness output).
PROFILES: list[dict] = [
    {
        "customer_id": "synthetic_baby_essentials",
        "min_interactions": 5, "max_interactions": 10,
        "product_type_in": [
            "diaper", "pacifier", "teether", "bath_product",
            "changing_mat", "bib", "bodysuit", "bouncer",
        ],
        "age_group_in": ["infant"],
    },
    {
        "customer_id": "synthetic_toy_focused",
        "min_interactions": 5, "max_interactions": 10,
        "product_type_in": [
            "playset", "vehicle_toy", "plush_toy", "learning_toy",
            "construction_toy", "action_figure", "ride_on", "sensory_toy",
            "walker_toy", "bath_toy", "musical_toy", "puzzle",
        ],
        "age_group_in": None,
    },
    {
        "customer_id": "synthetic_apparel_focused",
        "min_interactions": 5, "max_interactions": 10,
        "product_type_in": [
            "shirt", "bodysuit", "socks", "swim_accessory", "costume",
        ],
        "age_group_in": None,
    },
    {
        "customer_id": "synthetic_book_heavy",
        "min_interactions": 5, "max_interactions": 10,
        "product_type_in": ["book"],
        "age_group_in": None,
    },
    {
        "customer_id": "synthetic_mixed_category",
        "min_interactions": 5, "max_interactions": 10,
        "product_type_in": [
            "book", "puzzle", "skincare", "water_bottle", "towel",
            "stationery", "art_supply", "lunch_box", "stroller_accessory",
            "backpack",
        ],
        "age_group_in": None,
        # mixed: take ONE product per type rather than first N of all types,
        # so behaviour is genuinely diverse.
        "one_per_type": True,
    },
    # New customer that intentionally OVERLAPS PARTIALLY with
    # synthetic_baby_essentials. The product_type filter is a strict
    # subset (pacifier, teether), so the first picks include products
    # baby_essentials also bought (the first pacifier/teether in the
    # catalog by Product.id) plus additional pacifiers/teethers further
    # down the list that baby_essentials did not pick.
    #
    # Effect on the behavioural co-occurrence graph:
    #   shared sources (e.g. that pacifier) get edges to BOTH
    #   baby_essentials' other purchases (bodysuits, bibs, etc.) AND
    #   alt's other purchases. For alt, those baby_essentials-only
    #   targets are NOT in alt's history, so RepurchaseSuppression
    #   does not drop them and BehavioralCoOccurrenceBooster fires.
    #
    # No manual product selection -- the overlap is a deterministic
    # consequence of the filter being a subset.
    {
        "customer_id": "synthetic_baby_essentials_alt",
        "min_interactions": 5, "max_interactions": 8,
        "product_type_in": ["pacifier", "teether"],
        "age_group_in": ["infant"],
    },
]


def _pick_products(
    db, workspace_id: int, profile: dict,
) -> list[Product]:
    """Deterministic product pick. Lowest Product.id first; for the
    'one_per_type' profile pick the first product per product_type."""
    product_types: list[str] = profile["product_type_in"]
    age_groups: list[str] | None = profile.get("age_group_in")

    base_q = (
        db.query(Product)
        .join(ProductAttribute, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == "product_type",
                ProductAttribute.attribute_value.in_(product_types))
    )
    if age_groups:
        # Restrict to products whose db_id ALSO has age_group in the list.
        ag_ids = [pid for (pid,) in db.query(ProductAttribute.product_id)
                   .join(Product, ProductAttribute.product_id == Product.id)
                   .filter(Product.workspace_id == workspace_id,
                           ProductAttribute.attribute_id == "age_group",
                           ProductAttribute.attribute_value.in_(age_groups))
                   .all()]
        base_q = base_q.filter(Product.id.in_(ag_ids))

    products = base_q.order_by(Product.id.asc()).all()
    # Dedup by db_id (the join can fan out if a product has multiple PA rows).
    seen: set[int] = set()
    unique: list[Product] = []
    for p in products:
        if p.id in seen:
            continue
        seen.add(p.id)
        unique.append(p)

    if profile.get("one_per_type"):
        # Need to inspect product_type per product to pick first per type.
        pids = [p.id for p in unique]
        type_by_db: dict[int, str] = {}
        for db_id, val in (
            db.query(ProductAttribute.product_id, ProductAttribute.attribute_value)
            .filter(ProductAttribute.product_id.in_(pids),
                    ProductAttribute.attribute_id == "product_type")
            .all()
        ):
            # First seen wins — the join above ordered by Product.id, so
            # this preserves "lowest id per type".
            type_by_db.setdefault(db_id, val)
        per_type_first: dict[str, Product] = {}
        for p in unique:
            t = type_by_db.get(p.id)
            if t in product_types and t not in per_type_first:
                per_type_first[t] = p
        unique = list(per_type_first.values())

    return unique[: profile["max_interactions"]]


def _ensure_customer(db, *, workspace_id: int, customer_id: str) -> Customer:
    existing = (
        db.query(Customer)
        .filter(Customer.workspace_id == workspace_id,
                Customer.customer_id == customer_id)
        .first()
    )
    if existing is not None:
        return existing
    c = Customer(workspace_id=workspace_id, customer_id=customer_id)
    db.add(c)
    db.flush()
    return c


def _existing_interaction_pids(
    db, *, workspace_id: int, customer_id: str,
) -> set[str]:
    return {
        pid for (pid,) in db.query(CustomerInteraction.product_id).filter(
            CustomerInteraction.workspace_id == workspace_id,
            CustomerInteraction.customer_id == customer_id,
        ).all()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="mumzworld_v3_sample")
    parser.add_argument(
        "--reset", action="store_true",
        help="delete existing synthetic customers and interactions for this workspace before generating",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).one()
        ws_id = ws.id

        if args.reset:
            cust_ids = [p["customer_id"] for p in PROFILES]
            db.query(CustomerInteraction).filter(
                CustomerInteraction.workspace_id == ws_id,
                CustomerInteraction.customer_id.in_(cust_ids),
            ).delete(synchronize_session=False)
            db.query(Customer).filter(
                Customer.workspace_id == ws_id,
                Customer.customer_id.in_(cust_ids),
            ).delete(synchronize_session=False)
            db.commit()

        print(f"workspace={args.workspace}  ws_id={ws_id}")
        print()

        for profile in PROFILES:
            cust_id = profile["customer_id"]
            picks = _pick_products(db, ws_id, profile)
            if len(picks) < profile["min_interactions"]:
                print(f"  {cust_id:<32} SKIPPED  only found {len(picks)} matching "
                      f"products in catalog (min={profile['min_interactions']})")
                continue
            picks = picks[: profile["max_interactions"]]

            _ensure_customer(db, workspace_id=ws_id, customer_id=cust_id)
            already = _existing_interaction_pids(
                db, workspace_id=ws_id, customer_id=cust_id,
            )
            new_count = 0
            # Spread occurred_at: most recent first, then 7 days apart back.
            now = datetime.now(timezone.utc)
            for idx, prod in enumerate(picks):
                if prod.product_id in already:
                    continue
                db.add(CustomerInteraction(
                    workspace_id=ws_id,
                    customer_id=cust_id,
                    product_id=prod.product_id,
                    interaction_type=INTERACTION_PURCHASE,
                    occurred_at=now - timedelta(days=7 * idx),
                ))
                new_count += 1
            db.commit()
            print(f"  {cust_id:<32} interactions={len(picks):>2}  "
                  f"new={new_count:>2}  existing={len(picks) - new_count}")
            for p in picks:
                print(f"      -> {p.product_id:<32} {(p.name or '')[:60]}")
            print()
    finally:
        db.close()


if __name__ == "__main__":
    main()
