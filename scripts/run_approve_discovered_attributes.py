"""Approve layering_role and use_environment, then persist to seed definitions.

Steps:
    1. Re-ingest discovery events and refresh aggregates (transactional).
    2. Approve layering_role and use_environment via the review service.
    3. Capture the returned AttributeDefinition payloads.
    4. Rename use_environment -> environment in the payload.
    5. Append both to seed_data/attribute_definitions.json.
    6. Validate the final file.

DB writes are rolled back (these are workspace-scoped aggregates in the
demo workspace). The seed JSON update is the durable output.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.database import SessionLocal
from app.models.proposed_attribute import ProposedAttributeAggregate
from app.models.workspace import Workspace
from app.schemas.attribute_discovery import (
    AttributeDiscoveryOutput,
    ProposedAttribute,
)
from app.services.proposed_attribute_service import (
    approve_attribute_aggregate,
    attribute_promotion_readiness,
    record_attribute_events,
    refresh_attribute_aggregates,
)

WORKSPACE_SLUG = "personafirst-starter"
SEED_FILE = Path(__file__).resolve().parent.parent / "seed_data" / "attribute_definitions.json"

DISCOVERY_EVENTS: dict[str, list[dict]] = {
    "PX05": [{"attribute_name": "layering_role", "confidence": 0.88, "description": "The role this garment plays in a layering system.", "evidence": ["\"lightweight hoodie\"", "\"Packs into hood pocket\""], "suggested_values": ["base", "mid", "outer"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX08": [{"attribute_name": "layering_role", "confidence": 0.89, "description": "The role this garment plays in a layering system.", "evidence": ["\"Airport Layer Jacket\"", "\"packable travel jacket\""], "suggested_values": ["base", "mid", "outer"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX14": [{"attribute_name": "layering_role", "confidence": 0.91, "description": "The role this garment plays in a layering system.", "evidence": ["\"Ultra-light windbreaker\""], "suggested_values": ["base", "mid", "outer"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX18": [{"attribute_name": "layering_role", "confidence": 0.90, "description": "The role this garment plays in a layering system.", "evidence": ["\"Protective jacket for hiking\""], "suggested_values": ["base", "mid", "outer"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"},
             {"attribute_name": "use_environment", "confidence": 0.92, "description": "The physical setting or environment a product is designed to be used in.", "evidence": ["\"extended outdoor activity\""], "suggested_values": ["indoor", "outdoor", "studio"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX19": [{"attribute_name": "layering_role", "confidence": 0.95, "description": "The role this garment plays in a layering system.", "evidence": ["\"Merino base layer designed for multi-day backpacking\""], "suggested_values": ["base", "mid", "outer"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"},
             {"attribute_name": "use_environment", "confidence": 0.90, "description": "The physical setting or environment a product is designed to be used in.", "evidence": ["\"extended outdoor travel\""], "suggested_values": ["indoor", "outdoor", "studio"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX04": [{"attribute_name": "use_environment", "confidence": 0.93, "description": "The physical setting or environment a product is designed to be used in.", "evidence": ["\"indoor cycling and spin class\""], "suggested_values": ["indoor", "outdoor", "studio"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX15": [{"attribute_name": "use_environment", "confidence": 0.91, "description": "The physical setting or environment a product is designed to be used in.", "evidence": ["\"low-impact studio sessions\""], "suggested_values": ["indoor", "outdoor", "studio"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
    "PX17": [{"attribute_name": "use_environment", "confidence": 0.94, "description": "The physical setting or environment a product is designed to be used in.", "evidence": ["\"long outdoor walks and rugged terrain\""], "suggested_values": ["indoor", "outdoor", "studio"], "suggested_class_name": "contextual_semantic", "suggested_targeting_mode": "categorical_affinity"}],
}


def main() -> None:
    db = SessionLocal()
    payloads: list[dict] = []
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # -- Ingest + aggregate --
        for pid, proposals in DISCOVERY_EVENTS.items():
            output = AttributeDiscoveryOutput(
                proposed_attributes=[ProposedAttribute(**p) for p in proposals],
            )
            record_attribute_events(
                db, workspace_id=ws.id, product_id=pid, output=output,
            )
        refresh_attribute_aggregates(db, workspace_id=ws.id)

        # -- Approve layering_role --
        lr_agg = (
            db.query(ProposedAttributeAggregate)
            .filter(
                ProposedAttributeAggregate.workspace_id == ws.id,
                ProposedAttributeAggregate.cluster_key == "layering_role",
            )
            .one()
        )
        check = attribute_promotion_readiness(lr_agg)
        print(f"layering_role: ready={check.ready} count={lr_agg.proposal_count} products={lr_agg.distinct_product_count}")
        lr_agg_out, lr_payload = approve_attribute_aggregate(
            db, aggregate_id=lr_agg.id,
            review_note="Distinct dimension for outfit-building recommendations.",
        )
        print(f"  approved: status={lr_agg_out.status}")
        payloads.append(lr_payload)

        # -- Approve use_environment --
        ue_agg = (
            db.query(ProposedAttributeAggregate)
            .filter(
                ProposedAttributeAggregate.workspace_id == ws.id,
                ProposedAttributeAggregate.cluster_key == "use_environment",
            )
            .one()
        )
        check = attribute_promotion_readiness(ue_agg)
        print(f"use_environment: ready={check.ready} count={ue_agg.proposal_count} products={ue_agg.distinct_product_count}")
        ue_agg_out, ue_payload = approve_attribute_aggregate(
            db, aggregate_id=ue_agg.id,
            review_note="Distinct dimension for context filtering (indoor vs outdoor vs studio).",
        )
        # Rename use_environment -> environment
        ue_payload["name"] = "environment"
        ue_payload["description"] = "The physical setting or environment a product is designed to be used in."
        print(f"  approved: status={ue_agg_out.status} (renamed to 'environment')")
        payloads.append(ue_payload)

    finally:
        db.rollback()
        db.close()

    # -- Update seed JSON --
    defs = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    existing_names = {d["name"] for d in defs}
    added = []
    for payload in payloads:
        if payload["name"] in existing_names:
            print(f"  SKIP: {payload['name']!r} already in definitions")
            continue
        defs.append(payload)
        added.append(payload["name"])

    SEED_FILE.write_text(
        json.dumps(defs, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"\nAppended to {SEED_FILE.name}: {added}")
    print(f"Total definitions: {len(defs)}")

    # -- Validate --
    reloaded = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    names = [d["name"] for d in reloaded]
    assert len(names) == len(set(names)), "DUPLICATE detected!"
    for name in added:
        entry = next(d for d in reloaded if d["name"] == name)
        print(f"\n  {name}:")
        print(f"    class_name      = {entry['class_name']}")
        print(f"    targeting_mode  = {entry['targeting_mode']}")
        print(f"    value_mode      = {entry['value_mode']}")
        print(f"    allowed_values  = {entry['allowed_values']}")
        print(f"    description     = {entry['description']}")
    print("\nValidation passed. No duplicates. Enrichment will treat these as known attributes.")


if __name__ == "__main__":
    main()
