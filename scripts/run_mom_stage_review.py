"""Execute the documented mom_stage flat-taxonomy review decisions.

Decisions (from the model module docstring):
    pregnancy  -> approve
    postpartum -> approve
    nursing    -> merge into postpartum as flattened_child
    newborn    -> hold pending (below thresholds)

Runs against fixture data, rolls back at the end.
"""
from __future__ import annotations

from app.core.database import SessionLocal
from app.models.proposed_attribute_value import (
    MERGE_REASON_FLATTENED_CHILD,
    ProposedAttributeValueAggregate,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.proposed_attribute_value_service import (
    approve_aggregate,
    merge_aggregate,
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)
from tests.fixtures.mom_stage_test_data import MODEL_OUTPUTS, PRODUCTS

WORKSPACE_SLUG = "personafirst-starter"
ATTRIBUTE = "mom_stage"


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


def _print_agg(agg: ProposedAttributeValueAggregate) -> None:
    check = promotion_readiness(agg)
    print(f"  {agg.canonical_value:14s} status={agg.status:10s} "
          f"promoted_to={str(agg.promoted_to_allowed_value):14s} "
          f"merge_reason={str(agg.merge_reason):22s} "
          f"review_note={agg.review_note or ''}")


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")

        # -- Ingest + aggregate -----------------------------------------------
        for product in PRODUCTS:
            pid = product["product_id"]
            raw = MODEL_OUTPUTS[(pid, ATTRIBUTE)]
            output = _build_output(raw)
            record_events_from_output(db, workspace_id=ws.id, product_id=pid, output=output)
        refresh_aggregates(db, workspace_id=ws.id, attribute_name=ATTRIBUTE)

        print("=" * 90)
        print("Aggregates before review")
        print("=" * 90)
        for val in ["pregnancy", "postpartum", "nursing", "newborn"]:
            _print_agg(_get_agg(db, ws.id, val))

        # -- Review decisions -------------------------------------------------
        print()
        print("=" * 90)
        print("Executing documented review decisions")
        print("=" * 90)

        # 1. Approve pregnancy
        preg = _get_agg(db, ws.id, "pregnancy")
        approve_aggregate(
            db,
            aggregate_id=preg.id,
            current_allowed_values=[],
            review_note="Distinct stage with high evidence across 3 products.",
        )
        print("  approved:  pregnancy")

        # 2. Approve postpartum
        pp = _get_agg(db, ws.id, "postpartum")
        approve_aggregate(
            db,
            aggregate_id=pp.id,
            current_allowed_values=["pregnancy"],
            review_note="Distinct stage with high evidence across 3 products.",
        )
        print("  approved:  postpartum")

        # 3. Merge nursing -> postpartum as flattened_child
        nurs = _get_agg(db, ws.id, "nursing")
        merge_aggregate(
            db,
            aggregate_id=nurs.id,
            target_allowed_value="postpartum",
            current_allowed_values=["pregnancy", "postpartum"],
            merge_reason=MERGE_REASON_FLATTENED_CHILD,
            review_note="Nursing is a sub-stage of postpartum; flatten until hierarchy support arrives.",
        )
        print("  merged:    nursing -> postpartum (flattened_child)")

        # 4. Newborn stays pending (below thresholds, no action taken)
        print("  held:      newborn (pending, below thresholds)")

        # -- Final state ------------------------------------------------------
        print()
        print("=" * 90)
        print("Aggregates after review")
        print("=" * 90)
        for val in ["pregnancy", "postpartum", "nursing", "newborn"]:
            _print_agg(_get_agg(db, ws.id, val))

        # -- Resulting flat taxonomy ------------------------------------------
        print()
        print("=" * 90)
        print("Resulting flat taxonomy for mom_stage")
        print("=" * 90)
        from app.services.attribute_taxonomy_service import get_allowed_values
        live = get_allowed_values(db, ws.id, ATTRIBUTE)
        print(f"  allowed_values = {live}")
        print(f"  note: 'nursing' is NOT in allowed_values (merged into postpartum)")
        print(f"  note: 'newborn' is NOT in allowed_values (held pending)")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
