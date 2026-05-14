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

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.attribute_synonym_override import (
    SYNONYM_SOURCE_APPROVAL,
    AttributeSynonymOverride,
)
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


def _raw_variants_for_aggregate(
    db: Session, agg: ProposedAttributeValueAggregate,
) -> list[str]:
    """Return distinct proposed_value_raw forms that fed this aggregate.

    Used by the approval flow to surface "what variants did the engine
    see for this concept?" -- the raw material the reviewer needs to
    decide whether to harden the synonym map.
    """
    rows = (
        db.query(ProposedAttributeValueEvent.proposed_value_raw)
        .filter(
            ProposedAttributeValueEvent.workspace_id == agg.workspace_id,
            ProposedAttributeValueEvent.attribute_name == agg.attribute_name,
            ProposedAttributeValueEvent.normalized_value == agg.cluster_key,
        )
        .distinct()
        .all()
    )
    return sorted({r[0] for r in rows if r[0]})


def _suggested_synonyms(
    raw_variants: list[str], canonical: str,
) -> list[dict]:
    """Suggest synonym additions: raw forms that DIFFER from the canonical
    (case-insensitive) and aren't trivial empty/whitespace. The reviewer
    accepts/rejects each at approval time."""
    canon_lower = canonical.strip().lower() if canonical else ""
    out: list[dict] = []
    for raw in raw_variants:
        if not raw:
            continue
        if raw.strip().lower() == canon_lower:
            continue
        out.append({"raw_value": raw, "canonical_value": canonical})
    return out


def _persist_synonym_additions(
    db: Session,
    *,
    workspace_id: int,
    attribute_name: str,
    aggregate_id: int,
    additions: list[dict] | None,
    created_by: str | None,
) -> int:
    """Insert synonym override rows. Idempotent: existing (workspace,
    attribute, raw_value) pairs are updated to the new canonical;
    duplicates are NOT created. Returns count of rows touched."""
    if not additions:
        return 0
    touched = 0
    for entry in additions:
        raw = (entry.get("raw_value") or "").strip()
        canonical = (entry.get("canonical_value") or "").strip()
        if not raw or not canonical:
            continue
        existing = (
            db.query(AttributeSynonymOverride)
            .filter(
                AttributeSynonymOverride.workspace_id == workspace_id,
                AttributeSynonymOverride.attribute_name == attribute_name,
                AttributeSynonymOverride.raw_value == raw,
            )
            .first()
        )
        if existing is not None:
            if existing.canonical_value != canonical:
                existing.canonical_value = canonical
                existing.source_aggregate_id = aggregate_id
                existing.created_by = created_by
                touched += 1
            continue
        db.add(AttributeSynonymOverride(
            workspace_id=workspace_id,
            attribute_name=attribute_name,
            raw_value=raw,
            canonical_value=canonical,
            source=SYNONYM_SOURCE_APPROVAL,
            source_aggregate_id=aggregate_id,
            created_by=created_by,
        ))
        touched += 1
    if touched:
        db.flush()
        # Invalidate normalizer's per-workspace cache so the next
        # ingest sees the new overrides.
        from app.services.attribute_normalizer_service import (
            invalidate_override_cache,
        )
        invalidate_override_cache(
            workspace_id=workspace_id, attribute_name=attribute_name,
        )
    return touched


@dataclass
class ApprovalReport:
    """Returned alongside an approval/merge so the caller can render
    "raw variants seen" and "suggested synonym additions" without
    re-querying. Phase 8.5b discipline."""
    aggregate_id: int
    new_allowed_values: list[str]
    raw_variants_seen: list[str]
    suggested_synonym_additions: list[dict]
    synonyms_added: int


