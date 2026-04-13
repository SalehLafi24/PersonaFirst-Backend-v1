"""Tests for the attribute_merge_service.

Generic merge behavior across text and visual enrichment sources.
"""

import pytest

from app.schemas.attribute_enrichment import (
    AttributeBehavior,
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
)
from app.services.attribute_merge_service import merge_enrichment_outputs


def _make_attr(
    *,
    value_mode: str = "single",
    name: str = "color",
    class_name: str = "descriptive_literal",
    allowed_values: list[str] | None = None,
) -> AttributeDefinition:
    return AttributeDefinition(
        name=name,
        object_type="product",
        class_name=class_name,
        value_mode=value_mode,
        allowed_values=allowed_values or ["red", "blue", "green"],
        description=f"{name} merge-test attribute.",
        evidence_sources=["name", "image"],
        behavior=AttributeBehavior(multi_value_allowed=(value_mode == "multi")),
    )


def _text_output(
    values: list[dict],
    *,
    name: str = "color",
    class_name: str = "descriptive_literal",
    warnings: list[str] | None = None,
) -> EnrichmentOutput:
    return EnrichmentOutput(
        attribute_name=name,
        attribute_class=class_name,
        values=[EnrichedValue(source=EnrichmentSource.TEXT, **v) for v in values],
        warnings=warnings or [],
        source=EnrichmentSource.TEXT,
    )


def _visual_output(
    values: list[dict],
    *,
    name: str = "color",
    class_name: str = "descriptive_literal",
    warnings: list[str] | None = None,
) -> EnrichmentOutput:
    return EnrichmentOutput(
        attribute_name=name,
        attribute_class=class_name,
        values=[EnrichedValue(source=EnrichmentSource.VISUAL, **v) for v in values],
        warnings=warnings or [],
        source=EnrichmentSource.VISUAL,
    )


class TestSingleSourcePassthrough:
    def test_visual_empty_passes_text_through(self):
        attr = _make_attr()
        text = _text_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["description: red dress"]}]
        )
        visual = _visual_output([])
        result = merge_enrichment_outputs(attr, text, visual)
        assert result.source == EnrichmentSource.MERGED
        assert len(result.values) == 1
        assert result.values[0].value == "red"
        assert result.values[0].source == EnrichmentSource.TEXT

    def test_text_empty_passes_visual_through(self):
        attr = _make_attr()
        text = _text_output([])
        visual = _visual_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["visible red fabric"]}]
        )
        result = merge_enrichment_outputs(attr, text, visual)
        assert len(result.values) == 1
        assert result.values[0].source == EnrichmentSource.VISUAL

    def test_both_empty_yields_empty(self):
        attr = _make_attr()
        result = merge_enrichment_outputs(attr, _text_output([]), _visual_output([]))
        assert result.values == []
        assert result.source == EnrichmentSource.MERGED


class TestAgreement:
    def test_same_value_boosts_confidence_and_combines_evidence(self):
        attr = _make_attr()
        text = _text_output(
            [{"value": "red", "confidence": 0.8, "evidence": ["name: red dress"]}]
        )
        visual = _visual_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["visible red fabric"]}]
        )
        result = merge_enrichment_outputs(attr, text, visual)
        assert len(result.values) == 1
        merged = result.values[0]
        assert merged.source == EnrichmentSource.MERGED
        assert merged.confidence > 0.9
        assert merged.confidence <= 1.0
        assert "name: red dress" in merged.evidence
        assert "visible red fabric" in merged.evidence

    def test_agreement_preserves_contributing_sources(self):
        attr = _make_attr()
        text = _text_output(
            [{"value": "red", "confidence": 0.8, "evidence": ["name: red dress"]}]
        )
        visual = _visual_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["visible red fabric"]}]
        )
        result = merge_enrichment_outputs(attr, text, visual)
        merged = result.values[0]
        assert merged.contributing_sources == [
            EnrichmentSource.TEXT,
            EnrichmentSource.VISUAL,
        ]

    def test_agreement_does_not_apply_source_weights(self):
        # compatibility has asymmetric weights (text 1.1, visual 0.8) but
        # agreement must ignore them — noisy-OR of raw confidences only.
        attr = _make_attr(class_name="compatibility", name="support_level")
        text = _text_output(
            [{"value": "medium", "confidence": 0.80, "evidence": ["name: medium"]}],
            class_name="compatibility",
        )
        visual = _visual_output(
            [{"value": "medium", "confidence": 0.80, "evidence": ["visible mid support"]}],
            class_name="compatibility",
        )
        result = merge_enrichment_outputs(attr, text, visual)
        merged = result.values[0]
        expected = round(1.0 - (1.0 - 0.8) * (1.0 - 0.8), 6)  # 0.96
        assert merged.confidence == pytest.approx(expected)


