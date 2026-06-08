"""Attribute manifest loader.

The manifest declares per-attribute config: taxonomy, input modes, proposal
gates, backfill strategy. The engine reads this single source of truth to
process any attribute through the same pipeline.

A new attribute is added by appending an entry here and (for closed
taxonomies) a normalization block in seed_data/attribute_normalization.json.
No code changes required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_PATH = (
    Path(__file__).resolve().parents[3] / "seed_data" / "attribute_manifest.json"
)


_VALID_KINDS = frozenset({"closed", "open"})
_VALID_CARDINALITY = frozenset({"single", "multi"})
_VALID_MODES = frozenset({
    "csv_direct", "regex_extract", "llm_evidence",
    # Phase A: contextual defaults fire only when no event from prior
    # modes exists for the candidate. Generic mechanism for "if no
    # explicit signal, default by product_type" (and similar).
    "contextual_defaults",
})

# Backfill multi-event resolution policies. "highest_confidence" is the
# default and only recommended policy.
#
# "most_specific" is EXPERIMENTAL and NOT recommended for production. The
# Phase A pilot enabled it on age_group and reverted it: on "Probiotic
# Gummies For Kids" the regex layer emitted toddler/kids/teen events all
# at conf=0.98; specificity-band ranking picked toddler, but the gold
# label was kids (driven by the explicit "For Kids" phrase in the title).
# Specificity ranking can't distinguish "named age band in title" from
# "age range covered by product". The correct fix is phrase-aware
# extraction at the regex layer, not a backfill-time resolution policy.
# Schema is kept so the mechanism can be re-evaluated on attributes where
# specificity *is* the dominant signal (none today).
_VALID_MULTI_EVENT_RESOLUTIONS = frozenset({"highest_confidence", "most_specific"})


@dataclass(frozen=True)
class TaxonomySpec:
    kind: str
    cardinality: str
    normalization_ref: str | None
    unmatched_policy: str
    allowed_values: list[str]


@dataclass(frozen=True)
class CsvDirectSpec:
    csv_path: str
    product_id_column: str
    source_columns: list[str]


@dataclass(frozen=True)
class RegexPattern:
    pattern: str
    value: str
    confidence: float
    # Phase A: don't fire this pattern when the candidate's existing
    # product_type ProductAttribute is in this set. Used to suppress
    # over-extraction from bundled-component tokens (e.g., the literal
    # word "Plush" inside an apparel product name shouldn't fire
    # `use_case=play`). Empty tuple = no guard. Evaluated at event-emit
    # time against pre-existing ProductAttribute rows; if product_type
    # isn't yet populated, the guard is best-effort (pattern fires).
    block_when_product_type_in: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegexExtractSpec:
    fields: list[str]
    patterns: list[RegexPattern]


@dataclass(frozen=True)
class ContextualDefault:
    """One conditional default rule.

    Fires only when the candidate's existing product_type ProductAttribute
    is in `if_product_type_in` AND no event exists for the target
    attribute from earlier modes (the dispatcher's precedence skip-set
    enforces the second condition automatically).

    Generic mechanism. Examples:
      - age_group: if product_type in {supplement, feminine_care} -> adult @0.65
      - lifecycle_stage: if product_type in {nursing_pillow, breast_pump} -> pregnancy
      - seasonality: if product_type in {swim_accessory} -> summer
    """
    if_product_type_in: tuple[str, ...]
    value: str
    confidence: float
    rationale: str | None = None


@dataclass(frozen=True)
class ContextualDefaultsSpec:
    rules: tuple[ContextualDefault, ...]


@dataclass(frozen=True)
class LlmEvidenceSpec:
    fields: list[str]
    class_name: str
    max_objects_per_run: int | None


@dataclass(frozen=True)
class ProposalSpec:
    confidence_min: float
    max_values_per_object: int
    require_evidence: bool


@dataclass(frozen=True)
class ApprovalSpec:
    min_proposal_count: int
    min_distinct_objects: int
    min_avg_confidence: float


@dataclass(frozen=True)
class BackfillSpec:
    strategy: str
    single_row_per_object: bool
    idempotent: bool
    # Phase A: how backfill resolves multiple events per (product, attribute)
    # pair when cardinality is single. Default mirrors legacy behaviour.
    multi_event_resolution: str = "highest_confidence"
    # Required when multi_event_resolution=="most_specific". An ordered
    # list of allowed_values from most specific (index 0) to least
    # specific. Backfill picks the event whose value has the lowest
    # specificity index; confidence is the tie-breaker.
    specificity_order: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecommendationSpec:
    """How an attribute participates in recommendation services.

    Concrete fields, each with a single consumer:

      persona_relevant   read by persona_extraction_service to decide
                         whether to include this attribute in distribution
                         + focus + confidence math.
      score_weight       read by customer_recommendation_service. Weights
                         of all persona_relevant attributes must sum to
                         1.0 (validator-enforced).
      usage              read by future signals that branch on cohort vs
                         ranking vs filter behaviour. Existing field;
                         retained for backwards compatibility.
      signal_tags        opt-in list of intent-layer signal tags. Each
                         tag corresponds 1:1 to a registered signal that
                         consumes attributes by tag (multi-tenant friendly:
                         a new client tags their own attributes; the
                         signal applies automatically).

                         Today the only registered tag is `saturatable`,
                         consumed by SaturationDemoter. The validator
                         rejects unknown tags by reading from the
                         intent_layer_service signal-tag registry at
                         load time.

      weight_reason      Free-text documentation of WHY the current
                         score_weight is what it is. Stored in JSON as
                         `_weight_reason` (leading underscore = doc
                         convention). PURELY DOCUMENTATION -- no service
                         reads it for behaviour. Examples:
                           "initial_manual_tuning_v1"
                           "suggester_v1"
                           "fashion_team_override_2026Q2"
                         The weight_suggester CLI sets this to
                         "suggester_v1" when --apply rewrites a weight.
    """
    persona_relevant: bool
    score_weight: float
    usage: str | None
    signal_tags: tuple[str, ...] = ()
    weight_reason: str | None = None


@dataclass(frozen=True)
class AttributeManifestEntry:
    name: str
    object_type: str
    taxonomy: TaxonomySpec
    modes: list[str]
    precedence: list[str]
    csv_direct: CsvDirectSpec | None
    regex_extract: RegexExtractSpec | None
    llm_evidence: LlmEvidenceSpec | None
    contextual_defaults: ContextualDefaultsSpec | None
    proposal: ProposalSpec
    approval: ApprovalSpec
    backfill: BackfillSpec
    default_policy: dict[str, Any]
    # New: dedicated recommendation block. `recommendation_usage` kept as
    # a convenience alias matching `recommendation.usage`.
    recommendation: RecommendationSpec
    recommendation_usage: str | None


@dataclass
class AttributeManifest:
    version: str
    entries: dict[str, AttributeManifestEntry] = field(default_factory=dict)

    def get(self, name: str) -> AttributeManifestEntry:
        if name not in self.entries:
            raise KeyError(
                f"attribute {name!r} not in manifest "
                f"(known: {sorted(self.entries)})"
            )
        return self.entries[name]

    def names(self) -> list[str]:
        return sorted(self.entries)

    def persona_relevant_attributes(self) -> list[str]:
        """Attributes the persona service should include in its distributions.

        Reads `recommendation.persona_relevant` from each entry. Order is
        manifest declaration order, which is also the order weights were
        validated in.
        """
        return [name for name, entry in self.entries.items()
                if entry.recommendation.persona_relevant]

    def score_weights(self) -> dict[str, float]:
        """Weights for persona-relevant attributes; used by the customer
        rec service. Sum is 1.0 (validator-enforced)."""
        return {
            name: entry.recommendation.score_weight
            for name, entry in self.entries.items()
            if entry.recommendation.persona_relevant
        }


def _parse_entry(name: str, raw: dict[str, Any]) -> AttributeManifestEntry:
    tax_raw = raw.get("taxonomy") or {}
    if tax_raw.get("kind") not in _VALID_KINDS:
        raise ValueError(
            f"attribute {name!r}: taxonomy.kind must be one of {sorted(_VALID_KINDS)}"
        )
    if tax_raw.get("cardinality") not in _VALID_CARDINALITY:
        raise ValueError(
            f"attribute {name!r}: taxonomy.cardinality must be one of "
            f"{sorted(_VALID_CARDINALITY)}"
        )
    taxonomy = TaxonomySpec(
        kind=tax_raw["kind"],
        cardinality=tax_raw["cardinality"],
        normalization_ref=tax_raw.get("normalization_ref"),
        unmatched_policy=tax_raw.get("unmatched_policy", "discard"),
        allowed_values=list(tax_raw.get("allowed_values") or []),
    )

    input_raw = raw.get("input") or {}
    modes = list(input_raw.get("modes") or [])
    precedence = list(input_raw.get("precedence") or modes)
    for m in modes:
        if m not in _VALID_MODES:
            raise ValueError(
                f"attribute {name!r}: unknown mode {m!r}; "
                f"must be one of {sorted(_VALID_MODES)}"
            )
    if set(precedence) != set(modes):
        raise ValueError(
            f"attribute {name!r}: precedence list must permute modes list"
        )

    csv_direct = None
    if "csv_direct" in input_raw:
        cd = input_raw["csv_direct"]
        csv_direct = CsvDirectSpec(
            csv_path=cd["csv_path"],
            product_id_column=cd["product_id_column"],
            source_columns=list(cd["source_columns"]),
        )

    regex_extract = None
    if "regex_extract" in input_raw:
        re_raw = input_raw["regex_extract"]
        patterns = [
            RegexPattern(
                pattern=p["pattern"],
                value=p["value"],
                confidence=float(p.get("confidence", 0.85)),
                block_when_product_type_in=tuple(
                    p.get("block_when_product_type_in") or []
                ),
            )
            for p in re_raw.get("patterns", [])
        ]
        regex_extract = RegexExtractSpec(
            fields=list(re_raw.get("fields") or ["name"]),
            patterns=patterns,
        )

    contextual_defaults = None
    if "contextual_defaults" in input_raw:
        cd_raw = input_raw["contextual_defaults"]
        cd_rules = []
        for rule in (cd_raw.get("rules") or []):
            cd_rules.append(ContextualDefault(
                if_product_type_in=tuple(rule.get("if_product_type_in") or []),
                value=str(rule["value"]),
                confidence=float(rule.get("confidence", 0.65)),
                rationale=rule.get("rationale"),
            ))
        contextual_defaults = ContextualDefaultsSpec(rules=tuple(cd_rules))

    llm_evidence = None
    if "llm_evidence" in input_raw:
        ll = input_raw["llm_evidence"]
        llm_evidence = LlmEvidenceSpec(
            fields=list(ll.get("fields") or ["name"]),
            class_name=ll.get("class_name", "contextual_semantic"),
            max_objects_per_run=ll.get("max_objects_per_run"),
        )

    prop_raw = raw.get("proposal") or {}
    proposal = ProposalSpec(
        confidence_min=float(prop_raw.get("confidence_min", 0.5)),
        max_values_per_object=int(prop_raw.get("max_values_per_object", 1)),
        require_evidence=bool(prop_raw.get("require_evidence", True)),
    )

    appr_raw = raw.get("approval") or {}
    approval = ApprovalSpec(
        min_proposal_count=int(appr_raw.get("min_proposal_count", 3)),
        min_distinct_objects=int(appr_raw.get("min_distinct_objects", 2)),
        min_avg_confidence=float(appr_raw.get("min_avg_confidence", 0.85)),
    )

    back_raw = raw.get("backfill") or {}
    multi_event_res = back_raw.get("multi_event_resolution", "highest_confidence")
    if multi_event_res not in _VALID_MULTI_EVENT_RESOLUTIONS:
        raise ValueError(
            f"attribute {name!r}: backfill.multi_event_resolution={multi_event_res!r} "
            f"not in {sorted(_VALID_MULTI_EVENT_RESOLUTIONS)}"
        )
    specificity_order = tuple(back_raw.get("specificity_order") or [])
    if multi_event_res == "most_specific" and not specificity_order:
        raise ValueError(
            f"attribute {name!r}: backfill.multi_event_resolution=most_specific "
            f"requires backfill.specificity_order"
        )
    if multi_event_res == "most_specific" and taxonomy.kind == "closed":
        # Sanity: every allowed_value should appear in specificity_order
        # so backfill always knows where to rank values it sees.
        missing = [v for v in taxonomy.allowed_values if v not in specificity_order]
        if missing:
            raise ValueError(
                f"attribute {name!r}: backfill.specificity_order is missing "
                f"closed-taxonomy values: {missing}"
            )
    backfill = BackfillSpec(
        strategy=back_raw.get("strategy", "highest_confidence"),
        single_row_per_object=bool(back_raw.get("single_row_per_object", True)),
        idempotent=bool(back_raw.get("idempotent", True)),
        multi_event_resolution=multi_event_res,
        specificity_order=specificity_order,
    )

    # Recommendation block. Accepts both the new nested form and the
    # legacy top-level `recommendation_usage` for backwards compatibility.
    rec_raw = raw.get("recommendation") or {}
    legacy_usage = raw.get("recommendation_usage")
    persona_relevant = bool(rec_raw.get("persona_relevant", False))
    score_weight = float(rec_raw.get("score_weight", 0.0))
    usage = rec_raw.get("usage", legacy_usage)
    signal_tags_raw = rec_raw.get("signal_tags") or []
    if not isinstance(signal_tags_raw, list):
        raise ValueError(
            f"attribute {name!r}: recommendation.signal_tags must be a "
            f"list of strings"
        )
    signal_tags = tuple(str(t).strip() for t in signal_tags_raw if str(t).strip())
    if persona_relevant and score_weight <= 0:
        raise ValueError(
            f"attribute {name!r}: recommendation.persona_relevant=true "
            f"requires recommendation.score_weight > 0"
        )
    if not persona_relevant and score_weight != 0.0:
        raise ValueError(
            f"attribute {name!r}: recommendation.score_weight set "
            f"({score_weight}) but persona_relevant is false; "
            f"set persona_relevant=true or remove the weight"
        )
    weight_reason_raw = rec_raw.get("_weight_reason") or rec_raw.get("weight_reason")
    weight_reason = str(weight_reason_raw).strip() if weight_reason_raw else None
    recommendation = RecommendationSpec(
        persona_relevant=persona_relevant,
        score_weight=score_weight,
        usage=usage,
        signal_tags=signal_tags,
        weight_reason=weight_reason,
    )

    return AttributeManifestEntry(
        name=name,
        object_type=raw.get("object_type", "product"),
        taxonomy=taxonomy,
        modes=modes,
        precedence=precedence,
        csv_direct=csv_direct,
        regex_extract=regex_extract,
        llm_evidence=llm_evidence,
        contextual_defaults=contextual_defaults,
        proposal=proposal,
        approval=approval,
        backfill=backfill,
        default_policy=dict(raw.get("default_policy") or {}),
        recommendation=recommendation,
        recommendation_usage=usage,
    )


def load_manifest(path: Path | None = None) -> AttributeManifest:
    """Load and validate the attribute manifest. Single source of truth."""
    p = path or _DEFAULT_PATH
    if not p.exists():
        raise FileNotFoundError(f"attribute manifest not found at {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    entries: dict[str, AttributeManifestEntry] = {}
    for attr_name, block in (raw.get("attributes") or {}).items():
        entries[attr_name] = _parse_entry(attr_name, block)

    # Cross-entry validator: persona-relevant weights must sum to 1.0.
    weights = [e.recommendation.score_weight for e in entries.values()
               if e.recommendation.persona_relevant]
    if weights:
        total = sum(weights)
        if abs(total - 1.0) > 1e-6:
            persona_attrs = [n for n, e in entries.items()
                             if e.recommendation.persona_relevant]
            raise ValueError(
                f"persona_relevant attribute score_weights must sum to 1.0; "
                f"got {total:.6f} across {persona_attrs}"
            )

    # Validator: signal_tags must be in the registered tag set. Imported
    # lazily to avoid a manifest <-> intent_layer import cycle.
    try:
        from app.services.intent_layer_service import REGISTERED_SIGNAL_TAGS
        registered = set(REGISTERED_SIGNAL_TAGS.keys())
    except Exception:
        registered = None  # registry unavailable; skip validation
    if registered is not None:
        for name, entry in entries.items():
            for tag in entry.recommendation.signal_tags:
                if tag not in registered:
                    raise ValueError(
                        f"attribute {name!r}: signal_tag {tag!r} not in "
                        f"registered set {sorted(registered)}. "
                        f"Tags must be added with the consuming signal -- "
                        f"see intent_layer_service.REGISTERED_SIGNAL_TAGS."
                    )

    return AttributeManifest(
        version=str(raw.get("version", "1.0")),
        entries=entries,
    )