def approve_aggregate(
    db: Session,
    *,
    aggregate_id: int,
    current_allowed_values: list[str],
    force: bool = False,
    review_note: str | None = None,
    synonym_additions: list[dict] | None = None,
    created_by: str | None = None,
) -> tuple[ProposedAttributeValueAggregate, list[str], ApprovalReport]:
    """Approve an aggregate, persist to the DB taxonomy, and return the
    extended allowed_values list.

    In addition to flipping aggregate.status to ``approved``, this now
    persists the promoted value into the ``attribute_allowed_values``
    table for the aggregate's workspace + attribute_name so that future
    enrichment runs pick it up immediately.

    Phase 8.5b: also returns an ApprovalReport carrying the raw variants
    seen on the events feeding this aggregate plus suggested synonym
    additions. When ``synonym_additions`` is provided, those are
    persisted to ``attribute_synonym_overrides`` so future ingest of the
    same raw forms collapses on entry.

    The returned tuple is (aggregate, updated_allowed_values, report).
    Backwards-compatible callers that ignore the third element still
    work.

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

    # Capture raw variants + suggestions BEFORE writing synonyms so the
    # report reflects what the reviewer was offered.
    raw_variants = _raw_variants_for_aggregate(db, agg)
    suggested = _suggested_synonyms(raw_variants, promoted)

    # Persist any synonym additions the reviewer chose. Idempotent.
    n_synonyms = _persist_synonym_additions(
        db,
        workspace_id=agg.workspace_id,
        attribute_name=agg.attribute_name,
        aggregate_id=agg.id,
        additions=synonym_additions,
        created_by=created_by,
    )

    # Build the extended in-memory list for backward-compatible callers.
    lowered_existing = {v.lower() for v in current_allowed_values}
    updated = list(current_allowed_values)
    if promoted.lower() not in lowered_existing:
        updated.append(promoted)
    db.flush()
    report = ApprovalReport(
        aggregate_id=agg.id,
        new_allowed_values=updated,
        raw_variants_seen=raw_variants,
        suggested_synonym_additions=suggested,
        synonyms_added=n_synonyms,
    )
    return agg, updated, report


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


# ==========================================================================
# Reviewer queue hygiene (Phase 8.5a)
# ==========================================================================

# Pending aggregates older than this are flagged "stale" -- they've been
# sitting in the queue without a reviewer decision.
QUEUE_STALE_THRESHOLD_DAYS = 14

# Priority score knobs -- calibrated against intuition; tunable from one
# place. The formula is heuristic; its job is to give reviewers a useful
# ordering, not certify "correct" priority.
_QUEUE_AGE_DECAY_HALFLIFE = 14.0    # age multiplier doubles every 14 days
_QUEUE_AGE_DECAY_CAP_DAYS = 90      # age multiplier capped at 90 days
_QUEUE_DISTINCT_PRODUCT_FLOOR = 5   # distinct_count >= floor saturates this term


@dataclass
class ReviewQueueItem:
    """Per-aggregate row for the prioritised reviewer queue.

    Plain dataclass with JSON-serialisable types so the API layer can
    return it directly. Does NOT carry the SQLAlchemy aggregate object.
    """
    aggregate_id: int
    workspace_id: int
    attribute_name: str
    cluster_key: str
    canonical_value: str
    proposal_count: int
    distinct_product_count: int
    avg_confidence: float
    max_confidence: float
    status: str
    created_at: str                   # ISO8601
    age_days: int
    priority_score: float
    is_stale: bool
    raw_variants_seen: list[str] = field(default_factory=list)
    sample_evidence: list[str] = field(default_factory=list)
    sample_product_ids: list[str] = field(default_factory=list)


def _age_decay(age_days: float) -> float:
    """Older = higher priority, capped at _QUEUE_AGE_DECAY_CAP_DAYS.
    At 0 days: 1.0. At 14 days: 2.0. At 90 days: ~7.4."""
    capped = min(max(0.0, age_days), _QUEUE_AGE_DECAY_CAP_DAYS)
    return 1.0 + (capped / _QUEUE_AGE_DECAY_HALFLIFE)


def _priority_score(
    *, proposal_count: int, distinct_product_count: int,
    avg_confidence: float, age_days: float,
) -> float:
    """log2(1 + count) × min(1, distinct/floor) × avg_conf × age_decay."""
    count_term = math.log2(1.0 + max(0, proposal_count))
    distinct_term = min(1.0, max(0, distinct_product_count) / _QUEUE_DISTINCT_PRODUCT_FLOOR)
    conf_term = max(0.0, min(1.0, float(avg_confidence or 0.0)))
    age_term = _age_decay(age_days)
    return count_term * distinct_term * conf_term * age_term


def _ensure_aware(dt: datetime) -> datetime:
    """Postgres returns naive datetimes; treat as UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def list_review_queue(
    db: Session,
    *,
    workspace_id: int,
    attribute_name: str | None = None,
    sort_by: str = "priority",
    stale_only: bool = False,
    limit: int = 100,
) -> list[ReviewQueueItem]:
    """Return the prioritised reviewer queue for pending aggregates.

    Pure read-only. The reviewer dashboard / CLI calls this to decide
    what to look at next. No DB writes.
    """
    if sort_by not in {"priority", "age", "count"}:
        raise ValueError(
            f"sort_by must be one of 'priority' / 'age' / 'count'; "
            f"got {sort_by!r}"
        )

    q = (
        db.query(ProposedAttributeValueAggregate)
        .filter(ProposedAttributeValueAggregate.workspace_id == workspace_id,
                ProposedAttributeValueAggregate.status == PROPOSAL_STATUS_PENDING)
    )
    if attribute_name is not None:
        q = q.filter(ProposedAttributeValueAggregate.attribute_name == attribute_name)
    aggs = q.all()
    if not aggs:
        return []

    # Bulk-load raw variants per (attribute, cluster_key) so the caller
    # sees what raw forms the events on this aggregate carry. Single
    # query; cheap.
    keys = {(a.attribute_name, a.cluster_key) for a in aggs}
    attr_names = {a for a, _ in keys}
    cluster_keys = {ck for _, ck in keys}
    raw_rows = (
        db.query(
            ProposedAttributeValueEvent.attribute_name,
            ProposedAttributeValueEvent.normalized_value,
            ProposedAttributeValueEvent.proposed_value_raw,
        )
        .filter(ProposedAttributeValueEvent.workspace_id == workspace_id,
                ProposedAttributeValueEvent.attribute_name.in_(attr_names),
                ProposedAttributeValueEvent.normalized_value.in_(cluster_keys))
        .distinct()
        .all()
    )
    raw_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    seen_per_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for attr, norm, raw in raw_rows:
        if not raw:
            continue
        if raw in seen_per_key[(attr, norm)]:
            continue
        seen_per_key[(attr, norm)].add(raw)
        raw_by_key[(attr, norm)].append(raw)

    now = datetime.now(timezone.utc)
    items: list[ReviewQueueItem] = []
    for agg in aggs:
        created = _ensure_aware(agg.created_at)
        age_days = max(0, (now - created).days)
        is_stale = age_days >= QUEUE_STALE_THRESHOLD_DAYS
        if stale_only and not is_stale:
            continue
        score = _priority_score(
            proposal_count=agg.proposal_count,
            distinct_product_count=agg.distinct_product_count,
            avg_confidence=agg.avg_confidence,
            age_days=age_days,
        )
        items.append(ReviewQueueItem(
            aggregate_id=agg.id,
            workspace_id=agg.workspace_id,
            attribute_name=agg.attribute_name,
            cluster_key=agg.cluster_key,
            canonical_value=agg.canonical_value,
            proposal_count=agg.proposal_count,
            distinct_product_count=agg.distinct_product_count,
            avg_confidence=float(agg.avg_confidence or 0.0),
            max_confidence=float(agg.max_confidence or 0.0),
            status=agg.status,
            created_at=created.isoformat(),
            age_days=age_days,
            priority_score=round(score, 4),
            is_stale=is_stale,
            raw_variants_seen=sorted(raw_by_key.get(
                (agg.attribute_name, agg.cluster_key), []
            )),
            sample_evidence=list(agg.sample_evidence or [])[:5],
            sample_product_ids=list(agg.sample_product_ids or [])[:5],
        ))

    # Sort. Tie-break on aggregate_id for determinism.
    if sort_by == "priority":
        items.sort(key=lambda i: (-i.priority_score, i.aggregate_id))
    elif sort_by == "age":
        items.sort(key=lambda i: (-i.age_days, i.aggregate_id))
    elif sort_by == "count":
        items.sort(key=lambda i: (-i.proposal_count, i.aggregate_id))

    return items[: max(0, limit)]