class TestConflict:
    def test_large_confidence_gap_prefers_higher_source(self):
        # descriptive_literal: text=0.9, visual=1.0
        # effective: text 0.95*0.9 = 0.855, visual 0.6*1.0 = 0.6, gap 0.255 ≥ 0.15
        attr = _make_attr()
        text = _text_output(
            [{"value": "red", "confidence": 0.95, "evidence": ["name: red dress"]}]
        )
        visual = _visual_output(
            [
                {
                    "value": "blue",
                    "confidence": 0.6,
                    "evidence": ["bluish tint under low light"],
                }
            ]
        )
        result = merge_enrichment_outputs(
            attr, text, visual, confidence_gap_threshold=0.15
        )
        assert len(result.values) == 1
        winner = result.values[0]
        assert winner.value == "red"
        assert winner.source == EnrichmentSource.TEXT
        assert winner.contributing_sources == [EnrichmentSource.TEXT]
        assert "cross_source_conflict" not in result.warnings

    def test_small_confidence_gap_returns_empty_with_warning(self):
        # descriptive_literal: effective text 0.85*0.9=0.765, visual 0.82*1.0=0.82
        # gap 0.055 < 0.15 → conflict
        attr = _make_attr()
        text = _text_output(
            [{"value": "red", "confidence": 0.85, "evidence": ["name: scarlet"]}]
        )
        visual = _visual_output(
            [{"value": "blue", "confidence": 0.82, "evidence": ["visible blue hue"]}]
        )
        result = merge_enrichment_outputs(
            attr, text, visual, confidence_gap_threshold=0.15
        )
        assert result.values == []
        assert "cross_source_conflict" in result.warnings


class TestClassAwareConflictPriors:
    """Conflict resolution under class-specific source weighting."""

    def test_descriptive_literal_visual_wins_despite_lower_raw_text_gap(self):
        # descriptive_literal: text=0.9, visual=1.0
        # raw: text 0.80 vs visual 0.90 (raw gap 0.10 < threshold)
        # effective: text 0.80*0.9=0.72, visual 0.90*1.0=0.90, gap 0.18 ≥ 0.15
        attr = _make_attr(class_name="descriptive_literal")
        text = _text_output(
            [{"value": "red", "confidence": 0.80, "evidence": ["name: red dress"]}]
        )
        visual = _visual_output(
            [{"value": "blue", "confidence": 0.90, "evidence": ["visible blue fabric"]}]
        )
        result = merge_enrichment_outputs(
            attr, text, visual, confidence_gap_threshold=0.15
        )
        assert len(result.values) == 1
        winner = result.values[0]
        assert winner.value == "blue"
        assert winner.source == EnrichmentSource.VISUAL
        assert winner.contributing_sources == [EnrichmentSource.VISUAL]
        assert "cross_source_conflict" not in result.warnings

    def test_contextual_semantic_text_wins_over_similar_raw_visual(self):
        # contextual_semantic: text=1.0, visual=0.85
        # raw: text 0.85 vs visual 0.82 (raw gap 0.03 would conflict)
        # effective: text 0.85*1.0=0.85, visual 0.82*0.85=0.697, gap 0.153 ≥ 0.15
        attr = _make_attr(
            class_name="contextual_semantic",
            name="occasion",
            allowed_values=["casual", "formal", "athletic"],
        )
        text = _text_output(
            [
                {
                    "value": "formal",
                    "confidence": 0.85,
                    "evidence": ["name: 'Formal Evening Dress'"],
                }
            ],
            name="occasion",
            class_name="contextual_semantic",
        )
        visual = _visual_output(
            [
                {
                    "value": "casual",
                    "confidence": 0.82,
                    "evidence": ["bright outdoor café setting"],
                }
            ],
            name="occasion",
            class_name="contextual_semantic",
        )
        result = merge_enrichment_outputs(
            attr, text, visual, confidence_gap_threshold=0.15
        )
        assert len(result.values) == 1
        winner = result.values[0]
        assert winner.value == "formal"
        assert winner.source == EnrichmentSource.TEXT
        assert winner.contributing_sources == [EnrichmentSource.TEXT]
        assert "cross_source_conflict" not in result.warnings

    def test_compatibility_text_wins_despite_higher_raw_visual(self):
        # compatibility: text=1.1, visual=0.8
        # raw: text 0.80 vs visual 0.85 (visual has higher RAW confidence)
        # effective: text 0.80*1.1=0.88, visual 0.85*0.8=0.68, gap 0.20 ≥ 0.15
        attr = _make_attr(
            class_name="compatibility",
            name="support_level",
            allowed_values=["low", "medium", "high"],
        )
        text = _text_output(
            [
                {
                    "value": "medium",
                    "confidence": 0.80,
                    "evidence": ["description: 'moderate support'"],
                }
            ],
            name="support_level",
            class_name="compatibility",
        )
        visual = _visual_output(
            [
                {
                    "value": "low",
                    "confidence": 0.85,
                    "evidence": ["thin fabric and minimal visible reinforcement"],
                }
            ],
            name="support_level",
            class_name="compatibility",
        )
        result = merge_enrichment_outputs(
            attr, text, visual, confidence_gap_threshold=0.15
        )
        assert len(result.values) == 1
        winner = result.values[0]
        assert winner.value == "medium"
        assert winner.source == EnrichmentSource.TEXT
        assert winner.contributing_sources == [EnrichmentSource.TEXT]
        assert "cross_source_conflict" not in result.warnings

    def test_compatibility_small_effective_gap_still_yields_conflict(self):
        # compatibility: text=1.1, visual=0.8
        # raw: text 0.75 vs visual 0.90
        # effective: text 0.825, visual 0.72, gap 0.105 < 0.15 → conflict
        attr = _make_attr(
            class_name="compatibility",
            name="support_level",
            allowed_values=["low", "medium", "high"],
        )
        text = _text_output(
            [
                {
                    "value": "medium",
                    "confidence": 0.75,
                    "evidence": ["description: 'moderate support'"],
                }
            ],
            name="support_level",
            class_name="compatibility",
        )
        visual = _visual_output(
            [
                {
                    "value": "low",
                    "confidence": 0.90,
                    "evidence": ["visible thin strap construction"],
                }
            ],
            name="support_level",
            class_name="compatibility",
        )
        result = merge_enrichment_outputs(
            attr, text, visual, confidence_gap_threshold=0.15
        )
        assert result.values == []
        assert "cross_source_conflict" in result.warnings

    def test_weights_are_not_applied_to_single_source_passthrough(self):
        # compatibility has heavy weights but passthrough must not re-rank.
        attr = _make_attr(class_name="compatibility", name="support_level")
        text = _text_output([], class_name="compatibility")
        visual = _visual_output(
            [{"value": "high", "confidence": 0.6, "evidence": ["visible reinforcement"]}],
            class_name="compatibility",
        )
        result = merge_enrichment_outputs(attr, text, visual)
        assert len(result.values) == 1
        assert result.values[0].value == "high"
        assert result.values[0].source == EnrichmentSource.VISUAL
        assert result.values[0].confidence == pytest.approx(0.6)
        assert result.values[0].contributing_sources == [EnrichmentSource.VISUAL]


