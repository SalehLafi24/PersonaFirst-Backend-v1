"""Tests for the cross-segment guard on BehavioralCoOccurrenceBooster.

Same guard mechanism as AttributeRelationshipBooster (shared helper
`_cross_segment_guard_attrs_from_ctx`). Behavioral edges with low
customer-overlap can surface cross-segment products (kids buyer's
single co-purchased adult item) -- the guard suppresses those without
losing signal on properly-segmented edges.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.intent_layer_service import (
    BehavioralCoOccurrenceBooster,
    BehavioralEdge,
)


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
    customer_purchase_history: dict = field(default_factory=lambda: {"X": None})
    customer_purchase_groups: dict = field(default_factory=dict)
    behavioral_targets: dict = field(default_factory=dict)
    _cross_segment_guard_attrs_cache: list = field(
        default_factory=lambda: ["age_group"]
    )


@dataclass
class _Candidate:
    product_id: str


def _edge(strength: float = 0.5, overlap: int = 1) -> BehavioralEdge:
    return BehavioralEdge(
        source_db_id=99, source_product_id="SRC", source_name="src",
        strength=strength, customer_overlap_count=overlap,
        source_customer_count=overlap + 1,
    )


def _make_ctx(
    *, cand_attrs: dict[str, str], persona: dict[str, dict[str, float]],
    edges: list[BehavioralEdge] | None = None,
) -> tuple[_Ctx, _Candidate]:
    cand = _Candidate(product_id="P1")
    info = _CandInfo(db_id=1, product_id="P1", attributes=dict(cand_attrs))
    ctx = _Ctx()
    ctx.catalog_index["P1"] = info
    ctx.customer_affinities = dict(persona)
    if edges is not None:
        ctx.behavioral_targets[info.db_id] = list(edges)
    return ctx, cand


# ============================================================================


class TestBehavioralGuard:

    def test_guard_blocks_cross_segment_co_occurrence(self):
        """The book_heavy/skincare scenario: customer is kids/teen, edge
        from kids book to adult skincare via a single shared customer.
        Guard must suppress the boost."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "product_type": "skincare",
                "age_group": "adult",
            },
            persona={
                "age_group": {"kids": 0.5, "teen": 0.25, "toddler": 0.25},
            },
            edges=[_edge(strength=0.5, overlap=1)],
        )
        result = BehavioralCoOccurrenceBooster().evaluate(cand, ctx)
        assert result is None

    def test_guard_allows_in_segment_co_occurrence(self):
        """In-segment co-occurrence: persona kids and edge to a kids
        product. Edge fires normally."""
        ctx, cand = _make_ctx(
            cand_attrs={
                "product_type": "puzzle",
                "age_group": "kids",
            },
            persona={
                "age_group": {"kids": 1.0},
            },
            edges=[_edge(strength=0.7, overlap=3)],
        )
        result = BehavioralCoOccurrenceBooster().evaluate(cand, ctx)
        assert result is not None
        assert result.value > 0

    def test_guard_disabled_when_no_attribute_tagged(self):
        ctx, cand = _make_ctx(
            cand_attrs={"age_group": "adult"},
            persona={"age_group": {"kids": 1.0}},
            edges=[_edge(strength=0.5, overlap=2)],
        )
        ctx._cross_segment_guard_attrs_cache = []  # nothing tagged
        result = BehavioralCoOccurrenceBooster().evaluate(cand, ctx)
        assert result is not None  # edge fires; guard inactive

    def test_no_edges_returns_none(self):
        ctx, cand = _make_ctx(
            cand_attrs={"age_group": "kids"},
            persona={"age_group": {"kids": 1.0}},
            edges=[],
        )
        assert BehavioralCoOccurrenceBooster().evaluate(cand, ctx) is None

    def test_guard_passes_when_persona_has_no_age_group(self):
        ctx, cand = _make_ctx(
            cand_attrs={"age_group": "adult"},
            persona={},  # no info to discriminate
            edges=[_edge(strength=0.5, overlap=2)],
        )
        result = BehavioralCoOccurrenceBooster().evaluate(cand, ctx)
        assert result is not None
