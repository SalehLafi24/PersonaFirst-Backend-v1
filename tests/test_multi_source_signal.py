"""Tests for multi-source signal strength scoring.

These tests are pure (no DB) — they exercise the compute functions in
app.services.multi_source_signal_service directly on synthetic
EnrichmentOutput objects.
"""

import pytest

from app.schemas.attribute_enrichment import (
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
)
from app.services.multi_source_signal_service import (
    aggregate_conflict_penalty,
    compatibility_certainty_from_scores,
    compute_match_confidence,
    compute_product_signal_strength,
    extend_customer_signal_with_source_quality,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _merged_value(value, confidence, *, evidence=None) -> EnrichedValue:
    return EnrichedValue(
        value=value,
        confidence=confidence,
        evidence=evidence or ["stub evidence"],
        source=EnrichmentSource.MERGED,
        contributing_sources=[EnrichmentSource.TEXT, EnrichmentSource.VISUAL],
    )


def _text_value(value, confidence, *, evidence=None) -> EnrichedValue:
    return EnrichedValue(
        value=value,
        confidence=confidence,
        evidence=evidence or ["stub"],
        source=EnrichmentSource.TEXT,
        contributing_sources=[EnrichmentSource.TEXT],
    )


def _output(
    attribute_name: str,
    values: list[EnrichedValue],
    *,
    warnings: list[str] | None = None,
    attribute_class: str = "descriptive_literal",
    source: EnrichmentSource = EnrichmentSource.MERGED,
) -> EnrichmentOutput:
    return EnrichmentOutput(
        attribute_name=attribute_name,
        attribute_class=attribute_class,
        values=values,
        warnings=warnings or [],
        source=source,
    )


# ---------------------------------------------------------------------------
# Product signal strength
# ---------------------------------------------------------------------------


class TestProductSignalStrength:
    def test_strong_agreement_across_text_and_visual_produces_high_signal(self):
        outputs = {
            "color": _output("color", [_merged_value("red", 0.96)]),
            "fit": _output("fit", [_merged_value("slim", 0.94)]),
            "occasion": _output(
                "occasion",
                [_merged_value("formal", 0.93)],
                attribute_class="contextual_semantic",
            ),
        }
        result = compute_product_signal_strength(outputs)
        assert result.coverage == pytest.approx(1.0)
        assert result.agreement == pytest.approx(1.0)
        assert result.avg_confidence > 0.9
        assert result.conflict_penalty == pytest.approx(0.0)
        assert result.strength > 0.9

    def test_cross_source_conflict_lowers_signal_strength(self):
        strong = {
            "color": _output("color", [_merged_value("red", 0.96)]),
            "fit": _output("fit", [_merged_value("slim", 0.94)]),
            "occasion": _output("occasion", [_merged_value("formal", 0.93)]),
        }
        with_conflict = {
            "color": _output("color", [_merged_value("red", 0.96)]),
            "fit": _output("fit", [_merged_value("slim", 0.94)]),
            "occasion": _output(
                "occasion",
                [],
                warnings=["cross_source_conflict"],
            ),
        }
        strong_result = compute_product_signal_strength(strong)
        conflict_result = compute_product_signal_strength(with_conflict)
        assert conflict_result.strength < strong_result.strength
        assert conflict_result.conflict_penalty > 0.0

    def test_sparse_product_data_lowers_signal(self):
        sparse = {
            "color": _output("color", [_text_value("red", 0.9)]),
            "fit": _output("fit", [], warnings=["no_supported_value_found"]),
            "occasion": _output("occasion", []),
        }
        result = compute_product_signal_strength(sparse)
        assert result.coverage == pytest.approx(1 / 3, abs=1e-6)
        assert result.agreement == pytest.approx(0.0)
        assert result.strength < 0.5

    def test_expected_attributes_drops_coverage_when_outputs_missing(self):
        outputs = {
            "color": _output("color", [_merged_value("red", 0.96)]),
        }
        # Same output, but the caller expected three attributes to be
        # enriched. Coverage should drop and strength should fall below the
        # full-coverage reference.
        sparse = compute_product_signal_strength(outputs, expected_attributes=3)
        full = compute_product_signal_strength(outputs, expected_attributes=1)
        assert sparse.coverage == pytest.approx(1 / 3, abs=1e-6)
        assert full.coverage == pytest.approx(1.0)
        assert sparse.strength < full.strength

    def test_single_source_reduces_agreement(self):
        outputs = {
            "color": _output("color", [_text_value("red", 0.9)]),
            "fit": _output("fit", [_text_value("slim", 0.88)]),
        }
        result = compute_product_signal_strength(outputs)
        assert result.coverage == pytest.approx(1.0)
        assert result.agreement == pytest.approx(0.0)
        assert result.strength < 0.8

    def test_empty_outputs_returns_zero(self):
        result = compute_product_signal_strength({})
        assert result.strength == pytest.approx(0.0)
        assert result.coverage == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Conflict aggregation
# ---------------------------------------------------------------------------


class TestConflictAggregation:
    def test_cross_source_conflict_dominates(self):
        outputs = [
            _output("a", [], warnings=["cross_source_conflict"]),
            _output("b", [_merged_value("x", 0.95)]),
        ]
        penalty = aggregate_conflict_penalty(outputs)
        assert penalty == pytest.approx(0.5)

    def test_weak_confidence_is_penalised(self):
        outputs = [_output("a", [_text_value("x", 0.3)])]
        assert aggregate_conflict_penalty(outputs) == pytest.approx(0.4)

    def test_no_indicators_yields_zero(self):
        outputs = [_output("a", [_merged_value("x", 0.95)])]
        assert aggregate_conflict_penalty(outputs) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Customer signal extension
# ---------------------------------------------------------------------------


class TestCustomerSignalExtension:
    def test_stronger_customer_evidence_increases_signal(self):
        base = 0.50
        weak_products = [0.30, 0.40, 0.20]
        strong_products = [0.95, 0.98, 0.90]
        weak_ext = extend_customer_signal_with_source_quality(base, weak_products)
        strong_ext = extend_customer_signal_with_source_quality(base, strong_products)
        assert strong_ext.extended > weak_ext.extended
        assert strong_ext.source_quality > weak_ext.source_quality
        assert weak_ext.base == pytest.approx(strong_ext.base)  # base unchanged

    def test_empty_contributing_signals_defaults_to_base(self):
        base = 0.60
        result = extend_customer_signal_with_source_quality(base, [])
        assert result.source_quality == pytest.approx(0.0)
        # base * (1 - default_weight 0.2) + 0 = 0.48
        assert result.extended == pytest.approx(0.48)

    def test_weight_tunability(self):
        base = 0.4
        products = [1.0, 1.0]
        result = extend_customer_signal_with_source_quality(
            base, products, source_quality_weight=0.5
        )
        # 0.5 * 0.4 + 0.5 * 1.0 = 0.7
        assert result.extended == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Match confidence
# ---------------------------------------------------------------------------


class TestMatchConfidence:
    def test_high_certainty_match_yields_high_match_confidence(self):
        result = compute_match_confidence(
            customer_signal_strength=0.9,
            product_signal_strength=0.9,
            attribute_match_strength=0.9,
            compatibility_certainty=0.9,
            conflict_penalty=0.0,
        )
        assert result.match_confidence == pytest.approx(0.9)

    def test_weak_conflicted_match_yields_low_match_confidence(self):
        result = compute_match_confidence(
            customer_signal_strength=0.3,
            product_signal_strength=0.3,
            attribute_match_strength=0.2,
            compatibility_certainty=0.1,
            conflict_penalty=0.5,
        )
        assert result.match_confidence < 0.15

    def test_higher_certainty_beats_conflicted(self):
        high = compute_match_confidence(
            customer_signal_strength=0.85,
            product_signal_strength=0.95,
            attribute_match_strength=0.80,
            compatibility_certainty=0.90,
            conflict_penalty=0.0,
        )
        low = compute_match_confidence(
            customer_signal_strength=0.45,
            product_signal_strength=0.40,
            attribute_match_strength=0.30,
            compatibility_certainty=0.20,
            conflict_penalty=0.6,
        )
        assert high.match_confidence > low.match_confidence
        assert high.match_confidence > 0.7
        assert low.match_confidence < 0.2

    def test_conflict_penalty_reduces_score(self):
        base = compute_match_confidence(
            customer_signal_strength=0.8,
            product_signal_strength=0.8,
            attribute_match_strength=0.8,
            compatibility_certainty=0.8,
            conflict_penalty=0.0,
        )
        penalised = compute_match_confidence(
            customer_signal_strength=0.8,
            product_signal_strength=0.8,
            attribute_match_strength=0.8,
            compatibility_certainty=0.8,
            conflict_penalty=0.5,
        )
        assert base.match_confidence > penalised.match_confidence
        assert base.match_confidence - penalised.match_confidence == pytest.approx(
            0.15
        )

    def test_inputs_are_clamped(self):
        result = compute_match_confidence(
            customer_signal_strength=1.5,
            product_signal_strength=-0.2,
            attribute_match_strength=2.0,
            compatibility_certainty=0.5,
            conflict_penalty=-0.3,
        )
        assert result.customer_signal == pytest.approx(1.0)
        assert result.product_signal == pytest.approx(0.0)
        assert result.attribute_match == pytest.approx(1.0)
        assert result.conflict_penalty == pytest.approx(0.0)
        assert 0.0 <= result.match_confidence <= 1.0


# ---------------------------------------------------------------------------
# Compatibility certainty helper
# ---------------------------------------------------------------------------


class TestCompatibilityCertainty:
    def test_all_positive(self):
        assert compatibility_certainty_from_scores(1.2, 0.0) == pytest.approx(1.0)

    def test_all_negative(self):
        assert compatibility_certainty_from_scores(0.0, -0.6) == pytest.approx(0.0)

    def test_mixed(self):
        # positive 0.6, negative magnitude 0.2 → 0.6 / 0.8 = 0.75
        assert compatibility_certainty_from_scores(0.6, -0.2) == pytest.approx(0.75)

    def test_no_evidence_yields_zero(self):
        assert compatibility_certainty_from_scores(0.0, 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Schema integration
# ---------------------------------------------------------------------------


class TestRecommendationReadSchemaIntegration:
    def test_new_fields_default_to_none(self):
        from app.schemas.recommendation import RecommendationRead

        rec = RecommendationRead(
            product_id="p1",
            sku="sku-1",
            name="Test",
            group_id=None,
            matched_attributes=[],
            direct_score=0.5,
            relationship_score=0.0,
            popularity_score=0.0,
            behavioral_score=0.0,
            recommendation_score=0.5,
            recommendation_source="direct",
            explanation="",
            relationship_matches=[],
            behavioral_matches=[],
        )
        assert rec.product_signal_strength is None
        assert rec.customer_signal_strength is None
        assert rec.match_confidence is None
        assert rec.signal_summary is None

    def test_new_fields_can_be_populated(self):
        from app.schemas.recommendation import RecommendationRead, SignalSummary

        rec = RecommendationRead(
            product_id="p1",
            sku="sku-1",
            name="Test",
            group_id=None,
            matched_attributes=[],
            direct_score=0.5,
            relationship_score=0.0,
            popularity_score=0.0,
            behavioral_score=0.0,
            recommendation_score=0.5,
            recommendation_source="direct",
            explanation="",
            relationship_matches=[],
            behavioral_matches=[],
            product_signal_strength=0.92,
            customer_signal_strength=0.71,
            match_confidence=0.84,
            signal_summary=SignalSummary(
                matched_attribute_count=3,
                compatibility_positive=0.9,
                compatibility_negative=0.0,
                conflict_indicators=[],
            ),
        )
        assert rec.product_signal_strength == pytest.approx(0.92)
        assert rec.customer_signal_strength == pytest.approx(0.71)
        assert rec.match_confidence == pytest.approx(0.84)
        assert rec.signal_summary is not None
        assert rec.signal_summary.matched_attribute_count == 3


# ---------------------------------------------------------------------------
# Runtime integration — apply_multi_source_signals
# ---------------------------------------------------------------------------


def _make_rec(
    product_id: str,
    *,
    recommendation_score: float,
    compatibility_positive: float = 0.0,
    compatibility_negative: float = 0.0,
    matched_attributes=None,
):
    from app.schemas.recommendation import MatchedAttribute, RecommendationRead

    return RecommendationRead(
        product_id=product_id,
        sku=f"sku-{product_id}",
        name=f"Product {product_id}",
        group_id=None,
        matched_attributes=matched_attributes or [
            MatchedAttribute(
                attribute_id="attr_a",
                attribute_value="x",
                score=0.9,
                weight=0.5,
                targeting_mode="categorical_affinity",
            )
        ],
        direct_score=recommendation_score,
        relationship_score=0.0,
        popularity_score=0.0,
        behavioral_score=0.0,
        affinity_contribution=recommendation_score,
        compatibility_positive_contribution=compatibility_positive,
        compatibility_negative_contribution=compatibility_negative,
        recommendation_score=recommendation_score,
        recommendation_source="direct",
        explanation="",
        relationship_matches=[],
        behavioral_matches=[],
    )


class TestApplyMultiSourceSignalsFieldPopulation:
    def test_fields_populate_when_full_data_is_available(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        rec = _make_rec(
            "p1",
            recommendation_score=1.2,
            compatibility_positive=0.9,
            compatibility_negative=0.0,
        )
        outputs = {
            "p1": {
                "color": _output("color", [_merged_value("red", 0.95)]),
                "fit": _output("fit", [_merged_value("slim", 0.93)]),
            }
        }
        result = apply_multi_source_signals(
            [rec],
            customer_signal_strength=0.72,
            product_enrichment_outputs=outputs,
        )
        assert len(result) == 1
        r = result[0]
        assert r.customer_signal_strength == pytest.approx(0.72)
        assert r.product_signal_strength is not None
        assert r.product_signal_strength > 0.9
        assert r.match_confidence is not None
        assert r.match_confidence > 0.6
        assert r.signal_summary is not None
        assert r.signal_summary.matched_attribute_count == 1
        assert r.signal_summary.compatibility_positive == pytest.approx(0.9)
        assert r.signal_summary.conflict_indicators == []

    def test_missing_enrichment_leaves_product_and_match_fields_none(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        rec = _make_rec("p1", recommendation_score=1.0)
        result = apply_multi_source_signals(
            [rec],
            customer_signal_strength=0.5,
            product_enrichment_outputs=None,
        )
        r = result[0]
        assert r.customer_signal_strength == pytest.approx(0.5)
        assert r.product_signal_strength is None
        assert r.match_confidence is None
        # Signal summary is still populated (from existing fields).
        assert r.signal_summary is not None

    def test_no_inputs_leaves_all_signal_fields_none(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        rec = _make_rec("p1", recommendation_score=1.0)
        result = apply_multi_source_signals([rec])
        r = result[0]
        assert r.customer_signal_strength is None
        assert r.product_signal_strength is None
        assert r.match_confidence is None
        assert r.recommendation_score == pytest.approx(1.0)  # untouched

    def test_conflict_indicators_are_collected(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        rec = _make_rec("p1", recommendation_score=0.8)
        outputs = {
            "p1": {
                "color": _output("color", [_merged_value("red", 0.95)]),
                "fit": _output("fit", [], warnings=["cross_source_conflict"]),
                "occasion": _output(
                    "occasion", [], warnings=["no_supported_value_found"]
                ),
            }
        }
        result = apply_multi_source_signals(
            [rec],
            customer_signal_strength=0.6,
            product_enrichment_outputs=outputs,
        )
        r = result[0]
        assert "cross_source_conflict" in r.signal_summary.conflict_indicators
        assert "no_supported_value_found" in r.signal_summary.conflict_indicators


class TestApplyMultiSourceSignalsOrdering:
    def test_primary_sort_remains_recommendation_score(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        high = _make_rec("p_high", recommendation_score=1.5)
        low = _make_rec("p_low", recommendation_score=0.5)
        # Feed in reverse order so a naive pass-through would fail.
        outputs = {
            "p_high": {"color": _output("color", [_merged_value("red", 0.95)])},
            "p_low": {"color": _output("color", [_merged_value("blue", 0.95)])},
        }
        result = apply_multi_source_signals(
            [low, high],  # primary order on input is wrong...
            customer_signal_strength=0.8,
            product_enrichment_outputs=outputs,
            tiebreak_by_match_confidence=True,
        )
        # Re-sort must restore order by recommendation_score DESC.
        assert [r.product_id for r in result] == ["p_high", "p_low"]

    def test_match_confidence_only_affects_ties(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        # Two recs with identical recommendation_score but different
        # enrichment quality → match_confidence differs → tie-break picks
        # the higher one first.
        tied_a = _make_rec(
            "p_tied_weak",
            recommendation_score=1.0,
            compatibility_positive=0.4,
            compatibility_negative=0.3,
        )
        tied_b = _make_rec(
            "p_tied_strong",
            recommendation_score=1.0,
            compatibility_positive=0.9,
            compatibility_negative=0.0,
        )
        # Non-tied rec at a different score should NOT be reordered.
        other = _make_rec("p_other", recommendation_score=0.7)

        outputs = {
            "p_tied_weak": {
                "a": _output("a", [_merged_value("x", 0.60)]),
                "b": _output("b", [], warnings=["cross_source_conflict"]),
            },
            "p_tied_strong": {
                "a": _output("a", [_merged_value("x", 0.96)]),
                "b": _output("b", [_merged_value("y", 0.95)]),
            },
            "p_other": {"a": _output("a", [_merged_value("x", 0.90)])},
        }
        result = apply_multi_source_signals(
            [tied_a, tied_b, other],
            customer_signal_strength=0.8,
            product_enrichment_outputs=outputs,
            tiebreak_by_match_confidence=True,
        )
        ids = [r.product_id for r in result]
        # The tied pair is reordered by match_confidence, with the strong
        # one first. The non-tied `p_other` stays below the tied pair.
        assert ids.index("p_tied_strong") < ids.index("p_tied_weak")
        assert ids[-1] == "p_other"
        # Primary score order is preserved.
        scores = [r.recommendation_score for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_tiebreak_disabled_preserves_input_order(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        tied_a = _make_rec("a", recommendation_score=1.0)
        tied_b = _make_rec("b", recommendation_score=1.0)
        outputs = {
            "a": {"x": _output("x", [_merged_value("1", 0.60)])},
            "b": {"x": _output("x", [_merged_value("1", 0.99)])},
        }
        result = apply_multi_source_signals(
            [tied_a, tied_b],
            customer_signal_strength=0.8,
            product_enrichment_outputs=outputs,
            tiebreak_by_match_confidence=False,
        )
        # With tiebreak disabled, input order is preserved even though b
        # has stronger signals.
        assert [r.product_id for r in result] == ["a", "b"]

    def test_none_match_confidence_does_not_break_sort(self):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        # Some recs have match_confidence, others don't. Sort must not
        # crash and the items with match_confidence should lead their
        # equal-score group.
        with_data = _make_rec("with_data", recommendation_score=1.0)
        no_data = _make_rec("no_data", recommendation_score=1.0)
        outputs = {
            "with_data": {"x": _output("x", [_merged_value("1", 0.95)])},
            # "no_data" intentionally absent
        }
        result = apply_multi_source_signals(
            [no_data, with_data],
            customer_signal_strength=0.8,
            product_enrichment_outputs=outputs,
            tiebreak_by_match_confidence=True,
        )
        ids = [r.product_id for r in result]
        assert ids.index("with_data") < ids.index("no_data")
        # Legacy field untouched.
        for r in result:
            assert r.recommendation_score == pytest.approx(1.0)


class TestGetRecommendationsWiring:
    """Sanity check that get_recommendations accepts the new params and
    passes them through. Does NOT exercise the full DB-backed pipeline —
    that's covered by the direct apply_multi_source_signals tests above.
    """

    def test_new_params_are_accepted(self):
        import inspect

        from app.services.recommendation_service import get_recommendations

        sig = inspect.signature(get_recommendations)
        assert "customer_signal_strength" in sig.parameters
        assert "product_enrichment_outputs" in sig.parameters
        assert "tiebreak_by_match_confidence" in sig.parameters
        # Defaults must be backward compatible.
        assert sig.parameters["customer_signal_strength"].default is None
        assert sig.parameters["product_enrichment_outputs"].default is None
        assert sig.parameters["tiebreak_by_match_confidence"].default is False
