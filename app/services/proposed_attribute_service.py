"""Attribute-discovery pipeline: ingest -> aggregate -> review.

Parallel to proposed_attribute_value_service but one level up: proposes
new taxonomy *dimensions*, not new values for existing dimensions.

Stage 1 — ingest
    ``record_attribute_events`` writes raw events from an
    ``AttributeDiscoveryOutput``.

Stage 2 — aggregate
    ``refresh_attribute_aggregates`` rolls up raw events by cluster_key.
    Reviewer-touched aggregates are preserved.

Stage 3 — review
    ``approve_attribute_aggregate`` returns a structured
    ``AttributeDefinition``-shaped payload. It does NOT inject it into the
    system — the caller decides whether/how to persist it.
    ``reject_attribute_aggregate`` and ``merge_attribute_aggregate`` flip
    status like the value pipeline.

PROMOTION DEFAULTS (conservative, guidance only):
    proposal_count          >= 4
    avg_confidence          >= 0.88
    distinct_product_count  >= 3
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.proposed_attribute import (
    ATTR_PROPOSAL_STATUS_APPROVED,
    ATTR_PROPOSAL_STATUS_MERGED,
    ATTR_PROPOSAL_STATUS_PENDING,
    ATTR_PROPOSAL_STATUS_REJECTED,
    ProposedAttributeAggregate,
    ProposedAttributeEvent,
)
from app.schemas.attribute_discovery import AttributeDiscoveryOutput
from app.services.proposed_attribute_normalizer import normalize_attribute_name

ATTR_PROMOTION_MIN_PROPOSAL_COUNT = 4
ATTR_PROMOTION_MIN_AVG_CONFIDENCE = 0.88
ATTR_PROMOTION_MIN_DISTINCT_PRODUCTS = 3

_MAX_SAMPLE_EVIDENCE = 10
_MAX_SAMPLE_PRODUCTS = 10
_MAX_SUGGESTED_VALUES = 20


@dataclass
class AttrPromotionCheck:
    ready: bool
    reasons: list[str]


# ==========================================================================
# Stage 1 — ingest
# ==========================================================================


def record_attribute_events(
    db: Session,
    *,
    workspace_id: int,
    product_id: str,
    output: AttributeDiscoveryOutput,
    source: str = "text",
) -> list[ProposedAttributeEvent]:
    """Persist one raw event per proposed attribute on *output*."""
    created: list[ProposedAttributeEvent] = []
    for pa in output.proposed_attributes or []:
        normalized = normalize_attribute_name(pa.attribute_name)
        if not normalized:
            continue
        event = ProposedAttributeEvent(
            workspace_id=workspace_id,
            product_id=product_id,
            proposed_attribute_name=pa.attribute_name,
            normalized_attribute_name=normalized,
            confidence=pa.confidence,
            description=pa.description,
            evidence=list(pa.evidence),
            suggested_values=list(pa.suggested_values),
            suggested_class_name=pa.suggested_class_name,
            suggested_targeting_mode=pa.suggested_targeting_mode,
            source=source,
        )
        db.add(event)
        created.append(event)
    if created:
        db.flush()
    return created


# ==========================================================================
# Stage 2 — aggregate
# ==========================================================================


def refresh_attribute_aggregates(
    db: Session,
    *,
    workspace_id: int,
) -> list[ProposedAttributeAggregate]:
    """Recompute attribute aggregates from raw events.

    Same semantics as the value pipeline: reviewer-touched rows are
    preserved; only pending aggregates are overwritten.
    """
    events = (
        db.query(ProposedAttributeEvent)
        .filter(ProposedAttributeEvent.workspace_id == workspace_id)
        .order_by(ProposedAttributeEvent.created_at.asc())
        .all()
    )

    buckets: dict[str, list[ProposedAttributeEvent]] = {}
    for ev in events:
        buckets.setdefault(ev.normalized_attribute_name, []).append(ev)

    existing_aggs = (
        db.query(ProposedAttributeAggregate)
        .filter(ProposedAttributeAggregate.workspace_id == workspace_id)
        .all()
    )
    existing_by_key: dict[str, ProposedAttributeAggregate] = {
        agg.cluster_key: agg for agg in existing_aggs
    }

    for cluster_key, bucket in buckets.items():
        existing = existing_by_key.get(cluster_key)
        if existing is not None and existing.status != ATTR_PROPOSAL_STATUS_PENDING:
            continue

        confidences = [ev.confidence for ev in bucket]
        distinct_products = list({ev.product_id for ev in bucket})

        canonical = max(
            bucket, key=lambda e: (e.confidence, e.created_at),
        ).proposed_attribute_name

        # Union suggested values across all events.
        all_values: set[str] = set()
        for ev in bucket:
            all_values.update(ev.suggested_values or [])
        merged_values = sorted(all_values)[:_MAX_SUGGESTED_VALUES]

        # Pick the most common class/targeting from events.
        class_counts: dict[str, int] = {}
        mode_counts: dict[str, int] = {}
        for ev in bucket:
            class_counts[ev.suggested_class_name] = class_counts.get(ev.suggested_class_name, 0) + 1
            mode_counts[ev.suggested_targeting_mode] = mode_counts.get(ev.suggested_targeting_mode, 0) + 1
        best_class = max(class_counts, key=class_counts.get)  # type: ignore[arg-type]
        best_mode = max(mode_counts, key=mode_counts.get)  # type: ignore[arg-type]

        sample_evidence: list[str] = []
        seen: set[str] = set()
        for ev in bucket:
            for quote in ev.evidence or []:
                if quote not in seen:
                    seen.add(quote)
                    sample_evidence.append(quote)
                    if len(sample_evidence) >= _MAX_SAMPLE_EVIDENCE:
                        break
            if len(sample_evidence) >= _MAX_SAMPLE_EVIDENCE:
                break

        sample_pids = distinct_products[:_MAX_SAMPLE_PRODUCTS]

        if existing is None:
            agg = ProposedAttributeAggregate(
                workspace_id=workspace_id,
                cluster_key=cluster_key,
                canonical_attribute_name=canonical,
                proposal_count=len(bucket),
                distinct_product_count=len(distinct_products),
                avg_confidence=sum(confidences) / len(confidences),
                max_confidence=max(confidences),
                sample_evidence=sample_evidence,
                sample_product_ids=sample_pids,
                merged_suggested_values=merged_values,
                suggested_class_name=best_class,
                suggested_targeting_mode=best_mode,
                status=ATTR_PROPOSAL_STATUS_PENDING,
            )
            db.add(agg)
            existing_by_key[cluster_key] = agg
        else:
            existing.canonical_attribute_name = canonical
            existing.proposal_count = len(bucket)
            existing.distinct_product_count = len(distinct_products)
            existing.avg_confidence = sum(confidences) / len(confidences)
            existing.max_confidence = max(confidences)
            existing.sample_evidence = sample_evidence
            existing.sample_product_ids = sample_pids
            existing.merged_suggested_values = merged_values
            existing.suggested_class_name = best_class
            existing.suggested_targeting_mode = best_mode

    db.flush()
    return list(existing_by_key.values())


def attribute_promotion_readiness(
    aggregate: ProposedAttributeAggregate,
) -> AttrPromotionCheck:
    """Evaluate an aggregate against the conservative promotion defaults."""
    reasons: list[str] = []
    if aggregate.proposal_count < ATTR_PROMOTION_MIN_PROPOSAL_COUNT:
        reasons.append(
            f"proposal_count={aggregate.proposal_count} < "
            f"min={ATTR_PROMOTION_MIN_PROPOSAL_COUNT}"
        )
    if aggregate.avg_confidence < ATTR_PROMOTION_MIN_AVG_CONFIDENCE:
        reasons.append(
            f"avg_confidence={aggregate.avg_confidence:.3f} < "
            f"min={ATTR_PROMOTION_MIN_AVG_CONFIDENCE}"
        )
    if aggregate.distinct_product_count < ATTR_PROMOTION_MIN_DISTINCT_PRODUCTS:
        reasons.append(
            f"distinct_product_count={aggregate.distinct_product_count} < "
            f"min={ATTR_PROMOTION_MIN_DISTINCT_PRODUCTS}"
        )
    return AttrPromotionCheck(ready=not reasons, reasons=reasons)


# ==========================================================================
# Stage 3 — review
# ==========================================================================


def approve_attribute_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    force: bool = False,
    review_note: str | None = None,
) -> tuple[ProposedAttributeAggregate, dict[str, Any]]:
    """Approve an attribute aggregate and return a proposed
    AttributeDefinition payload.

    Does NOT inject the attribute into the system — the caller decides
    whether/how to persist it (e.g. write to seed JSON, call
    set_allowed_values, etc.).

    Returns ``(aggregate, definition_payload)`` where definition_payload
    is a dict shaped like an AttributeDefinition constructor argument.
    """
    agg = db.query(ProposedAttributeAggregate).get(aggregate_id)
    if agg is None:
        raise ValueError(f"aggregate {aggregate_id} not found")

    if not force:
        check = attribute_promotion_readiness(agg)
        if not check.ready:
            raise ValueError(
                "attribute not ready for promotion: " + "; ".join(check.reasons)
            )

    promoted_name = agg.canonical_attribute_name
    agg.status = ATTR_PROPOSAL_STATUS_APPROVED
    agg.promoted_attribute_name = promoted_name
    agg.merge_reason = None
    agg.review_note = review_note

    definition_payload = {
        "name": promoted_name,
        "object_type": "product",
        "class_name": agg.suggested_class_name,
        "value_mode": "multi" if agg.suggested_class_name == "contextual_semantic" else "single",
        "allowed_values": list(agg.merged_suggested_values),
        "description": f"Discovered attribute: {promoted_name}",
        "evidence_sources": ["text", "image"],
        "behavior": {
            "taxonomy_sensitive": True,
            "ordered_values": False,
            "can_propose_values": True,
            "multi_value_allowed": agg.suggested_class_name == "contextual_semantic",
            "prefer_conservative_inference": True,
            "value_order": None,
            "negative_scoring_enabled": False,
        },
        "targeting_mode": agg.suggested_targeting_mode,
    }

    db.flush()
    return agg, definition_payload


def reject_attribute_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    merge_reason: str | None = None,
    review_note: str | None = None,
) -> ProposedAttributeAggregate:
    """Reject an attribute aggregate."""
    agg = db.query(ProposedAttributeAggregate).get(aggregate_id)
    if agg is None:
        raise ValueError(f"aggregate {aggregate_id} not found")
    agg.status = ATTR_PROPOSAL_STATUS_REJECTED
    agg.promoted_attribute_name = None
    agg.merge_reason = merge_reason
    agg.review_note = review_note
    db.flush()
    return agg


def merge_attribute_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    target_attribute_name: str,
    existing_attribute_names: list[str],
    merge_reason: str | None = None,
    review_note: str | None = None,
) -> ProposedAttributeAggregate:
    """Merge an attribute proposal into an existing attribute.

    Validates that *target_attribute_name* is in *existing_attribute_names*
    before flipping status.
    """
    lowered = {n.lower() for n in existing_attribute_names}
    if target_attribute_name.lower() not in lowered:
        raise ValueError(
            f"target '{target_attribute_name}' is not in existing attributes"
        )
    agg = db.query(ProposedAttributeAggregate).get(aggregate_id)
    if agg is None:
        raise ValueError(f"aggregate {aggregate_id} not found")
    agg.status = ATTR_PROPOSAL_STATUS_MERGED
    agg.promoted_attribute_name = target_attribute_name
    agg.merge_reason = merge_reason
    agg.review_note = review_note
    db.flush()
    return agg
