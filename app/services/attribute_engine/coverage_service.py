"""Per-attribute observability: coverage, pending, ready-for-approval, distribution."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.services.attribute_engine.manifest import AttributeManifestEntry
from app.services.proposed_attribute_value_service import promotion_readiness


@dataclass
class TopAggregate:
    cluster_key: str
    canonical_value: str
    proposal_count: int
    distinct_product_count: int
    avg_confidence: float
    status: str


@dataclass
class PendingAggregateBlocker:
    """One pending aggregate together with the human-readable reasons it
    isn't ready for promotion. `reasons` is empty when an aggregate is
    pending AND ready (waiting only on a reviewer)."""
    cluster_key: str
    canonical_value: str
    proposal_count: int
    distinct_product_count: int
    avg_confidence: float
    reasons: list[str]


@dataclass
class ValueDrift:
    """A value the engine produced into events that doesn't resolve to
    an active AttributeAllowedValue in this workspace.

    Without this signal, the produced value is silently dropped at
    backfill (cluster_to_canonical can't map it). The check is grounded
    in *produced* values rather than manifest.allowed_values: a manifest
    value never produced isn't a real drift, and a produced value never
    declared in the manifest still drops silently."""
    produced_value: str
    event_count: int
    aggregate_status: str | None  # 'pending' / 'approved' / ... / None


@dataclass
class CoverageReport:
    attribute_name: str
    workspace_id: int
    total_products: int
    products_with_attribute: int
    coverage_pct: float
    aav_active_count: int
    events_total: int
    events_by_source: dict[str, int]
    aggregates_by_status: dict[str, int]
    aggregates_ready_for_approval: int
    confidence_buckets: dict[str, int]
    top_aggregates: list[TopAggregate] = field(default_factory=list)
    pending_blockers: list[PendingAggregateBlocker] = field(default_factory=list)
    value_aav_drift: list[ValueDrift] = field(default_factory=list)


_BUCKETS = [
    ("0.00-0.50", 0.0, 0.5),
    ("0.50-0.70", 0.5, 0.7),
    ("0.70-0.85", 0.7, 0.85),
    ("0.85-0.95", 0.85, 0.95),
    ("0.95-1.00", 0.95, 1.0001),
]


def coverage_report(
    db: Session,
    *,
    workspace_id: int,
    attribute_name: str,
    manifest_entry: AttributeManifestEntry | None = None,
    top_n: int = 15,
) -> CoverageReport:
    """Build a CoverageReport for one attribute in one workspace."""
    total_products = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).count()

    products_with_attr = (
        db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == attribute_name)
        .distinct().count()
    )

    aav_active = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == workspace_id,
        AttributeAllowedValue.attribute_name == attribute_name,
        AttributeAllowedValue.is_active == True,  # noqa: E712
    ).count()

    events = db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == workspace_id,
        ProposedAttributeValueEvent.attribute_name == attribute_name,
    ).all()
    events_by_source: Counter = Counter(ev.source for ev in events)

    confidence_buckets: dict[str, int] = {label: 0 for label, _, _ in _BUCKETS}
    for ev in events:
        c = float(ev.confidence or 0)
        for label, lo, hi in _BUCKETS:
            if lo <= c < hi:
                confidence_buckets[label] += 1
                break

    aggs = db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id,
        ProposedAttributeValueAggregate.attribute_name == attribute_name,
    ).all()
    by_status: Counter = Counter()
    ready_for_approval = 0
    pending_blockers: list[PendingAggregateBlocker] = []
    for agg in aggs:
        by_status[agg.status] += 1
        if agg.status == PROPOSAL_STATUS_PENDING:
            check = promotion_readiness(agg)
            if check.ready:
                ready_for_approval += 1
            pending_blockers.append(PendingAggregateBlocker(
                cluster_key=agg.cluster_key,
                canonical_value=agg.canonical_value,
                proposal_count=agg.proposal_count,
                distinct_product_count=agg.distinct_product_count,
                avg_confidence=float(agg.avg_confidence or 0.0),
                reasons=list(check.reasons),
            ))

    aggs_sorted = sorted(aggs, key=lambda a: -a.proposal_count)
    top_aggregates = [
        TopAggregate(
            cluster_key=a.cluster_key,
            canonical_value=a.canonical_value,
            proposal_count=a.proposal_count,
            distinct_product_count=a.distinct_product_count,
            avg_confidence=float(a.avg_confidence),
            status=a.status,
        )
        for a in aggs_sorted[:top_n]
    ]

    # Value/AAV drift: a value the engine produced into events for which
    # there is no active AAV in this workspace. Grounded in produced
    # values, not manifest.allowed_values -- a manifest value never
    # produced is not a real drift; a produced value never declared in
    # the manifest still drops silently and IS a real drift.
    aav_active_lower: dict[str, str] = {
        v.lower(): v for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute_name,
            AttributeAllowedValue.is_active == True,  # noqa: E712
        ).all()
    }
    aggregate_status_by_value: dict[str, str] = {
        a.cluster_key: a.status for a in aggs
    }
    produced_event_counts: Counter = Counter()
    for ev in events:
        v = ev.normalized_value
        if v:
            produced_event_counts[v] += 1
    value_aav_drift: list[ValueDrift] = []
    for value, n in produced_event_counts.items():
        if value.lower() in aav_active_lower:
            continue
        value_aav_drift.append(ValueDrift(
            produced_value=value,
            event_count=n,
            aggregate_status=aggregate_status_by_value.get(value),
        ))
    # Sort by event_count desc so the highest-impact drift surfaces first.
    value_aav_drift.sort(key=lambda d: (-d.event_count, d.produced_value))

    return CoverageReport(
        attribute_name=attribute_name,
        workspace_id=workspace_id,
        total_products=total_products,
        products_with_attribute=products_with_attr,
        coverage_pct=(100.0 * products_with_attr / max(total_products, 1)),
        aav_active_count=aav_active,
        events_total=len(events),
        events_by_source=dict(events_by_source),
        aggregates_by_status={
            s: by_status.get(s, 0)
            for s in (PROPOSAL_STATUS_PENDING, PROPOSAL_STATUS_APPROVED,
                      PROPOSAL_STATUS_MERGED, PROPOSAL_STATUS_REJECTED)
        },
        aggregates_ready_for_approval=ready_for_approval,
        confidence_buckets=confidence_buckets,
        top_aggregates=top_aggregates,
        pending_blockers=pending_blockers,
        value_aav_drift=value_aav_drift,
    )
