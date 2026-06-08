"""Multi-source signal strength scoring.

Complements (does not replace) the existing customer signal strength
framework and the recommendation_score pipeline. Consumes already-merged
EnrichmentOutput objects produced by the attribute merge service, so it
remains agnostic of how attributes are modelled in the database.

Everything here is generic and driven only by:
    - confidence (on EnrichedValue)
    - source / contributing_sources (on EnrichedValue)
    - warnings (on EnrichmentOutput)
    - attribute_class / targeting_mode / behaviors (via AttributeDefinition)

No attribute name is ever hardcoded.
"""

from typing import Iterable

from pydantic import BaseModel

from app.schemas.attribute_enrichment import (
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
)
from app.schemas.recommendation import RecommendationRead, SignalSummary


# ---------------------------------------------------------------------------
# Tunable weights (kept round and explainable)
# ---------------------------------------------------------------------------

PRODUCT_SIGNAL_WEIGHTS = {
    "coverage": 0.35,
    "avg_confidence": 0.35,
    "agreement": 0.30,
    "conflict_penalty": 0.40,
}

MATCH_CONFIDENCE_WEIGHTS = {
    "customer_signal": 0.30,
    "product_signal": 0.30,
    "attribute_match": 0.20,
    "compatibility": 0.20,
    "conflict_penalty": 0.30,
}

DEFAULT_WEAK_CONFIDENCE_THRESHOLD = 0.6

# Warning tokens produced by the enrichment / merge services. Listed here
# (not hardcoded inside formulas) so tuning and future additions are easy.
CONFLICT_WARNINGS = frozenset({"cross_source_conflict"})
MISSING_WARNINGS = frozenset({"no_supported_value_found"})
AMBIGUOUS_WARNINGS = frozenset({"ambiguous_evidence"})


# ---------------------------------------------------------------------------
# Component / result models
# ---------------------------------------------------------------------------


class ProductSignalComponents(BaseModel):
    coverage: float
    avg_confidence: float
    agreement: float
    conflict_penalty: float
    strength: float


class ExtendedCustomerSignal(BaseModel):
    base: float
    source_quality: float
    extended: float


class MatchConfidenceComponents(BaseModel):
    customer_signal: float
    product_signal: float
    attribute_match: float
    compatibility: float
    conflict_penalty: float
    match_confidence: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _top_value(output: EnrichmentOutput) -> EnrichedValue | None:
    if not output.values:
        return None
    return max(output.values, key=lambda v: v.confidence)


def _is_conflict(output: EnrichmentOutput) -> bool:
    return any(w in CONFLICT_WARNINGS for w in output.warnings)


def _is_missing(output: EnrichmentOutput) -> bool:
    if not output.values:
        return True
    return any(w in MISSING_WARNINGS for w in output.warnings)


def _is_weak(
    output: EnrichmentOutput,
    threshold: float = DEFAULT_WEAK_CONFIDENCE_THRESHOLD,
) -> bool:
    top = _top_value(output)
    if top is None:
        return False  # classified as missing, not weak
    return top.confidence < threshold


# ---------------------------------------------------------------------------
# Conflict aggregation
# ---------------------------------------------------------------------------


def aggregate_conflict_penalty(
    outputs: Iterable[EnrichmentOutput],
    *,
    weak_threshold: float = DEFAULT_WEAK_CONFIDENCE_THRESHOLD,
) -> float:
    """Collapse cross_source_conflict / missing / weak indicators into a
    single penalty score in [0, 1]. Generic: driven only by warnings and
    confidence, never by attribute names.
    """
    outs = list(outputs)
    if not outs:
        return 0.0
    score = 0.0
    for o in outs:
        if _is_conflict(o):
            score += 1.0
            continue
        if _is_missing(o):
            score += 0.6
            continue
        if _is_weak(o, threshold=weak_threshold):
            score += 0.4
            continue
        if any(w in AMBIGUOUS_WARNINGS for w in o.warnings):
            score += 0.3
    return _clamp(score / len(outs))


# ---------------------------------------------------------------------------
# Product signal strength
# ---------------------------------------------------------------------------


