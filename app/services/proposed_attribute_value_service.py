"""Taxonomy-evolution pipeline: ingest → aggregate → review.

Stage 1 — ingest
    `record_events_from_output` writes one raw event per proposed value on
    an EnrichmentOutput. The raw events are append-only evidence.

Stage 2 — aggregate
    `refresh_aggregates` re-rolls raw events into one row per
    (workspace, attribute_name, cluster_key). Any aggregate already in a
    reviewer-touched state (approved / rejected / merged) is left alone;
    only pending aggregates are overwritten with fresh stats.

Stage 3 — review
    `approve_aggregate`, `reject_aggregate`, `merge_aggregate` flip status
    and set `promoted_to_allowed_value` where applicable. Nothing in stage
    1/2 ever promotes values automatically — promotion requires an
    explicit reviewer call.

PROMOTION GUIDANCE (conservative defaults, enforced as warnings only —
reviewers can still approve below the bar with an override):
    - proposal_count           >= 3
    - avg_confidence           >= 0.85
    - distinct_product_count   >= 2
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.proposed_attribute_value import (
    MERGE_REASON_NOISE,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.schemas.attribute_enrichment import EnrichmentOutput
from app.services.proposed_value_normalizer import normalize_proposed_value


# --------------------------------------------------------------------------
# Conservative promotion thresholds. Not enforced automatically — these are
# the floor a reviewer is expected to honor when approving an aggregate.
# --------------------------------------------------------------------------
PROMOTION_MIN_PROPOSAL_COUNT = 3
PROMOTION_MIN_AVG_CONFIDENCE = 0.85
PROMOTION_MIN_DISTINCT_PRODUCTS = 2

_MAX_SAMPLE_EVIDENCE = 10
_MAX_SAMPLE_PRODUCTS = 10


@dataclass
class PromotionCheck:
    """Structured result returned by `promotion_readiness` so reviewers can
    see exactly why an aggregate is / isn't ready."""
    ready: bool
    reasons: list[str]


# ==========================================================================
# Stage 1 — ingest raw events from an EnrichmentOutput
# ==========================================================================


def record_events_from_output(
    db: Session,
    *,
    workspace_id: int,
    product_id: str,
    output: EnrichmentOutput,
) -> list[ProposedAttributeValueEvent]:
    """Persist one raw event per ProposedValue on `output`.

    Returns the list of created events. No aggregation happens here — call
    `refresh_aggregates` after a batch ingest to materialize the review
    rollups.
    """
    created: list[ProposedAttributeValueEvent] = []
    for pv in output.proposed_values or []:
        normalized = normalize_proposed_value(pv.value)
        if not normalized:
            continue
        event = ProposedAttributeValueEvent(
            workspace_id=workspace_id,
            product_id=product_id,
            attribute_name=output.attribute_name,
            proposed_value_raw=pv.value,
            normalized_value=normalized,
            confidence=pv.confidence,
            evidence=list(pv.evidence),
            source=output.source.value if hasattr(output.source, "value") else str(output.source),
        )
        db.add(event)
        created.append(event)
    if created:
        db.flush()
    return created


def record_events_from_outputs(
    db: Session,
    *,
    workspace_id: int,
    product_outputs: dict[str, list[EnrichmentOutput]],
) -> list[ProposedAttributeValueEvent]:
    """Convenience batch ingest. `product_outputs` maps product_id → list
    of EnrichmentOutputs (one per attribute)."""
    created: list[ProposedAttributeValueEvent] = []
    for product_id, outputs in product_outputs.items():
        for out in outputs:
            created.extend(
                record_events_from_output(
                    db,
                    workspace_id=workspace_id,
                    product_id=product_id,
                    output=out,
                )
            )
    return created


# ==========================================================================
# Stage 2 — aggregate raw events into reviewable rows
# ==========================================================================


