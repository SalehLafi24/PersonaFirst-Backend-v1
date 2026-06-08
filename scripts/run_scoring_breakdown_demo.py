"""Scoring breakdown demo for the four new attributes.

Sets up a synthetic customer (C_DEMO) with affinities for the new
attributes, then scores against selected products to show exactly how
each attribute contributes.

Customer profile:
    activity_type  = hiit (0.9), running (0.7)
    workout_intensity = high (0.85)
    environment    = indoor (0.8)
    layering_role  = base (0.75)  ← already owns a base layer

Products scored:
    P_HIIT_BRA    — HIIT bra, indoor, base layer       → should score well on
                    activity_type + intensity, get penalized on layering (dup)
    P_OUTER_JACK  — Outer jacket, outdoor               → should get layering
                    complement bonus, environment mismatch penalty
    P_YOGA_PANTS  — Yoga pants, studio, low intensity   → should get low scores,
                    environment mismatch
    P_SPRINT_BASE — Sprint base layer, indoor            → activity match + env
                    match + layering duplicate penalty

Rolls back all DB writes.
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
CUSTOMER_ID = "C_DEMO"
SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"


DEMO_PRODUCTS = [
    {
        "product_id": "P_HIIT_BRA", "sku": "DEMO-01", "name": "HIIT Studio Bra",
        "group_id": None,
        "attrs": {
            "category": "bras", "activity_type": "hiit",
            "workout_intensity": "high", "environment": "indoor",
            "layering_role": "base",
        },
    },
    {
        "product_id": "P_OUTER_JACK", "sku": "DEMO-02", "name": "Trail Outer Shell",
        "group_id": None,
        "attrs": {
            "category": "jackets", "activity_type": "hiking",
            "workout_intensity": "moderate", "environment": "outdoor",
            "layering_role": "outer",
        },
    },
    {
        "product_id": "P_YOGA_PANTS", "sku": "DEMO-03", "name": "Yin Yoga Pants",
        "group_id": None,
        "attrs": {
            "category": "leggings", "activity_type": "yoga",
            "workout_intensity": "low", "environment": "studio",
            "layering_role": "base",
        },
    },
    {
        "product_id": "P_SPRINT_BASE", "sku": "DEMO-04", "name": "Sprint Base Layer",
        "group_id": None,
        "attrs": {
            "category": "tops", "activity_type": "running",
            "workout_intensity": "high", "environment": "indoor",
            "layering_role": "base",
        },
    },
    {
        "product_id": "P_HIIT_MID", "sku": "DEMO-05", "name": "HIIT Training Hoodie",
        "group_id": None,
        "attrs": {
            "category": "tops", "activity_type": "hiit",
            "workout_intensity": "high", "environment": "indoor",
            "layering_role": "mid",
        },
    },
    {
        "product_id": "P_RUN_OUTER", "sku": "DEMO-06", "name": "Running Wind Shell",
        "group_id": None,
        "attrs": {
            "category": "jackets", "activity_type": "running",
            "workout_intensity": "high", "environment": "outdoor",
            "layering_role": "outer",
        },
    },
]

CUSTOMER_AFFINITIES = [
    ("activity_type", "hiit", 0.9),
    ("activity_type", "running", 0.7),
    ("workout_intensity", "high", 0.85),
    ("environment", "indoor", 0.8),
    ("layering_role", "base", 0.75),
]


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # -- Seed products --
        for p in DEMO_PRODUCTS:
            prod = Product(
                workspace_id=ws.id,
                product_id=p["product_id"],
                sku=p["sku"],
                name=p["name"],
                group_id=p["group_id"],
            )
            db.add(prod)
            db.flush()
            for attr_id, attr_val in p["attrs"].items():
                db.add(ProductAttribute(
                    product_id=prod.id,
                    attribute_id=attr_id,
                    attribute_value=attr_val,
                ))
        db.flush()

        # -- Seed affinities --
        for attr_id, attr_val, score in CUSTOMER_AFFINITIES:
            db.add(CustomerAttributeAffinity(
                workspace_id=ws.id,
                customer_id=CUSTOMER_ID,
                attribute_id=attr_id,
                attribute_value=attr_val,
                score=score,
            ))
        db.flush()

        # -- Load attribute definitions for targeting/behavior --
        defs = json.loads(SEED_DIR.joinpath("attribute_definitions.json").read_text("utf-8"))
        attr_defs = [AttributeDefinition(**d) for d in defs]
        targeting_modes = {d.name: d.targeting_mode.value for d in attr_defs}
        behaviors = {d.name: d.behavior for d in attr_defs}

        print("=" * 80)
        print(f"Customer: {CUSTOMER_ID}")
        print("=" * 80)
        print("  Affinities:")
        for attr_id, attr_val, score in CUSTOMER_AFFINITIES:
            mode = targeting_modes.get(attr_id, "?")
            print(f"    {attr_id:20s} = {attr_val:8s} score={score:.2f}  mode={mode}")

        # -- Score --
        results, _ = get_recommendations(
            db,
            workspace_id=ws.id,
            customer_id=CUSTOMER_ID,
            top_n=10,
            attribute_targeting_modes=targeting_modes,
            attribute_behaviors=behaviors,
        )

        print()
        print("=" * 80)
        print("Scoring breakdown")
        print("=" * 80)

        for rec in results:
            prod_def = next(p for p in DEMO_PRODUCTS if p["product_id"] == rec.product_id)
            print(f"\n  {rec.product_id:16s} {rec.name}")
            print(f"  product attrs: {prod_def['attrs']}")
            print(f"  ---")
            print(f"  affinity_contribution (categorical)  = {rec.affinity_contribution:+.4f}")
            print(f"  compat_positive (match/complement)   = {rec.compatibility_positive_contribution:+.4f}")
            print(f"  compat_negative (mismatch/duplicate)  = {rec.compatibility_negative_contribution:+.4f}")
            print(f"  contextual_negative (env mismatch)    = {rec.contextual_negative_contribution:+.4f}")
            print(f"  ---")
            print(f"  recommendation_score                  = {rec.recommendation_score:+.4f}")
            print(f"  matched_attributes:")
            for m in rec.matched_attributes:
                print(f"    {m.attribute_id:20s} = {m.attribute_value:8s} "
                      f"aff={m.score:.2f} w={m.weight:.1f} mode={m.targeting_mode}")
            print(f"  explanation: {rec.explanation}")

        # -- Summary --
        print()
        print("=" * 80)
        print("Ranking summary")
        print("=" * 80)
        for i, rec in enumerate(results, 1):
            print(f"  #{i} {rec.product_id:16s} score={rec.recommendation_score:+.4f}  "
                  f"aff={rec.affinity_contribution:+.4f} "
                  f"compat+={rec.compatibility_positive_contribution:+.4f} "
                  f"compat-={rec.compatibility_negative_contribution:+.4f} "
                  f"ctx-={rec.contextual_negative_contribution:+.4f}")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
