"""Execute activity_type taxonomy review decisions and re-run enrichment.

Flow:
    1. Ingest activity_type proposal events from all 20 PX products.
    2. Refresh aggregates.
    3. Approve: hiit, yoga, hiking, running.
    4. Merge: plyometrics -> hiit (flattened_child).
    5. Show final taxonomy from DB.
    6. Re-build enrichment prompt to show the new allowed_values in action.

Rolls back all DB writes at the end.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.database import SessionLocal
from app.models.proposed_attribute_value import (
    MERGE_REASON_FLATTENED_CHILD,
    ProposedAttributeValueAggregate,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.attribute_taxonomy_service import get_allowed_values
from app.services.proposed_attribute_value_service import (
    approve_aggregate,
    merge_aggregate,
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)
from app.services.attribute_enrichment_service import get_prompt_for_attribute

WORKSPACE_SLUG = "personafirst-starter"
ATTRIBUTE = "activity_type"
SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"

ACTIVITY_TYPE_OUTPUTS: dict[str, dict] = {
    "PX01": {
        "proposed_values": [
            {"value": "hiit", "confidence": 0.97, "evidence": ["\"built for HIIT, box jumps, and plyometric circuits\""]},
            {"value": "plyometrics", "confidence": 0.93, "evidence": ["\"box jumps, and plyometric circuits\""]},
        ],
    },
    "PX02": {
        "proposed_values": [
            {"value": "sprinting", "confidence": 0.95, "evidence": ["\"engineered for sprint intervals and track repeats\""]},
        ],
    },
    "PX03": {
        "proposed_values": [
            {"value": "crossfit", "confidence": 0.97, "evidence": ["\"designed for CrossFit WODs and high-intensity functional training\""]},
            {"value": "hiit", "confidence": 0.93, "evidence": ["\"high-intensity functional training\""]},
        ],
    },
    "PX04": {
        "proposed_values": [
            {"value": "cycling", "confidence": 0.95, "evidence": ["\"indoor cycling and spin class\""]},
        ],
    },
    "PX05": {
        "proposed_values": [
            {"value": "hiking", "confidence": 0.94, "evidence": ["\"moderate-effort hiking and fast-packing\""]},
        ],
    },
    "PX06": {
        "proposed_values": [
            {"value": "yoga", "confidence": 0.96, "evidence": ["\"yin yoga and restorative practice\""]},
        ],
    },
    "PX07": {"proposed_values": []},
    "PX08": {"proposed_values": []},
    "PX09": {"proposed_values": []},
    "PX10": {"proposed_values": []},
    "PX11": {
        "proposed_values": [
            {"value": "hiit", "confidence": 0.97, "evidence": ["\"designed for HIIT workouts, box jumps, and explosive circuit training\""]},
        ],
    },
    "PX12": {
        "proposed_values": [
            {"value": "plyometrics", "confidence": 0.94, "evidence": ["\"plyometric drills and HIIT sessions\""]},
            {"value": "hiit", "confidence": 0.93, "evidence": ["\"plyometric drills and HIIT sessions\""]},
        ],
    },
    "PX13": {
        "proposed_values": [
            {"value": "running", "confidence": 0.96, "evidence": ["\"built for long-distance running and endurance sessions\""]},
        ],
    },
    "PX14": {
        "proposed_values": [
            {"value": "running", "confidence": 0.95, "evidence": ["\"designed for running in variable weather\""]},
        ],
    },
    "PX15": {
        "proposed_values": [
            {"value": "yoga", "confidence": 0.95, "evidence": ["\"designed for yoga, stretching, and low-impact studio sessions\""]},
        ],
    },
    "PX16": {
        "proposed_values": [
            {"value": "yoga", "confidence": 0.96, "evidence": ["\"optimized for yoga flow and mobility work\""]},
        ],
    },
    "PX17": {
        "proposed_values": [
            {"value": "hiking", "confidence": 0.96, "evidence": ["\"Durable pants for hiking and trail exploration\""]},
        ],
    },
    "PX18": {
        "proposed_values": [
            {"value": "hiking", "confidence": 0.94, "evidence": ["\"Protective jacket for hiking in changing weather\""]},
        ],
    },
    "PX19": {
        "proposed_values": [
            {"value": "backpacking", "confidence": 0.96, "evidence": ["\"designed for multi-day backpacking trips\""]},
        ],
    },
    "PX20": {"proposed_values": []},
}


def _build_output(raw: dict) -> EnrichmentOutput:
    proposed = []
    for item in raw.get("proposed_values") or []:
        conf = float(item.get("confidence", 0))
        evidence = list(item.get("evidence") or [])
        if conf >= 0.8 and evidence:
            proposed.append(ProposedValue(value=item["value"], confidence=conf, evidence=evidence))
    return EnrichmentOutput(
        attribute_name=ATTRIBUTE,
        attribute_class="contextual_semantic",
        values=[],
        proposed_values=proposed,
        warnings=[],
        source=EnrichmentSource.TEXT,
    )


def _get_agg(db, ws_id: int, value: str) -> ProposedAttributeValueAggregate:
    return (
        db.query(ProposedAttributeValueAggregate)
        .filter(
            ProposedAttributeValueAggregate.workspace_id == ws_id,
            ProposedAttributeValueAggregate.attribute_name == ATTRIBUTE,
            ProposedAttributeValueAggregate.cluster_key == value,
        )
        .one()
    )


def _print_agg(agg):
    check = promotion_readiness(agg)
    print(f"  {agg.canonical_value:16s} status={agg.status:10s} "
          f"count={agg.proposal_count} products={agg.distinct_product_count} "
          f"avg_conf={agg.avg_confidence:.3f} "
          f"promoted_to={str(agg.promoted_to_allowed_value):16s} "
          f"merge_reason={str(agg.merge_reason):20s} "
          f"ready={check.ready}")


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # ---- Step 1: Ingest ----
        print("=" * 80)
        print("Step 1 -- Ingest proposal events")
        print("=" * 80)
        total = 0
        for pid, raw in sorted(ACTIVITY_TYPE_OUTPUTS.items()):
            output = _build_output(raw)
            events = record_events_from_output(
                db, workspace_id=ws.id, product_id=pid, output=output,
            )
            for ev in events:
                print(f"  {ev.product_id:6s} {ev.normalized_value:16s} conf={ev.confidence:.2f}")
            total += len(events)
        print(f"\n  total events: {total}")

        # ---- Step 2: Refresh aggregates ----
        print()
        print("=" * 80)
        print("Step 2 -- Refresh aggregates")
        print("=" * 80)
        aggregates = refresh_aggregates(db, workspace_id=ws.id, attribute_name=ATTRIBUTE)
        aggregates.sort(key=lambda a: (-a.proposal_count, a.canonical_value))
        for agg in aggregates:
            _print_agg(agg)

        # ---- Step 3: Approve hiit, yoga, hiking, running ----
        print()
        print("=" * 80)
        print("Step 3 -- Approve: hiit, yoga, hiking, running")
        print("=" * 80)

        current = []
        for value in ["hiit", "yoga", "hiking", "running"]:
            agg = _get_agg(db, ws.id, value)
            check = promotion_readiness(agg)
            force = not check.ready
            if force:
                print(f"  {value:16s} below thresholds -- using force=True "
                      f"(count={agg.proposal_count}, products={agg.distinct_product_count})")
            _, current = approve_aggregate(
                db,
                aggregate_id=agg.id,
                current_allowed_values=current,
                force=force,
                review_note=f"Distinct activity type with clear evidence.",
            )
            print(f"  approved: {value}")

        print(f"\n  allowed_values after approvals: {current}")

        # ---- Step 4: Merge plyometrics -> hiit ----
        print()
        print("=" * 80)
        print("Step 4 -- Merge: plyometrics -> hiit (flattened_child)")
        print("=" * 80)
        plyo = _get_agg(db, ws.id, "plyometrics")
        merge_aggregate(
            db,
            aggregate_id=plyo.id,
            target_allowed_value="hiit",
            current_allowed_values=current,
            merge_reason=MERGE_REASON_FLATTENED_CHILD,
            review_note="Plyometrics is a sub-type of HIIT training; flatten until hierarchy support.",
        )
        print(f"  merged: plyometrics -> hiit")

        # ---- Step 5: Final state ----
        print()
        print("=" * 80)
        print("Step 5 -- Final aggregate state")
        print("=" * 80)
        all_aggs = (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.attribute_name == ATTRIBUTE,
            )
            .order_by(ProposedAttributeValueAggregate.cluster_key)
            .all()
        )
        for agg in all_aggs:
            _print_agg(agg)

        print()
        print("=" * 80)
        print("Step 6 -- DB-backed taxonomy for activity_type")
        print("=" * 80)
        live = get_allowed_values(db, ws.id, ATTRIBUTE)
        print(f"  allowed_values = {live}")

        # ---- Step 7: Re-run enrichment prompt ----
        print()
        print("=" * 80)
        print("Step 7 -- Enrichment prompt with DB-backed allowed_values")
        print("=" * 80)
        defs = json.loads(
            (SEED_DIR / "attribute_definitions.json").read_text(encoding="utf-8")
        )
        attr_def = AttributeDefinition(
            **next(d for d in defs if d["name"] == ATTRIBUTE)
        )
        obj = {
            "product_id": "PX01",
            "name": "HIIT Performance Bra",
            "category": "bras",
            "description": (
                "Black high-support sports bra built for HIIT, box jumps, "
                "and plyometric circuits. Bonded seams eliminate chafing "
                "during high-rep explosive movements."
            ),
            "material": "nylon spandex",
            "fit": "compression",
        }
        prompt = get_prompt_for_attribute(attr_def, obj, db=db, workspace_id=ws.id)
        for line in prompt.split("\n"):
            if "Allowed values" in line or line.startswith("- "):
                print(f"  {line}")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