def refresh_aggregates(
    db: Session,
    *,
    workspace_id: int,
    attribute_name: str | None = None,
) -> list[ProposedAttributeValueAggregate]:
    """Recompute aggregates from raw events.

    For each `(workspace_id, attribute_name, cluster_key)` bucket:
      - if an aggregate already exists AND its status is not `pending`,
        leave it alone — reviewers have touched it.
      - otherwise upsert a pending aggregate with fresh counts / samples.

    Returns the full set of aggregates that exist for the workspace (and
    optionally filtered to one attribute) after the refresh.
    """
    event_q = db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == workspace_id
    )
    if attribute_name is not None:
        event_q = event_q.filter(
            ProposedAttributeValueEvent.attribute_name == attribute_name
        )
    events = event_q.order_by(ProposedAttributeValueEvent.created_at.asc()).all()

    # Group events by (attribute_name, cluster_key).
    buckets: dict[tuple[str, str], list[ProposedAttributeValueEvent]] = {}
    for ev in events:
        buckets.setdefault((ev.attribute_name, ev.normalized_value), []).append(ev)

    # Load existing aggregates so we can respect reviewer-touched rows.
    agg_q = db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id
    )
    if attribute_name is not None:
        agg_q = agg_q.filter(
            ProposedAttributeValueAggregate.attribute_name == attribute_name
        )
    existing_by_key: dict[tuple[str, str], ProposedAttributeValueAggregate] = {
        (agg.attribute_name, agg.cluster_key): agg for agg in agg_q.all()
    }

    for (attr, cluster_key), bucket in buckets.items():
        existing = existing_by_key.get((attr, cluster_key))
        if existing is not None and existing.status != PROPOSAL_STATUS_PENDING:
            # Reviewer has already acted on this cluster — do not overwrite.
            continue

        confidences = [ev.confidence for ev in bucket]
        distinct_products = list({ev.product_id for ev in bucket})
        # Pick the highest-confidence raw spelling as the canonical display
        # form. Ties break on the most-recently-seen spelling.
        canonical = max(
            bucket,
            key=lambda e: (e.confidence, e.created_at),
        ).proposed_value_raw

        sample_evidence: list[str] = []
        seen_evidence: set[str] = set()
        for ev in bucket:
            for quote in ev.evidence or []:
                if quote in seen_evidence:
                    continue
                seen_evidence.add(quote)
                sample_evidence.append(quote)
                if len(sample_evidence) >= _MAX_SAMPLE_EVIDENCE:
                    break
            if len(sample_evidence) >= _MAX_SAMPLE_EVIDENCE:
                break

        sample_product_ids = distinct_products[:_MAX_SAMPLE_PRODUCTS]

        if existing is None:
            agg = ProposedAttributeValueAggregate(
                workspace_id=workspace_id,
                attribute_name=attr,
                canonical_value=canonical,
                cluster_key=cluster_key,
                proposal_count=len(bucket),
                distinct_product_count=len(distinct_products),
                avg_confidence=sum(confidences) / len(confidences),
                max_confidence=max(confidences),
                sample_evidence=sample_evidence,
                sample_product_ids=sample_product_ids,
                status=PROPOSAL_STATUS_PENDING,
                promoted_to_allowed_value=None,
            )
            db.add(agg)
            existing_by_key[(attr, cluster_key)] = agg
        else:
            existing.canonical_value = canonical
            existing.proposal_count = len(bucket)
            existing.distinct_product_count = len(distinct_products)
            existing.avg_confidence = sum(confidences) / len(confidences)
            existing.max_confidence = max(confidences)
            existing.sample_evidence = sample_evidence
            existing.sample_product_ids = sample_product_ids

    db.flush()
    return list(existing_by_key.values())


def promotion_readiness(
    aggregate: ProposedAttributeValueAggregate,
) -> PromotionCheck:
    """Evaluate an aggregate against the conservative promotion defaults.

    Reviewers can override the result — this is guidance, not a gate.
    """
    reasons: list[str] = []
    if aggregate.proposal_count < PROMOTION_MIN_PROPOSAL_COUNT:
        reasons.append(
            f"proposal_count={aggregate.proposal_count} < "
            f"PROMOTION_MIN_PROPOSAL_COUNT={PROMOTION_MIN_PROPOSAL_COUNT}"
        )
    if aggregate.avg_confidence < PROMOTION_MIN_AVG_CONFIDENCE:
        reasons.append(
            f"avg_confidence={aggregate.avg_confidence:.3f} < "
            f"PROMOTION_MIN_AVG_CONFIDENCE={PROMOTION_MIN_AVG_CONFIDENCE}"
        )
    if aggregate.distinct_product_count < PROMOTION_MIN_DISTINCT_PRODUCTS:
        reasons.append(
            f"distinct_product_count={aggregate.distinct_product_count} < "
            f"PROMOTION_MIN_DISTINCT_PRODUCTS={PROMOTION_MIN_DISTINCT_PRODUCTS}"
        )
    return PromotionCheck(ready=not reasons, reasons=reasons)


