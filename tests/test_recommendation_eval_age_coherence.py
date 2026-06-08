"""Tests for the age-coherence floor check (Patch B).

The check is the eval-side mirror of PersonaCoherenceDemoter: it flags rec
sets that collapse onto an age the customer barely purchases, but only when
the customer has a clear purchase center of mass (focused-/spread-buyer
guard). Pure-function tests on crafted response stand-ins; no DB.
"""
from dataclasses import dataclass, field

from app.services.eval.recommendation_eval import (
    _AGE_COHERENCE_DOMINANCE_MIN,
    _AGE_COHERENCE_MAX_OFFDIST_SHARE,
    AGE_COHERENCE_CHECKS,
    check_age_coherence,
)


# -----------------------------------------------------------------------------
# Response stand-ins: only the fields check_age_coherence reads.
# -----------------------------------------------------------------------------


@dataclass
class _Rec:
    product_id: str
    attributes: dict = field(default_factory=dict)


@dataclass
class _Aff:
    distribution: dict


@dataclass
class _Persona:
    attribute_affinities: dict = field(default_factory=dict)


@dataclass
class _Resp:
    recommendations: list
    persona: object = None


def _resp(*, purchase_dist, rec_ages):
    """purchase_dist: age_group -> prob (the persona). rec_ages: list of the
    age_group value on each recommendation."""
    persona = _Persona(attribute_affinities={
        "age_group": _Aff(distribution=dict(purchase_dist)),
    })
    recs = [_Rec(product_id=f"P{i}", attributes={"age_group": a})
            for i, a in enumerate(rec_ages)]
    return _Resp(recommendations=recs, persona=persona)


def _passed(resp):
    return check_age_coherence(resp, customer_id="c")["age_coherence_age_group"].passed


# =============================================================================


class TestAgeCoherenceCheck:

    def test_collapse_to_off_distribution_age_fails(self):
        """gift_buyer collapse: purchases skew kids, recs are all infant
        (5% purchase share) -> violation."""
        resp = _resp(
            purchase_dist={"kids": 0.8, "teen": 0.15, "infant": 0.05},
            rec_ages=["infant"] * 6,
        )
        result = check_age_coherence(resp, customer_id="c")["age_coherence_age_group"]
        assert result.passed is False
        assert result.violations

    def test_coherent_recs_pass(self):
        """Recs on the dominant purchase age -> pass."""
        resp = _resp(
            purchase_dist={"kids": 0.8, "teen": 0.15, "infant": 0.05},
            rec_ages=["kids"] * 5 + ["teen"],
        )
        assert _passed(resp) is True

    def test_off_share_at_cap_passes(self):
        """Exactly max_offdist_share off-distribution -> still passes (boundary)."""
        # 5 of 10 = 0.5 == cap -> pass.
        resp = _resp(
            purchase_dist={"kids": 0.85, "infant": 0.05, "toddler": 0.10},
            rec_ages=["kids"] * 5 + ["infant"] * 5,
        )
        assert _AGE_COHERENCE_MAX_OFFDIST_SHARE == 0.5
        assert _passed(resp) is True

    def test_off_share_over_cap_fails(self):
        """6 of 10 off-distribution -> over the 50% cap -> fail."""
        resp = _resp(
            purchase_dist={"kids": 0.85, "infant": 0.05, "toddler": 0.10},
            rec_ages=["kids"] * 4 + ["infant"] * 6,
        )
        assert _passed(resp) is False

    def test_spread_buyer_with_no_dominant_passes(self):
        """No purchase value reaches DOMINANCE_MIN (a genuinely diverse
        gift-buyer) -> never flagged, even for an all-infant rec set."""
        resp = _resp(
            purchase_dist={"kids": 0.3, "teen": 0.3, "toddler": 0.3, "infant": 0.1},
            rec_ages=["infant"] * 6,
        )
        assert _AGE_COHERENCE_DOMINANCE_MIN > 0.3
        assert _passed(resp) is True

    def test_no_purchase_distribution_passes(self):
        """No age_group purchase signal -> nothing to judge."""
        resp = _resp(purchase_dist={}, rec_ages=["infant"] * 6)
        assert _passed(resp) is True

    def test_empty_recs_passes(self):
        resp = _resp(purchase_dist={"kids": 0.9, "infant": 0.1}, rec_ages=[])
        assert _passed(resp) is True

    def test_null_rec_age_values_ignored(self):
        """Recs without an age_group value are excluded from the share base;
        the remaining (coherent) recs pass."""
        resp = _resp(
            purchase_dist={"kids": 0.9, "infant": 0.1},
            rec_ages=["kids", None, None, "kids"],
        )
        assert _passed(resp) is True

    def test_check_name_registered(self):
        assert AGE_COHERENCE_CHECKS == ("age_coherence_age_group",)
        resp = _resp(purchase_dist={"kids": 0.9, "infant": 0.1}, rec_ages=["kids"])
        out = check_age_coherence(resp, customer_id="c")
        assert set(out.keys()) == set(AGE_COHERENCE_CHECKS)
