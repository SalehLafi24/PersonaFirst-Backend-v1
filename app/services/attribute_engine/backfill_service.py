"""Generic backfill: read approved aggregates, materialize ProductAttribute.

Replaces the per-attribute backfill scripts. Logic mirrors the existing
backfill_product_type / approve_gender_and_backfill scripts:

  - Build cluster_key -> canonical resolution from approved/merged aggregates
    (filtered to active AttributeAllowedValue rows).
  - For each product without a ProductAttribute row for this attribute,
    pick the highest-confidence event whose normalized_value resolves to
    a canonical; tie-break on most-recent created_at.
  - Insert exactly one ProductAttribute row (single cardinality) or up to
    max_values_per_object (multi).
  - Idempotent: never updates or deletes existing rows.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.services.attribute_engine.manifest import AttributeManifestEntry


@dataclass
class BackfillResult:
    attribute_name: str
    inserted: int = 0
    skipped_already_assigned: int = 0
    skipped_no_event: int = 0
    skipped_no_resolved_canonical: int = 0
    inserted_by_canonical: dict[str, int] = field(default_factory=dict)
    cluster_to_canonical_size: int = 0


def _build_cluster_to_canonical(
    db: Session, workspace_id: int, attribute_name: str,
) -> dict[str, str]:
    """Resolution map from approved/merged aggregates filtered through the
    active AttributeAllowedValue set. Pending/rejected aggregates are
    skipped."""
    active_aav: set[str] = {
        v for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == workspace_id,
            AttributeAllowedValue.attribute_name == attribute_name,
            AttributeAllowedValue.is_active == True,  # noqa: E712
        ).all()
    }
    active_lower = {v.lower() for v in active_aav}

    out: dict[str, str] = {}
    for agg in db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id,
        ProposedAttributeValueAggregate.attribute_name == attribute_name,
    ).all():
        if agg.status == PROPOSAL_STATUS_APPROVED:
            canonical = agg.canonical_value
        elif agg.status == PROPOSAL_STATUS_MERGED:
            canonical = agg.promoted_to_allowed_value
        else:
            continue
        if not canonical or canonical.lower() not in active_lower:
            continue
        # Use the cased AAV value to avoid case drift.
        cased = next(
            (v for v in active_aav if v.lower() == canonical.lower()),
            canonical,
        )
        out[agg.cluster_key] = cased
    return out


def backfill_attribute(
    db: Session,
    *,
    workspace_id: int,
    attribute_name: str,
    manifest_entry: AttributeManifestEntry,
) -> BackfillResult:
    """Materialize ProductAttribute rows for *attribute_name* in *workspace_id*."""
    res = BackfillResult(attribute_name=attribute_name)

    cluster_to_canonical = _build_cluster_to_canonical(
        db, workspace_id, attribute_name,
    )
    res.cluster_to_canonical_size = len(cluster_to_canonical)

    products = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).all()

    # DB-id of products already having any ProductAttribute row for this attr.
    already_assigned: set[int] = {
        pid for (pid,) in db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == attribute_name)
        .distinct().all()
    }

    events_by_pid: dict[str, list[ProposedAttributeValueEvent]] = defaultdict(list)
    for ev in db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == workspace_id,
        ProposedAttributeValueEvent.attribute_name == attribute_name,
    ).all():
        events_by_pid[ev.product_id].append(ev)

    inserted_by_canonical: Counter = Counter()
    single = manifest_entry.taxonomy.cardinality == "single"
    max_per_object = manifest_entry.proposal.max_values_per_object

    # Phase A: multi-event resolution policy.
    resolution = manifest_entry.backfill.multi_event_resolution
    specificity_order = manifest_entry.backfill.specificity_order
    specificity_idx_map: dict[str, int] = {
        v: i for i, v in enumerate(specificity_order)
    }
    # Values not in the order map sort to the end (least specific).
    fallback_idx = len(specificity_order)

    def _pick_assignable_for_single(
        assignable: list[tuple[ProposedAttributeValueEvent, str]]
    ) -> tuple[ProposedAttributeValueEvent, str]:
        if resolution == "most_specific":
            # Lowest specificity index wins; confidence as tie-breaker;
            # most-recent created_at as final tie-breaker.
            return min(
                assignable,
                key=lambda x: (
                    specificity_idx_map.get(x[1], fallback_idx),
                    -float(x[0].confidence or 0),
                    -(x[0].created_at.timestamp() if x[0].created_at else 0),
                ),
            )
        # Default: highest_confidence (legacy behaviour).
        return max(
            assignable,
            key=lambda x: (float(x[0].confidence or 0), x[0].created_at),
        )

    for prod in products:
        if prod.id in already_assigned and manifest_entry.backfill.idempotent:
            res.skipped_already_assigned += 1
            continue
        evs = events_by_pid.get(prod.product_id, [])
        if not evs:
            res.skipped_no_event += 1
            continue

        assignable: list[tuple[ProposedAttributeValueEvent, str]] = []
        for ev in evs:
            canonical = cluster_to_canonical.get(ev.normalized_value)
            if canonical is None:
                continue
            assignable.append((ev, canonical))
        if not assignable:
            res.skipped_no_resolved_canonical += 1
            continue

        if single:
            ev, canonical = _pick_assignable_for_single(assignable)
            chosen = [(ev, canonical)]
        else:
            assignable.sort(
                key=lambda x: (-float(x[0].confidence or 0), x[0].created_at),
            )
            seen: set[str] = set()
            chosen = []
            for ev, canonical in assignable:
                if canonical in seen:
                    continue
                chosen.append((ev, canonical))
                seen.add(canonical)
                if len(chosen) >= max_per_object:
                    break

        for _ev, canonical in chosen:
            db.add(ProductAttribute(
                product_id=prod.id,
                attribute_id=attribute_name,
                attribute_value=canonical,
            ))
            res.inserted += 1
            inserted_by_canonical[canonical] += 1

    res.inserted_by_canonical = dict(inserted_by_canonical)
    if res.inserted:
        db.commit()
    return res