# ==========================================================================
# Stage 3 — review actions
# ==========================================================================


def approve_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    current_allowed_values: list[str],
    force: bool = False,
    review_note: str | None = None,
) -> tuple[ProposedAttributeValueAggregate, list[str]]:
    """Approve an aggregate, persist to the DB taxonomy, and return the
    extended allowed_values list.

    In addition to flipping aggregate.status to ``approved``, this now
    persists the promoted value into the ``attribute_allowed_values``
    table for the aggregate's workspace + attribute_name so that future
    enrichment runs pick it up immediately.

    The returned list is also computed from the in-memory
    *current_allowed_values* + promoted value so callers that haven't
    switched to the DB-backed read path yet still see the extension.

    Refuses to approve if the aggregate is not ready per
    `promotion_readiness`, unless `force=True`.
    """
    from app.services.attribute_taxonomy_service import upsert_allowed_value

    agg = db.query(ProposedAttributeValueAggregate).get(aggregate_id)
    if agg is None:
        raise ValueError(f"aggregate {aggregate_id} not found")

    if not force:
        check = promotion_readiness(agg)
        if not check.ready:
            raise ValueError(
                "aggregate not ready for promotion: " + "; ".join(check.reasons)
            )

    promoted = agg.canonical_value
    agg.status = PROPOSAL_STATUS_APPROVED
    agg.promoted_to_allowed_value = promoted
    agg.merge_reason = None
    agg.review_note = review_note

    # Persist to the DB-backed taxonomy so enrichment picks it up.
    upsert_allowed_value(db, agg.workspace_id, agg.attribute_name, promoted)

    # Build the extended in-memory list for backward-compatible callers.
    lowered_existing = {v.lower() for v in current_allowed_values}
    updated = list(current_allowed_values)
    if promoted.lower() not in lowered_existing:
        updated.append(promoted)
    db.flush()
    return agg, updated


def reject_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    merge_reason: str | None = None,
    review_note: str | None = None,
) -> ProposedAttributeValueAggregate:
    """Reject an aggregate. Status flip only -- raw events are preserved so
    a later reviewer can inspect the evidence and reverse the decision.

    ``merge_reason`` is typically ``noise`` for low-signal rejections.
    """
    agg = db.query(ProposedAttributeValueAggregate).get(aggregate_id)
    if agg is None:
        raise ValueError(f"aggregate {aggregate_id} not found")
    agg.status = PROPOSAL_STATUS_REJECTED
    agg.promoted_to_allowed_value = None
    agg.merge_reason = merge_reason
    agg.review_note = review_note
    db.flush()
    return agg


def merge_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    target_allowed_value: str,
    current_allowed_values: list[str],
    merge_reason: str | None = None,
    review_note: str | None = None,
) -> ProposedAttributeValueAggregate:
    """Merge an aggregate into an EXISTING allowed value.

    Validates that ``target_allowed_value`` is actually in
    ``current_allowed_values`` (case-insensitive) before flipping the
    status.

    Merge-reason vocabulary (optional, for audit trail):
        normalized_duplicate -- formatting variant (HIIT -> hiit)
        synonym_to_existing  -- different word, same concept
        flattened_child      -- distinct child concept temporarily
                                collapsed into a parent value
    """
    lowered = {v.lower() for v in current_allowed_values}
    if target_allowed_value.lower() not in lowered:
        raise ValueError(
            f"target '{target_allowed_value}' is not in current allowed_values"
        )
    agg = db.query(ProposedAttributeValueAggregate).get(aggregate_id)
    if agg is None:
        raise ValueError(f"aggregate {aggregate_id} not found")
    agg.status = PROPOSAL_STATUS_MERGED
    agg.promoted_to_allowed_value = target_allowed_value
    agg.merge_reason = merge_reason
    agg.review_note = review_note
    db.flush()
    return agg
