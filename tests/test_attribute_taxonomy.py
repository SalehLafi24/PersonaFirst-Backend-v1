"""Tests for the DB-backed attribute taxonomy pipeline.

Covers:
    - get_allowed_values falls back to defaults when DB is empty
    - get_allowed_values returns DB rows when present
    - approve_aggregate writes a new allowed-value row
    - merge_aggregate does NOT create duplicate allowed-value rows
    - enrichment uses DB-backed values when workspace_id is provided
    - enrichment still works when workspace_id is omitted (backward compat)
"""
import pytest

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeBehavior,
    AttributeDefinition,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.attribute_enrichment_service import get_prompt_for_attribute
from app.services.attribute_taxonomy_service import (
    get_allowed_values,
    set_allowed_values,
    upsert_allowed_value,
)
from app.services.proposed_attribute_value_service import (
    approve_aggregate,
    merge_aggregate,
    record_events_from_output,
    refresh_aggregates,
)


DEFAULTS = ["running", "training", "yoga", "pilates", "lounge", "travel"]


def _ws(db) -> Workspace:
    ws = Workspace(name="test-ws", slug="test-ws-slug")
    db.add(ws)
    db.flush()
    return ws


def _attr_def(
    allowed_values: list[str] | None = None,
) -> AttributeDefinition:
    return AttributeDefinition(
        name="activity",
        object_type="product",
        class_name="contextual_semantic",
        value_mode="multi",
        allowed_values=allowed_values if allowed_values is not None else DEFAULTS,
        description="Activity taxonomy test attribute.",
        evidence_sources=["description"],
        behavior=AttributeBehavior(multi_value_allowed=True),
    )


# ======================================================================
# get_allowed_values — fallback and DB-backed
# ======================================================================


class TestGetAllowedValues:

    def test_fallback_to_defaults_when_db_empty(self, db):
        ws = _ws(db)
        result = get_allowed_values(
            db, ws.id, "activity", default_values=DEFAULTS,
        )
        assert result == DEFAULTS

    def test_fallback_returns_empty_when_no_defaults_and_no_rows(self, db):
        ws = _ws(db)
        result = get_allowed_values(db, ws.id, "activity")
        assert result == []

    def test_returns_db_values_when_present(self, db):
        ws = _ws(db)
        set_allowed_values(db, ws.id, "activity", ["hiking", "running"])
        result = get_allowed_values(
            db, ws.id, "activity", default_values=DEFAULTS,
        )
        assert result == ["hiking", "running"]

    def test_ignores_inactive_rows(self, db):
        ws = _ws(db)
        upsert_allowed_value(db, ws.id, "activity", "hiking")
        upsert_allowed_value(db, ws.id, "activity", "running")
        # Deactivate hiking.
        row = (
            db.query(AttributeAllowedValue)
            .filter(
                AttributeAllowedValue.workspace_id == ws.id,
                AttributeAllowedValue.value == "hiking",
            )
            .one()
        )
        row.is_active = False
        db.flush()
        result = get_allowed_values(
            db, ws.id, "activity", default_values=DEFAULTS,
        )
        assert result == ["running"]

    def test_workspace_isolation(self, db):
        ws1 = _ws(db)
        ws2 = Workspace(name="other", slug="other-slug")
        db.add(ws2)
        db.flush()
        set_allowed_values(db, ws1.id, "activity", ["hiking"])
        result = get_allowed_values(
            db, ws2.id, "activity", default_values=DEFAULTS,
        )
        assert result == DEFAULTS


# ======================================================================
# upsert_allowed_value
# ======================================================================


class TestUpsert:

    def test_idempotent_insert(self, db):
        ws = _ws(db)
        r1 = upsert_allowed_value(db, ws.id, "activity", "hiking")
        r2 = upsert_allowed_value(db, ws.id, "activity", "hiking")
        assert r1.id == r2.id
        count = (
            db.query(AttributeAllowedValue)
            .filter(
                AttributeAllowedValue.workspace_id == ws.id,
                AttributeAllowedValue.value == "hiking",
            )
            .count()
        )
        assert count == 1

    def test_reactivates_inactive(self, db):
        ws = _ws(db)
        row = upsert_allowed_value(db, ws.id, "activity", "hiking")
        row.is_active = False
        db.flush()
        reactivated = upsert_allowed_value(db, ws.id, "activity", "hiking")
        assert reactivated.is_active is True
        assert reactivated.id == row.id


# ======================================================================
# approve_aggregate → writes allowed-value row
# ======================================================================


