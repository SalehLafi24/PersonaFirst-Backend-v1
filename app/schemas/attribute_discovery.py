"""Schemas for attribute discovery — proposing entirely new attributes.

Separate from the proposed-value pipeline (which proposes new values for
existing attributes). This pipeline proposes new *dimensions* that the
taxonomy doesn't cover yet.
"""
from pydantic import BaseModel


class ProposedAttribute(BaseModel):
    """A single proposed attribute surfaced by the discovery layer."""
    attribute_name: str
    confidence: float
    description: str
    evidence: list[str]
    suggested_values: list[str]
    suggested_class_name: str
    suggested_targeting_mode: str


class AttributeDiscoveryOutput(BaseModel):
    """Output of a single attribute-discovery run on one product."""
    proposed_attributes: list[ProposedAttribute] = []
