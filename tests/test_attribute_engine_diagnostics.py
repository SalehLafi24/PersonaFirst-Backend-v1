"""Diagnostic-output tests for the attribute engine pipeline.

Covers two read-only signals on `CoverageReport`:

  - `value_aav_drift`: produced values with no active AAV in the workspace
    (would silently drop at backfill).
  - `pending_blockers`: per-pending-aggregate readiness reasons surfaced
    from `promotion_readiness` so operators see *why* an aggregate isn't
    promotable.

These are diagnostic only — neither field mutates the database.
"""
from datetime import datetime, timezone

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product
from app.models.proposed_attribute_value import (
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_PENDING,
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.workspace import Workspace
from app.services.attribute_engine.coverage_service import coverage_report


ATTR = "age_group"


def _ws(db) -> Workspace:
    ws = Workspace(name="diag-ws", slug="diag-ws")
    db.add(ws)
    db.flush()
    return ws


def _product(db, ws: Workspace, pid: str) -> Product:
    p = Product(workspace_id=ws.id, product_id=pid, sku=pid, name=pid)
    db.add(p)
    db.flush()
    return p


def _event(db, ws: Workspace, pid: str, value: str, conf: float) -> None:
    db.add(ProposedAttributeValueEvent(
        workspace_id=ws.id,
        product_id=pid,
        attribute_name=ATTR,
        proposed_value_raw=value,
        normalized_value=value,
        confidence=conf,
        evidence=["seed"],
        source="contextual_defaults",
    ))
    db.flush()


def _aggregate(
    db, ws: Workspace, value: str, n: int, distinct: int, avg_conf: float,
    status: str = PROPOSAL_STATUS_PENDING,
) -> ProposedAttributeValueAggregate:
    agg = ProposedAttributeValueAggregate(
        workspace_id=ws.id,
        attribute_name=ATTR,
        canonical_value=value,
        cluster_key=value,
        proposal_count=n,
        distinct_product_count=distinct,
        avg_confidence=avg_conf,
        max_confidence=avg_conf,
        sample_evidence=["seed"],
        sample_product_ids=[],
        status=status,
        promoted_to_allowed_value=value if status == PROPOSAL_STATUS_APPROVED else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(agg)
    db.flush()
    return agg


def _aav(db, ws: Workspace, value: str, *, active: bool = True) -> None:
    db.add(AttributeAllowedValue(
        workspace_id=ws.id,
        attribute_name=ATTR,
        value=value,
        is_active=active,
    ))
    db.flush()


# ======================================================================
# Drift detection
# ======================================================================


class TestValueAavDrift:

    def test_healthy_run_has_no_drift(self, db):
        ws = _ws(db)
        _product(db, ws, "p1")
        _event(db, ws, "p1", "kids", 0.9)
        _aggregate(db, ws, "kids", n=1, distinct=1, avg_conf=0.9,
                   status=PROPOSAL_STATUS_APPROVED)
        _aav(db, ws, "kids", active=True)

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        assert rep.value_aav_drift == []

    def test_produced_value_without_active_aav_is_drift(self, db):
        ws = _ws(db)
        _product(db, ws, "p1")
        _product(db, ws, "p2")
        _event(db, ws, "p1", "adult", 0.65)
        _event(db, ws, "p2", "adult", 0.65)
        _aggregate(db, ws, "adult", n=2, distinct=2, avg_conf=0.65)
        # No AAV row at all for 'adult'.

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        assert len(rep.value_aav_drift) == 1
        d = rep.value_aav_drift[0]
        assert d.produced_value == "adult"
        assert d.event_count == 2
        assert d.aggregate_status == PROPOSAL_STATUS_PENDING

    def test_inactive_aav_counts_as_drift(self, db):
        ws = _ws(db)
        _product(db, ws, "p1")
        _event(db, ws, "p1", "adult", 0.65)
        _aav(db, ws, "adult", active=False)

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        assert [d.produced_value for d in rep.value_aav_drift] == ["adult"]

    def test_manifest_value_never_produced_is_not_drift(self, db):
        """The check is grounded in produced values, not manifest values.
        An allowed_value declared but never emitted is NOT drift."""
        ws = _ws(db)
        _product(db, ws, "p1")
        _event(db, ws, "p1", "kids", 0.9)
        _aav(db, ws, "kids", active=True)
        # 'newborn' is an allowed_value in the manifest but never produced.
        # It must NOT appear in drift even though there is no AAV row.

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        assert rep.value_aav_drift == []

    def test_drift_sorted_by_event_count_desc(self, db):
        ws = _ws(db)
        _product(db, ws, "p1")
        _product(db, ws, "p2")
        _product(db, ws, "p3")
        _event(db, ws, "p1", "low_volume", 0.7)
        _event(db, ws, "p2", "high_volume", 0.7)
        _event(db, ws, "p3", "high_volume", 0.7)

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        produced = [d.produced_value for d in rep.value_aav_drift]
        assert produced == ["high_volume", "low_volume"]

    def test_drift_check_is_read_only(self, db):
        """Computing the report must not insert AAV rows or mutate aggregates."""
        ws = _ws(db)
        _product(db, ws, "p1")
        _event(db, ws, "p1", "adult", 0.65)
        _aggregate(db, ws, "adult", n=1, distinct=1, avg_conf=0.65)

        before_aav = db.query(AttributeAllowedValue).count()
        before_agg_status = db.query(
            ProposedAttributeValueAggregate.status
        ).filter_by(cluster_key="adult").scalar()

        coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)

        after_aav = db.query(AttributeAllowedValue).count()
        after_agg_status = db.query(
            ProposedAttributeValueAggregate.status
        ).filter_by(cluster_key="adult").scalar()

        assert before_aav == after_aav
        assert before_agg_status == after_agg_status


# ======================================================================
# Pending blockers
# ======================================================================


class TestPendingBlockers:

    def test_low_confidence_aggregate_surfaces_blocker(self, db):
        ws = _ws(db)
        for i in range(5):
            pid = f"p{i}"
            _product(db, ws, pid)
            _event(db, ws, pid, "adult", 0.65)
        _aggregate(db, ws, "adult", n=5, distinct=5, avg_conf=0.65)

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        blocked = [pb for pb in rep.pending_blockers if pb.reasons]
        assert len(blocked) == 1
        pb = blocked[0]
        assert pb.cluster_key == "adult"
        # Format must be greppable against manifest field names.
        assert any("avg_confidence" in r and "PROMOTION_MIN_AVG_CONFIDENCE" in r
                   for r in pb.reasons)

    def test_ready_pending_aggregate_has_empty_reasons(self, db):
        """A pending aggregate that meets all thresholds is 'pending,
        ready' — it should appear in `pending_blockers` with an empty
        `reasons` list (waiting only on a reviewer)."""
        ws = _ws(db)
        for i in range(5):
            pid = f"p{i}"
            _product(db, ws, pid)
            _event(db, ws, pid, "kids", 0.95)
        _aggregate(db, ws, "kids", n=5, distinct=5, avg_conf=0.95,
                   status=PROPOSAL_STATUS_PENDING)

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        assert len(rep.pending_blockers) == 1
        assert rep.pending_blockers[0].reasons == []
        assert rep.aggregates_ready_for_approval == 1

    def test_approved_aggregate_does_not_surface(self, db):
        ws = _ws(db)
        _product(db, ws, "p1")
        _event(db, ws, "p1", "kids", 0.95)
        _aggregate(db, ws, "kids", n=1, distinct=1, avg_conf=0.95,
                   status=PROPOSAL_STATUS_APPROVED)

        rep = coverage_report(db, workspace_id=ws.id, attribute_name=ATTR)
        assert rep.pending_blockers == []
