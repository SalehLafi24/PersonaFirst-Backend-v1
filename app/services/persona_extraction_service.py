"""Customer persona extraction.

Pure read-only transform: customer interactions + ProductAttribute -> a
structured CustomerPersona. Deterministic, explainable, no LLM, no labels.

Public entry point:

    extract_persona(db, workspace_id, customer_id) -> CustomerPersona

Design (matches the persona model design doc):

    - Per-attribute distribution over observed values (sums to 1.0).
    - focus_score = 1 - normalised_entropy over observed values.
    - confidence per attribute = min(1, effective_n / 10).
    - cold_start = (interaction_count < 3) or (effective_n_total < 3).

V1 weighting:
    - interaction_type='purchase' contributes weight 1.0; others 0.0
      (open vocabulary, but only purchases drive the persona today).
    - No recency decay in V1 — kept as a deliberate simplification; the
      service is structured to add it without redesign (a per-interaction
      weight is computed in one place).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.customer import (
    INTERACTION_PURCHASE,
    Customer,
    CustomerInteraction,
)
from app.models.product import Product, ProductAttribute
from app.services.attribute_engine import load_manifest
from app.services.attribute_engine.manifest import AttributeManifest


# Attributes the persona tracks are now derived from the manifest's
# `recommendation.persona_relevant` field. The legacy module-level
# constant is preserved as a fallback for callers that haven't been
# migrated, but the recommended path is `_persona_attributes(manifest)`.
PERSONA_ATTRIBUTES: tuple[str, ...] = ("product_type", "age_group", "gender", "use_case")


def _persona_attributes(manifest: AttributeManifest | None) -> list[str]:
    """Resolve the list of persona-relevant attribute names.

    Reads the manifest when provided; falls back to load_manifest().
    Used by extract_persona to know which attributes feed the persona
    distributions. The customer rec service reads the same list (and
    the matching `score_weight` per attribute) for scoring.
    """
    m = manifest or load_manifest()
    return m.persona_relevant_attributes()

# Type weights. 'purchase' is 1.0; everything else is 0.0 in V1 -- they're
# accepted into the table for future signal but excluded from persona math.
_INTERACTION_TYPE_WEIGHTS: dict[str, float] = {
    INTERACTION_PURCHASE: 1.0,
}

# Confidence calibration: confidence = min(1, effective_n / _CONF_DENOM).
_CONF_DENOM = 10.0

# Cold-start thresholds.
_COLD_START_INTERACTION_FLOOR = 3
_COLD_START_EFFECTIVE_N_FLOOR = 3.0


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AttributeAffinity:
    attribute_name: str
    distribution: dict[str, float]      # value -> probability, sums to 1.0
    effective_n: float                   # weighted count of contributing interactions
    coverage_pct: float                  # fraction of interactions with this attr known
    focus_score: float                   # 0..1, 1 = monomaniac, 0 = uniform across observed
    top_values: list[tuple[str, float]]  # top 3 (value, probability)
    confidence: float                    # min(1, effective_n / 10)

    def to_json(self) -> dict:
        return {
            "attribute_name": self.attribute_name,
            "distribution": {k: round(v, 4) for k, v in self.distribution.items()},
            "effective_n": round(self.effective_n, 3),
            "coverage_pct": round(self.coverage_pct, 4),
            "focus_score": round(self.focus_score, 4),
            "top_values": [(v, round(p, 4)) for v, p in self.top_values],
            "confidence": round(self.confidence, 4),
        }


@dataclass
class CustomerPersona:
    customer_id: str
    workspace_id: int
    computed_at: str                                  # ISO8601
    interaction_count_total: int
    interaction_count_contributing: int               # subset whose type is weighted > 0
    attribute_affinities: dict[str, AttributeAffinity] = field(default_factory=dict)
    confidence_overall: float = 0.0
    diversity_score: float = 0.0                      # mean of (1 - focus) across attrs with data
    cold_start: bool = True
    cold_start_reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "workspace_id": self.workspace_id,
            "computed_at": self.computed_at,
            "interaction_count_total": self.interaction_count_total,
            "interaction_count_contributing": self.interaction_count_contributing,
            "attribute_affinities": {
                k: a.to_json() for k, a in self.attribute_affinities.items()
            },
            "confidence_overall": round(self.confidence_overall, 4),
            "diversity_score": round(self.diversity_score, 4),
            "cold_start": self.cold_start,
            "cold_start_reasons": list(self.cold_start_reasons),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _interaction_weight(interaction: CustomerInteraction) -> float:
    """V1: purchase=1.0, anything else=0.0."""
    return _INTERACTION_TYPE_WEIGHTS.get(interaction.interaction_type, 0.0)


def _focus_from_distribution(dist: dict[str, float]) -> float:
    """Normalised-entropy focus. 1.0 = single value; 0.0 = uniform across
    *observed* values. Returns 1.0 trivially for k=1 (only one value seen).
    """
    if not dist:
        return 0.0
    k = sum(1 for p in dist.values() if p > 0)
    if k <= 1:
        return 1.0
    h = -sum(p * math.log2(p) for p in dist.values() if p > 0)
    h_max = math.log2(k)
    return max(0.0, min(1.0, 1.0 - (h / h_max)))


def _build_affinity(
    attribute_name: str,
    weighted_counts: dict[str, float],
    total_weight: float,
) -> AttributeAffinity:
    """Pure: turn a weighted count map + total denominator into one affinity."""
    effective_n = sum(weighted_counts.values())
    distribution: dict[str, float] = {}
    if effective_n > 0:
        distribution = {
            v: w / effective_n for v, w in weighted_counts.items() if w > 0
        }
    coverage_pct = (effective_n / total_weight) if total_weight > 0 else 0.0
    focus = _focus_from_distribution(distribution)
    confidence = min(1.0, effective_n / _CONF_DENOM)
    top = sorted(distribution.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return AttributeAffinity(
        attribute_name=attribute_name,
        distribution=distribution,
        effective_n=effective_n,
        coverage_pct=coverage_pct,
        focus_score=focus,
        top_values=top,
        confidence=confidence,
    )


def _load_attrs_for_products(
    db: Session, workspace_id: int, product_ext_ids: Iterable[str],
    persona_attrs: tuple[str, ...] = PERSONA_ATTRIBUTES,
) -> dict[str, dict[str, str]]:
    """external product_id -> {attribute_name: value}, restricted to persona_attrs."""
    ext_ids = list(set(product_ext_ids))
    if not ext_ids:
        return {}
    rows = (
        db.query(Product.product_id,
                 ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .join(ProductAttribute, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                Product.product_id.in_(ext_ids),
                ProductAttribute.attribute_id.in_(persona_attrs))
        .all()
    )
    out: dict[str, dict[str, str]] = {}
    for ext_id, attr, val in rows:
        out.setdefault(ext_id, {})[attr] = val
    return out


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def extract_persona(
    db: Session, *, workspace_id: int, customer_id: str,
    manifest: AttributeManifest | None = None,
) -> CustomerPersona:
    """Build a CustomerPersona from the customer's interactions and the
    current ProductAttribute state. Deterministic. No LLM.

    The set of persona attributes is derived from the manifest's
    `recommendation.persona_relevant` field. Callers that don't pass a
    manifest get the default loaded from disk (cached on second call).
    """
    persona_attrs = tuple(_persona_attributes(manifest))

    # Load all interactions (no recency window in V1 — kept simple).
    interactions = (
        db.query(CustomerInteraction)
        .filter(CustomerInteraction.workspace_id == workspace_id,
                CustomerInteraction.customer_id == customer_id)
        .order_by(CustomerInteraction.occurred_at.asc())
        .all()
    )

    persona = CustomerPersona(
        customer_id=customer_id,
        workspace_id=workspace_id,
        computed_at=datetime.utcnow().isoformat(timespec="seconds"),
        interaction_count_total=len(interactions),
        interaction_count_contributing=0,
    )

    if not interactions:
        persona.cold_start = True
        persona.cold_start_reasons.append("no interactions")
        for attr in persona_attrs:
            persona.attribute_affinities[attr] = _build_affinity(attr, {}, 0.0)
        return persona

    # Pre-compute per-interaction weights and the attribute-by-attribute
    # weighted counts. We pass over the data once for total_weight, then
    # use the same weights when bucketing per attribute.
    attr_lookup = _load_attrs_for_products(
        db, workspace_id, [i.product_id for i in interactions], persona_attrs,
    )

    total_weight = 0.0
    contributing_count = 0
    weighted_counts: dict[str, dict[str, float]] = {a: {} for a in persona_attrs}

    for interaction in interactions:
        w = _interaction_weight(interaction)
        if w <= 0:
            continue
        total_weight += w
        contributing_count += 1
        attrs = attr_lookup.get(interaction.product_id, {})
        for attr in persona_attrs:
            v = attrs.get(attr)
            if v:
                weighted_counts[attr][v] = weighted_counts[attr].get(v, 0.0) + w

    persona.interaction_count_contributing = contributing_count

    for attr in persona_attrs:
        persona.attribute_affinities[attr] = _build_affinity(
            attr, weighted_counts[attr], total_weight,
        )

    # Overall confidence: simple mean of per-attribute confidences. Equal
    # weights chosen here -- the rec service applies its own weighted
    # damping (0.50/0.30/0.20 per the spec) to the persona-fit score.
    confs = [a.confidence for a in persona.attribute_affinities.values()]
    persona.confidence_overall = sum(confs) / len(confs) if confs else 0.0

    # Diversity: only include attributes that have any data.
    foci = [
        a.focus_score for a in persona.attribute_affinities.values()
        if a.effective_n > 0
    ]
    persona.diversity_score = (
        sum(1.0 - f for f in foci) / len(foci) if foci else 0.0
    )

    # Cold start.
    cold_reasons: list[str] = []
    if contributing_count < _COLD_START_INTERACTION_FLOOR:
        cold_reasons.append(
            f"contributing interactions={contributing_count} < "
            f"{_COLD_START_INTERACTION_FLOOR}"
        )
    if total_weight < _COLD_START_EFFECTIVE_N_FLOOR:
        cold_reasons.append(
            f"total weighted_n={total_weight:.2f} < "
            f"{_COLD_START_EFFECTIVE_N_FLOOR}"
        )
    persona.cold_start = bool(cold_reasons)
    persona.cold_start_reasons = cold_reasons
    return persona


def list_customers(
    db: Session, *, workspace_id: int,
) -> list[dict]:
    """Lightweight customer listing for the Explorer picker. Returns a
    summary per customer: id, interaction count, first-seen, last-seen."""
    customers = (
        db.query(Customer)
        .filter(Customer.workspace_id == workspace_id)
        .order_by(Customer.customer_id.asc())
        .all()
    )
    if not customers:
        return []
    counts: dict[str, int] = {}
    last_seen: dict[str, datetime] = {}
    for cid, occ in (
        db.query(CustomerInteraction.customer_id, CustomerInteraction.occurred_at)
        .filter(CustomerInteraction.workspace_id == workspace_id)
        .all()
    ):
        counts[cid] = counts.get(cid, 0) + 1
        prev = last_seen.get(cid)
        if prev is None or occ > prev:
            last_seen[cid] = occ
    return [
        {
            "customer_id": c.customer_id,
            "interaction_count": counts.get(c.customer_id, 0),
            "last_interaction_at": (
                last_seen[c.customer_id].isoformat()
                if c.customer_id in last_seen else None
            ),
        }
        for c in customers
    ]
