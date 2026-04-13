"""Tests for negative compatibility scoring in the recommendation engine.

These tests are deliberately generic — no attribute names are hardcoded into
the production logic. The tests use "type" (a CORE_ATTR) as a positive anchor
that satisfies the meaningfulness gate, and a separate generic compatibility
attribute ("level" / "fit") that the test drives via attribute_targeting_modes
and attribute_behaviors.
"""
from datetime import date

import pytest

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import AttributeBehavior
from app.services.recommendation_service import (
    _COMPATIBILITY_SCORE_MULTIPLIER,
    _ordered_mismatch_severity,
    get_recommendations,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_workspace(db) -> Workspace:
    ws = Workspace(name="neg-compat-test", slug="neg-compat-test")
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _seed_product(db, workspace_id: int, product_id: str, attrs: dict[str, str]) -> Product:
    p = Product(
        workspace_id=workspace_id,
        product_id=product_id,
        sku=product_id.upper(),
        name=product_id.replace("_", " ").title(),
    )
    db.add(p)
    db.flush()
    for attr_id, attr_val in attrs.items():
        db.add(ProductAttribute(
            product_id=p.id,
            attribute_id=attr_id,
            attribute_value=attr_val,
        ))
    db.commit()
    db.refresh(p)
    return p


def _seed_affinity(db, workspace_id: int, customer_id: str,
                   attribute_id: str, attribute_value: str, score: float):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id,
        customer_id=customer_id,
        attribute_id=attribute_id,
        attribute_value=attribute_value,
        score=score,
    ))
    db.commit()


def _by_pid(results) -> dict[str, object]:
    return {r.product_id: r for r in results}


# ---------------------------------------------------------------------------
# _ordered_mismatch_severity — pure helper unit tests
# ---------------------------------------------------------------------------

class TestOrderedMismatchSeverity:
    VALUE_ORDER = ["low", "medium", "high"]

    def test_distance_one_of_two_returns_half(self):
        # product=low, customer=medium → distance 1, max 2 → 0.5
        assert _ordered_mismatch_severity("low", {"medium": 0.8}, self.VALUE_ORDER) == 0.5

    def test_max_distance_returns_one(self):
        # product=high, customer=low → distance 2, max 2 → 1.0
        assert _ordered_mismatch_severity("high", {"low": 0.8}, self.VALUE_ORDER) == 1.0

    def test_zero_distance_returns_zero(self):
        # product=medium, customer=medium → distance 0 → 0.0
        # (this isn't technically a mismatch, but the helper should still
        # return 0 cleanly so it composes correctly with caller logic)
        assert _ordered_mismatch_severity("medium", {"medium": 0.8}, self.VALUE_ORDER) == 0.0

    def test_chooses_closest_customer_value(self):
        # product=low, customer={low, high} → closest is low at distance 0
        result = _ordered_mismatch_severity("low", {"low": 0.8, "high": 0.5}, self.VALUE_ORDER)
        assert result == 0.0

    def test_product_not_in_order_returns_zero(self):
        # Defensive: unknown product values produce no penalty rather than crash
        assert _ordered_mismatch_severity("extreme", {"medium": 0.8}, self.VALUE_ORDER) == 0.0

    def test_no_customer_value_in_order_returns_zero(self):
        assert _ordered_mismatch_severity("low", {"unknown": 0.8}, self.VALUE_ORDER) == 0.0

    def test_single_item_order_returns_zero(self):
        # max_distance would be 0; no meaningful distance to compute
        assert _ordered_mismatch_severity("only", {"only": 0.8}, ["only"]) == 0.0

    def test_empty_order_returns_zero(self):
        assert _ordered_mismatch_severity("anything", {"medium": 0.8}, []) == 0.0


