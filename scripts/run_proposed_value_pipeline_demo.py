"""End-to-end demo of the taxonomy-evolution pipeline.

Stages exercised:
    1. Build EnrichmentOutputs (hand-crafted to simulate what the
       extraction service would emit for a few products).
    2. Ingest proposed values as raw events.
    3. Refresh aggregates.
    4. Show the reviewable rollups.
    5. Demonstrate an `approve` action for "hiking" (enough evidence) and
       a `reject` (or low-evidence `force` approve) for "hiit".
    6. Show the extended allowed_values list after approval.

The demo rolls back its DB writes at the end so the workspace isn't
polluted between runs.
"""
from __future__ import annotations

import json

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.proposed_attribute_value_service import (
    PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
    PROMOTION_MIN_PROPOSAL_COUNT,
    approve_aggregate,
    promotion_readiness,
    record_events_from_outputs,
    refresh_aggregates,
    reject_aggregate,
)

WORKSPACE_SLUG = "personafirst-starter"
ATTRIBUTE_NAME = "activity"
CURRENT_ALLOWED_VALUES = [
    "running",
    "training",
    "yoga",
    "pilates",
    "lounge",
    "travel",
]


def _activity_output(value: str, confidence: float, evidence: list[str]) -> EnrichmentOutput:
    return EnrichmentOutput(
        attribute_name=ATTRIBUTE_NAME,
        attribute_class="contextual_semantic",
        values=[],  # not relevant for this demo
        proposed_values=[
            ProposedValue(value=value, confidence=confidence, evidence=evidence),
        ],
        warnings=[],
        source=EnrichmentSource.TEXT,
    )


def _build_sample_outputs() -> dict[str, list[EnrichmentOutput]]:
    """Hand-crafted enrichment outputs matching what the real extraction
    service would return for each sample product.

    "hiit": only 1 product (P001) → should NOT meet the conservative
        promotion defaults (distinct_product_count < 2).
    "hiking": 3 different products → meets the default promotion bar.
    """
    return {
        "P001": [
            _activity_output("hiit", 0.95, ['"running and HIIT"']),
        ],
        "P007": [
            _activity_output(
                "hiking",
                0.96,
                ['"designed for hiking, backpacking, and long travel days"'],
            ),
        ],
        # Two more products independently propose "hiking" to push the
        # aggregate past PROMOTION_MIN_DISTINCT_PRODUCTS and
        # PROMOTION_MIN_PROPOSAL_COUNT. In a real run these come from
        # actual enrichment outputs; here we fake the emissions.
        "P901_SimHikerTop": [
            _activity_output(
                "Hiking",
                0.92,
                ['"built for day hiking and summit scrambles"'],
            ),
        ],
        "P902_SimHikerBottom": [
            _activity_output(
                "hiking  ",
                0.88,
                ['"ideal for hiking and fast packing"'],
            ),
        ],
    }


def _print_aggregates(db, workspace_id: int) -> None:
    from app.models.proposed_attribute_value import ProposedAttributeValueAggregate
    aggs = (
        db.query(ProposedAttributeValueAggregate)
        .filter(
            ProposedAttributeValueAggregate.workspace_id == workspace_id,
            ProposedAttributeValueAggregate.attribute_name == ATTRIBUTE_NAME,
        )
        .order_by(ProposedAttributeValueAggregate.cluster_key)
        .all()
    )
    for a in aggs:
        check = promotion_readiness(a)
        print(
            f"  id={a.id} cluster_key={a.cluster_key!r} canonical={a.canonical_value!r}"
        )
        print(
            f"    proposal_count={a.proposal_count} distinct_products={a.distinct_product_count}"
            f" avg_conf={a.avg_confidence:.3f} max_conf={a.max_confidence:.3f}"
        )
        print(f"    sample_products={a.sample_product_ids}")
        print(f"    sample_evidence={a.sample_evidence}")
        print(f"    status={a.status} promoted_to={a.promoted_to_allowed_value}")
        print(f"    promotion_ready={check.ready}")
        if check.reasons:
            for r in check.reasons:
                print(f"      blocked by: {r}")


def main() -> None:
    db = SessionLocal()
    try:
        ws = (
            db.query(Workspace)
            .filter(Workspace.slug == WORKSPACE_SLUG)
            .first()
        )
        if ws is None:
            raise SystemExit(
                f"Workspace '{WORKSPACE_SLUG}' not found — seed the starter dataset first."
            )

        sample_outputs = _build_sample_outputs()

        print("=" * 72)
        print("Stage 1 — ingest raw proposal events")
        print("=" * 72)
        events = record_events_from_outputs(
            db,
            workspace_id=ws.id,
            product_outputs=sample_outputs,
        )
        print(f"  wrote {len(events)} raw events")
        for ev in events:
            print(
                f"    product={ev.product_id:28s} attr={ev.attribute_name:10s}"
                f" raw={ev.proposed_value_raw!r:12s} norm={ev.normalized_value!r:12s}"
                f" conf={ev.confidence:.2f}"
            )

        print()
        print("=" * 72)
        print("Stage 2 — refresh aggregates")
        print("=" * 72)
        refresh_aggregates(db, workspace_id=ws.id, attribute_name=ATTRIBUTE_NAME)
        _print_aggregates(db, ws.id)

        print()
        print("=" * 72)
        print(
            "Stage 3 — review decisions ("
            f"defaults: count>={PROMOTION_MIN_PROPOSAL_COUNT}"
            f" avg_conf>={PROMOTION_MIN_AVG_CONFIDENCE}"
            f" distinct_products>={PROMOTION_MIN_DISTINCT_PRODUCTS})"
        )
        print("=" * 72)

        from app.models.proposed_attribute_value import ProposedAttributeValueAggregate
        hiit_agg = (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.cluster_key == "hiit",
            )
            .one()
        )
        hiking_agg = (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.cluster_key == "hiking",
            )
            .one()
        )

        print(f"  hiit:   promotion_readiness = {promotion_readiness(hiit_agg)}")
        print(f"  hiking: promotion_readiness = {promotion_readiness(hiking_agg)}")

        print("\n  action 1: approve 'hiking' (meets defaults)")
        _, extended = approve_aggregate(
            db,
            aggregate_id=hiking_agg.id,
            current_allowed_values=CURRENT_ALLOWED_VALUES,
        )
        print(f"    aggregate status   = {hiking_agg.status}")
        print(f"    promoted_to        = {hiking_agg.promoted_to_allowed_value!r}")
        print(f"    allowed_values before = {CURRENT_ALLOWED_VALUES}")
        print(f"    allowed_values after  = {extended}")

        print("\n  action 2: reject 'hiit' (below defaults)")
        reject_aggregate(db, aggregate_id=hiit_agg.id)
        print(f"    aggregate status   = {hiit_agg.status}")
        print(f"    promoted_to        = {hiit_agg.promoted_to_allowed_value}")
        print(
            "    raw events preserved — a later reviewer can re-inspect them "
            "and override the decision."
        )

        print("\n  guardrail: trying to approve 'hiit' without --force")
        try:
            approve_aggregate(
                db,
                aggregate_id=hiit_agg.id,
                current_allowed_values=CURRENT_ALLOWED_VALUES,
            )
        except ValueError as exc:
            print(f"    refused: {exc}")

        print("\n  final aggregate state after review:")
        _print_aggregates(db, ws.id)
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
