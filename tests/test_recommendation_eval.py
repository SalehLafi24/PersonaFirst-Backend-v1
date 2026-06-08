"""Tests for recommendation eval hard-constraint checkers.

Each checker is exercised with a small synthetic response object plus a
seeded DB fixture where DB lookups are needed.
"""
from dataclasses import dataclass, field

import pytest

from app.models.customer import Customer, CustomerInteraction, INTERACTION_PURCHASE
from app.models.product import Product
from app.models.workspace import Workspace
from app.services.eval.recommendation_eval import (
    DIVERSITY_FLOORS,
    HARD_CONSTRAINT_CHECKS,
    WEIGHT_CONFORMANCE_CHECKS,
    check_diversity_floor,
    check_hard_constraints,
    check_weight_conformance,
)


# --------------------------------------------------------------------------
# Lightweight stand-ins for the rec engine response shape. The eval reads
# `response.recommendations[*].product_id` and `.score`. Anything heavier
# would couple tests to the engine's full response dataclass.
# --------------------------------------------------------------------------


@dataclass
class _Rec:
    product_id: str
    score: float
    persona_fit: float = 0.0
    attributes: dict = field(default_factory=dict)
    matched_signals: list = field(default_factory=list)
    intent_contributions: list = field(default_factory=list)


@dataclass
class _MatchedSignal:
    attribute_name: str
    candidate_value: str | None
    persona_probability: float
    weight: float
    contribution: float


@dataclass
class _Response:
    recommendations: list[_Rec] = field(default_factory=list)


def _make_rec(
    pid: str, signals: list[tuple[str, float, float]],
    confidence: float, *, intent_contribs: list | None = None,
) -> _Rec:
    """Build a rec where each signal is (attr, weight, prob).
    contribution = weight*prob; persona_fit = sum; score = pf * confidence."""
    matched = [
        _MatchedSignal(attribute_name=a, candidate_value=a + "_v",
                       persona_probability=p, weight=w, contribution=w * p)
        for a, w, p in signals
    ]
    pf = sum(m.contribution for m in matched)
    return _Rec(
        product_id=pid, persona_fit=pf, score=pf * confidence,
        matched_signals=matched, intent_contributions=intent_contribs or [],
    )


def _ws(db) -> Workspace:
    ws = Workspace(name="rec-eval-ws", slug="rec-eval-ws")
    db.add(ws)
    db.flush()
    return ws


def _seed_products(db, ws: Workspace, pids: list[str]) -> None:
    for pid in pids:
        db.add(Product(
            workspace_id=ws.id, product_id=pid, sku=pid, name=pid,
        ))
    db.flush()


def _seed_purchase(db, ws: Workspace, customer_id: str, product_id: str) -> None:
    if not db.query(Customer).filter_by(
        workspace_id=ws.id, customer_id=customer_id,
    ).first():
        db.add(Customer(workspace_id=ws.id, customer_id=customer_id))
        db.flush()
    db.add(CustomerInteraction(
        workspace_id=ws.id, customer_id=customer_id,
        product_id=product_id, interaction_type=INTERACTION_PURCHASE,
    ))
    db.flush()


# ======================================================================
# Individual checks
# ======================================================================