@dataclass
class QueueHealthSnapshot:
    """High-level vital signs of the review queue. Used by the CLI to
    append to seed_data/queue_health_history.jsonl and by ops dashboards
    to track trend over time."""
    pending_total: int
    stale_count: int
    oldest_age_days: int
    avg_age_days: float
    by_attribute: dict[str, int]
    by_attribute_stale: dict[str, int]


def queue_health_snapshot(
    db: Session, *, workspace_id: int,
) -> QueueHealthSnapshot:
    """Cheap aggregate-level summary; reads pending rows once."""
    aggs = (
        db.query(ProposedAttributeValueAggregate)
        .filter(ProposedAttributeValueAggregate.workspace_id == workspace_id,
                ProposedAttributeValueAggregate.status == PROPOSAL_STATUS_PENDING)
        .all()
    )
    if not aggs:
        return QueueHealthSnapshot(
            pending_total=0, stale_count=0,
            oldest_age_days=0, avg_age_days=0.0,
            by_attribute={}, by_attribute_stale={},
        )
    now = datetime.now(timezone.utc)
    ages = [
        max(0, (now - _ensure_aware(a.created_at)).days) for a in aggs
    ]
    by_attr: dict[str, int] = defaultdict(int)
    by_attr_stale: dict[str, int] = defaultdict(int)
    stale_count = 0
    for a, age in zip(aggs, ages):
        by_attr[a.attribute_name] += 1
        if age >= QUEUE_STALE_THRESHOLD_DAYS:
            stale_count += 1
            by_attr_stale[a.attribute_name] += 1
    return QueueHealthSnapshot(
        pending_total=len(aggs),
        stale_count=stale_count,
        oldest_age_days=max(ages),
        avg_age_days=round(sum(ages) / len(ages), 2),
        by_attribute=dict(by_attr),
        by_attribute_stale=dict(by_attr_stale),
    )


# ==========================================================================
# Stage 3 — review actions (continued)
# ==========================================================================


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
