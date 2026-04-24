"""Run attribute discovery on the 20-product PX dataset.

Simulates what the discovery model would return for each product given
the existing 9 attributes (color, occasion, activity, support_level,
fit_type, mom_stage, workout_intensity, travel_friendly, activity_type).

Only proposes attributes that represent genuinely new dimensions not
covered by existing attributes. Most products return empty because
existing attributes already capture their signals.

Rolls back all DB writes at the end.
"""
from __future__ import annotations

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.schemas.attribute_discovery import (
    AttributeDiscoveryOutput,
    ProposedAttribute,
)
from app.services.proposed_attribute_service import (
    attribute_promotion_readiness,
    record_attribute_events,
    refresh_attribute_aggregates,
)

WORKSPACE_SLUG = "personafirst-starter"

DISCOVERY_OUTPUTS: dict[str, list[dict]] = {
    # ------------------------------------------------------------------
    # PX01 — HIIT Performance Bra
    # All signals covered by: workout_intensity, activity_type, support_level.
    # ------------------------------------------------------------------
    "PX01": [],
    # ------------------------------------------------------------------
    # PX02 — Sprint Interval Tights
    # Covered by: workout_intensity, activity_type, fit_type.
    # ------------------------------------------------------------------
    "PX02": [],
    # ------------------------------------------------------------------
    # PX03 — CrossFit Training Tee
    # Covered by: workout_intensity, activity_type.
    # ------------------------------------------------------------------
    "PX03": [],
    # ------------------------------------------------------------------
    # PX04 — Studio Cycling Leggings
    # "indoor cycling" — indoor is a USE ENVIRONMENT not captured by any
    # existing attribute (occasion=athletic covers the purpose, not the
    # physical setting).
    # ------------------------------------------------------------------
    "PX04": [
        {
            "attribute_name": "use_environment",
            "confidence": 0.93,
            "description": "The physical setting or environment a product is designed to be used in.",
            "evidence": ['"indoor cycling and spin class"'],
            "suggested_values": ["indoor", "outdoor", "studio"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
    ],
    # ------------------------------------------------------------------
    # PX05 — Trail Pace Hoodie
    # "hoodie" implies mid-layer in a layering system. Layering role is
    # not captured by fit_type or any existing attribute.
    # ------------------------------------------------------------------
    "PX05": [
        {
            "attribute_name": "layering_role",
            "confidence": 0.88,
            "description": "The role this garment plays in a layering system.",
            "evidence": ['"lightweight hoodie"', '"Packs into hood pocket"'],
            "suggested_values": ["base", "mid", "outer"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
    ],
    # ------------------------------------------------------------------
    # PX06 — Yin Yoga Flow Pants
    # Covered by: activity_type=yoga, workout_intensity=low, fit_type.
    # ------------------------------------------------------------------
    "PX06": [],
    # ------------------------------------------------------------------
    # PX07 — Recovery Lounge Set
    # Covered by: occasion=lounge, workout_intensity=low.
    # ------------------------------------------------------------------
    "PX07": [],
    # ------------------------------------------------------------------
    # PX08 — Airport Layer Jacket
    # "Layer" in name. Jacket = outer layer. Also packable.
    # ------------------------------------------------------------------
    "PX08": [
        {
            "attribute_name": "layering_role",
            "confidence": 0.89,
            "description": "The role this garment plays in a layering system.",
            "evidence": ['"Airport Layer Jacket"', '"packable travel jacket"'],
            "suggested_values": ["base", "mid", "outer"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
    ],
    # ------------------------------------------------------------------
    # PX09 — Travel-Day Slim Pants
    # Covered by: travel_friendly, occasion, fit_type.
    # ------------------------------------------------------------------
    "PX09": [],
    # ------------------------------------------------------------------
    # PX10 — Everyday Scoop Bralette
    # Control: no discovery signals beyond existing attributes.
    # ------------------------------------------------------------------
    "PX10": [],
    # ------------------------------------------------------------------
    # PX11 — Explosive Training Shorts
    # Covered by: workout_intensity, activity_type.
    # ------------------------------------------------------------------
    "PX11": [],
    # ------------------------------------------------------------------
    # PX12 — Plyometric Performance Tank
    # Covered by: workout_intensity, activity_type.
    # ------------------------------------------------------------------
    "PX12": [],
    # ------------------------------------------------------------------
    # PX13 — Marathon Distance Tights
    # Covered by: activity_type=running, workout_intensity.
    # ------------------------------------------------------------------
    "PX13": [],
    # ------------------------------------------------------------------
    # PX14 — Lightweight Running Windbreaker
    # Outer layer for weather. Both layering_role and weather_resistance.
    # ------------------------------------------------------------------
    "PX14": [
        {
            "attribute_name": "layering_role",
            "confidence": 0.91,
            "description": "The role this garment plays in a layering system.",
            "evidence": ['"Ultra-light windbreaker"'],
            "suggested_values": ["base", "mid", "outer"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
        {
            "attribute_name": "weather_resistance",
            "confidence": 0.89,
            "description": "The degree of weather protection the product provides.",
            "evidence": ['"designed for running in variable weather"'],
            "suggested_values": ["none", "light", "moderate", "full"],
            "suggested_class_name": "compatibility",
            "suggested_targeting_mode": "compatibility_signal",
        },
    ],
    # ------------------------------------------------------------------
    # PX15 — Studio Yoga Bra
    # "studio sessions" — use environment not captured by existing attrs.
    # ------------------------------------------------------------------
    "PX15": [
        {
            "attribute_name": "use_environment",
            "confidence": 0.91,
            "description": "The physical setting or environment a product is designed to be used in.",
            "evidence": ['"low-impact studio sessions"'],
            "suggested_values": ["indoor", "outdoor", "studio"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
    ],
    # ------------------------------------------------------------------
    # PX16 — Flow Yoga Leggings
    # Covered by: activity_type=yoga, workout_intensity=low.
    # ------------------------------------------------------------------
    "PX16": [],
    # ------------------------------------------------------------------
    # PX17 — Trail Hiking Pants
    # "outdoor" environment. Distinct from occasion/activity.
    # ------------------------------------------------------------------
    "PX17": [
        {
            "attribute_name": "use_environment",
            "confidence": 0.94,
            "description": "The physical setting or environment a product is designed to be used in.",
            "evidence": ['"long outdoor walks and rugged terrain"'],
            "suggested_values": ["indoor", "outdoor", "studio"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
    ],
    # ------------------------------------------------------------------
    # PX18 — All-Weather Hiking Jacket
    # Outer layer + outdoor environment + weather.
    # ------------------------------------------------------------------
    "PX18": [
        {
            "attribute_name": "layering_role",
            "confidence": 0.90,
            "description": "The role this garment plays in a layering system.",
            "evidence": ['"Protective jacket for hiking"'],
            "suggested_values": ["base", "mid", "outer"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
        {
            "attribute_name": "use_environment",
            "confidence": 0.92,
            "description": "The physical setting or environment a product is designed to be used in.",
            "evidence": ['"extended outdoor activity"'],
            "suggested_values": ["indoor", "outdoor", "studio"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
        {
            "attribute_name": "weather_resistance",
            "confidence": 0.90,
            "description": "The degree of weather protection the product provides.",
            "evidence": ['"hiking in changing weather"', '"Breathable and durable"'],
            "suggested_values": ["none", "light", "moderate", "full"],
            "suggested_class_name": "compatibility",
            "suggested_targeting_mode": "compatibility_signal",
        },
    ],
    # ------------------------------------------------------------------
    # PX19 — Backpacking Base Layer
    # Explicit "base layer" + outdoor environment.
    # ------------------------------------------------------------------
    "PX19": [
        {
            "attribute_name": "layering_role",
            "confidence": 0.95,
            "description": "The role this garment plays in a layering system.",
            "evidence": ['"Merino base layer designed for multi-day backpacking"'],
            "suggested_values": ["base", "mid", "outer"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
        {
            "attribute_name": "use_environment",
            "confidence": 0.90,
            "description": "The physical setting or environment a product is designed to be used in.",
            "evidence": ['"extended outdoor travel"'],
            "suggested_values": ["indoor", "outdoor", "studio"],
            "suggested_class_name": "contextual_semantic",
            "suggested_targeting_mode": "categorical_affinity",
        },
    ],
    # ------------------------------------------------------------------
    # PX20 — Minimal Everyday Tank
    # Control: no discovery signals.
    # ------------------------------------------------------------------
    "PX20": [],
}


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # ---- Step 1: Ingest ----
        print("=" * 80)
        print("Step 1 -- Ingest discovery events")
        print("=" * 80)
        total = 0
        for pid, proposals in sorted(DISCOVERY_OUTPUTS.items()):
            if not proposals:
                continue
            output = AttributeDiscoveryOutput(
                proposed_attributes=[ProposedAttribute(**p) for p in proposals],
            )
            events = record_attribute_events(
                db, workspace_id=ws.id, product_id=pid, output=output,
            )
            for ev in events:
                print(f"  {ev.product_id:6s} {ev.normalized_attribute_name:22s} "
                      f"conf={ev.confidence:.2f}  values={ev.suggested_values}")
            total += len(events)

        control_pids = [pid for pid, p in DISCOVERY_OUTPUTS.items() if not p]
        print(f"\n  total events:    {total}")
        print(f"  control (empty): {sorted(control_pids)}")

        # ---- Step 2: Refresh aggregates ----
        print()
        print("=" * 80)
        print("Step 2 -- Refresh aggregates")
        print("=" * 80)
        aggregates = refresh_attribute_aggregates(db, workspace_id=ws.id)
        aggregates.sort(key=lambda a: (-a.proposal_count, a.canonical_attribute_name))

        for agg in aggregates:
            check = attribute_promotion_readiness(agg)
            print(f"\n  canonical_name         = {agg.canonical_attribute_name!r}")
            print(f"  cluster_key            = {agg.cluster_key!r}")
            print(f"  proposal_count         = {agg.proposal_count}")
            print(f"  distinct_products      = {agg.distinct_product_count}")
            print(f"  avg_confidence         = {agg.avg_confidence:.3f}")
            print(f"  max_confidence         = {agg.max_confidence:.3f}")
            print(f"  sample_products        = {agg.sample_product_ids}")
            print(f"  sample_evidence        = {agg.sample_evidence}")
            print(f"  merged_suggested_vals  = {agg.merged_suggested_values}")
            print(f"  suggested_class        = {agg.suggested_class_name}")
            print(f"  suggested_targeting    = {agg.suggested_targeting_mode}")
            print(f"  status                 = {agg.status}")
            print(f"  promotion_ready        = {check.ready}")
            if check.reasons:
                for r in check.reasons:
                    print(f"    blocked: {r}")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
