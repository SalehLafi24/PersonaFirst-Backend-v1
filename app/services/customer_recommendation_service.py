"""Customer recommendation V1.

Pipeline:
    1. Extract persona via persona_extraction_service.
    2. Pull workspace catalog with attribute lookup.
    3. Exclude products the customer has already interacted with.
    4. Score each candidate by persona-fit:

        persona_fit = (
            0.50 * persona.product_type.distribution.get(c.product_type, 0)
          + 0.30 * persona.age_group.distribution.get(c.age_group,    0)
          + 0.20 * persona.gender.distribution.get(c.gender,          0)
        )
        score = persona_fit * persona.confidence_overall

    5. Sort by score desc, deterministic tie-break, return top N with
       structured per-attribute matched-signals breakdown.

This service does NOT touch the V1 product-to-product scorer. It does
NOT use an LLM. It does NOT impute customer preferences -- it only
projects the customer's observed attribute distribution onto candidate
products.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.customer import CustomerInteraction
from app.models.product import Product, ProductAttribute
from app.services.attribute_engine import load_manifest
from app.services.attribute_engine.manifest import AttributeManifest
from app.services.intent_layer_service import (
    RerankResult,
    build_context,
    rerank,
)
from app.services.persona_extraction_service import (
    CustomerPersona,
    extract_persona,
)


# Persona-fit weights are read from the manifest at request time
# (recommendation.score_weight per attribute, validator-enforced to sum
# to 1.0 across persona_relevant attributes). The legacy hardcoded
# constants are gone -- all weight tuning happens in the manifest.


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MatchedSignal:
    attribute_name: str
    candidate_value: str | None
    persona_probability: float           # persona's affinity for that value
    weight: float                         # configured per-attribute weight
    contribution: float                   # weight * probability

    def to_json(self) -> dict:
        return {
            "attribute_name": self.attribute_name,
            "candidate_value": self.candidate_value,
            "persona_probability": round(self.persona_probability, 4),
            "weight": round(self.weight, 4),
            "contribution": round(self.contribution, 4),
        }


@dataclass
class CustomerRecommendation:
    product_id: str
    name: str
    attributes: dict[str, str | None]
    persona_fit: float                    # raw persona_fit before damping
    score: float                          # damped final score (post-rerank)
    matched_signals: list[MatchedSignal] = field(default_factory=list)
    explanation: str = ""
    # Populated by the intent layer (intent_layer_service.rerank). Each
    # entry is the JSON form of a Contribution: {signal, kind, value, reason}.
    intent_contributions: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "attributes": self.attributes,
            "persona_fit": round(self.persona_fit, 4),
            "score": round(self.score, 4),
            "matched_signals": [m.to_json() for m in self.matched_signals],
            "explanation": self.explanation,
            "intent_contributions": list(self.intent_contributions),
        }


@dataclass
class CustomerRecommendationResponse:
    customer_id: str
    workspace_id: int
    computed_at: str
    persona: CustomerPersona
    recommendations: list[CustomerRecommendation]
    excluded_count: int
    candidates_considered: int
    intent_summary: dict | None = None
    intent_dropped: list[dict] | None = None  # name, product_id, reasons

    def to_json(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "workspace_id": self.workspace_id,
            "computed_at": self.computed_at,
            "persona": self.persona.to_json(),
            "recommendations": [r.to_json() for r in self.recommendations],
            "excluded_count": self.excluded_count,
            "candidates_considered": self.candidates_considered,
            "intent_summary": self.intent_summary,
            "intent_dropped": self.intent_dropped,
        }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _explanation_sentence(matched: list[MatchedSignal]) -> str:
    """Build a one-line plain-English explanation from the matched signals."""
    parts: list[str] = []
    for m in matched:
        if m.contribution <= 0 or not m.candidate_value:
            continue
        # Format probability as a percent, e.g. "62% infant on age_group".
        pct = round(m.persona_probability * 100)
        parts.append(f"{pct}% {m.candidate_value} on {m.attribute_name}")
    if not parts:
        return "No persona-driven signal — candidate did not match any persona attribute."
    return "Matches: " + "; ".join(parts) + "."


def _stable_hash(customer_id: str, candidate_id: str) -> int:
    return int(
        hashlib.sha256(f"{customer_id}|{candidate_id}".encode("utf-8")).hexdigest()[:12],
        16,
    )


# Diversity policy: top-N selection cap on a single attribute. Addresses
# the "90% books for toy buyer" pattern surfaced in the rubric pass: when
# many candidates tie at the top of the ranked list, the engine can fill
# top-N with one product_type. Cap forces variety by skipping over-cap
# candidates, then filling from the skipped pool only after diverse
# slots are exhausted. Hardcoded for V1.1; configurable knob is task #126.
_DIVERSITY_CAP_ATTRIBUTE = "product_type"
_DIVERSITY_CAP_SHARE = 0.5  # max fraction of top-N for any single value


def _apply_top_n_diversity_cap(
    sorted_recs: list,
    *,
    top_n: int,
    cap_attribute: str = _DIVERSITY_CAP_ATTRIBUTE,
    cap_share: float = _DIVERSITY_CAP_SHARE,
) -> list:
    """Greedy top-N selection with a per-attribute cap.

    Walks `sorted_recs` (assumed score-descending). Includes a rec if
    its `cap_attribute` value's running count is below
    ceil(top_n * cap_share). Skipped recs are pooled and used to fill
    remaining slots if the cap leaves slots empty.

    Score order is preserved within and across cap buckets. The cap only
    activates when a single attribute value would otherwise dominate the
    top-N; on already-diverse rec sets the function is a no-op
    truncation.
    """
    import math
    if top_n <= 0:
        return []
    if top_n <= 1 or not sorted_recs:
        return list(sorted_recs[:top_n])

    per_value_cap = max(1, math.ceil(top_n * cap_share))

    selected: list = []
    skipped: list = []
    counts: dict = {}

    for rec in sorted_recs:
        if len(selected) >= top_n:
            break
        value = (rec.attributes or {}).get(cap_attribute)
        if counts.get(value, 0) >= per_value_cap:
            skipped.append(rec)
            continue
        selected.append(rec)
        counts[value] = counts.get(value, 0) + 1

    # Fill remaining slots from skipped pool (preserve score order).
    if len(selected) < top_n:
        for rec in skipped:
            if len(selected) >= top_n:
                break
            selected.append(rec)

    return selected[:top_n]


def _load_catalog_attrs(
    db: Session, workspace_id: int, persona_attrs: tuple[str, ...],
) -> tuple[list[Product], dict[int, dict[str, str]]]:
    products = (
        db.query(Product)
        .filter(Product.workspace_id == workspace_id)
        .all()
    )
    if not products:
        return [], {}
    rows = (
        db.query(ProductAttribute.product_id,
                 ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id.in_(persona_attrs))
        .all()
    )
    by_dbid: dict[int, dict[str, str]] = {}
    for db_id, attr, val in rows:
        by_dbid.setdefault(db_id, {})[attr] = val
    return products, by_dbid


def _interacted_product_ids(
    db: Session, *, workspace_id: int, customer_id: str,
) -> set[str]:
    rows = (
        db.query(CustomerInteraction.product_id)
        .filter(CustomerInteraction.workspace_id == workspace_id,
                CustomerInteraction.customer_id == customer_id)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def recommend_for_customer(
    db: Session,
    *,
    workspace_id: int,
    customer_id: str,
    top_n: int = 10,
    manifest: AttributeManifest | None = None,
) -> CustomerRecommendationResponse:
    """Score the catalog against the customer's persona; return top-N
    candidates excluding already-interacted products.

    When the persona is cold-start, confidence is low and the damped
    score reflects that -- candidates aren't strongly preferred over each
    other, which matches what we know about the customer (very little).
    Callers can read persona.cold_start to decide whether to surface the
    output or fall back to a popularity / anchor-based path.
    """
    mfst = manifest or load_manifest()
    persona_attrs: tuple[str, ...] = tuple(mfst.persona_relevant_attributes())
    weights = mfst.score_weights()  # name -> weight, sums to 1.0

    persona = extract_persona(
        db, workspace_id=workspace_id, customer_id=customer_id, manifest=mfst,
    )

    products, attrs_by_dbid = _load_catalog_attrs(db, workspace_id, persona_attrs)
    # NOTE: previously we pre-excluded already-interacted products here.
    # Phase 1 moved that responsibility into the intent layer so suppression
    # is observable (in `intent_dropped`) and window-aware re-suggestions
    # work for repurchasable products outside their window. We still report
    # the count for parity with the previous response shape.
    excluded = _interacted_product_ids(
        db, workspace_id=workspace_id, customer_id=customer_id,
    )

    # Per-attribute persona distributions, keyed by attribute name.
    distributions: dict[str, dict[str, float]] = {
        attr: (persona.attribute_affinities[attr].distribution
               if attr in persona.attribute_affinities else {})
        for attr in persona_attrs
    }
    confidence = persona.confidence_overall

    scored: list[CustomerRecommendation] = []
    candidates_considered = 0
    for prod in products:
        # Intent layer (downstream) decides whether to suppress; keep all
        # products as candidates so the suppression is observable.
        candidates_considered += 1
        attrs = attrs_by_dbid.get(prod.id, {})

        matched: list[MatchedSignal] = []
        persona_fit = 0.0
        for attr in persona_attrs:
            value = attrs.get(attr)
            weight = float(weights.get(attr, 0.0))
            prob = distributions.get(attr, {}).get(value, 0.0) if value else 0.0
            contribution = weight * prob
            persona_fit += contribution
            matched.append(MatchedSignal(
                attribute_name=attr,
                candidate_value=value,
                persona_probability=prob,
                weight=weight,
                contribution=contribution,
            ))
        score = persona_fit * confidence

        rec = CustomerRecommendation(
            product_id=prod.product_id,
            name=prod.name or prod.product_id,
            attributes={k: attrs.get(k) for k in persona_attrs},
            persona_fit=persona_fit,
            score=score,
            matched_signals=matched,
        )
        rec.explanation = _explanation_sentence(matched)
        scored.append(rec)

    # Sort: score desc, then deterministic hash tie-break.
    scored.sort(
        key=lambda r: (-r.score, _stable_hash(customer_id, r.product_id))
    )

    # Intent layer (Phase 1): post-process the FULL ranked candidate list
    # before truncating to top_n. The intent_layer is a pure read-only
    # post-processor; it never modifies persona-fit scoring.
    intent_ctx = build_context(
        db, workspace_id=workspace_id,
        customer_id=customer_id, persona=persona,
    )
    rerank_result: RerankResult = rerank(db, scored, intent_ctx)
    kept = rerank_result.kept

    # Re-sort after intent boost/demote effects (Phase 1's RepurchaseSuppression
    # only drops, but this keeps future signals correct without changes here).
    kept.sort(
        key=lambda r: (-r.score, _stable_hash(customer_id, r.product_id))
    )
    top = _apply_top_n_diversity_cap(kept, top_n=top_n)

    intent_dropped_payload = [
        {
            "product_id": d.product_id,
            "name": d.name,
            "attributes": d.attributes,
            "reasons": [
                k.reason
                for k in (rerank_result.contributions_by_pid.get(d.product_id) or [])
                if k.kind == "drop"
            ],
        }
        for d in rerank_result.dropped
    ]

    return CustomerRecommendationResponse(
        customer_id=customer_id,
        workspace_id=workspace_id,
        computed_at=datetime.utcnow().isoformat(timespec="seconds"),
        persona=persona,
        recommendations=top,
        excluded_count=len(excluded),
        candidates_considered=candidates_considered,
        intent_summary=rerank_result.to_summary(),
        intent_dropped=intent_dropped_payload,
    )
