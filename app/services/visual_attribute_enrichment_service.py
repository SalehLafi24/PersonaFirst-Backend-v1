"""Visual attribute enrichment service.

Parallel signal source to the text-based attribute_enrichment_service. Takes
an AttributeDefinition plus an image reference and generates enrichment
output in the same general schema shape. Every value produced here is tagged
with source=visual so downstream merge logic can combine text and visual
results generically.

The service is intentionally agnostic about which multimodal model is used:
callers pass an `analyzer` callable to `generate_visual_enrichment`.
"""

import json
from typing import Callable, Literal

from pydantic import BaseModel

from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
)


# ---------------------------------------------------------------------------
# Generic image input model
# ---------------------------------------------------------------------------


class VisualInput(BaseModel):
    """Generic reference to a single image input for visual enrichment."""

    image_ref: str
    ref_type: Literal["url", "path", "data_uri"] = "url"
    alt_text: str | None = None
    metadata: dict | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_visual_input(visual: VisualInput) -> str:
    lines = [
        f"image_ref : {visual.image_ref}",
        f"ref_type  : {visual.ref_type}",
    ]
    if visual.alt_text:
        lines.append(f"alt_text  : {visual.alt_text}")
    if visual.metadata:
        lines.append(f"metadata  : {json.dumps(visual.metadata, ensure_ascii=False)}")
    return "\n".join(lines)


def _allowed_block(attr: AttributeDefinition) -> str:
    if attr.allowed_values:
        return "\n".join(f"- {v}" for v in attr.allowed_values)
    return "(none provided)"


# ---------------------------------------------------------------------------
# Class-specific visual prompt builders
# ---------------------------------------------------------------------------


def _build_descriptive_literal_visual_prompt(
    attr: AttributeDefinition,
    visual: VisualInput,
) -> str:
    return f"""\
You are a visual attribute extraction engine.

You are determining the value(s) of a single attribute for a product by looking at its image.

--------------------------------
ATTRIBUTE CONTEXT
--------------------------------
Attribute name: {attr.name}
Class: descriptive_literal
Description: {attr.description}

Allowed values:
{_allowed_block(attr)}

Rules:
- Extract only values that are directly and visually observable in the image.
- Do not infer from context, setting, model pose, or implied usage.
- If allowed_values are provided, only return values from that list.
- Map a visually observable cue to an allowed value only when the mapping is direct and unambiguous.
- If no explicit visual cue is clearly visible, return no values.

--------------------------------
CLASS BEHAVIOR (VISUAL)
--------------------------------
- This is a strict visual extraction task, not an inference task.
- Only include values directly supported by visible surface features (color, shape, visible text, printed labels, material texture, visible components).
- Do not convert contextual or implied cues into attribute values.
- When in doubt, exclude the value.

--------------------------------
VISUAL INPUT
--------------------------------
{_format_visual_input(visual)}

--------------------------------
OUTPUT FORMAT (STRICT)
--------------------------------
Return valid JSON only.

{{
  "attribute_name": "string",
  "attribute_class": "descriptive_literal",
  "values": [
    {{
      "value": "string",
      "confidence": 0.0,
      "evidence": ["string"],
      "reasoning_mode": "visual_explicit"
    }}
  ],
  "proposed_values": [],
  "warnings": ["string"]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- Each value must have its own confidence and evidence.
- Evidence must describe specific visible features (e.g., "visible red fabric across the garment body").
- Do not cite text-based facts; evidence must be grounded in what is visible in the image.
- reasoning_mode must always be "visual_explicit"
- proposed_values must be empty
- If signals are ambiguous or conflicting, return:
    values = []
    warnings = ["ambiguous_evidence"]
- Else if nothing is visually observable, return:
    values = []
    warnings = ["no_supported_value_found"]
- Confidence guidelines:
    0.95–1.00 = clearly and unambiguously visible
    0.85–0.94 = visible but partially occluded or lower image quality
    below 0.85 = do not include

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""


def _build_contextual_semantic_visual_prompt(
    attr: AttributeDefinition,
    visual: VisualInput,
) -> str:
    return f"""\
You are a visual attribute extraction engine.

You are determining the value(s) of a single attribute for a product by looking at its image.

--------------------------------
ATTRIBUTE CONTEXT
--------------------------------
Attribute name: {attr.name}
Class: contextual_semantic
Description: {attr.description}

Allowed values:
{_allowed_block(attr)}

Rules:
- Only select values from the allowed_values list.
- Do not invent new values.
- You may infer from the visible scene, setting, styling, or visual mood.
- Only include values that are clearly supported by what is visible.
- Do not assign a value just because the product looks generically performant, comfortable, or well-made.

--------------------------------
CLASS BEHAVIOR (VISUAL)
--------------------------------
- You may infer meaning from the visual scene, background, pose, styling, and visual cues.
- Use visual semantics — what the image implies about intended use, setting, or mood.
- Remain conservative — weak or ambiguous visual signals should not produce a value.
- If multiple values are clearly and independently supported, you may return multiple values.
- Do not include speculative or secondary matches.

--------------------------------
VISUAL INPUT
--------------------------------
{_format_visual_input(visual)}

--------------------------------
OUTPUT FORMAT (STRICT)
--------------------------------
Return valid JSON only.

