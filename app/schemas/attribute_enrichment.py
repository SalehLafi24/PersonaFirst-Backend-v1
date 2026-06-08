from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, model_validator


class TargetingMode(str, Enum):
    CATEGORICAL_AFFINITY = "categorical_affinity"
    COMPATIBILITY_SIGNAL = "compatibility_signal"
    CATEGORICAL_FILTER = "categorical_filter"
    DESCRIPTIVE_METADATA = "descriptive_metadata"


DEFAULT_TARGETING_BY_CLASS: dict[str, str] = {
    "descriptive_literal": "categorical_affinity",
    "contextual_semantic": "categorical_affinity",
    "compatibility": "compatibility_signal",
    "taxonomy_discovery": "categorical_affinity",
}


class AttributeBehavior(BaseModel):
    taxonomy_sensitive: bool = False
    ordered_values: bool = False
    can_propose_values: bool = False
    multi_value_allowed: bool = False
    prefer_conservative_inference: bool = True
    # When ordered_values is True, value_order defines the canonical scale used
    # to compute mismatch severity in compatibility scoring. The list is treated
    # as an ordered axis from low → high.
    value_order: list[str] | None = None
    # When True, the recommendation engine penalises products whose value for
    # this attribute clearly mismatches the customer's compatibility signal.
    # When False (default), only positive matches contribute — preserving
    # pre-existing compatibility behaviour for callers that don't opt in.
    negative_scoring_enabled: bool = False


class AttributeDefinition(BaseModel):
    name: str
    object_type: Literal["product", "customer"]
    class_name: Literal[
        "descriptive_literal",
        "contextual_semantic",
        "compatibility",
        "taxonomy_discovery",
    ]
    value_mode: Literal["single", "multi", "boolean"]
    allowed_values: list[str] | None = None
    description: str
    evidence_sources: list[str]
    behavior: AttributeBehavior
    targeting_mode: TargetingMode

    @model_validator(mode="before")
    @classmethod
    def _assign_default_targeting_mode(cls, values: dict) -> dict:
        if isinstance(values, dict) and "targeting_mode" not in values:
            class_name = values.get("class_name")
            if class_name in DEFAULT_TARGETING_BY_CLASS:
                values["targeting_mode"] = DEFAULT_TARGETING_BY_CLASS[class_name]
        return values


class EnrichmentRequest(BaseModel):
    attribute: AttributeDefinition
    obj: dict


class ProposedValue(BaseModel):
    """A taxonomy-evolution candidate.

    Emitted when the model finds a value that is clearly supported by the
    object data but does NOT exist in the attribute's allowed_values list.
    These never flow into `values` — they surface as suggestions for
    extending the taxonomy.
    """
    value: str
    confidence: float
    evidence: list[str] = []


class EnrichmentResult(BaseModel):
    attribute_name: str
    value: Any
    confidence: float
    evidence: list[str]
    proposed_values: list[ProposedValue] | None = None


# ---------------------------------------------------------------------------
# Multi-source enrichment schema
#
# Used by the visual enrichment service and the text/visual merge service.
# Kept parallel to (not replacing) EnrichmentResult so that existing text
# enrichment callers continue to work unchanged.
# ---------------------------------------------------------------------------


class EnrichmentSource(str, Enum):
    TEXT = "text"
    VISUAL = "visual"
    MERGED = "merged"


class EnrichedValue(BaseModel):
    value: Any
    confidence: float
    evidence: list[str] = []
    reasoning_mode: str | None = None
    source: EnrichmentSource
    contributing_sources: list[EnrichmentSource] = []


class EnrichmentOutput(BaseModel):
    attribute_name: str
    attribute_class: str
    values: list[EnrichedValue] = []
    proposed_values: list[ProposedValue] = []
    warnings: list[str] = []
    source: EnrichmentSource
