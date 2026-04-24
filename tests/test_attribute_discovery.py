"""Tests for the attribute-discovery pipeline.

Covers:
    - proposal event capture
    - aggregate refresh
    - attribute name normalization
    - conservative gating (promotion readiness)
    - approve / reject / merge
    - worked examples for layering_role and climate_suitability
"""
import pytest

from app.models.proposed_attribute import (
    ATTR_PROPOSAL_STATUS_APPROVED,
    ATTR_PROPOSAL_STATUS_MERGED,
    ATTR_PROPOSAL_STATUS_PENDING,
    ATTR_PROPOSAL_STATUS_REJECTED,
    ProposedAttributeAggregate,
    ProposedAttributeEvent,
)
from app.models.workspace import Workspace
from app.schemas.attribute_discovery import (
    AttributeDiscoveryOutput,
    ProposedAttribute,
)
from app.services.proposed_attribute_normalizer import normalize_attribute_name
from app.services.proposed_attribute_service import (
    ATTR_PROMOTION_MIN_DISTINCT_PRODUCTS,
    ATTR_PROMOTION_MIN_PROPOSAL_COUNT,
    approve_attribute_aggregate,
    attribute_promotion_readiness,
    merge_attribute_aggregate,
    record_attribute_events,
    refresh_attribute_aggregates,
    reject_attribute_aggregate,
)


def _ws(db) -> Workspace:
    ws = Workspace(name="test-ws", slug="test-ws-disc")
    db.add(ws)
    db.flush()
    return ws


def _discovery_output(*proposed: ProposedAttribute) -> AttributeDiscoveryOutput:
    return AttributeDiscoveryOutput(proposed_attributes=list(proposed))


def _pa(
    name: str = "layering_role",
    confidence: float = 0.92,
    description: str = "The role this garment plays in a layering system.",
    evidence: list[str] | None = None,
    values: list[str] | None = None,
    class_name: str = "contextual_semantic",
    targeting: str = "categorical_affinity",
) -> ProposedAttribute:
    return ProposedAttribute(
        attribute_name=name,
        confidence=confidence,
        description=description,
        evidence=evidence or ['"base layer"'],
        suggested_values=values or ["base", "mid", "outer"],
        suggested_class_name=class_name,
        suggested_targeting_mode=targeting,
    )


# ======================================================================
# Normalizer
# ======================================================================


class TestNormalizer:

    def test_lowercase_and_underscore(self):
        assert normalize_attribute_name("Layering Role") == "layering_role"

    def test_hyphen_to_underscore(self):
        assert normalize_attribute_name("climate-suitability") == "climate_suitability"

    def test_collapse_whitespace(self):
        assert normalize_attribute_name("  layering   role  ") == "layering_role"

    def test_strip_edge_punctuation(self):
        assert normalize_attribute_name("_layering_role_") == "layering_role"

    def test_none_returns_empty(self):
        assert normalize_attribute_name(None) == ""


# ======================================================================
# Event capture
# ======================================================================


class TestRecordEvents:

    def test_creates_event_per_proposed_attribute(self, db):
        ws = _ws(db)
        output = _discovery_output(
            _pa("layering_role"),
            _pa("climate_suitability", description="Weather conditions."),
        )
        events = record_attribute_events(
            db, workspace_id=ws.id, product_id="P001", output=output,
        )
        assert len(events) == 2
        assert events[0].normalized_attribute_name == "layering_role"
        assert events[1].normalized_attribute_name == "climate_suitability"

    def test_skips_empty_name(self, db):
        ws = _ws(db)
        output = _discovery_output(_pa(""))
        events = record_attribute_events(
            db, workspace_id=ws.id, product_id="P001", output=output,
        )
        assert len(events) == 0

    def test_no_events_for_empty_output(self, db):
        ws = _ws(db)
        output = _discovery_output()
        events = record_attribute_events(
            db, workspace_id=ws.id, product_id="P001", output=output,
        )
        assert len(events) == 0


# ======================================================================
# Aggregate refresh
# ======================================================================