# ---------------------------------------------------------------------------
# Backward compatibility — attribute_behaviors not provided
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """When attribute_behaviors is None or the attribute is missing from the
    map, the engine must behave exactly as it did before this feature."""

    def test_no_attribute_behaviors_means_no_penalty(self, db):
        ws = _seed_workspace(db)
        # Customer prefers fit=slim as a compatibility signal
        _seed_affinity(db, ws.id, "cust1", "type", "tee", 0.8)
        _seed_affinity(db, ws.id, "cust1", "fit", "slim", 0.8)

        # Product has fit=loose — would be a mismatch IF negative scoring were on
        _seed_product(db, ws.id, "p_loose", {"type": "tee", "fit": "loose"})

        results, _ = get_recommendations(
            db, workspace_id=ws.id, customer_id="cust1",
            attribute_targeting_modes={"fit": "compatibility_signal"},
            # attribute_behaviors intentionally omitted
        )
        rec = _by_pid(results)["p_loose"]
        assert rec.compatibility_negative_contribution == 0.0
        assert rec.compatibility_positive_contribution == 0.0
        # final score reflects only the positive type=tee anchor: 0.8 * 1.0 = 0.8
        assert rec.recommendation_score == 0.8

    def test_negative_scoring_disabled_means_no_penalty(self, db):
        ws = _seed_workspace(db)
        _seed_affinity(db, ws.id, "cust1", "type", "tee", 0.8)
        _seed_affinity(db, ws.id, "cust1", "fit", "slim", 0.8)
        _seed_product(db, ws.id, "p_loose", {"type": "tee", "fit": "loose"})

        results, _ = get_recommendations(
            db, workspace_id=ws.id, customer_id="cust1",
            attribute_targeting_modes={"fit": "compatibility_signal"},
            attribute_behaviors={"fit": AttributeBehavior(negative_scoring_enabled=False)},
        )
        rec = _by_pid(results)["p_loose"]
        assert rec.compatibility_negative_contribution == 0.0
        assert rec.recommendation_score == 0.8


# ---------------------------------------------------------------------------
# Unordered compatibility — flat penalty for any explicit mismatch
# ---------------------------------------------------------------------------

class TestUnorderedCompatibility:
    """negative_scoring_enabled=True, ordered_values=False (default).
    Any explicit non-matching value gets a flat penalty."""

    def _setup(self, db):
        ws = _seed_workspace(db)
        _seed_affinity(db, ws.id, "cust1", "type", "tee", 0.8)
        _seed_affinity(db, ws.id, "cust1", "fit", "slim", 0.8)
        _seed_product(db, ws.id, "p_match", {"type": "tee", "fit": "slim"})
        _seed_product(db, ws.id, "p_loose", {"type": "tee", "fit": "loose"})
        _seed_product(db, ws.id, "p_missing", {"type": "tee"})
        return ws

    def _call(self, db, ws):
        return get_recommendations(
            db, workspace_id=ws.id, customer_id="cust1",
            attribute_targeting_modes={"fit": "compatibility_signal"},
            attribute_behaviors={
                "fit": AttributeBehavior(negative_scoring_enabled=True),
            },
        )

    def test_matching_product_gets_positive_no_negative(self, db):
        ws = self._setup(db)
        results, _ = self._call(db, ws)
        rec = _by_pid(results)["p_match"]
        # positive = 0.8 * 0.5 * 1.5 = 0.6
        assert rec.compatibility_positive_contribution == pytest.approx(0.6)
        assert rec.compatibility_negative_contribution == 0.0
        # final = direct(0.8) + positive(0.6) - 0 = 1.4
        assert rec.recommendation_score == pytest.approx(1.4)

    def test_mismatching_product_gets_flat_penalty(self, db):
        ws = self._setup(db)
        results, _ = self._call(db, ws)
        rec = _by_pid(results)["p_loose"]
        assert rec.compatibility_positive_contribution == 0.0
        # flat penalty = 0.8 * 0.5 * 1.5 = 0.6
        assert rec.compatibility_negative_contribution == pytest.approx(0.6)
        # final = 0.8 - 0.6 = 0.2
        assert rec.recommendation_score == pytest.approx(0.2)

    def test_missing_data_is_neutral(self, db):
        ws = self._setup(db)
        results, _ = self._call(db, ws)
        rec = _by_pid(results)["p_missing"]
        assert rec.compatibility_positive_contribution == 0.0
        assert rec.compatibility_negative_contribution == 0.0
        # final = 0.8 (only direct anchor)
        assert rec.recommendation_score == pytest.approx(0.8)

    def test_ordering_reflects_penalty(self, db):
        ws = self._setup(db)
        results, _ = self._call(db, ws)
        # Expected order: p_match (1.4) > p_missing (0.8) > p_loose (0.2)
        ids = [r.product_id for r in results]
        assert ids == ["p_match", "p_missing", "p_loose"]


# ---------------------------------------------------------------------------
# Ordered compatibility — distance-based severity
# ---------------------------------------------------------------------------

