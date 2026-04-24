"""Attribute enrichment merge service.

Combines text and visual enrichment results into a single merged output.
Generic by design: only consults attribute.value_mode, attribute.class_name,
and the source / confidence / warnings fields of the inputs. No
attribute-specific logic is baked in.

Merge semantics:
    - Only one source returns a value → pass it through.
    - Both sources agree on a value → combine evidence, boost confidence.
    - Both sources disagree (no overlap) on a single/boolean-mode attribute:
        * effective confidence gap ≥ threshold → prefer the higher
          effective-confidence value
        * otherwise → return no value and add warning "cross_source_conflict"
    - Multi-value attributes union non-overlapping values from both sides.

Class-aware source priors:
    During conflict resolution only, each source's raw confidence is
    multiplied by a class-specific weight before winner selection and
    threshold evaluation. Priors are intentionally NOT applied when only
    one source returns a value or when both sources agree.
"""

from typing import Any, Hashable

from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)


# ---------------------------------------------------------------------------
# Class-aware source priors
#
# Used ONLY during conflict resolution. Expresses how much each source is
# trusted for a given attribute class, without hardcoding any attribute names.
# ---------------------------------------------------------------------------

CLASS_SOURCE_WEIGHTS: dict[str, dict[EnrichmentSource, float]] = {
    "descriptive_literal": {
        EnrichmentSource.TEXT: 0.9,
        EnrichmentSource.VISUAL: 1.0,
    },
    "contextual_semantic": {
        EnrichmentSource.TEXT: 1.0,
        EnrichmentSource.VISUAL: 0.85,
    },
    "compatibility": {
        EnrichmentSource.TEXT: 1.1,
        EnrichmentSource.VISUAL: 0.8,
    },
}

_DEFAULT_WEIGHTS: dict[EnrichmentSource, float] = {
    EnrichmentSource.TEXT: 1.0,
    EnrichmentSource.VISUAL: 1.0,
}


def _source_weight(class_name: str, source: EnrichmentSource) -> float:
    return CLASS_SOURCE_WEIGHTS.get(class_name, _DEFAULT_WEIGHTS).get(source, 1.0)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _value_key(value: Any) -> Hashable:
    """Return a hashable key identifying a value for equality comparison."""
    if isinstance(value, list):
        return ("__list__", tuple(value))
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted(value.items())))
    return value


def _noisy_or(a: float, b: float) -> float:
    """Combine two independent confidences into an agreement-boosted score."""
    return round(min(1.0, 1.0 - (1.0 - a) * (1.0 - b)), 6)


def _ensure_contributing(val: EnrichedValue) -> EnrichedValue:
    """Populate contributing_sources on outgoing values if the caller hasn't.

    Merged values default to [text, visual]; single-source values default to
    [source]. Respects any explicit value already set by the caller.
    """
    if val.contributing_sources:
        return val
    if val.source == EnrichmentSource.MERGED:
        return val.model_copy(
            update={
                "contributing_sources": [
                    EnrichmentSource.TEXT,
                    EnrichmentSource.VISUAL,
                ]
            }
        )
    return val.model_copy(update={"contributing_sources": [val.source]})


def _merge_agreement(
    text_val: EnrichedValue,
    visual_val: EnrichedValue,
) -> EnrichedValue:
    return EnrichedValue(
        value=text_val.value,
        confidence=_noisy_or(text_val.confidence, visual_val.confidence),
        evidence=list(text_val.evidence) + list(visual_val.evidence),
        reasoning_mode=text_val.reasoning_mode or visual_val.reasoning_mode,
        source=EnrichmentSource.MERGED,
        contributing_sources=[EnrichmentSource.TEXT, EnrichmentSource.VISUAL],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def merge_enrichment_outputs(
    attribute: AttributeDefinition,
    text_output: EnrichmentOutput,
    visual_output: EnrichmentOutput,
    *,
    confidence_gap_threshold: float = 0.15,
) -> EnrichmentOutput:
    if text_output.source != EnrichmentSource.TEXT:
        raise ValueError("text_output must have source=EnrichmentSource.TEXT")
    if visual_output.source != EnrichmentSource.VISUAL:
        raise ValueError("visual_output must have source=EnrichmentSource.VISUAL")

    warnings: set[str] = set(text_output.warnings) | set(visual_output.warnings)
    # Merge structured proposed_values by value. When both sides propose the
    # same value, take the higher confidence and union the evidence.
    _merged_proposed: dict[str, ProposedValue] = {}
    for pv in (
        list(text_output.proposed_values or [])
        + list(visual_output.proposed_values or [])
    ):
        existing = _merged_proposed.get(pv.value)
        if existing is None:
            _merged_proposed[pv.value] = ProposedValue(
                value=pv.value,
                confidence=pv.confidence,
                evidence=list(pv.evidence),
            )
        else:
            _merged_proposed[pv.value] = ProposedValue(
                value=pv.value,
                confidence=max(existing.confidence, pv.confidence),
                evidence=list({*existing.evidence, *pv.evidence}),
            )
    proposed: list[ProposedValue] = sorted(
        _merged_proposed.values(), key=lambda p: p.value
    )

    def _build(
        values: list[EnrichedValue],
        extra: set[str] | None = None,
    ) -> EnrichmentOutput:
        return EnrichmentOutput(
            attribute_name=attribute.name,
            attribute_class=attribute.class_name,
            values=[_ensure_contributing(v) for v in values],
            proposed_values=proposed,
            warnings=sorted(warnings | (extra or set())),
            source=EnrichmentSource.MERGED,
        )

    text_by_key: dict[Hashable, EnrichedValue] = {
        _value_key(v.value): v for v in text_output.values
    }
    visual_by_key: dict[Hashable, EnrichedValue] = {
        _value_key(v.value): v for v in visual_output.values
    }

    # Single-source passthrough — no weighting applied.
    if not text_by_key and not visual_by_key:
        return _build([])
    if not text_by_key:
        return _build(list(visual_output.values))
    if not visual_by_key:
        return _build(list(text_output.values))

    common = set(text_by_key) & set(visual_by_key)
    merged_values: list[EnrichedValue] = [
        _merge_agreement(text_by_key[k], visual_by_key[k]) for k in common
    ]

    if attribute.value_mode in ("single", "boolean"):
        # Agreement — no weighting applied.
        if common:
            return _build(merged_values)

        # Conflict — apply class-aware source priors.
        best_text = max(text_by_key.values(), key=lambda v: v.confidence)
        best_visual = max(visual_by_key.values(), key=lambda v: v.confidence)

        text_weight = _source_weight(attribute.class_name, EnrichmentSource.TEXT)
        visual_weight = _source_weight(attribute.class_name, EnrichmentSource.VISUAL)

        text_effective = best_text.confidence * text_weight
        visual_effective = best_visual.confidence * visual_weight

        gap = abs(text_effective - visual_effective)
        if gap >= confidence_gap_threshold:
            winner = (
                best_text if text_effective >= visual_effective else best_visual
            )
            return _build([winner])
        return _build([], {"cross_source_conflict"})

    # Multi-value: union disjoint, keep agreements merged.
    for k in set(text_by_key) - common:
        merged_values.append(text_by_key[k])
    for k in set(visual_by_key) - common:
        merged_values.append(visual_by_key[k])
    return _build(merged_values)
