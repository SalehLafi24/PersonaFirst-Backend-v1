"""Tests for app/services/attribute_engine/aav_sync.py.

Covers:
  - dry-run writes nothing
  - apply inserts inactive rows
  - existing rows are untouched (active and inactive)
  - open taxonomies skipped entirely
  - idempotent re-run inserts zero
  - case-insensitive matching against existing AAV rows
"""
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.workspace import Workspace
from app.services.attribute_engine.aav_sync import (
    apply_sync_plan,
    compute_sync_plan,
)
from app.services.attribute_engine.manifest import (
    ApprovalSpec,
    AttributeManifest,
    AttributeManifestEntry,
    BackfillSpec,
    ProposalSpec,
    RecommendationSpec,
    TaxonomySpec,
)


# ======================================================================
# Helpers
# ======================================================================


def _ws(db) -> Workspace:
    ws = Workspace(name="aav-sync-ws", slug="aav-sync-ws")
    db.add(ws)
    db.flush()
    return ws


def _entry(
    name: str,
    *,
    kind: str,
    allowed_values: list[str] | None = None,
) -> AttributeManifestEntry:
    """Build a minimal AttributeManifestEntry. Only the fields aav_sync
    actually reads (name, taxonomy.kind, taxonomy.allowed_values) need
    to be meaningful; the rest are filled with safe defaults."""
    return AttributeManifestEntry(
        name=name,
        object_type="product",
        taxonomy=TaxonomySpec(
            kind=kind,
            cardinality="single",
            normalization_ref=None,
            unmatched_policy="discard",
            allowed_values=list(allowed_values or []),
        ),
        modes=[],
        precedence=[],
        csv_direct=None,
        regex_extract=None,
        llm_evidence=None,
        contextual_defaults=None,
        proposal=ProposalSpec(
            confidence_min=0.5, max_values_per_object=1, require_evidence=True,
        ),
        approval=ApprovalSpec(
            min_proposal_count=3, min_distinct_objects=2, min_avg_confidence=0.85,
        ),
        backfill=BackfillSpec(
            strategy="highest_confidence",
            single_row_per_object=True,
            idempotent=True,
        ),
        default_policy={"on_unresolved": "leave_null"},
        recommendation=RecommendationSpec(
            persona_relevant=False, score_weight=0.0, usage=None,
        ),
        recommendation_usage=None,
    )


def _manifest(*entries: AttributeManifestEntry) -> AttributeManifest:
    return AttributeManifest(
        version="test",
        entries={e.name: e for e in entries},
    )


def _aav(db, ws: Workspace, attribute_name: str, value: str, *, active: bool) -> None:
    db.add(AttributeAllowedValue(
        workspace_id=ws.id,
        attribute_name=attribute_name,
        value=value,
        is_active=active,
    ))
    db.flush()


# ======================================================================
# Plan computation
# ======================================================================


class TestComputeSyncPlan:

    def test_closed_attribute_with_no_aav_rows_is_all_missing(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "toddler", "kids", "adult"],
        ))

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        assert len(plan.attributes) == 1
        ap = plan.attributes[0]
        assert ap.kind == "closed"
        assert sorted(ap.missing_in_aav) == ["adult", "infant", "kids", "toddler"]
        assert ap.already_active == []
        assert ap.already_inactive == []
        assert plan.total_missing == 4

    def test_partition_active_inactive_missing(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "toddler", "adult"],
        ))
        _aav(db, ws, "age_group", "infant", active=True)
        _aav(db, ws, "age_group", "toddler", active=False)
        # 'adult' missing entirely.

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        ap = plan.attributes[0]
        assert ap.already_active == ["infant"]
        assert ap.already_inactive == ["toddler"]
        assert ap.missing_in_aav == ["adult"]

    def test_open_taxonomy_is_skipped(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry("product_type", kind="open"))

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        assert len(plan.open_attributes) == 1
        assert len(plan.closed_attributes) == 0
        ap = plan.open_attributes[0]
        assert ap.skipped_reason is not None
        assert "open" in ap.skipped_reason.lower()
        assert ap.missing_in_aav == []
        assert plan.total_missing == 0

    def test_case_insensitive_match_against_existing(self, db):
        """If AAV holds 'Adult' (capitalised) and manifest says 'adult'
        (lowercase), we must NOT mark it missing -- inserting a second
        row would violate the unique constraint and create a casing
        duplicate."""
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed", allowed_values=["adult"],
        ))
        _aav(db, ws, "age_group", "Adult", active=True)

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        ap = plan.attributes[0]
        assert ap.missing_in_aav == []
        assert ap.already_active == ["adult"]

    def test_compute_does_not_mutate_db(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "adult"],
        ))
        before = db.query(AttributeAllowedValue).count()

        compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)

        after = db.query(AttributeAllowedValue).count()
        assert before == after