class TestOrderedCompatibility:
    """negative_scoring_enabled=True, ordered_values=True with value_order.
    Penalty magnitude scales with distance along the ordered axis."""

    VALUE_ORDER = ["low", "medium", "high"]

    def _setup(self, db, customer_signal_value: str = "medium"):
        ws = _seed_workspace(db)
        _seed_affinity(db, ws.id, "cust1", "type", "tee", 0.8)
        _seed_affinity(db, ws.id, "cust1", "level", customer_signal_value, 0.8)
        _seed_product(db, ws.id, "p_low", {"type": "tee", "level": "low"})
        _seed_product(db, ws.id, "p_medium", {"type": "tee", "level": "medium"})
        _seed_product(db, ws.id, "p_high", {"type": "tee", "level": "high"})
        return ws

    def _call(self, db, ws):
        return get_recommendations(
            db, workspace_id=ws.id, customer_id="cust1",
            attribute_targeting_modes={"level": "compatibility_signal"},
            attribute_behaviors={
                "level": AttributeBehavior(
                    negative_scoring_enabled=True,
                    ordered_values=True,
                    value_order=self.VALUE_ORDER,
                ),
            },
        )

    def test_exact_match_gets_positive_no_negative(self, db):
        ws = self._setup(db, customer_signal_value="medium")
        results, _ = self._call(db, ws)
        rec = _by_pid(results)["p_medium"]
        assert rec.compatibility_positive_contribution == pytest.approx(0.6)
        assert rec.compatibility_negative_contribution == 0.0
        assert rec.recommendation_score == pytest.approx(1.4)

    def test_one_step_away_gets_half_severity(self, db):
        ws = self._setup(db, customer_signal_value="medium")
        results, _ = self._call(db, ws)
        # severity = distance 1 / max distance 2 = 0.5
        # penalty = 0.8 * 0.5 * 1.5 * 0.5 = 0.3
        for pid in ("p_low", "p_high"):
            rec = _by_pid(results)[pid]
            assert rec.compatibility_positive_contribution == 0.0
            assert rec.compatibility_negative_contribution == pytest.approx(0.3)
            # final = 0.8 - 0.3 = 0.5
            assert rec.recommendation_score == pytest.approx(0.5)

    def test_max_distance_gets_full_severity(self, db):
        # Customer signal at one end of the scale, product at the other end.
        ws = self._setup(db, customer_signal_value="low")
        results, _ = self._call(db, ws)
        rec_high = _by_pid(results)["p_high"]
        # severity = distance 2 / max distance 2 = 1.0
        # penalty = 0.8 * 0.5 * 1.5 * 1.0 = 0.6
        assert rec_high.compatibility_negative_contribution == pytest.approx(0.6)
        # final = 0.8 - 0.6 = 0.2
        assert rec_high.recommendation_score == pytest.approx(0.2)

    def test_severity_scales_monotonically_with_distance(self, db):
        # With customer=low: p_low=match, p_medium=distance 1, p_high=distance 2
        ws = self._setup(db, customer_signal_value="low")
        results, _ = self._call(db, ws)
        results_by_id = _by_pid(results)
        assert results_by_id["p_low"].compatibility_negative_contribution == 0.0
        assert (
            results_by_id["p_medium"].compatibility_negative_contribution
            < results_by_id["p_high"].compatibility_negative_contribution
        )
        # And final scores are in the same order
        assert (
            results_by_id["p_low"].recommendation_score
            > results_by_id["p_medium"].recommendation_score
            > results_by_id["p_high"].recommendation_score
        )

    def test_ordering_reflects_distance_severity(self, db):
        ws = self._setup(db, customer_signal_value="medium")
        results, _ = self._call(db, ws)
        # Expected: p_medium (1.4) > p_low (0.5) = p_high (0.5)
        # The two equal-score products fall back to deterministic db_id ordering.
        assert results[0].product_id == "p_medium"
        assert {results[1].product_id, results[2].product_id} == {"p_low", "p_high"}


# ---------------------------------------------------------------------------
# Edge case: product carries a value not in value_order
# ---------------------------------------------------------------------------

class TestOrderedEdgeCases:
    def test_product_value_outside_order_is_neutral(self, db):
        """If the product carries a value the value_order axis doesn't know
        about, the helper returns severity 0 and no penalty is applied — the
        engine should not crash and should not penalize."""
        ws = _seed_workspace(db)
        _seed_affinity(db, ws.id, "cust1", "type", "tee", 0.8)
        _seed_affinity(db, ws.id, "cust1", "level", "medium", 0.8)
        _seed_product(db, ws.id, "p_offscale", {"type": "tee", "level": "extreme"})

        results, _ = get_recommendations(
            db, workspace_id=ws.id, customer_id="cust1",
            attribute_targeting_modes={"level": "compatibility_signal"},
            attribute_behaviors={
                "level": AttributeBehavior(
                    negative_scoring_enabled=True,
                    ordered_values=True,
                    value_order=["low", "medium", "high"],
                ),
            },
        )
        rec = _by_pid(results)["p_offscale"]
        assert rec.compatibility_negative_contribution == 0.0
        assert rec.recommendation_score == pytest.approx(0.8)