def compute_product_signal_strength(
    outputs: dict[str, EnrichmentOutput],
    *,
    expected_attributes: int | None = None,
    weak_threshold: float = DEFAULT_WEAK_CONFIDENCE_THRESHOLD,
) -> ProductSignalComponents:
    """Compute a product-level signal strength from merged enrichment outputs.

    Factors:
        - coverage: fraction of expected attributes that produced values
        - avg_confidence: mean top-value confidence across filled attributes
        - agreement: fraction of filled attributes whose top value is backed
          by 2+ contributing sources (text + visual agreement)
        - conflict_penalty: aggregated indicator of
          cross_source_conflict / missing / weak / ambiguous outputs

    Formula:
        strength = clamp(
            0.35 * coverage
          + 0.35 * avg_confidence
          + 0.30 * agreement
          - 0.40 * conflict_penalty
        )

    `expected_attributes` can be passed explicitly (e.g. when the caller
    knows the full attribute set it wanted enriched but only some outputs
    were produced). When omitted, coverage is relative to the outputs dict.
    """
    total = (
        expected_attributes
        if expected_attributes is not None
        else len(outputs)
    )
    if total <= 0:
        return ProductSignalComponents(
            coverage=0.0,
            avg_confidence=0.0,
            agreement=0.0,
            conflict_penalty=0.0,
            strength=0.0,
        )

    filled = [o for o in outputs.values() if o.values]
    n_filled = len(filled)

    coverage = _clamp(n_filled / total)

    if n_filled > 0:
        avg_confidence = sum(
            _top_value(o).confidence for o in filled  # type: ignore[union-attr]
        ) / n_filled
        agreement = sum(
            1
            for o in filled
            if len((_top_value(o) or EnrichedValue(  # safety fallback
                value=None, confidence=0.0, source=EnrichmentSource.TEXT
            )).contributing_sources) >= 2
        ) / n_filled
    else:
        avg_confidence = 0.0
        agreement = 0.0

    conflict_penalty = aggregate_conflict_penalty(
        outputs.values(), weak_threshold=weak_threshold
    )

    raw = (
        PRODUCT_SIGNAL_WEIGHTS["coverage"] * coverage
        + PRODUCT_SIGNAL_WEIGHTS["avg_confidence"] * avg_confidence
        + PRODUCT_SIGNAL_WEIGHTS["agreement"] * agreement
        - PRODUCT_SIGNAL_WEIGHTS["conflict_penalty"] * conflict_penalty
    )
    strength = _clamp(raw)

    return ProductSignalComponents(
        coverage=round(coverage, 6),
        avg_confidence=round(avg_confidence, 6),
        agreement=round(agreement, 6),
        conflict_penalty=round(conflict_penalty, 6),
        strength=round(strength, 6),
    )


# ---------------------------------------------------------------------------
# Customer signal extension (source-quality awareness)
# ---------------------------------------------------------------------------


def extend_customer_signal_with_source_quality(
    base_signal: float,
    contributing_product_signals: Iterable[float],
    *,
    source_quality_weight: float = 0.2,
) -> ExtendedCustomerSignal:
    """Blend the existing customer signal strength with a source-quality
    component reflecting how strong the underlying products' signals are.

    This is a *blend*, not a replacement — the existing purchase-depth /
    attribute-richness / behavioral-graph customer signal continues to
    drive most of the score. Only a small slice (default 20%) is allocated
    to source quality so existing callers see stable values.
    """
    signals = [s for s in contributing_product_signals]
    source_quality = (sum(signals) / len(signals)) if signals else 0.0
    extended = (
        (1.0 - source_quality_weight) * _clamp(base_signal)
        + source_quality_weight * _clamp(source_quality)
    )
    return ExtendedCustomerSignal(
        base=round(_clamp(base_signal), 6),
        source_quality=round(_clamp(source_quality), 6),
        extended=round(_clamp(extended), 6),
    )


# ---------------------------------------------------------------------------
# Match confidence (recommendation-level)
# ---------------------------------------------------------------------------


def compute_match_confidence(
    *,
    customer_signal_strength: float,
    product_signal_strength: float,
    attribute_match_strength: float,
    compatibility_certainty: float,
    conflict_penalty: float,
) -> MatchConfidenceComponents:
    """Combine customer-side, product-side, match quality, compatibility, and
    conflict indicators into a single recommendation-level confidence in
    [0, 1].

    Formula:
        match_confidence = clamp(
              0.30 * customer_signal
            + 0.30 * product_signal
            + 0.20 * attribute_match
            + 0.20 * compatibility
            - 0.30 * conflict_penalty
        )

    All inputs are expected to be in [0, 1]; they are clamped defensively.
    """
    c = _clamp(customer_signal_strength)
    p = _clamp(product_signal_strength)
    m = _clamp(attribute_match_strength)
    compat = _clamp(compatibility_certainty)
    pen = _clamp(conflict_penalty)

    raw = (
        MATCH_CONFIDENCE_WEIGHTS["customer_signal"] * c
        + MATCH_CONFIDENCE_WEIGHTS["product_signal"] * p
        + MATCH_CONFIDENCE_WEIGHTS["attribute_match"] * m
        + MATCH_CONFIDENCE_WEIGHTS["compatibility"] * compat
        - MATCH_CONFIDENCE_WEIGHTS["conflict_penalty"] * pen
    )
    mc = _clamp(raw)

    return MatchConfidenceComponents(
        customer_signal=round(c, 6),
        product_signal=round(p, 6),
        attribute_match=round(m, 6),
        compatibility=round(compat, 6),
        conflict_penalty=round(pen, 6),
        match_confidence=round(mc, 6),
    )


# ---------------------------------------------------------------------------
# Helpers for extracting inputs from existing recommendation scoring
# ---------------------------------------------------------------------------


