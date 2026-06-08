"""Tests for attribute extraction eval (app/services/eval/attribute_eval.py).

Most logic lives in pure functions (`classify`, `_classify_pairs`) and is
tested without DB. The DB-aware path (`evaluate_attributes`) gets one
end-to-end test against an in-memory workspace.
"""
import pytest

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.services.eval.attribute_eval import (
    AttributeEvalCounts,
    _classify_pairs,
    classify,
    classify_kind,
    evaluate_attributes,
)


# ======================================================================
# Pure classifier
# ======================================================================


class TestClassify:

    @pytest.mark.parametrize("gold,sys,expected", [
        ("kids", "kids", "tp"),
        (None, None, "tn"),
        (None, "kids", "fp"),         # system invented
        ("kids", None, "fn"),         # system missed
        ("kids", "teen", "mismatch"),
        ("", "", "tn"),               # empty == None
        ("", "kids", "fp"),
        ("kids", "", "fn"),
    ])
    def test_classification_outcomes(self, gold, sys, expected):
        assert classify(gold, sys) == expected


class TestCounts:

    def test_metric_denominators(self):
        # 3 TP, 1 TN, 1 FP, 1 FN, 1 mismatch -> total 7
        c = AttributeEvalCounts(tp=3, tn=1, fp=1, fn=1, mismatch=1)
        assert c.total == 7
        assert c.agreement_rate == pytest.approx(4 / 7)
        assert c.disagreement_rate == pytest.approx(3 / 7)
        # precision = TP / (TP + FP + Mismatch) = 3 / 5
        assert c.precision == pytest.approx(3 / 5)
        # recall = TP / (TP + FN + Mismatch) = 3 / 5
        assert c.recall == pytest.approx(3 / 5)

    def test_metrics_with_no_positives(self):
        c = AttributeEvalCounts(tn=5)
        assert c.total == 5
        assert c.agreement_rate == 1.0
        assert c.disagreement_rate == 0.0
        # No positives -> precision/recall are undefined.
        assert c.precision is None
        assert c.recall is None

    def test_empty_counts(self):
        c = AttributeEvalCounts()
        assert c.total == 0
        assert c.agreement_rate is None
        assert c.disagreement_rate is None
        assert c.precision is None
        assert c.recall is None


