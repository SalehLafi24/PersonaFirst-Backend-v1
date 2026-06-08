"""Tests for the cross-segment guard on AttributeRelationshipBooster.

The guard exists because curated attribute-relationship edges declare
`(source_attr, source_value) -> (target_attr, target_value)` without any
constraint on the candidate's other attributes. Without the guard a
customer with `use_case=learning` affinity would surface adult skincare
candidates whose product_type or age_group is wildly off-persona.

The guard: if the candidate has a value for `age_group` AND the
customer's age_group affinity distribution is non-empty, the candidate's
age_group must be present in the distribution at >= the floor weight.
Otherwise the booster returns None for that candidate.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.intent_layer_service import (
    _ATTR_REL_CROSS_SEGMENT_GUARD_FLOOR,
    REGISTERED_SIGNAL_TAGS,
    AttributeRelationshipBooster,
    AttributeRelationshipEdge,
)


# -----------------------------------------------------------------------------
# Lightweight stand-ins for IntentContext / candidate. We only need the fields
# AttributeRelationshipBooster.evaluate() reads.
# -----------------------------------------------------------------------------


@dataclass
class _CandInfo:
    db_id: int = 1
    product_id: str = "P_TEST"
    name: str = "test"
    group_id: str | None = None
    repurchase_behavior: str | None = None
    repurchase_window_days: int | None = None
    recommendation_role: str = "main"
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class _Ctx:
    workspace_id: int = 1
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    catalog_index: dict[str, _CandInfo] = field(default_factory=dict)
    customer_affinities: dict[str, dict[str, float]] = field(default_factory=dict)
    attr_relationships: dict[tuple[str, str], list[AttributeRelationshipEdge]] = (
        field(default_factory=dict)
    )
    customer_purchase_history: dict = field(default_factory=dict)
    customer_purchase_groups: dict = field(default_factory=dict)
    # Pre-populated guard cache so tests bypass manifest load. Real
    # production reads this from the manifest's recommendation.signal_tags.
    _cross_segment_guard_attrs_cache: list = field(default_factory=lambda: ["age_group"])


@dataclass
class _Candidate:
    product_id: str


def _make_ctx(
    *,
    cand_attrs: dict[str, str],
    persona: dict[str, dict[str, float]],
    edges: list[AttributeRelationshipEdge],
) -> tuple[_Ctx, _Candidate]:
    """Build a minimal context plus one candidate."""
    cand = _Candidate(product_id="P1")
    info = _CandInfo(product_id="P1", attributes=dict(cand_attrs))
    ctx = _Ctx()
    ctx.catalog_index["P1"] = info
    ctx.customer_affinities = dict(persona)
    by_source: dict[tuple[str, str], list[AttributeRelationshipEdge]] = {}
    for e in edges:
        by_source.setdefault((e.source_attr, e.source_value), []).append(e)
    ctx.attr_relationships = by_source
    return ctx, cand


def _edge(src_attr: str, src_value: str, tgt_attr: str, tgt_value: str,
          strength: float = 0.8) -> AttributeRelationshipEdge:
    """AttributeRelationshipEdge factory with sensible defaults."""
    return AttributeRelationshipEdge(
        source_attr=src_attr, source_value=src_value,
        target_attr=tgt_attr, target_value=tgt_value,
        strength=strength,
        confidence=1.0, lift=1.0, pair_count=10,
    )


# =============================================================================


class TestCrossSegmentGuard:

    def test_guard_blocks_off_persona_age_group(self):
        """The book_heavy / adult-skincare scenario verbatim:
        customer's persona is all kids/teen/toddler; candidate is age_group=adult;
        edge would otherwise fire on use_case=learning -> use_case=learning.
        Guard must return None."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "product_type": "skincare",
                "age_group": "adult",      # OFF-PERSONA
                "gender": "unisex",
                "use_case": "learning",
            },
            persona={
                "use_case": {"learning": 1.0},
                "age_group": {"kids": 0.5, "teen": 0.25, "toddler": 0.25},
            },
            edges=[_edge("use_case", "learning", "use_case", "learning",
                         strength=0.8)],
        )
        result = AttributeRelationshipBooster().evaluate(cand, ctx)
        assert result is None

    def test_guard_allows_in_persona_age_group(self):
        """Candidate's age_group is in the persona at >= floor.
        Edge fires normally."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "product_type": "book",
                "age_group": "kids",       # IN persona at 50%
                "gender": "unisex",
                "use_case": "learning",
            },
            persona={
                "use_case": {"learning": 1.0},
                "age_group": {"kids": 0.5, "teen": 0.25, "toddler": 0.25},
            },
            edges=[_edge("use_case", "learning", "use_case", "learning",
                         strength=0.8)],
        )
        result = AttributeRelationshipBooster().evaluate(cand, ctx)
        assert result is not None
        assert result.value > 0

    def test_guard_allows_when_candidate_has_no_age_group(self):
        """A product without age_group is universally applicable; do not
        block. Towels, generic accessories etc. fall here."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "product_type": "towel",
                "use_case": "bathing",
                # age_group missing
            },
            persona={
                "use_case": {"bathing": 0.8},
                "age_group": {"kids": 1.0},
            },
            edges=[_edge("use_case", "bathing", "use_case", "bathing")],
        )
        result = AttributeRelationshipBooster().evaluate(cand, ctx)
        assert result is not None

    def test_guard_allows_when_persona_has_no_age_group(self):
        """No customer age_group affinity = no info to discriminate.
        Don't block."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "product_type": "book",
                "age_group": "kids",
                "use_case": "learning",
            },
            persona={
                "use_case": {"learning": 1.0},
                # age_group not in persona
            },
            edges=[_edge("use_case", "learning", "use_case", "learning")],
        )
        result = AttributeRelationshipBooster().evaluate(cand, ctx)
        assert result is not None

    def test_guard_floor_boundary(self):
        """Weight just below the floor blocks; just at/above allows."""
        floor = _ATTR_REL_CROSS_SEGMENT_GUARD_FLOOR
        # Just below floor -> blocked.
        ctx_below, cand_below = _make_ctx(
            cand_attrs={"age_group": "adult", "use_case": "learning"},
            persona={
                "use_case": {"learning": 1.0},
                "age_group": {"adult": floor - 0.001, "kids": 1.0 - (floor - 0.001)},
            },
            edges=[_edge("use_case", "learning", "use_case", "learning")],
        )
        assert AttributeRelationshipBooster().evaluate(cand_below, ctx_below) is None

        # At floor -> allowed.
        ctx_at, cand_at = _make_ctx(
            cand_attrs={"age_group": "adult", "use_case": "learning"},
            persona={
                "use_case": {"learning": 1.0},
                "age_group": {"adult": floor, "kids": 1.0 - floor},
            },
            edges=[_edge("use_case", "learning", "use_case", "learning")],
        )
        assert AttributeRelationshipBooster().evaluate(cand_at, ctx_at) is not None

    def test_guard_tag_registered_with_attribute_relationship_booster(self):
        """The `cross_segment_guard` tag is consumed by AttributeRelationship-
        Booster. Pin it so the manifest validator stays in sync."""
        assert REGISTERED_SIGNAL_TAGS["cross_segment_guard"] is AttributeRelationshipBooster

    def test_guard_disabled_when_no_attributes_tagged(self):
        """If a workspace's manifest doesn't tag any attribute as
        cross_segment_guard, the booster's guard step is a no-op even
        for off-persona age_group candidates."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "age_group": "adult",
                "use_case": "learning",
            },
            persona={
                "use_case": {"learning": 1.0},
                "age_group": {"kids": 1.0},  # adult would be off-persona
            },
            edges=[_edge("use_case", "learning", "use_case", "learning")],
        )
        ctx._cross_segment_guard_attrs_cache = []  # nothing tagged
        result = AttributeRelationshipBooster().evaluate(cand, ctx)
        # No guard active -> edge fires normally even though age_group
        # would otherwise have blocked it.
        assert result is not None
        assert result.value > 0

    def test_guard_works_for_arbitrary_tagged_attribute(self):
        """If a workspace tags a different attribute (e.g. `use_case`),
        the guard applies to that attribute instead of age_group."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "age_group": "adult",       # off-persona but NOT tagged
                "use_case": "swimming",     # tagged + off-persona -> blocked
            },
            persona={
                "use_case": {"learning": 1.0, "swimming": 0.0},
                "age_group": {"kids": 1.0},
            },
            edges=[_edge("use_case", "learning", "age_group", "adult")],
        )
        ctx._cross_segment_guard_attrs_cache = ["use_case"]  # tagged attribute
        result = AttributeRelationshipBooster().evaluate(cand, ctx)
        assert result is None
