"""Attribute extraction quality eval against the gold sample.

Compares each gold-labeled product's `labels` against the system's current
ProductAttribute rows. Per-attribute counts (TP/FP/FN/TN/mismatch), top
mismatch pairs, AND per-disagreement-kind tagging so the operator can
route work without misreading aggregate signal.

Boundary:
  - Pure read. Never writes.
  - Classification helpers are pure (no DB) so most of the logic is
    testable without seeded fixtures.
  - DB-aware function `evaluate_attributes` only loads ProductAttribute
    rows for the gold-labeled products and runs the pure classifier.

Outcome (per (product, attribute) pair):
  TP        gold == system, both non-null
  TN        gold is null, system is null
  FP        gold is null, system is non-null   (system invented a value)
  FN        gold is non-null, system is null   (system missed a value)
  Mismatch  gold and system both non-null but different values

Disagreement kind (only set when outcome is fp/fn/mismatch):
  extraction_error    A change to extraction-layer code (regex / LLM /
                      contextual_defaults) could fix this disagreement.
                      Either the system has no value and an extraction
                      mode is configured, OR the system's value came
                      from an extraction-layer source.
  policy_divergence   Disagreement reflects editorial-vs-upstream-
                      source-of-truth, not an extraction bug.
                      Triggered in two cases:
                        (1) The attribute has NO extraction layer
                            (modes==[]); gender and product_type today.
                        (2) The system's value came from a non-
                            extraction source (csv_direct / 'text'),
                            and dispatcher precedence prevents
                            extraction modes from overriding it. The
                            "fix" requires changing upstream data or
                            building override architecture, not
                            extraction code.
  taxonomy_gap        Gold uses a value that isn't in the workspace's
                      active AAVs. The system literally cannot produce
                      it regardless of extraction quality. Routing is
                      to taxonomy_admin.

Metrics:
  agreement_rate    (TP + TN) / total
  disagreement_rate (FP + FN + Mismatch) / total           -- gating
  precision         TP / (TP + FP + Mismatch)
  recall            TP / (TP + FN + Mismatch)

The gating metric to compare against improvement targets is
`extraction_disagreement_rate` (per attribute or aggregate), which counts
ONLY the extraction_error subset of disagreements. A 35% raw
disagreement made up entirely of policy_divergence is not the same as a
35% raw disagreement made up of extraction errors.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Literal

from sqlalchemy.orm import Session

from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import ProposedAttributeValueEvent


Outcome = Literal["tp", "tn", "fp", "fn", "mismatch"]
DisagreementKind = Literal["extraction_error", "policy_divergence", "taxonomy_gap"]


# Source-mode strings recognised as extraction-layer in the events table.
# 'text' is legacy csv_direct (used by import_gender_on_v3.py and the
# bulk CSV importer); it is NOT extraction-layer. 'csv_direct' would be
# the same. Anything else is treated as extraction-layer for tagging.
_EXTRACTION_SOURCES = frozenset({
    "regex_extract", "contextual_defaults", "llm_evidence",
})
_NON_EXTRACTION_SOURCES = frozenset({"text", "csv_direct"})


def classify(gold: str | None, system: str | None) -> Outcome:
    """Pure classifier. Treats empty string as null."""
    g = gold or None
    s = system or None
    if g is None and s is None:
        return "tn"
    if g is None and s is not None:
        return "fp"
    if g is not None and s is None:
        return "fn"
    return "tp" if g == s else "mismatch"


def classify_kind(
    outcome: Outcome,
    gold: str | None,
    system: str | None,
    *,
    has_extraction_modes: bool,
    active_aav_lower: frozenset[str],
    system_source: str | None = None,
) -> DisagreementKind | None:
    """Pure kind classifier. None when outcome is tp/tn (no disagreement).

    Decision order:
      1. tp / tn -> None
      2. gold non-null AND gold not in active AAVs -> taxonomy_gap
         (system can't produce this value at all)
      3. system value came from a non-extraction source ('text' /
         'csv_direct') -> policy_divergence (dispatcher precedence
         prevents extraction modes from overriding)
      4. attribute has no extraction modes -> policy_divergence
      5. otherwise -> extraction_error

    `system_source` is the `source` field of the event that produced
    the system's value. None when there is no system value (FN), in
    which case we fall through to step 4/5.
    """
    if outcome in ("tp", "tn"):
        return None
    if gold is not None and gold.lower() not in active_aav_lower:
        return "taxonomy_gap"
    if system_source is not None and system_source in _NON_EXTRACTION_SOURCES:
        return "policy_divergence"
    if not has_extraction_modes:
        return "policy_divergence"
    return "extraction_error"


@dataclass
class AttributeEvalCounts:
    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0
    mismatch: int = 0
    # Disagreement kind tags (sum is fp + fn + mismatch).
    extraction_error: int = 0
    policy_divergence: int = 0
    taxonomy_gap: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn + self.mismatch

    @property
    def agreement_rate(self) -> float | None:
        return (self.tp + self.tn) / self.total if self.total else None

    @property
    def disagreement_rate(self) -> float | None:
        if not self.total:
            return None
        return (self.fp + self.fn + self.mismatch) / self.total

    @property
    def extraction_disagreement_rate(self) -> float | None:
        """Engineering-actionable subset. Excludes policy_divergence and
        taxonomy_gap. This is the gating metric for extraction quality."""
        if not self.total:
            return None
        return self.extraction_error / self.total

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp + self.mismatch
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn + self.mismatch
        return self.tp / denom if denom else None


@dataclass
class MismatchPair:
    """Gold/system pair across products. `count` is how many gold products
    showed this pair; `sample_product_ids` is up to 5 examples."""
    gold: str | None
    system: str | None
    count: int
    kind: DisagreementKind
    sample_product_ids: list[str] = field(default_factory=list)


@dataclass
class AttributeEvalReport:
    attribute: str
    has_extraction_modes: bool
    counts: AttributeEvalCounts
    top_mismatches: list[MismatchPair]


@dataclass
class EvalReport:
    workspace_id: int
    gold_total: int
    labeled_total: int
    pilot_only: bool
    attributes_evaluated: list[str]
    per_attribute: dict[str, AttributeEvalReport]

    @property
    def aggregate_disagreement_rate(self) -> float | None:
        total_slots = sum(r.counts.total for r in self.per_attribute.values())
        if not total_slots:
            return None
        total_disagree = sum(
            r.counts.fp + r.counts.fn + r.counts.mismatch
            for r in self.per_attribute.values()
        )
        return total_disagree / total_slots

    @property
    def aggregate_extraction_disagreement_rate(self) -> float | None:
        """Aggregate extraction-only disagreement. The metric to compare
        against improvement targets; ignores policy_divergence and
        taxonomy_gap."""
        total_slots = sum(r.counts.total for r in self.per_attribute.values())
        if not total_slots:
            return None
        total_extr = sum(
            r.counts.extraction_error for r in self.per_attribute.values()
        )
        return total_extr / total_slots


def _classify_pairs(
    pairs: Iterable[tuple[str, str | None, str | None, str | None]],
    *,
    has_extraction_modes: bool,
    active_aav_lower: frozenset[str],
) -> tuple[AttributeEvalCounts, list[MismatchPair]]:
    """Pure aggregation over (product_id, gold, system, system_source)
    quadruples. Returns counts plus the top mismatch pairs (any
    non-tp/tn outcome) tagged with disagreement kind.

    `system_source` is the source field of the event that produced
    the system value (None when system is None or no event found).
    Used to distinguish csv-direct disagreements (policy_divergence)
    from extraction-layer disagreements (extraction_error).
    """
    counts = AttributeEvalCounts()
    bucket_counts: Counter = Counter()
    # Note: kind is keyed by (gold, system, source_kind_class) because two
    # products with the same (gold, system) pair can have different sources
    # if the system value came from different modes. In practice for
    # cardinality=single this is rare, but we bucket conservatively.
    bucket_kind: dict[tuple, DisagreementKind] = {}
    bucket_examples: dict[tuple, list[str]] = defaultdict(list)
    for product_id, gold, system, system_source in pairs:
        outcome = classify(gold, system)
        setattr(counts, outcome, getattr(counts, outcome) + 1)
        if outcome in ("fp", "fn", "mismatch"):
            kind = classify_kind(
                outcome, gold, system,
                has_extraction_modes=has_extraction_modes,
                active_aav_lower=active_aav_lower,
                system_source=system_source,
            )
            assert kind is not None
            setattr(counts, kind, getattr(counts, kind) + 1)
            # Bucket key includes kind so a (gold,system) pair that splits
            # across kinds (extraction_error vs policy_divergence) is
            # reported as separate rows.
            key = (gold or None, system or None, kind)
            bucket_counts[key] += 1
            bucket_kind[key] = kind
            if len(bucket_examples[key]) < 5:
                bucket_examples[key].append(product_id)
    pairs_out = [
        MismatchPair(gold=g, system=s, count=n,
                     kind=bucket_kind[(g, s, k)],
                     sample_product_ids=list(bucket_examples[(g, s, k)]))
        for (g, s, k), n in bucket_counts.most_common()
    ]
    return counts, pairs_out


def evaluate_attributes(
    db: Session,
    *,
    workspace_id: int,
    gold_products: list[dict],
    attributes: list[str],
    pilot_only: bool = False,
    top_n_mismatches: int = 5,
    manifest=None,
) -> EvalReport:
    """Compute per-attribute eval against the gold sample.

    `manifest` is optional; when provided, each attribute's
    `input.modes` determines whether disagreements tag as
    extraction_error or policy_divergence. When omitted, all
    disagreements that aren't taxonomy_gap default to extraction_error.
    """
    if manifest is None:
        from app.services.attribute_engine import load_manifest
        manifest = load_manifest()

    eligible = [p for p in gold_products if p.get("labels")]
    if pilot_only:
        eligible = [p for p in eligible if p.get("is_pilot")]
    labeled_total = len(eligible)

    if not eligible:
        return EvalReport(
            workspace_id=workspace_id,
            gold_total=len(gold_products),
            labeled_total=0,
            pilot_only=pilot_only,
            attributes_evaluated=list(attributes),
            per_attribute={},
        )

    gold_pids = [p["product_id"] for p in eligible]

    products_by_pid: dict[str, Product] = {
        p.product_id: p for p in db.query(Product).filter(
            Product.workspace_id == workspace_id,
            Product.product_id.in_(gold_pids),
        ).all()
    }
    db_ids = [p.id for p in products_by_pid.values()]

    pa_rows = db.query(ProductAttribute).filter(
        ProductAttribute.product_id.in_(db_ids),
        ProductAttribute.attribute_id.in_(attributes),
    ).all() if db_ids else []
    sys_by_dbid_attr: dict[tuple[int, str], str] = {
        (pa.product_id, pa.attribute_id): pa.attribute_value
        for pa in pa_rows
    }

    # Source-mode lookup: for each (product, attribute) where the system
    # has a value, find the highest-confidence event whose normalized_value
    # matches the PA row's value. That's the event backfill picked
    # (highest_confidence strategy) and its `source` tells us whether the
    # decision came from an extraction layer or csv-direct.
    event_source_by_pid_attr_value: dict[tuple[str, str, str], str] = {}
    if gold_pids and attributes:
        ev_rows = db.query(
            ProposedAttributeValueEvent.product_id,
            ProposedAttributeValueEvent.attribute_name,
            ProposedAttributeValueEvent.normalized_value,
            ProposedAttributeValueEvent.source,
            ProposedAttributeValueEvent.confidence,
        ).filter(
            ProposedAttributeValueEvent.workspace_id == workspace_id,
            ProposedAttributeValueEvent.product_id.in_(gold_pids),
            ProposedAttributeValueEvent.attribute_name.in_(attributes),
        ).all()
        # Pick highest-confidence event per (pid, attr, value).
        for pid, attr, val, source, conf in ev_rows:
            if not val:
                continue
            key = (pid, attr, val)
            existing = event_source_by_pid_attr_value.get(key)
            if existing is None or (conf or 0) > existing[1]:
                event_source_by_pid_attr_value[key] = (source, float(conf or 0))
    # Flatten to {(pid, attr, value): source}.
    sys_source_lookup: dict[tuple[str, str, str], str] = {
        k: v[0] for k, v in event_source_by_pid_attr_value.items()
    }

    # Per-attribute lookups: extraction modes + active AAV vocabulary.
    aav_rows = db.query(
        AttributeAllowedValue.attribute_name,
        AttributeAllowedValue.value,
    ).filter(
        AttributeAllowedValue.workspace_id == workspace_id,
        AttributeAllowedValue.attribute_name.in_(attributes),
        AttributeAllowedValue.is_active == True,  # noqa: E712
    ).all() if attributes else []
    aav_by_attr: dict[str, set[str]] = defaultdict(set)
    for attr_name, value in aav_rows:
        if value:
            aav_by_attr[attr_name].add(value.lower())

    per_attribute: dict[str, AttributeEvalReport] = {}
    for attr in attributes:
        entry = manifest.entries.get(attr)
        has_modes = bool(entry.modes) if entry is not None else True
        aav_lower = frozenset(aav_by_attr.get(attr, set()))

        quads: list[tuple[str, str | None, str | None, str | None]] = []
        for p in eligible:
            pid = p["product_id"]
            gold_val = (p.get("labels") or {}).get(attr)
            prod = products_by_pid.get(pid)
            sys_val: str | None = None
            if prod is not None:
                sys_val = sys_by_dbid_attr.get((prod.id, attr))
            sys_source: str | None = None
            if sys_val is not None:
                sys_source = sys_source_lookup.get((pid, attr, sys_val))
            quads.append((pid, gold_val, sys_val, sys_source))
        counts, mismatches = _classify_pairs(
            quads,
            has_extraction_modes=has_modes,
            active_aav_lower=aav_lower,
        )
        per_attribute[attr] = AttributeEvalReport(
            attribute=attr,
            has_extraction_modes=has_modes,
            counts=counts,
            top_mismatches=mismatches[:top_n_mismatches],
        )

    return EvalReport(
        workspace_id=workspace_id,
        gold_total=len(gold_products),
        labeled_total=labeled_total,
        pilot_only=pilot_only,
        attributes_evaluated=list(attributes),
        per_attribute=per_attribute,
    )