def compatibility_certainty_from_scores(
    positive_contribution: float,
    negative_contribution: float,
) -> float:
    """Normalise compatibility positive/negative contributions into a
    [0, 1] certainty score. Generic — takes the already-computed numbers
    from RecommendationRead and does not care which attributes produced
    them.

    Returns 0.0 when there is no compatibility evidence either way.
    """
    total = positive_contribution + abs(negative_contribution)
    if total <= 0:
        return 0.0
    return _clamp(positive_contribution / total)


# ---------------------------------------------------------------------------
# Runtime integration — starter mode
# ---------------------------------------------------------------------------


def _estimate_attribute_match_strength(rec: RecommendationRead) -> float:
    """Approximate attribute match strength from matched_attributes.

    Generic: uses only the per-match score and weight fields already
    computed by the recommendation engine. No attribute names are
    referenced.
    """
    if not rec.matched_attributes:
        return 0.0
    weighted = sum(m.score * m.weight for m in rec.matched_attributes)
    avg = weighted / len(rec.matched_attributes)
    return _clamp(avg)


def _collect_conflict_indicators(
    outputs: dict[str, EnrichmentOutput],
) -> list[str]:
    """Collect distinct conflict / missing / ambiguous warning tokens
    across the product's merged enrichment outputs.

    Returns a sorted list of generic token strings (no attribute names)
    so downstream UIs can render a single-line summary.
    """
    indicators: set[str] = set()
    all_warnings = CONFLICT_WARNINGS | MISSING_WARNINGS | AMBIGUOUS_WARNINGS
    for o in outputs.values():
        for w in o.warnings:
            if w in all_warnings:
                indicators.add(w)
    return sorted(indicators)


def apply_multi_source_signals(
    recommendations: list[RecommendationRead],
    *,
    customer_signal_strength: float | None = None,
    product_enrichment_outputs: dict[str, dict[str, EnrichmentOutput]] | None = None,
    tiebreak_by_match_confidence: bool = True,
) -> list[RecommendationRead]:
    """Populate multi-source signal fields on a list of recommendations and
    optionally re-order by match_confidence as a secondary tiebreaker.

    Behavior:
        - Fields are only populated when the underlying data is available:
            * customer_signal_strength ← from the param (caller decides)
            * product_signal_strength  ← from product_enrichment_outputs
            * match_confidence         ← when both above are available
            * signal_summary           ← always (from already-computed fields)
        - The primary ordering stays driven by recommendation_score. When
          tiebreak_by_match_confidence is True AND at least one match
          confidence was computed, items with equal recommendation_score
          are re-ordered by match_confidence DESC. Items with None are
          pushed to the end within their equal-score group.
        - If nothing was computed (all inputs empty), the list order is
          preserved exactly, so legacy flows are unaffected.

    No scoring formulas are changed. recommendation_score is never mutated.
    """
    updated: list[RecommendationRead] = []
    any_match_confidence = False

    for rec in recommendations:
        product_signal: float | None = None
        conflict_penalty = 0.0
        conflict_indicators: list[str] = []

        if (
            product_enrichment_outputs is not None
            and rec.product_id in product_enrichment_outputs
        ):
            outputs = product_enrichment_outputs[rec.product_id]
            if outputs:
                product_comp = compute_product_signal_strength(outputs)
                product_signal = product_comp.strength
                conflict_penalty = product_comp.conflict_penalty
                conflict_indicators = _collect_conflict_indicators(outputs)

        match_conf: float | None = None
        if customer_signal_strength is not None and product_signal is not None:
            mc = compute_match_confidence(
                customer_signal_strength=customer_signal_strength,
                product_signal_strength=product_signal,
                attribute_match_strength=_estimate_attribute_match_strength(rec),
                compatibility_certainty=compatibility_certainty_from_scores(
                    rec.compatibility_positive_contribution,
                    rec.compatibility_negative_contribution,
                ),
                conflict_penalty=conflict_penalty,
            )
            match_conf = mc.match_confidence
            any_match_confidence = True

        summary = SignalSummary(
            matched_attribute_count=len(rec.matched_attributes),
            compatibility_positive=rec.compatibility_positive_contribution,
            compatibility_negative=rec.compatibility_negative_contribution,
            conflict_indicators=conflict_indicators,
        )

        updated.append(
            rec.model_copy(
                update={
                    "product_signal_strength": product_signal,
                    "customer_signal_strength": customer_signal_strength,
                    "match_confidence": match_conf,
                    "signal_summary": summary,
                }
            )
        )

    if tiebreak_by_match_confidence and any_match_confidence:
        # Python's sort is stable, so items with different
        # recommendation_score values keep their incoming relative order.
        # Within equal recommendation_score groups, match_confidence DESC
        # decides ordering; None sorts last.
        def _key(r: RecommendationRead):
            mc = r.match_confidence
            has_mc = mc is not None
            return (
                -r.recommendation_score,
                0 if has_mc else 1,
                -(mc if mc is not None else 0.0),
            )

        updated.sort(key=_key)

    return updated