class TestHardConstraints:

    def test_clean_response_passes_all_checks(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2", "P3"])
        resp = _Response([
            _Rec("P1", 0.9), _Rec("P2", 0.7), _Rec("P3", 0.5),
        ])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert set(results) == set(HARD_CONSTRAINT_CHECKS)
        assert all(r.passed for r in results.values()), {
            n: [v.message for v in r.violations]
            for n, r in results.items() if not r.passed
        }

    def test_duplicate_product_id_fails(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2"])
        resp = _Response([
            _Rec("P1", 0.9), _Rec("P2", 0.8), _Rec("P1", 0.5),
        ])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        dup = results["no_duplicates"]
        assert not dup.passed
        assert any("P1" in v.message and "indices" in v.detail
                   for v in dup.violations)

    def test_top_n_violation(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2", "P3"])
        resp = _Response([_Rec("P1", 0.9), _Rec("P2", 0.7), _Rec("P3", 0.5)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=2,
        )
        assert not results["top_n_contract"].passed
        assert results["top_n_contract"].violations[0].detail == {
            "returned": 3, "requested": 2,
        }

    def test_score_descending_violation(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2"])
        resp = _Response([_Rec("P1", 0.5), _Rec("P2", 0.9)])  # ascending!
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert not results["score_descending"].passed

    def test_score_equal_is_allowed(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2"])
        resp = _Response([_Rec("P1", 0.7), _Rec("P2", 0.7)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert results["score_descending"].passed

    def test_purchased_product_in_response_fails(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2"])
        _seed_purchase(db, ws, "customer-A", "P1")
        resp = _Response([_Rec("P1", 0.9), _Rec("P2", 0.5)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert not results["no_purchased_in_response"].passed
        assert results["no_purchased_in_response"].violations[0].detail["product_id"] == "P1"

    def test_complementary_bypass_allowed_in_response(self, db):
        """Per C12, complementary recs may surface even from history."""
        ws = _ws(db)
        # Seed a complementary product the customer bought.
        prod = Product(
            workspace_id=ws.id, product_id="P_COMP", sku="P_COMP", name="comp",
            recommendation_role="complementary",
            repurchase_behavior="one_time",
        )
        db.add(prod)
        db.flush()
        _seed_purchase(db, ws, "customer-A", "P_COMP")
        resp = _Response([_Rec("P_COMP", 0.9)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert results["no_purchased_in_response"].passed

    def test_repurchasable_outside_window_allowed(self, db):
        """Per C11 (outside window)."""
        from datetime import datetime, timedelta, timezone
        ws = _ws(db)
        prod = Product(
            workspace_id=ws.id, product_id="P_RP", sku="P_RP", name="rp",
            repurchase_behavior="repurchasable",
            repurchase_window_days=30,
        )
        db.add(prod)
        db.flush()
        # Customer bought it 100 days ago -> outside 30-day window -> allowed.
        if not db.query(Customer).filter_by(
            workspace_id=ws.id, customer_id="customer-A",
        ).first():
            db.add(Customer(workspace_id=ws.id, customer_id="customer-A"))
            db.flush()
        db.add(CustomerInteraction(
            workspace_id=ws.id, customer_id="customer-A",
            product_id="P_RP", interaction_type=INTERACTION_PURCHASE,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=100),
        ))
        db.flush()
        resp = _Response([_Rec("P_RP", 0.9)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert results["no_purchased_in_response"].passed

    def test_repurchasable_inside_window_caught(self, db):
        from datetime import datetime, timedelta, timezone
        ws = _ws(db)
        prod = Product(
            workspace_id=ws.id, product_id="P_RP_RECENT", sku="P_RP_RECENT",
            name="rp", repurchase_behavior="repurchasable",
            repurchase_window_days=90,
        )
        db.add(prod)
        db.flush()
        if not db.query(Customer).filter_by(
            workspace_id=ws.id, customer_id="customer-A",
        ).first():
            db.add(Customer(workspace_id=ws.id, customer_id="customer-A"))
            db.flush()
        db.add(CustomerInteraction(
            workspace_id=ws.id, customer_id="customer-A",
            product_id="P_RP_RECENT", interaction_type=INTERACTION_PURCHASE,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=46),
        ))
        db.flush()
        resp = _Response([_Rec("P_RP_RECENT", 0.9)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert not results["no_purchased_in_response"].passed

    def test_purchase_history_for_other_customer_does_not_leak(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1"])
        _seed_purchase(db, ws, "customer-B", "P1")  # different customer
        resp = _Response([_Rec("P1", 0.9)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert results["no_purchased_in_response"].passed

    def test_unknown_product_id_fails(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1"])
        resp = _Response([_Rec("P1", 0.9), _Rec("P_GHOST", 0.5)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert not results["products_exist"].passed
        assert results["products_exist"].violations[0].detail["product_id"] == "P_GHOST"

    def test_empty_response_passes_all_checks(self, db):
        ws = _ws(db)
        resp = _Response([])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert all(r.passed for r in results.values())

    def test_check_does_not_mutate_db(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1", "P2"])
        _seed_purchase(db, ws, "customer-A", "P1")
        before = (
            db.query(Product).count(),
            db.query(Customer).count(),
            db.query(CustomerInteraction).count(),
        )
        resp = _Response([_Rec("P2", 0.9)])
        check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        after = (
            db.query(Product).count(),
            db.query(Customer).count(),
            db.query(CustomerInteraction).count(),
        )
        assert before == after


class TestWeightConformance:

    WEIGHTS = {
        "product_type": 0.236311,
        "age_group": 0.366465,
        "gender": 0.301462,
        "use_case": 0.095762,
    }
    CONFIDENCE = 0.875

    def test_clean_response_passes_all(self):
        # All math derived from weights; everything internally consistent.
        rec = _make_rec("P1", [
            ("product_type", 0.236311, 0.5),
            ("age_group", 0.366465, 0.4),
            ("gender", 0.301462, 0.6),
            ("use_case", 0.095762, 0.2),
        ], confidence=self.CONFIDENCE)
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights=self.WEIGHTS,
            persona_confidence=self.CONFIDENCE,
        )
        assert sorted(results) == sorted(WEIGHT_CONFORMANCE_CHECKS)
        assert all(r.passed for r in results.values()), {
            n: [v.message for v in r.violations]
            for n, r in results.items() if not r.passed
        }

    def test_weights_sum_violation(self):
        bad_weights = {**self.WEIGHTS, "use_case": 0.5}  # sum > 1.0
        resp = _Response([])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights=bad_weights,
            persona_confidence=self.CONFIDENCE,
        )
        assert not results["manifest_weights_sum"].passed

    def test_weight_drift_from_manifest_caught(self):
        # rec carries a stale weight that no longer matches manifest.
        rec = _Rec(
            product_id="P1", persona_fit=0.0, score=0.0,
            matched_signals=[
                _MatchedSignal("product_type", "p", 0.5, weight=0.99,  # stale!
                               contribution=0.99 * 0.5),
            ],
        )
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights={"product_type": 0.236311},
            persona_confidence=self.CONFIDENCE,
        )
        assert not results["weight_matches_manifest"].passed

    def test_contribution_math_violation(self):
        # weight*prob != contribution (engine math drift).
        rec = _Rec(
            product_id="P1", persona_fit=0.5, score=0.5,
            matched_signals=[
                _MatchedSignal("product_type", "p", 0.5, weight=0.236311,
                               contribution=0.5),  # should be 0.118
            ],
        )
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights={"product_type": 0.236311},
            persona_confidence=self.CONFIDENCE,
        )
        assert not results["contribution_math"].passed

    def test_persona_fit_math_violation(self):
        # persona_fit doesn't match sum of contributions.
        rec = _Rec(
            product_id="P1", persona_fit=0.99, score=0.0,  # off
            matched_signals=[
                _MatchedSignal("product_type", "p", 0.5, weight=0.236311,
                               contribution=0.236311 * 0.5),
            ],
        )
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights={"product_type": 0.236311},
            persona_confidence=self.CONFIDENCE,
        )
        assert not results["persona_fit_math"].passed

    def test_score_math_skipped_when_intent_layer_active(self):
        # Intent layer modified score; pre-intent formula is allowed to
        # not match. Check should pass (skip), not fail.
        rec = _make_rec("P1", [
            ("product_type", 0.236311, 0.5),
        ], confidence=self.CONFIDENCE,
        intent_contribs=[{"signal": "behavioral_co_occurrence", "boost": 0.1}])
        rec.score = 0.999  # modified by intent layer
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights={"product_type": 0.236311},
            persona_confidence=self.CONFIDENCE,
        )
        # score_math skipped for intent-modified recs -> passes
        assert results["score_math"].passed

    def test_score_math_caught_without_intent(self):
        rec = _make_rec("P1", [
            ("product_type", 0.236311, 0.5),
        ], confidence=self.CONFIDENCE)
        rec.score = rec.persona_fit * (self.CONFIDENCE + 0.1)  # off
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights={"product_type": 0.236311},
            persona_confidence=self.CONFIDENCE,
        )
        assert not results["score_math"].passed

    def test_unknown_attribute_in_signal_caught(self):
        # rec carries a signal for an attribute not in manifest weights.
        rec = _Rec(
            product_id="P1", persona_fit=0.0, score=0.0,
            matched_signals=[
                _MatchedSignal("phantom_attr", "x", 0.5, weight=0.5,
                               contribution=0.25),
            ],
        )
        resp = _Response([rec])
        results = check_weight_conformance(
            resp, customer_id="c1",
            manifest_weights={"product_type": 1.0},
            persona_confidence=self.CONFIDENCE,
        )
        assert not results["weight_matches_manifest"].passed


class TestDiversityFloor:

    def _rec(self, pid: str, **attrs) -> _Rec:
        return _Rec(product_id=pid, score=0.5, attributes=attrs)

    def test_diverse_set_passes(self):
        resp = _Response([
            self._rec("P1", product_type="bib", age_group="infant", use_case="feeding"),
            self._rec("P2", product_type="pacifier", age_group="infant", use_case="feeding"),
            self._rec("P3", product_type="diaper", age_group="infant", use_case="diapering"),
            self._rec("P4", product_type="changing_mat", age_group="infant", use_case="diapering"),
            self._rec("P5", product_type="bath_product", age_group="toddler", use_case="bathing"),
        ])
        results = check_diversity_floor(resp, customer_id="c1")
        assert all(r.passed for r in results.values()), {
            n: [v.message for v in r.violations]
            for n, r in results.items() if not r.passed
        }

    def test_product_type_collapse_fails(self):
        # All 5 recs are bibs -> product_type top_share = 1.0, exceeds 0.6 cap.
        resp = _Response([
            self._rec(f"P{i}", product_type="bib", age_group="infant", use_case="feeding")
            for i in range(5)
        ])
        results = check_diversity_floor(resp, customer_id="c1")
        assert not results["diversity_top_share_product_type"].passed
        assert not results["diversity_distinct_product_type"].passed

    def test_age_group_homogeneity_is_warn_not_error(self):
        # All age_group=infant -> top_share=1.0, but the rule allows it
        # (severity=warn, max_top_share=1.0).
        resp = _Response([
            self._rec(f"P{i}", product_type="bib" if i < 2 else "pacifier",
                      age_group="infant")
            for i in range(5)
        ])
        results = check_diversity_floor(resp, customer_id="c1")
        # age_group passes structurally because the floor is permissive
        # (max_top_share=1.0). The check is a no-op for age_group at default config.
        assert results["diversity_top_share_age_group"].passed

    def test_empty_response_passes(self):
        resp = _Response([])
        results = check_diversity_floor(resp, customer_id="c1")
        assert all(r.passed for r in results.values())

    def test_null_attribute_values_excluded_from_count(self):
        # 3 recs have product_type=None (excluded), 2 have product_type=bib.
        # Distinct = 1, top_share = 1.0 -> error (collapse).
        resp = _Response([
            self._rec("P1", product_type="bib"),
            self._rec("P2", product_type="bib"),
            self._rec("P3", product_type=None),
            self._rec("P4", product_type=None),
            self._rec("P5", product_type=None),
        ])
        results = check_diversity_floor(resp, customer_id="c1")
        assert not results["diversity_distinct_product_type"].passed

    def test_custom_floors(self):
        # Stricter floors that should fail a previously-passing set.
        resp = _Response([
            self._rec("P1", product_type="bib"),
            self._rec("P2", product_type="bib"),
            self._rec("P3", product_type="diaper"),
        ])
        # bib=2/3 = 67% top_share. Default cap is 60%, so this would fail.
        # But with cap=80%, it passes.
        loose_floors = {
            "product_type": {"min_distinct": 2, "max_top_share": 0.8, "severity": "error"},
        }
        results = check_diversity_floor(resp, customer_id="c1", floors=loose_floors)
        assert results["diversity_top_share_product_type"].passed


class TestSuiteShape:

    def test_suite_returns_one_result_per_check(self, db):
        ws = _ws(db)
        _seed_products(db, ws, ["P1"])
        resp = _Response([_Rec("P1", 0.9)])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-A", requested_top_n=5,
        )
        assert sorted(results.keys()) == sorted(HARD_CONSTRAINT_CHECKS)

    def test_results_carry_customer_id(self, db):
        ws = _ws(db)
        resp = _Response([])
        results = check_hard_constraints(
            db, resp, workspace_id=ws.id,
            customer_id="customer-XYZ", requested_top_n=5,
        )
        assert all(r.customer_id == "customer-XYZ" for r in results.values())