# ======================================================================
# Apply (writes)
# ======================================================================


class TestApplySyncPlan:

    def test_apply_inserts_missing_as_inactive(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "adult", "newborn"],
        ))
        _aav(db, ws, "age_group", "infant", active=True)

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        result = apply_sync_plan(db, plan=plan)
        db.flush()

        assert result.inserted == 2
        assert sorted(result.inserted_by_attribute["age_group"]) == ["adult", "newborn"]

        rows = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws.id,
            AttributeAllowedValue.attribute_name == "age_group",
        ).all()
        by_value = {r.value: r.is_active for r in rows}
        assert by_value == {
            "infant": True,         # untouched
            "adult": False,         # inserted inactive
            "newborn": False,       # inserted inactive
        }

    def test_apply_does_not_modify_existing_rows(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "toddler", "adult"],
        ))
        _aav(db, ws, "age_group", "infant", active=True)
        _aav(db, ws, "age_group", "toddler", active=False)
        existing_ids_before = {r.id: (r.value, r.is_active) for r in db.query(
            AttributeAllowedValue
        ).filter(AttributeAllowedValue.workspace_id == ws.id).all()}

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        apply_sync_plan(db, plan=plan)
        db.flush()

        existing_ids_after = {r.id: (r.value, r.is_active) for r in db.query(
            AttributeAllowedValue
        ).filter(
            AttributeAllowedValue.workspace_id == ws.id,
            AttributeAllowedValue.id.in_(existing_ids_before.keys()),
        ).all()}
        # Same IDs still exist with same value + is_active.
        assert existing_ids_before == existing_ids_after

    def test_apply_skips_open_taxonomy(self, db):
        ws = _ws(db)
        manifest = _manifest(
            _entry("product_type", kind="open"),
            _entry("age_group", kind="closed",
                   allowed_values=["infant", "adult"]),
        )

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        apply_sync_plan(db, plan=plan)
        db.flush()

        # Only age_group rows; product_type untouched.
        rows = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws.id,
        ).all()
        assert {r.attribute_name for r in rows} == {"age_group"}
        assert all(r.is_active is False for r in rows)

    def test_idempotent_reapply_inserts_zero(self, db):
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "adult"],
        ))

        # First apply.
        plan1 = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        r1 = apply_sync_plan(db, plan=plan1)
        db.flush()
        assert r1.inserted == 2

        # Re-compute and re-apply: nothing missing.
        plan2 = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        assert plan2.total_missing == 0
        r2 = apply_sync_plan(db, plan=plan2)
        db.flush()
        assert r2.inserted == 0

        # Total row count unchanged after second apply.
        n = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws.id,
        ).count()
        assert n == 2

    def test_dry_run_workflow_writes_nothing(self, db):
        """The script's dry-run branch is: compute_sync_plan + print only,
        no apply_sync_plan call. Pin that compute alone never writes."""
        ws = _ws(db)
        manifest = _manifest(_entry(
            "age_group", kind="closed",
            allowed_values=["infant", "adult", "newborn"],
        ))
        before = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws.id,
        ).count()

        plan = compute_sync_plan(db, manifest=manifest, workspace_id=ws.id)
        # NOTE: deliberately NOT calling apply_sync_plan -- this is the
        # dry-run path.

        after = db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws.id,
        ).count()
        assert before == after == 0
        # Plan still describes the work that *would* be done.
        assert plan.total_missing == 3