class TestRefreshAggregates:

    def _seed(self, db, ws, name="layering_role", products=None, confidence=0.92):
        products = products or ["P001", "P002", "P003", "P004"]
        for pid in products:
            output = _discovery_output(
                _pa(name, confidence=confidence, evidence=[f'evidence from {pid}']),
            )
            record_attribute_events(
                db, workspace_id=ws.id, product_id=pid, output=output,
            )

    def test_creates_aggregate(self, db):
        ws = _ws(db)
        self._seed(db, ws, products=["P001"])
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        assert len(aggs) == 1
        assert aggs[0].canonical_attribute_name == "layering_role"
        assert aggs[0].proposal_count == 1
        assert aggs[0].status == ATTR_PROPOSAL_STATUS_PENDING

    def test_accumulates_across_products(self, db):
        ws = _ws(db)
        self._seed(db, ws, products=["P001", "P002", "P003"])
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        assert aggs[0].proposal_count == 3
        assert aggs[0].distinct_product_count == 3

    def test_merges_suggested_values(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(
                _pa("layering_role", values=["base", "mid"]),
            ),
        )
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P002",
            output=_discovery_output(
                _pa("layering_role", values=["mid", "outer"]),
            ),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        assert sorted(aggs[0].merged_suggested_values) == ["base", "mid", "outer"]

    def test_preserves_reviewer_touched_aggregates(self, db):
        ws = _ws(db)
        self._seed(db, ws, products=["P001"])
        refresh_attribute_aggregates(db, workspace_id=ws.id)
        agg = db.query(ProposedAttributeAggregate).filter(
            ProposedAttributeAggregate.workspace_id == ws.id,
        ).one()
        agg.status = ATTR_PROPOSAL_STATUS_REJECTED
        db.flush()
        # Add more events and re-refresh.
        self._seed(db, ws, products=["P002", "P003"])
        refresh_attribute_aggregates(db, workspace_id=ws.id)
        agg_after = db.query(ProposedAttributeAggregate).get(agg.id)
        assert agg_after.status == ATTR_PROPOSAL_STATUS_REJECTED
        assert agg_after.proposal_count == 1  # not updated


# ======================================================================
# Promotion readiness
# ======================================================================