class TestClassifyKind:

    AAV = frozenset({"kids", "teen", "adult", "infant", "toddler"})

    def test_extraction_error_when_modes_present_and_value_in_aav(self):
        # Has modes; gold is a known AAV value; system extracted wrong
        # via an extraction-layer source.
        assert classify_kind(
            "mismatch", "kids", "teen",
            has_extraction_modes=True, active_aav_lower=self.AAV,
            system_source="regex_extract",
        ) == "extraction_error"

    def test_policy_divergence_when_no_extraction_modes(self):
        # No extraction layer (e.g., csv_direct only). System reflects
        # upstream truth; gold reflects editorial. Not an engine bug.
        assert classify_kind(
            "mismatch", "unisex", "female",
            has_extraction_modes=False,
            active_aav_lower=frozenset({"male", "female", "unisex"}),
            system_source="text",
        ) == "policy_divergence"

    def test_policy_divergence_when_system_source_is_csv_direct(self):
        # Even if attribute has extraction modes configured, when the
        # system value came from csv_direct ('text'), dispatcher
        # precedence prevents extraction modes from overriding -- so
        # the disagreement is policy, not extraction.
        # Concrete case: age_group "Pioneers 8+" -> teen on adult-targeted product.
        assert classify_kind(
            "mismatch", "adult", "teen",
            has_extraction_modes=True,
            active_aav_lower=self.AAV,
            system_source="text",
        ) == "policy_divergence"

    def test_extraction_error_when_system_source_is_regex(self):
        assert classify_kind(
            "mismatch", "play", "swimming",
            has_extraction_modes=True,
            active_aav_lower=frozenset({"play", "swimming", "feeding"}),
            system_source="regex_extract",
        ) == "extraction_error"

    def test_extraction_error_when_system_source_is_contextual_defaults(self):
        assert classify_kind(
            "mismatch", "learning", "play",
            has_extraction_modes=True,
            active_aav_lower=frozenset({"play", "learning"}),
            system_source="contextual_defaults",
        ) == "extraction_error"

    def test_fn_with_modes_falls_through_to_extraction_error(self):
        # FN has no system source. With modes configured and gold in AAV,
        # it's an extraction_error (extraction missed it).
        assert classify_kind(
            "fn", "kids", None,
            has_extraction_modes=True,
            active_aav_lower=self.AAV,
            system_source=None,
        ) == "extraction_error"

    def test_taxonomy_gap_when_gold_not_in_aav(self):
        assert classify_kind(
            "fn", "potty", None,
            has_extraction_modes=True,
            active_aav_lower=frozenset({"diaper", "bib"}),
        ) == "taxonomy_gap"

    def test_taxonomy_gap_takes_precedence_over_source(self):
        # Even when system source is csv-direct, an out-of-vocab gold is
        # still a taxonomy_gap (vocabulary fix, not policy fix).
        assert classify_kind(
            "fn", "potty", None,
            has_extraction_modes=True,
            active_aav_lower=frozenset({"diaper"}),
            system_source="text",
        ) == "taxonomy_gap"

    def test_no_kind_for_tp_or_tn(self):
        assert classify_kind(
            "tp", "kids", "kids",
            has_extraction_modes=True, active_aav_lower=self.AAV,
            system_source="regex_extract",
        ) is None
        assert classify_kind(
            "tn", None, None,
            has_extraction_modes=True, active_aav_lower=self.AAV,
        ) is None

    def test_fp_with_csv_source_is_policy_divergence(self):
        # System invented a value where gold says null. If the value came
        # from csv_direct, that's upstream-data, not extraction.
        assert classify_kind(
            "fp", None, "female",
            has_extraction_modes=True,
            active_aav_lower=frozenset({"male", "female", "unisex"}),
            system_source="text",
        ) == "policy_divergence"

    def test_fp_with_extraction_source_is_extraction_error(self):
        assert classify_kind(
            "fp", None, "play",
            has_extraction_modes=True,
            active_aav_lower=frozenset({"play"}),
            system_source="regex_extract",
        ) == "extraction_error"

    def test_case_insensitive_aav_lookup(self):
        assert classify_kind(
            "fn", "Adult", None,
            has_extraction_modes=True,
            active_aav_lower=frozenset({"adult"}),
        ) == "extraction_error"


