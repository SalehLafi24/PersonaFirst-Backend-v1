"""End-to-end activity_type value-discovery flow.

Uses the enrichment fixture outputs from run_text_enrichment.py MODEL_OUTPUTS
for the 5 target products (P001, P007, P015, P016, P018).

Rolls back all DB writes at the end.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.proposed_attribute_value_service import (
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)

WORKSPACE_SLUG = "personafirst-starter"
ATTRIBUTE = "activity_type"
TARGET_PIDS = ["P001", "P007", "P015", "P016", "P018"]

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"

MODEL_OUTPUTS_BY_PID: dict[str, dict] = {
    "P001": {
        "attribute_name": "activity_type",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "running", "confidence": 0.96, "evidence": ["\"maximum support for running\""]},
            {"value": "hiit", "confidence": 0.95, "evidence": ["\"running and HIIT\""]},
        ],
        "warnings": [],
    },
    "P007": {
        "attribute_name": "activity_type",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "hiking", "confidence": 0.96, "evidence": ["\"designed for hiking, backpacking, and long travel days\""]},
            {"value": "backpacking", "confidence": 0.94, "evidence": ["\"designed for hiking, backpacking, and long travel days\""]},
        ],
        "warnings": [],
    },
    "P015": {
        "attribute_name": "activity_type",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    "P016": {
        "attribute_name": "activity_type",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    "P018": {
        "attribute_name": "activity_type",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {"value": "yoga", "confidence": 0.96, "evidence": ["\"for yoga and lounge wear\""]},
        ],
        "warnings": [],
    },
}


def _build_output(raw: dict) -> EnrichmentOutput:
    proposed = []
    for item in raw.get("proposed_values") or []:
        conf = float(item.get("confidence", 0))
        evidence = list(item.get("evidence") or [])
        if conf >= 0.8 and evidence:
            proposed.append(ProposedValue(value=item["value"], confidence=conf, evidence=evidence))
    return EnrichmentOutput(
        attribute_name=raw.get("attribute_name", ATTRIBUTE),
        attribute_class=raw.get("attribute_class", "contextual_semantic"),
        values=[],
        proposed_values=proposed,
        warnings=list(raw.get("warnings") or []),
        source=EnrichmentSource.TEXT,
    )


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # Step 1 -- record proposal events
        print("=" * 72)
        print("Step 1 -- Record proposal events")
        print("=" * 72)
        total = 0
        for pid in TARGET_PIDS:
            raw = MODEL_OUTPUTS_BY_PID[pid]
            output = _build_output(raw)
            events = record_events_from_output(
                db, workspace_id=ws.id, product_id=pid, output=output,
            )
            for ev in events:
                print(f"  {ev.product_id:6s} raw={ev.proposed_value_raw!r:16s} "
                      f"norm={ev.normalized_value!r:16s} conf={ev.confidence:.2f}")
            total += len(events)
        print(f"\n  total events: {total}")

        # Step 2 -- refresh aggregates
        print()
        print("=" * 72)
        print("Step 2 -- Refresh aggregates")
        print("=" * 72)
        aggregates = refresh_aggregates(
            db, workspace_id=ws.id, attribute_name=ATTRIBUTE,
        )
        aggregates.sort(key=lambda a: (-a.proposal_count, a.canonical_value))

        for agg in aggregates:
            check = promotion_readiness(agg)
            print(f"\n  canonical_value      = {agg.canonical_value!r}")
            print(f"  cluster_key          = {agg.cluster_key!r}")
            print(f"  proposal_count       = {agg.proposal_count}")
            print(f"  distinct_products    = {agg.distinct_product_count}")
            print(f"  avg_confidence       = {agg.avg_confidence:.3f}")
            print(f"  max_confidence       = {agg.max_confidence:.3f}")
            print(f"  sample_products      = {agg.sample_product_ids}")
            print(f"  sample_evidence      = {agg.sample_evidence}")
            print(f"  status               = {agg.status}")
            print(f"  promotion_ready      = {check.ready}")
            if check.reasons:
                for r in check.reasons:
                    print(f"    blocked: {r}")

        # Controls
        print()
        print("=" * 72)
        print("Controls")
        print("=" * 72)
        control_pids = {"P015", "P016"}
        control_proposals = sum(
            len(_build_output(MODEL_OUTPUTS_BY_PID[pid]).proposed_values)
            for pid in control_pids
        )
        print(f"  {sorted(control_pids)} -> {control_proposals} proposals (expected 0)")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