class TestPromotionReadiness:

    def test_below_thresholds(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(_pa()),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        check = attribute_promotion_readiness(aggs[0])
        assert check.ready is False
        assert len(check.reasons) >= 1

    def test_meets_thresholds(self, db):
        ws = _ws(db)
        for i in range(ATTR_PROMOTION_MIN_PROPOSAL_COUNT):
            record_attribute_events(
                db, workspace_id=ws.id, product_id=f"P{i:03d}",
                output=_discovery_output(_pa(confidence=0.92)),
            )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        check = attribute_promotion_readiness(aggs[0])
        assert check.ready is True


# ======================================================================
# Approve / reject / merge
# ======================================================================


class TestReviewActions:

    def _promotable_agg(self, db, ws, name="layering_role"):
        for i in range(ATTR_PROMOTION_MIN_PROPOSAL_COUNT):
            record_attribute_events(
                db, workspace_id=ws.id, product_id=f"P{i:03d}",
                output=_discovery_output(_pa(name, confidence=0.92)),
            )
        refresh_attribute_aggregates(db, workspace_id=ws.id)
        return (
            db.query(ProposedAttributeAggregate)
            .filter(
                ProposedAttributeAggregate.workspace_id == ws.id,
                ProposedAttributeAggregate.cluster_key == normalize_attribute_name(name),
            )
            .one()
        )

    def test_approve_returns_definition_payload(self, db):
        ws = _ws(db)
        agg = self._promotable_agg(db, ws)
        agg_out, payload = approve_attribute_aggregate(
            db, aggregate_id=agg.id, review_note="Layering is a key dimension.",
        )
        assert agg_out.status == ATTR_PROPOSAL_STATUS_APPROVED
        assert agg_out.promoted_attribute_name == "layering_role"
        assert payload["name"] == "layering_role"
        assert payload["class_name"] == "contextual_semantic"
        assert "base" in payload["allowed_values"]

    def test_approve_refuses_below_threshold(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(_pa()),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        with pytest.raises(ValueError, match="not ready"):
            approve_attribute_aggregate(db, aggregate_id=aggs[0].id)

    def test_approve_with_force(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(_pa()),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        agg, payload = approve_attribute_aggregate(
            db, aggregate_id=aggs[0].id, force=True,
        )
        assert agg.status == ATTR_PROPOSAL_STATUS_APPROVED

    def test_reject(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(_pa()),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        agg = reject_attribute_aggregate(
            db, aggregate_id=aggs[0].id, merge_reason="noise",
            review_note="Too vague.",
        )
        assert agg.status == ATTR_PROPOSAL_STATUS_REJECTED
        assert agg.merge_reason == "noise"

    def test_merge_into_existing(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(_pa("exercise_intensity")),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        agg = merge_attribute_aggregate(
            db,
            aggregate_id=aggs[0].id,
            target_attribute_name="workout_intensity",
            existing_attribute_names=["workout_intensity", "activity_type"],
            merge_reason="synonym_to_existing",
            review_note="Same concept as workout_intensity.",
        )
        assert agg.status == ATTR_PROPOSAL_STATUS_MERGED
        assert agg.promoted_attribute_name == "workout_intensity"

    def test_merge_rejects_unknown_target(self, db):
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(_pa("exercise_intensity")),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        with pytest.raises(ValueError, match="not in existing"):
            merge_attribute_aggregate(
                db,
                aggregate_id=aggs[0].id,
                target_attribute_name="nonexistent",
                existing_attribute_names=["workout_intensity"],
            )


# ======================================================================
# Worked example: layering_role (approve)
# ======================================================================


class TestLayeringRoleExample:

    def test_end_to_end_layering_role(self, db):
        ws = _ws(db)
        products = [
            ("P001", '"base layer for cold runs"'),
            ("P002", '"mid layer fleece for warmth"'),
            ("P003", '"outer shell for rain protection"'),
            ("P004", '"merino base layer for backpacking"'),
        ]
        for pid, evidence in products:
            record_attribute_events(
                db, workspace_id=ws.id, product_id=pid,
                output=_discovery_output(
                    _pa("layering_role", confidence=0.93, evidence=[evidence]),
                ),
            )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        assert len(aggs) == 1
        agg = aggs[0]
        assert agg.proposal_count == 4
        assert agg.distinct_product_count == 4

        check = attribute_promotion_readiness(agg)
        assert check.ready is True

        _, payload = approve_attribute_aggregate(
            db, aggregate_id=agg.id,
            review_note="Layering is a key dimension for outdoor products.",
        )
        assert payload["name"] == "layering_role"
        assert set(payload["allowed_values"]) == {"base", "mid", "outer"}


# ======================================================================
# Worked example: climate_suitability (merge into existing)
# ======================================================================


class TestClimateSuitabilityExample:

    def test_merge_into_travel_friendly(self, db):
        """climate_suitability overlaps with travel_friendly in a flat
        taxonomy. Merge it rather than creating a near-duplicate."""
        ws = _ws(db)
        record_attribute_events(
            db, workspace_id=ws.id, product_id="P001",
            output=_discovery_output(
                _pa(
                    "climate_suitability",
                    description="Weather suitability for the product.",
                    evidence=['"designed for cold weather runs"'],
                    values=["cold", "warm", "all-weather"],
                ),
            ),
        )
        aggs = refresh_attribute_aggregates(db, workspace_id=ws.id)
        agg = aggs[0]
        assert agg.canonical_attribute_name == "climate_suitability"

        # Reviewer decides it overlaps with travel_friendly.
        merged = merge_attribute_aggregate(
            db,
            aggregate_id=agg.id,
            target_attribute_name="travel_friendly",
            existing_attribute_names=[
                "occasion", "activity", "travel_friendly", "workout_intensity",
            ],
            merge_reason="synonym_to_existing",
            review_note="Climate context is captured by travel_friendly for now.",
        )
        assert merged.status == ATTR_PROPOSAL_STATUS_MERGED
        assert merged.promoted_attribute_name == "travel_friendly"
