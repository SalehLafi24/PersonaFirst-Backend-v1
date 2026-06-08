"""Tests for PersonaCoherenceDemoter (Patch B).

The demoter fixes the `gift_buyer` cross-age collapse: it soft-demotes
candidates whose attribute value is under-represented in the customer's
purchase distribution, but ONLY when the customer has a clear center of
mass (the focused-/spread-buyer guard). It is the inverse of
SaturationDemoter and composes with it in the rerank demote step.

These mirror the cross-segment guard tests: lightweight stand-ins for
IntentContext / candidate carrying only the fields evaluate() reads, with
the manifest-tag cache pre-populated to bypass manifest load.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services.intent_layer_service import (
    _COHERENCE_DOMINANCE_MIN,
    _COHERENCE_FLOOR,
    _COHERENCE_MAX_DEMOTE,
    CONTRIB_DEMOTE,
    REGISTERED_SIGNAL_TAGS,
    PersonaCoherenceDemoter,
)


# -----------------------------------------------------------------------------
# Stand-ins. Only the fields PersonaCoherenceDemoter.evaluate()/applies() read.
# -----------------------------------------------------------------------------


@dataclass
class _CandInfo:
    db_id: int = 1
    product_id: str = "P1"
    name: str = "test"
    group_id: str | None = None
    repurchase_behavior: str | None = None
    repurchase_window_days: int | None = None
    recommendation_role: str = "main"
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class _Aff:
    distribution: dict[str, float]


@dataclass
class _Persona:
    attribute_affinities: dict[str, _Aff] = field(default_factory=dict)


@dataclass
class _Ctx:
    workspace_id: int = 1
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    catalog_index: dict[str, _CandInfo] = field(default_factory=dict)
    customer_purchase_history: dict = field(default_factory=lambda: {"bought": None})
    persona: Any = None
    # Pre-populated tag cache so tests bypass manifest load. Real production
    # reads this from the manifest's recommendation.signal_tags.
    _persona_coherent_attrs_cache: list = field(default_factory=lambda: ["age_group"])


@dataclass
class _Candidate:
    product_id: str = "P1"


def _make(*, cand_attrs, persona_dist, role="main", tagged=None):
    """Build a minimal context + one candidate."""
    cand = _Candidate(product_id="P1")
    info = _CandInfo(product_id="P1", attributes=dict(cand_attrs),
                     recommendation_role=role)
    ctx = _Ctx()
    ctx.catalog_index["P1"] = info
    ctx.persona = _Persona(attribute_affinities={
        attr: _Aff(distribution=dict(dist)) for attr, dist in persona_dist.items()
    })
    if tagged is not None:
        ctx._persona_coherent_attrs_cache = tagged
    return ctx, cand


# =============================================================================


class TestPersonaCoherenceDemoter:

    def test_demotes_off_distribution_age(self):
        """gift_buyer verbatim: purchases skew older, candidate is infant
        (5% share) -> demoted."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.7, "age_6_8": 0.25,
                                        "infant": 0.05}},
        )
        c = PersonaCoherenceDemoter().evaluate(cand, ctx)
        assert c is not None
        assert c.kind == CONTRIB_DEMOTE
        assert 0.0 < c.value < 1.0

    def test_focused_buyer_keeps_own_dominant_age(self):
        """A candidate matching the customer's dominant age has high share,
        so it is never demoted."""
        ctx, cand = _make(
            cand_attrs={"age_group": "age_3_5"},
            persona_dist={"age_group": {"age_3_5": 0.9, "infant": 0.1}},
        )
        assert PersonaCoherenceDemoter().evaluate(cand, ctx) is None

    def test_spread_buyer_with_no_dominant_left_alone(self):
        """No value reaches DOMINANCE_MIN (a genuinely diverse gift-buyer or
        cold-start flat persona) -> no demote even for a low-share value, so
        we never strip the whole catalog."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"a": 0.3, "b": 0.3, "c": 0.3,
                                        "infant": 0.1}},
        )
        assert _COHERENCE_DOMINANCE_MIN > 0.3  # guards the premise
        assert PersonaCoherenceDemoter().evaluate(cand, ctx) is None

    def test_floor_boundary(self):
        """At/above the floor -> no demote; just below -> demote."""
        floor = _COHERENCE_FLOOR
        ctx_at, cand_at = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.7, "infant": floor,
                                        "other": 0.3 - floor}},
        )
        assert PersonaCoherenceDemoter().evaluate(cand_at, ctx_at) is None

        ctx_below, cand_below = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.7, "infant": floor - 0.001,
                                        "other": 0.3 - (floor - 0.001)}},
        )
        c = PersonaCoherenceDemoter().evaluate(cand_below, ctx_below)
        assert c is not None and c.value < 1.0

    def test_demote_magnitude_at_zero_share(self):
        """Candidate value absent from the distribution -> share=0 ->
        multiplier = 1 - MAX_DEMOTE (the maximum demote)."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.8, "age_6_8": 0.2}},
        )
        c = PersonaCoherenceDemoter().evaluate(cand, ctx)
        assert c is not None
        assert c.value == pytest.approx(1.0 - _COHERENCE_MAX_DEMOTE)

    def test_complementary_role_exempt(self):
        """Complementary picks (e.g. an accessory for something owned) are
        allowed through cross-segment by design."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.9, "infant": 0.05,
                                        "x": 0.05}},
            role="complementary",
        )
        assert PersonaCoherenceDemoter().evaluate(cand, ctx) is None

    def test_candidate_without_tagged_attribute_not_demoted(self):
        """A product with no value for the tagged attribute is universally
        applicable; don't demote."""
        ctx, cand = _make(
            cand_attrs={"product_type": "toy"},
            persona_dist={"age_group": {"age_3_5": 0.9, "infant": 0.1}},
        )
        assert PersonaCoherenceDemoter().evaluate(cand, ctx) is None

    def test_persona_missing_attribute_distribution_not_demoted(self):
        """Tagged attribute, but the persona carries no distribution for it
        -> no information to discriminate; don't demote."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"use_case": {"play": 1.0}},
        )
        assert PersonaCoherenceDemoter().evaluate(cand, ctx) is None

    def test_no_tagged_attributes_is_noop(self):
        """If a workspace tags nothing persona_coherent, the signal is a
        no-op even for off-distribution candidates."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.9, "infant": 0.1}},
            tagged=[],
        )
        assert PersonaCoherenceDemoter().evaluate(cand, ctx) is None

    def test_compounds_across_multiple_tagged_attributes(self):
        """Multiple tagged attributes both off-distribution compound
        multiplicatively."""
        ctx, cand = _make(
            cand_attrs={"age_group": "infant", "gender": "girl"},
            persona_dist={
                "age_group": {"age_3_5": 0.8, "infant": 0.0},
                "gender": {"boy": 0.8, "girl": 0.0},
            },
            tagged=["age_group", "gender"],
        )
        c = PersonaCoherenceDemoter().evaluate(cand, ctx)
        assert c is not None
        assert c.value == pytest.approx((1.0 - _COHERENCE_MAX_DEMOTE) ** 2)

    def test_tag_registered_with_demoter(self):
        """Pin the tag<->signal mapping so the manifest validator stays in
        sync."""
        assert REGISTERED_SIGNAL_TAGS["persona_coherent"] is PersonaCoherenceDemoter

    def test_applies_requires_persona_history_and_tag(self):
        ctx, cand = _make(
            cand_attrs={"age_group": "infant"},
            persona_dist={"age_group": {"age_3_5": 0.9, "infant": 0.1}},
        )
        sig = PersonaCoherenceDemoter()
        assert sig.applies(ctx) is True
        ctx.persona = None
        assert sig.applies(ctx) is False