class TestApproveWritesTaxonomy:

    def _seed_aggregate(self, db, ws, value="hiking", count=3, products=3):
        """Create raw events and refresh to get a promotable aggregate."""
        for i in range(count):
            pid = f"P{900+i}"
            out = EnrichmentOutput(
                attribute_name="activity",
                attribute_class="contextual_semantic",
                values=[],
                proposed_values=[
                    ProposedValue(
                        value=value,
                        confidence=0.90 + i * 0.02,
                        evidence=[f"evidence-{i}"],
                    ),
                ],
                warnings=[],
                source=EnrichmentSource.TEXT,
            )
            record_events_from_output(
                db, workspace_id=ws.id, product_id=pid, output=out,
            )
        refresh_aggregates(db, workspace_id=ws.id, attribute_name="activity")
        return (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.canonical_value == value,
            )
            .one()
        )

    def test_approve_creates_allowed_value_row(self, db):
        ws = _ws(db)
        agg = self._seed_aggregate(db, ws, "hiking")
        approve_aggregate(
            db,
            aggregate_id=agg.id,
            current_allowed_values=DEFAULTS,
        )
        # The DB should now have a row.
        result = get_allowed_values(db, ws.id, "activity", default_values=DEFAULTS)
        assert "hiking" in result

    def test_approve_idempotent(self, db):
        """Approving twice (via force) should not create duplicate rows."""
        ws = _ws(db)
        agg = self._seed_aggregate(db, ws, "hiking")
        approve_aggregate(
            db, aggregate_id=agg.id, current_allowed_values=DEFAULTS,
        )
        # Manually reset status so we can call approve again.
        agg.status = "pending"
        db.flush()
        approve_aggregate(
            db, aggregate_id=agg.id, current_allowed_values=DEFAULTS,
        )
        count = (
            db.query(AttributeAllowedValue)
            .filter(
                AttributeAllowedValue.workspace_id == ws.id,
                AttributeAllowedValue.attribute_name == "activity",
                AttributeAllowedValue.value == "hiking",
            )
            .count()
        )
        assert count == 1


# ======================================================================
# merge_aggregate → no new row
# ======================================================================


class TestMergeNoNewRow:

    def test_merge_does_not_create_new_allowed_value(self, db):
        ws = _ws(db)
        # Create raw events + aggregate for "hiit".
        out = EnrichmentOutput(
            attribute_name="activity",
            attribute_class="contextual_semantic",
            values=[],
            proposed_values=[
                ProposedValue(value="hiit", confidence=0.95, evidence=["HIIT"]),
            ],
            warnings=[],
            source=EnrichmentSource.TEXT,
        )
        record_events_from_output(
            db, workspace_id=ws.id, product_id="P001", output=out,
        )
        refresh_aggregates(db, workspace_id=ws.id, attribute_name="activity")
        agg = (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.canonical_value == "hiit",
            )
            .one()
        )
        merge_aggregate(
            db,
            aggregate_id=agg.id,
            target_allowed_value="training",
            current_allowed_values=DEFAULTS,
        )
        # No "hiit" row should be in allowed values.
        result = get_allowed_values(
            db, ws.id, "activity", default_values=DEFAULTS,
        )
        # DB has no active rows → fallback to defaults, which doesn't have "hiit".
        assert "hiit" not in result


# ======================================================================
# Enrichment integration — DB-backed allowed values
# ======================================================================


class TestEnrichmentIntegration:

    def test_prompt_uses_db_values_when_workspace_provided(self, db):
        ws = _ws(db)
        set_allowed_values(db, ws.id, "activity", ["hiking", "running"])
        attr = _attr_def()
        obj = {"name": "Trail Tee", "description": "For hiking."}
        prompt = get_prompt_for_attribute(
            attr, obj, db=db, workspace_id=ws.id,
        )
        assert "hiking" in prompt
        # Static default "pilates" should NOT appear since DB values took over.
        assert "pilates" not in prompt

    def test_prompt_falls_back_when_no_db_rows(self, db):
        ws = _ws(db)
        attr = _attr_def()
        obj = {"name": "Trail Tee", "description": "For hiking."}
        prompt = get_prompt_for_attribute(
            attr, obj, db=db, workspace_id=ws.id,
        )
        # No DB rows → should use static defaults which include "pilates".
        assert "pilates" in prompt

    def test_prompt_works_without_workspace(self):
        """Backward-compat: omitting db and workspace_id uses static values."""
        attr = _attr_def()
        obj = {"name": "Trail Tee", "description": "For hiking."}
        prompt = get_prompt_for_attribute(attr, obj)
        assert "pilates" in prompt
        assert "running" in prompt


# ======================================================================
# End-to-end: approve → enrichment picks up the new value
# ======================================================================


class TestEndToEnd:

    def test_approved_value_appears_in_enrichment_prompt(self, db):
        ws = _ws(db)
        # Bootstrap the workspace taxonomy with defaults.
        set_allowed_values(db, ws.id, "activity", DEFAULTS)
        # Create a promotable aggregate for "hiking".
        for i in range(3):
            out = EnrichmentOutput(
                attribute_name="activity",
                attribute_class="contextual_semantic",
                values=[],
                proposed_values=[
                    ProposedValue(
                        value="hiking",
                        confidence=0.90 + i * 0.02,
                        evidence=[f"evidence-{i}"],
                    ),
                ],
                warnings=[],
                source=EnrichmentSource.TEXT,
            )
            record_events_from_output(
                db, workspace_id=ws.id, product_id=f"P{900+i}", output=out,
            )
        refresh_aggregates(db, workspace_id=ws.id, attribute_name="activity")
        agg = (
            db.query(ProposedAttributeValueAggregate)
            .filter(
                ProposedAttributeValueAggregate.workspace_id == ws.id,
                ProposedAttributeValueAggregate.canonical_value == "hiking",
            )
            .one()
        )
        approve_aggregate(
            db, aggregate_id=agg.id, current_allowed_values=DEFAULTS,
        )
        # Now build a prompt — "hiking" should appear in allowed values.
        attr = _attr_def()
        obj = {"name": "Trail Tee", "description": "For hiking."}
        prompt = get_prompt_for_attribute(
            attr, obj, db=db, workspace_id=ws.id,
        )
        assert "hiking" in prompt
        # And the old defaults should still be there.
        assert "running" in prompt
        assert "pilates" in prompt
