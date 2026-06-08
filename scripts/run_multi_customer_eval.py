"""Multi-customer scoring evaluation against the current recommendation engine.

Seeds four customer profiles + a synthetic evaluation catalog inside a
transaction, runs the live scoring engine (no logic changes), reports top-N
with full scoring breakdown, and rolls back everything at the end. No data
is modified on disk.

Customer profiles:
  C_EVAL_1: activity_type=[hiit, running], intensity=high, env=[indoor],  owns base
  C_EVAL_2: activity_type=[yoga],          intensity=low,  env=[indoor],  owns base
  C_EVAL_3: activity_type=[hiking],        intensity=mod,  env=[outdoor], owns mid
  C_EVAL_4: activity_type=[running],       intensity=mod,  env=[indoor,outdoor], owns base+mid
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.database import SessionLocal
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import AttributeDefinition
from app.services.recommendation_service import get_recommendations

WORKSPACE_SLUG = "personafirst-starter"
SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"
TOP_N = 5


EVAL_PRODUCTS = [
    {
        "product_id": "EVAL_HIIT_BRA", "sku": "E-01", "name": "HIIT High-Impact Bra",
        "attrs": {"category": "bras", "activity_type": "hiit",
                   "workout_intensity": "high", "environment": "indoor",
                   "layering_role": "base"},
    },
    {
        "product_id": "EVAL_HIIT_MID", "sku": "E-02", "name": "HIIT Training Hoodie",
        "attrs": {"category": "tops", "activity_type": "hiit",
                   "workout_intensity": "high", "environment": "indoor",
                   "layering_role": "mid"},
    },
    {
        "product_id": "EVAL_RUN_BASE", "sku": "E-03", "name": "Sprint Base Tee",
        "attrs": {"category": "tops", "activity_type": "running",
                   "workout_intensity": "high", "environment": "indoor",
                   "layering_role": "base"},
    },
    {
        "product_id": "EVAL_RUN_MID_IN", "sku": "E-04", "name": "Treadmill Mid Layer",
        "attrs": {"category": "tops", "activity_type": "running",
                   "workout_intensity": "moderate", "environment": "indoor",
                   "layering_role": "mid"},
    },
    {
        "product_id": "EVAL_RUN_OUTER", "sku": "E-05", "name": "Running Wind Shell",
        "attrs": {"category": "jackets", "activity_type": "running",
                   "workout_intensity": "high", "environment": "outdoor",
                   "layering_role": "outer"},
    },
    {
        "product_id": "EVAL_YOGA_BASE", "sku": "E-06", "name": "Flow Yoga Leggings",
        "attrs": {"category": "leggings", "activity_type": "yoga",
                   "workout_intensity": "low", "environment": "indoor",
                   "layering_role": "base"},
    },
    {
        "product_id": "EVAL_YOGA_MID", "sku": "E-07", "name": "Yin Yoga Wrap",
        "attrs": {"category": "tops", "activity_type": "yoga",
                   "workout_intensity": "low", "environment": "indoor",
                   "layering_role": "mid"},
    },
    {
        "product_id": "EVAL_HIKE_BASE", "sku": "E-08", "name": "Trail Merino Tee",
        "attrs": {"category": "tops", "activity_type": "hiking",
                   "workout_intensity": "moderate", "environment": "outdoor",
                   "layering_role": "base"},
    },
    {
        "product_id": "EVAL_HIKE_MID", "sku": "E-09", "name": "Trail Fleece Mid",
        "attrs": {"category": "tops", "activity_type": "hiking",
                   "workout_intensity": "moderate", "environment": "outdoor",
                   "layering_role": "mid"},
    },
    {
        "product_id": "EVAL_HIKE_OUTER", "sku": "E-10", "name": "Hiker Shell Jacket",
        "attrs": {"category": "jackets", "activity_type": "hiking",
                   "workout_intensity": "moderate", "environment": "outdoor",
                   "layering_role": "outer"},
    },
    {
        "product_id": "EVAL_LOUNGE_SET", "sku": "E-11", "name": "Recovery Lounge Set",
        "attrs": {"category": "sets", "activity_type": "lounge",
                   "workout_intensity": "low", "environment": "indoor",
                   "layering_role": "base"},
    },
    {
        "product_id": "EVAL_PILATES", "sku": "E-12", "name": "Pilates Core Top",
        "attrs": {"category": "tops", "activity_type": "pilates",
                   "workout_intensity": "low", "environment": "studio",
                   "layering_role": "base"},
    },
]


CUSTOMERS = {
    "C_EVAL_1": [
        ("activity_type", "hiit", 0.9),
        ("activity_type", "running", 0.8),
        ("workout_intensity", "high", 0.85),
        ("environment", "indoor", 0.8),
        ("layering_role", "base", 0.75),
    ],
    "C_EVAL_2": [
        ("activity_type", "yoga", 0.9),
        ("workout_intensity", "low", 0.85),
        ("environment", "indoor", 0.8),
        ("layering_role", "base", 0.75),
    ],
    "C_EVAL_3": [
        ("activity_type", "hiking", 0.9),
        ("workout_intensity", "moderate", 0.85),
        ("environment", "outdoor", 0.8),
        ("layering_role", "mid", 0.75),
    ],
    "C_EVAL_4": [
        ("activity_type", "running", 0.9),
        ("workout_intensity", "moderate", 0.85),
        ("environment", "indoor", 0.8),
        ("environment", "outdoor", 0.8),
        ("layering_role", "base", 0.75),
        ("layering_role", "mid", 0.75),
    ],
}


def _fmt(x: float) -> str:
    return f"{x:+.4f}"


def _print_customer(customer_id, affinities, all_scored):
    print()
    print("=" * 100)
    print(f"Customer {customer_id}")
    print("-" * 100)
    for attr_id, attr_val, score in affinities:
        print(f"  affinity  {attr_id:20s} = {attr_val:10s}  score={score:.2f}")

    print()
    print(f"Top {TOP_N} recommendations:")
    print(f"  {'rank':<4} {'product_id':<20} {'final':>9} {'affinity':>10} "
          f"{'compat+':>9} {'compat-':>9} {'ctx-':>9}")
    for i, rec in enumerate(all_scored[:TOP_N], 1):
        print(
            f"  #{i:<3} {rec.product_id:<20} "
            f"{rec.recommendation_score:>+9.4f} "
            f"{rec.affinity_contribution:>+10.4f} "
            f"{rec.compatibility_positive_contribution:>+9.4f} "
            f"{rec.compatibility_negative_contribution:>+9.4f} "
            f"{rec.contextual_negative_contribution:>+9.4f}"
        )

    if len(all_scored) > TOP_N:
        print()
        print(f"Also scored (ranks {TOP_N+1}+):")
        for i, rec in enumerate(all_scored[TOP_N:], TOP_N + 1):
            print(
                f"  #{i:<3} {rec.product_id:<20} "
                f"{rec.recommendation_score:>+9.4f} "
                f"{rec.affinity_contribution:>+10.4f} "
                f"{rec.compatibility_positive_contribution:>+9.4f} "
                f"{rec.compatibility_negative_contribution:>+9.4f} "
                f"{rec.contextual_negative_contribution:>+9.4f}"
            )


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # Seed eval products
        prod_by_pid: dict[str, Product] = {}
        for p in EVAL_PRODUCTS:
            prod = Product(
                workspace_id=ws.id,
                product_id=p["product_id"],
                sku=p["sku"],
                name=p["name"],
                group_id=None,
            )
            db.add(prod)
            db.flush()
            prod_by_pid[p["product_id"]] = prod
            for attr_id, attr_val in p["attrs"].items():
                db.add(ProductAttribute(
                    product_id=prod.id,
                    attribute_id=attr_id,
                    attribute_value=attr_val,
                ))
        db.flush()

        # Load attribute definitions (for targeting modes + behaviors)
        defs = json.loads(SEED_DIR.joinpath("attribute_definitions.json").read_text("utf-8"))
        attr_defs = [AttributeDefinition(**d) for d in defs]
        targeting_modes = {d.name: d.targeting_mode.value for d in attr_defs}
        behaviors = {d.name: d.behavior for d in attr_defs}

        # Starter products have none of the new attributes (activity_type /
        # workout_intensity / environment / layering_role), so they will not
        # match any affinity for these customers and will be dropped by the
        # meaningfulness gate in the service. Leaving them in place.

        print("=" * 100)
        print(f"Eval catalog ({len(EVAL_PRODUCTS)} products):")
        for p in EVAL_PRODUCTS:
            attrs_s = ", ".join(f"{k}={v}" for k, v in p["attrs"].items())
            print(f"  {p['product_id']:<20} {attrs_s}")

        # Score each customer
        per_customer: dict[str, list] = {}
        for cust_id, affinities in CUSTOMERS.items():
            # Clear any prior affinities (shouldn't exist for these IDs)
            db.query(CustomerAttributeAffinity).filter(
                CustomerAttributeAffinity.workspace_id == ws.id,
                CustomerAttributeAffinity.customer_id == cust_id,
            ).delete(synchronize_session=False)
            for attr_id, attr_val, score in affinities:
                db.add(CustomerAttributeAffinity(
                    workspace_id=ws.id,
                    customer_id=cust_id,
                    attribute_id=attr_id,
                    attribute_value=attr_val,
                    score=score,
                ))
            db.flush()

            results, _fb = get_recommendations(
                db,
                workspace_id=ws.id,
                customer_id=cust_id,
                top_n=len(EVAL_PRODUCTS),  # return everything scored
                attribute_targeting_modes=targeting_modes,
                attribute_behaviors=behaviors,
            )
            per_customer[cust_id] = results
            _print_customer(cust_id, affinities, results)

        # Show NOT-surfaced detail per customer: every catalog product that
        # didn't appear in scored results.
        print()
        print("=" * 100)
        print("Products NOT surfaced per customer (with reason)")
        print("=" * 100)
        for cust_id, results in per_customer.items():
            surfaced = {r.product_id for r in results}
            not_surfaced = [p for p in EVAL_PRODUCTS if p["product_id"] not in surfaced]
            print(f"\n  {cust_id}:")
            if not not_surfaced:
                print("    (all 12 products surfaced)")
                continue
            for p in not_surfaced:
                attrs_s = ", ".join(f"{k}={v}" for k, v in p["attrs"].items())
                print(f"    {p['product_id']:<20} {attrs_s}")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
