"""Tests for the top-N diversity cap (`_apply_top_n_diversity_cap`).

Pure-function tests; no DB. The cap walks score-sorted recs and limits
how many of any single product_type can occupy top-N before falling back
to the skipped pool.
"""
from dataclasses import dataclass, field

from app.services.customer_recommendation_service import (
    _DIVERSITY_CAP_SHARE,
    _apply_top_n_diversity_cap,
)


@dataclass
class _Rec:
    product_id: str
    score: float = 0.0
    attributes: dict = field(default_factory=dict)


def _rec(pid: str, pt: str | None) -> _Rec:
    return _Rec(product_id=pid, attributes={"product_type": pt})


# =============================================================================


class TestDiversityCap:

    def test_no_op_on_already_diverse_set(self):
        recs = [
            _rec("P1", "bib"),
            _rec("P2", "diaper"),
            _rec("P3", "pacifier"),
            _rec("P4", "stroller"),
            _rec("P5", "book"),
        ]
        out = _apply_top_n_diversity_cap(recs, top_n=5)
        # No type exceeds cap; output identical to input.
        assert [r.product_id for r in out] == ["P1", "P2", "P3", "P4", "P5"]

    def test_collapse_capped_when_other_types_available(self):
        # Realistic toy_focused-like pool: 1 plush at top, then 9 books at
        # similar scores, then more diverse types at lower scores.
        # Cap should pull plush + 5 books, then fill remaining slots from
        # the lower-ranked diverse types (NOT from skipped books).
        recs = (
            [_rec("P0", "plush_toy")]
            + [_rec(f"B{i}", "book") for i in range(9)]
            + [_rec(f"L{i}", "learning_toy") for i in range(3)]
            + [_rec(f"S{i}", "sensory_toy") for i in range(3)]
        )
        out = _apply_top_n_diversity_cap(recs, top_n=10)
        types = [r.attributes["product_type"] for r in out]
        assert types.count("book") == 5
        assert types.count("plush_toy") == 1
        # The remaining 4 slots come from learning_toy + sensory_toy first,
        # NOT from skipped books, because the cap walks past position 9 in
        # the candidate list before falling back to skipped pool.
        assert types.count("learning_toy") + types.count("sensory_toy") == 4
        assert len(out) == 10

    def test_collapse_falls_back_to_skipped_when_no_diversity_available(self):
        # When the candidate pool truly contains nothing else, cap falls back
        # to the skipped pool and the dominant type fills past the cap.
        # (Better than returning < top_n.)
        recs = [_rec("P0", "plush_toy")] + [_rec(f"B{i}", "book") for i in range(9)]
        out = _apply_top_n_diversity_cap(recs, top_n=10)
        types = [r.attributes["product_type"] for r in out]
        # 1 plush + 5 books in initial pass, then 4 more books from skipped.
        assert types.count("plush_toy") == 1
        assert types.count("book") == 9
        assert len(out) == 10

    def test_score_order_preserved_within_buckets(self):
        # Recs in score-desc order; verify the first 5 books selected are the
        # 5 highest-scored (B0..B4), not B5..B9.
        recs = [_rec("P0", "plush_toy")] + [
            _rec(f"B{i}", "book") for i in range(9)
        ]
        out = _apply_top_n_diversity_cap(recs, top_n=10)
        # Within first 6 selected (1 plush + 5 books before any skipped fill),
        # books should be B0-B4.
        first_books = [r.product_id for r in out[:6]
                       if r.attributes["product_type"] == "book"]
        assert first_books == ["B0", "B1", "B2", "B3", "B4"]

    def test_skipped_pool_fills_remaining_slots(self):
        # Only one product_type available; cap doesn't actually reduce count.
        recs = [_rec(f"B{i}", "book") for i in range(20)]
        out = _apply_top_n_diversity_cap(recs, top_n=10)
        assert len(out) == 10
        # All 10 are books -- the cap can't help when there's nothing else.
        assert all(r.attributes["product_type"] == "book" for r in out)

    def test_three_categories_fair_distribution(self):
        # 10 books, 5 plush, 5 toys. top_n=10, cap=5.
        recs = (
            [_rec(f"B{i}", "book") for i in range(10)]
            + [_rec(f"P{i}", "plush_toy") for i in range(5)]
            + [_rec(f"T{i}", "toy") for i in range(5)]
        )
        out = _apply_top_n_diversity_cap(recs, top_n=10)
        types = [r.attributes["product_type"] for r in out]
        # Books capped at 5, plush at 5 (or fewer), toys at 5 (or fewer);
        # exactly 10 selected.
        assert types.count("book") == 5
        assert len(out) == 10

    def test_null_product_type_treated_as_separate_bucket(self):
        # Null bucket counted separately from book bucket. Both subject to cap.
        recs = (
            [_rec("N1", None), _rec("N2", None)]
            + [_rec(f"B{i}", "book") for i in range(8)]
        )
        out = _apply_top_n_diversity_cap(recs, top_n=10)
        types = [r.attributes.get("product_type") for r in out]
        # Books: 5 in cap, then 3 more from skipped. Nulls: 2 (under cap).
        assert types.count("book") == 8
        assert types.count(None) == 2
        assert len(out) == 10

    def test_cap_share_constant(self):
        assert _DIVERSITY_CAP_SHARE == 0.5

    def test_top_n_zero_returns_empty(self):
        recs = [_rec("P1", "book"), _rec("P2", "diaper")]
        assert _apply_top_n_diversity_cap(recs, top_n=0) == []

    def test_top_n_one_no_cap_applied(self):
        recs = [_rec("P1", "book"), _rec("P2", "book")]
        out = _apply_top_n_diversity_cap(recs, top_n=1)
        assert [r.product_id for r in out] == ["P1"]

    def test_empty_input(self):
        assert _apply_top_n_diversity_cap([], top_n=10) == []

    def test_small_top_n_round_up(self):
        # top_n=5, cap=0.5 -> ceil(2.5) = 3.
        recs = [_rec(f"B{i}", "book") for i in range(10)]
        out = _apply_top_n_diversity_cap(recs, top_n=5)
        # 3 books from cap, then 2 more from skipped fill = 5 total books.
        assert len(out) == 5

    def test_score_desc_order_in_output(self):
        # Output preserves overall score-descending order (within selected
        # subset, since we walk in input order).
        recs = [
            _rec("P0", "plush_toy"),  # highest
            _rec("B0", "book"),
            _rec("B1", "book"),
            _rec("B2", "book"),
            _rec("B3", "book"),
            _rec("B4", "book"),
            _rec("B5", "book"),  # would be 6th book -> skipped
            _rec("L0", "learning_toy"),  # lower score, but uncapped
        ]
        out = _apply_top_n_diversity_cap(recs, top_n=8)
        ids = [r.product_id for r in out]
        # Within selected: P0, B0, B1, B2, B3, B4, L0, then skipped fill (B5)
        assert "L0" in ids
        # P0 first since it's the top-scored
        assert ids[0] == "P0"