{{
  "attribute_name": "string",
  "attribute_class": "contextual_semantic",
  "values": [
    {{
      "value": "string",
      "confidence": 0.0,
      "evidence": ["string"],
      "reasoning_mode": "visual_inferred"
    }}
  ],
  "proposed_values": [],
  "warnings": ["string"]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- Each value must have its own confidence and evidence.
- Evidence must describe specific visible scene or styling cues.
- Only include values with strong visual support.
- Values with confidence below 0.80 must not be included.
- All returned values must be from the allowed_values list.
- reasoning_mode must always be "visual_inferred"
- proposed_values must be empty
- If signals are ambiguous or conflicting, return:
    values = []
    warnings = ["ambiguous_evidence"]
- Else if nothing supports a value, return:
    values = []
    warnings = ["no_supported_value_found"]
- Confidence guidelines:
    0.90–1.00 = very strong visual support
    0.80–0.89 = strong inferred visual support
    below 0.80 = do not include

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""


def _build_compatibility_visual_prompt(
    attr: AttributeDefinition,
    visual: VisualInput,
) -> str:
    return f"""\
You are a visual attribute extraction engine.

You are determining the value(s) of a single attribute for a product by looking at its image.

--------------------------------
ATTRIBUTE CONTEXT
--------------------------------
Attribute name: {attr.name}
Class: compatibility
Description: {attr.description}

Allowed values:
{_allowed_block(attr)}

Rules:
- Judge the product's visual suitability for each candidate value.
- Base judgment on visible structural, material, and design cues (construction strength, coverage, fabric weight, visible hardware, visible reinforcement, etc.).
- Do not assume compatibility from general appearance alone.
- If no visual cue clearly supports a value, return no values.

--------------------------------
CLASS BEHAVIOR (VISUAL)
--------------------------------
- This is a visual suitability assessment.
- Use visible construction and design cues to infer how well the product is functionally suited to each candidate value.
- Confidence must reflect the strength of visual evidence, not assumed compatibility.
- If multiple values are independently and strongly supported, you may return more than one.
- If visual signals are weak or ambiguous, return no values.

--------------------------------
VISUAL INPUT
--------------------------------
{_format_visual_input(visual)}

--------------------------------
OUTPUT FORMAT (STRICT)
--------------------------------
Return valid JSON only.

{{
  "attribute_name": "string",
  "attribute_class": "compatibility",
  "values": [
    {{
      "value": "string",
      "confidence": 0.0,
      "evidence": ["string"],
      "reasoning_mode": "visual_suitability"
    }}
  ],
  "proposed_values": [],
  "warnings": ["string"]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- Each value must have its own confidence and evidence.
- Evidence must describe specific visible construction or design cues.
- All returned values must be from the allowed_values list when provided.
- reasoning_mode must always be "visual_suitability"
- proposed_values must be empty
- If signals are ambiguous or conflicting, return:
    values = []
    warnings = ["ambiguous_evidence"]
- Else if no values are clearly supported, return:
    values = []
    warnings = ["no_supported_value_found"]
- Confidence guidelines:
    0.80–1.00 = strong visual evidence supports the value
    0.50–0.79 = moderate visual evidence
    below 0.50 = do not include

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""


# ---------------------------------------------------------------------------
# Class → builder dispatch
# ---------------------------------------------------------------------------


_VISUAL_BUILDERS = {
    "descriptive_literal": _build_descriptive_literal_visual_prompt,
    "contextual_semantic": _build_contextual_semantic_visual_prompt,
    "compatibility": _build_compatibility_visual_prompt,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_visual_prompt_for_attribute(
    attribute: AttributeDefinition,
    visual: VisualInput,
) -> str:
    """Build a Claude-ready multimodal prompt for visually extracting the
    given attribute from the provided image reference.

    Dispatches to a class-specific builder based on attribute.class_name.
    taxonomy_discovery is intentionally not supported for visual enrichment.
    """
    if attribute.class_name == "taxonomy_discovery":
        raise NotImplementedError(
            "Visual taxonomy discovery is not currently supported."
        )
    builder = _VISUAL_BUILDERS[attribute.class_name]
    return builder(attribute, visual)


def build_visual_enrichment_output(
    attribute: AttributeDefinition,
    raw: dict,
) -> EnrichmentOutput:
    """Shape a raw model JSON response into an EnrichmentOutput with
    source=visual.

    Every returned value is individually tagged with source=visual so the
    merge service can tell where each value originated.
    """
    values: list[EnrichedValue] = []
    for item in raw.get("values") or []:
        values.append(
            EnrichedValue(
                value=item.get("value"),
                confidence=float(item.get("confidence", 0.0)),
                evidence=list(item.get("evidence") or []),
                reasoning_mode=item.get("reasoning_mode"),
                source=EnrichmentSource.VISUAL,
                contributing_sources=[EnrichmentSource.VISUAL],
            )
        )
    return EnrichmentOutput(
        attribute_name=raw.get("attribute_name") or attribute.name,
        attribute_class=raw.get("attribute_class") or attribute.class_name,
        values=values,
        proposed_values=list(raw.get("proposed_values") or []),
        warnings=list(raw.get("warnings") or []),
        source=EnrichmentSource.VISUAL,
    )


def generate_visual_enrichment(
    attribute: AttributeDefinition,
    visual: VisualInput,
    *,
    analyzer: Callable[[str, VisualInput], dict],
) -> EnrichmentOutput:
    """End-to-end visual enrichment generator.

    Builds the class-specific visual prompt, delegates model execution to the
    caller-provided analyzer (so this module stays agnostic of which
    multimodal model/API is used), then shapes the raw response into an
    EnrichmentOutput with source=visual.
    """
    prompt = get_visual_prompt_for_attribute(attribute, visual)
    raw = analyzer(prompt, visual)
    return build_visual_enrichment_output(attribute, raw)