class TestMultiValue:
    def test_multi_value_unions_disjoint_values(self):
        attr = _make_attr(value_mode="multi")
        text = _text_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["name: red stripe"]}]
        )
        visual = _visual_output(
            [{"value": "blue", "confidence": 0.88, "evidence": ["visible blue panel"]}]
        )
        result = merge_enrichment_outputs(attr, text, visual)
        by_value = {v.value: v for v in result.values}
        assert set(by_value) == {"red", "blue"}
        assert by_value["red"].source == EnrichmentSource.TEXT
        assert by_value["blue"].source == EnrichmentSource.VISUAL

    def test_multi_value_merges_overlap_and_unions_rest(self):
        attr = _make_attr(value_mode="multi")
        text = _text_output(
            [
                {"value": "red", "confidence": 0.9, "evidence": ["description: red"]},
                {"value": "green", "confidence": 0.85, "evidence": ["description: green"]},
            ]
        )
        visual = _visual_output(
            [
                {"value": "red", "confidence": 0.88, "evidence": ["visible red"]},
                {"value": "blue", "confidence": 0.8, "evidence": ["visible blue"]},
            ]
        )
        result = merge_enrichment_outputs(attr, text, visual)
        by_value = {v.value: v for v in result.values}
        assert set(by_value) == {"red", "green", "blue"}
        assert by_value["red"].source == EnrichmentSource.MERGED
        assert by_value["red"].confidence > 0.9
        assert by_value["green"].source == EnrichmentSource.TEXT
        assert by_value["blue"].source == EnrichmentSource.VISUAL


class TestWarningsAndProposed:
    def test_existing_warnings_are_preserved(self):
        attr = _make_attr()
        text = _text_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["name: red"]}],
            warnings=["multiple_strong_values_detected"],
        )
        visual = _visual_output(
            [{"value": "red", "confidence": 0.9, "evidence": ["visible red"]}],
            warnings=["ambiguous_evidence"],
        )
        result = merge_enrichment_outputs(attr, text, visual)
        assert "multiple_strong_values_detected" in result.warnings
        assert "ambiguous_evidence" in result.warnings


class TestSourceValidation:
    def test_wrong_text_source_raises(self):
        attr = _make_attr()
        mislabeled_text = _visual_output([])
        with pytest.raises(ValueError):
            merge_enrichment_outputs(attr, mislabeled_text, _visual_output([]))

    def test_wrong_visual_source_raises(self):
        attr = _make_attr()
        with pytest.raises(ValueError):
            merge_enrichment_outputs(attr, _text_output([]), _text_output([]))
