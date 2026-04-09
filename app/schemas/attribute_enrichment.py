from typing import Any, Literal

from pydantic import BaseModel


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


class EnrichmentRequest(BaseModel):
    attribute: AttributeDefinition
    obj: dict


class EnrichmentResult(BaseModel):
    attribute_name: str
    value: Any
    confidence: float
    evidence: list[str]
    proposed_values: list[str] | None = None