class TestClassifyPairs:

    AAV = frozenset({"kids", "teen", "adult", "infant", "toddler"})

    def test_top_mismatches_sorted_by_count(self):
        # All quadruples carry source=regex_extract -> extraction_error.
        quads = [
            ("p1", "kids", "teen", "regex_extract"),
            ("p2", "kids", "teen", "regex_extract"),
            ("p3", "adult", None, None),
            ("p4", None, "kids", "regex_extract"),
            ("p5", "kids", "kids", "regex_extract"),  # TP
            ("p6", "kids", "teen", "regex_extract"),
        ]
        counts, mismatches = _classify_pairs(
            quads,
            has_extraction_modes=True,
            active_aav_lower=self.AAV,
        )
        assert counts.tp == 1
        assert counts.fp == 1
        assert counts.fn == 1
        assert counts.mismatch == 3
        assert counts.tn == 0
        assert counts.extraction_error == 5
        assert counts.policy_divergence == 0
        assert counts.taxonomy_gap == 0
        assert mismatches[0].gold == "kids"
        assert mismatches[0].system == "teen"
        assert mismatches[0].count == 3
        assert mismatches[0].kind == "extraction_error"
        assert mismatches[0].sample_product_ids == ["p1", "p2", "p6"]

    def test_sample_product_ids_capped_at_five(self):
        quads = [(f"p{i}", "kids", "teen", "regex_extract") for i in range(8)]
        _, mismatches = _classify_pairs(
            quads,
            has_extraction_modes=True,
            active_aav_lower=self.AAV,
        )
        assert mismatches[0].count == 8
        assert mismatches[0].sample_product_ids == [f"p{i}" for i in range(5)]

    def test_no_extraction_modes_tags_policy_divergence(self):
        quads = [
            ("p1", "unisex", "female", "text"),
            ("p2", "unisex", "male", "text"),
        ]
        counts, mismatches = _classify_pairs(
            quads,
            has_extraction_modes=False,
            active_aav_lower=frozenset({"male", "female", "unisex"}),
        )
        assert counts.mismatch == 2
        assert counts.policy_divergence == 2
        assert counts.extraction_error == 0
        assert all(m.kind == "policy_divergence" for m in mismatches)

    def test_csv_source_overrides_extraction_modes_to_policy_divergence(self):
        # Modes configured (e.g., age_group has csv_direct + regex_extract +
        # contextual_defaults) but the system value came from csv_direct.
        # Dispatcher precedence prevents extraction modes from overriding,
        # so disagreement is policy, not extraction.
        quads = [
            ("p1", "adult", "teen", "text"),       # csv-direct -> policy
            ("p2", "kids", "play", "regex_extract"),  # extraction
        ]
        counts, mismatches = _classify_pairs(
            quads,
            has_extraction_modes=True,
            active_aav_lower=frozenset({"adult", "teen", "kids", "play"}),
        )
        assert counts.policy_divergence == 1
        assert counts.extraction_error == 1
        kinds = {(m.gold, m.system): m.kind for m in mismatches}
        assert kinds[("adult", "teen")] == "policy_divergence"
        assert kinds[("kids", "play")] == "extraction_error"

    def test_same_pair_split_by_kind_when_sources_differ(self):
        # Two products with gold=adult, sys=teen -- one from csv, one from regex.
        # They should be reported as TWO separate mismatch rows because the
        # kinds differ (and thus the routing decision differs).
        quads = [
            ("p1", "adult", "teen", "text"),
            ("p2", "adult", "teen", "regex_extract"),
        ]
        counts, mismatches = _classify_pairs(
            quads,
            has_extraction_modes=True,
            active_aav_lower=frozenset({"adult", "teen"}),
        )
        assert counts.policy_divergence == 1
        assert counts.extraction_error == 1
        # Two separate rows -- the (adult, teen) pair is split by kind.
        assert len(mismatches) == 2
        kinds = sorted(m.kind for m in mismatches)
        assert kinds == ["extraction_error", "policy_divergence"]


# ======================================================================
# DB integration (one end-to-end test)
# ======================================================================


def _ws(db) -> Workspace:
    ws = Workspace(name="eval-ws", slug="eval-ws")
    db.add(ws)
    db.flush()
    return ws


def _product_with_attrs(
    db, ws: Workspace, pid: str, attrs: dict[str, str],
) -> Product:
    p = Product(workspace_id=ws.id, product_id=pid, sku=pid, name=pid)
    db.add(p)
    db.flush()
    for k, v in attrs.items():
        db.add(ProductAttribute(
            product_id=p.id, attribute_id=k, attribute_value=v,
        ))
    db.flush()
    return p


