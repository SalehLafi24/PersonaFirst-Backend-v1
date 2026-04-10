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


class EnrichmentResult(BaseModel):
    attribute_name: str
    value: Any
    confidence: float
    evidence: list[str]
    proposed_values: list[str] | None = None