class TestEvaluateAttributes:

    def test_end_to_end(self, db):
        ws = _ws(db)
        # System answers for 4 products on age_group / gender.
        _product_with_attrs(db, ws, "P1", {"age_group": "kids", "gender": "unisex"})
        _product_with_attrs(db, ws, "P2", {"age_group": "teen"})  # gender missing
        _product_with_attrs(db, ws, "P3", {"age_group": "kids", "gender": "female"})
        # P4 exists in DB but has no PA rows.
        _product_with_attrs(db, ws, "P4", {})
        # P5 doesn't exist in DB at all.

        gold_products = [
            {"product_id": "P1", "labels": {"age_group": "kids",   "gender": "unisex"}, "is_pilot": True},
            {"product_id": "P2", "labels": {"age_group": "kids",   "gender": "unisex"}, "is_pilot": False},  # mismatch + FN
            {"product_id": "P3", "labels": {"age_group": "kids",   "gender": "unisex"}, "is_pilot": False},  # TP + mismatch
            {"product_id": "P4", "labels": {"age_group": None,     "gender": None},     "is_pilot": False},  # TN + TN
            {"product_id": "P5", "labels": {"age_group": "adult",  "gender": "female"}, "is_pilot": False},  # FN + FN
            # Unlabeled: must be ignored.
            {"product_id": "Px", "labels": None, "is_pilot": False},
        ]

        rep = evaluate_attributes(
            db, workspace_id=ws.id, gold_products=gold_products,
            attributes=["age_group", "gender"],
        )

        assert rep.labeled_total == 5
        assert rep.gold_total == 6

        ag = rep.per_attribute["age_group"].counts
        # P1: TP, P2: mismatch (gold=kids, sys=teen), P3: TP, P4: TN, P5: FN
        assert (ag.tp, ag.tn, ag.fp, ag.fn, ag.mismatch) == (2, 1, 0, 1, 1)

        gd = rep.per_attribute["gender"].counts
        # P1: TP, P2: FN (gold=unisex, sys=null), P3: mismatch (unisex/female),
        # P4: TN, P5: FN
        assert (gd.tp, gd.tn, gd.fp, gd.fn, gd.mismatch) == (1, 1, 0, 2, 1)

    def test_pilot_only_filter(self, db):
        ws = _ws(db)
        _product_with_attrs(db, ws, "P1", {"age_group": "kids"})
        _product_with_attrs(db, ws, "P2", {"age_group": "teen"})

        gold_products = [
            {"product_id": "P1", "labels": {"age_group": "kids"},  "is_pilot": True},
            {"product_id": "P2", "labels": {"age_group": "kids"},  "is_pilot": False},
        ]
        rep = evaluate_attributes(
            db, workspace_id=ws.id, gold_products=gold_products,
            attributes=["age_group"], pilot_only=True,
        )
        assert rep.labeled_total == 1
        assert rep.per_attribute["age_group"].counts.tp == 1

    def test_unlabeled_products_ignored(self, db):
        ws = _ws(db)
        _product_with_attrs(db, ws, "P1", {"age_group": "kids"})

        gold_products = [
            {"product_id": "P1", "labels": {"age_group": "kids"}},
            {"product_id": "P2", "labels": None},
            {"product_id": "P3"},  # no labels key at all
        ]
        rep = evaluate_attributes(
            db, workspace_id=ws.id, gold_products=gold_products,
            attributes=["age_function" if False else "age_group"],
        )
        assert rep.labeled_total == 1

    def test_aggregate_disagreement_rate(self, db):
        ws = _ws(db)
        _product_with_attrs(db, ws, "P1", {"age_group": "kids", "gender": "unisex"})
        _product_with_attrs(db, ws, "P2", {"age_group": "teen"})  # missing gender

        gold_products = [
            {"product_id": "P1", "labels": {"age_group": "kids",  "gender": "unisex"}},
            {"product_id": "P2", "labels": {"age_group": "kids",  "gender": "unisex"}},
        ]
        rep = evaluate_attributes(
            db, workspace_id=ws.id, gold_products=gold_products,
            attributes=["age_group", "gender"],
        )
        # 4 slots total: 1 TP, 1 mismatch (age teen vs kids), 1 TP (gender),
        # 1 FN (gender). 2 disagreements out of 4 = 50%.
        assert rep.aggregate_disagreement_rate == pytest.approx(0.5)

    def test_eval_does_not_mutate_db(self, db):
        ws = _ws(db)
        _product_with_attrs(db, ws, "P1", {"age_group": "kids"})

        before_pa = db.query(ProductAttribute).count()
        before_prod = db.query(Product).count()

        evaluate_attributes(
            db, workspace_id=ws.id,
            gold_products=[{"product_id": "P1", "labels": {"age_group": "kids"}}],
            attributes=["age_group"],
        )

        assert db.query(ProductAttribute).count() == before_pa
        assert db.query(Product).count() == before_prod
