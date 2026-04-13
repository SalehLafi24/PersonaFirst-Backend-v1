import json

from app.schemas.attribute_enrichment import AttributeDefinition

# ---------------------------------------------------------------------------
# Shared output schema injected into every prompt
# ---------------------------------------------------------------------------

_OUTPUT_SCHEMA = """\
{
  "attribute_name": "<string>",
  "value": <string | list[string] | boolean | null>,
  "confidence": <float between 0.0 and 1.0>,
  "evidence": ["<quoted text or field that supports the value>"],
  "proposed_values": <list[string] | null>
}"""


# ---------------------------------------------------------------------------
# Behavior flag → natural-language instruction translation
# ---------------------------------------------------------------------------

def _behavior_instructions(attr: AttributeDefinition) -> list[str]:
    """Translate behavior flags into constraint lines for prompt injection."""
    b = attr.behavior
    instructions: list[str] = []

    if b.prefer_conservative_inference:
        instructions.append("When uncertain, prefer null over a speculative value.")

    if attr.value_mode == "boolean":
        instructions.append("Return true or false only.")
    elif attr.value_mode == "multi" and b.multi_value_allowed:
        instructions.append("You may return multiple values as a JSON array.")
    elif attr.value_mode == "single":
        instructions.append("Return exactly one value (or null if not determinable).")

    if b.taxonomy_sensitive:
        instructions.append(
            "This attribute is taxonomy-sensitive. Be precise about classification "
            "and do not conflate similar but distinct categories."
        )

    if b.ordered_values:
        instructions.append(
            "Values for this attribute have a natural ordering. "
            "Prefer the most applicable value in a ranked set."
        )

    if b.can_propose_values:
        instructions.append(
            "If the most accurate value is not in the allowed list, include it in "
            "proposed_values. Do not force a poor match from the allowed list."
        )

    return instructions


# ---------------------------------------------------------------------------
# Shared section helpers
# ---------------------------------------------------------------------------

def _output_section() -> list[str]:
    """Standard OUTPUT section injected into every prompt (FIX 2, FIX 3)."""
    return [
        "",
        "OUTPUT",
        "Respond with valid JSON only. No markdown fences. No explanation outside the JSON.",
        "Evidence must quote or clearly reference specific phrases or fields from the object data."
        " Do not provide generic or inferred evidence.",
        "If evidence is insufficient, return null rather than guessing.",
        _OUTPUT_SCHEMA,
    ]


def _allowed_values_lines(attr: AttributeDefinition) -> list[str]:
    """Standard allowed_values anchor for inference-capable classes (FIX 5)."""
    if not attr.allowed_values:
        return []
    return [
        f"  Allowed     : {json.dumps(attr.allowed_values)}",
        "  When allowed_values are provided, treat them as the primary value space.",
        "  Always attempt to map to the closest valid allowed value.",
        "  Only deviate if no reasonable mapping exists.",
    ]


def _normalize_obj(obj: dict) -> dict:
    """Replace newline characters in string values to prevent prompt line-break issues."""
    result = {}
    for k, v in obj.items():
        if isinstance(v, str):
            result[k] = v.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        elif isinstance(v, dict):
            result[k] = _normalize_obj(v)
        elif isinstance(v, list):
            result[k] = [
                item.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                if isinstance(item, str) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Class-specific prompt builders
# ---------------------------------------------------------------------------

def _build_descriptive_literal_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    Strictest class. Only extract values explicitly present in the data.
    No inference. Uses the standardised divider-based prompt format.
    """
    if attr.allowed_values:
        allowed_block = "\n".join(f"- {v}" for v in attr.allowed_values)
    else:
        allowed_block = "(none provided)"

    obj_json = json.dumps(_normalize_obj(obj), indent=2, ensure_ascii=False)

    return f"""\
You are an attribute extraction engine.

You are determining the value(s) of a single attribute for a product.

--------------------------------
ATTRIBUTE CONTEXT
--------------------------------
Attribute name: {attr.name}
Class: descriptive_literal
Description: {attr.description}

Allowed values:
{allowed_block}

Rules:
- Extract only values that are explicitly stated in the object data.
- Do not infer from product type, category, style, function, brand, or common sense.
- Do not guess.
- If allowed_values are provided:
    - Only return values from the allowed_values list.
    - If an explicitly stated value is a clear variant of an allowed value, map it to that allowed value.
    - The mapping must be direct and unambiguous (e.g., "blush pink" → "pink").
    - Do not map vague, poetic, or indirect language to an allowed value.
- If no explicit value is clearly stated, return no values.

--------------------------------
CLASS BEHAVIOR
--------------------------------
- This is a strict extraction task, not an inference task.
- Only include values directly supported by exact text or very close textual matches.
- Do not convert vague or poetic wording into attribute values.
- If multiple explicit values are present and allowed, include all of them.
- When in doubt, exclude the value.

--------------------------------
OBJECT DATA
--------------------------------
{obj_json}

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
      "reasoning_mode": "explicit"
    }}
  ],
  "proposed_values": [],
  "warnings": ["string"]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- Each value must have its own confidence and evidence.
- Evidence must quote exact or near-exact text from the object data.
- reasoning_mode must always be "explicit"
- proposed_values must be empty
- All returned values must be from the allowed_values list when provided.
- When a value is mapped to an allowed value, evidence must still reference the original source wording.
- If signals are ambiguous or conflicting, return:
    values = []
    warnings = ["ambiguous_evidence"]

- Else if no values are found, return:
    values = []
    warnings = ["no_supported_value_found"]

- If multiple strong explicit values are found:
    include all of them
    add warning: "multiple_strong_values_detected"

- Confidence guidelines:
    0.95–1.00 = exact or near-exact match
    0.85–0.94 = slightly indirect but explicit
    below 0.85 = do not include

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""


def _build_contextual_semantic_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    Allows semantic inference from context, descriptions, and implied meaning.
    Conservative by default. Uses the standardised divider-based prompt format.
    """
    if attr.allowed_values:
        allowed_block = "\n".join(f"- {v}" for v in attr.allowed_values)
    else:
        allowed_block = "(none provided)"

    obj_json = json.dumps(_normalize_obj(obj), indent=2, ensure_ascii=False)

    return f"""\
You are an attribute extraction engine.

You are determining the value(s) of a single attribute for a product.

--------------------------------
ATTRIBUTE CONTEXT
--------------------------------
Attribute name: {attr.name}
Class: contextual_semantic
Description: {attr.description}

Allowed values:
{allowed_block}

Rules:
- Only select values from the allowed_values list.
- Do not invent new values.
- Map to the closest valid value when evidence strongly supports it.
- If no value is clearly supported, return no values.
- Only include values that are directly supported as a clear intended use, context, or meaning of the product.
- Do not include a value just because the product has general performance, comfort, or technical features.

--------------------------------
CLASS BEHAVIOR
--------------------------------
- You may infer from product name, description, and attributes.
- Use semantic meaning, not just literal matches.
- Remain conservative — weak or ambiguous signals should not produce a value.
- If multiple values are clearly and independently supported, you may return multiple values.
- Do not include weak, secondary, or speculative matches.

--------------------------------
OBJECT DATA
--------------------------------
{obj_json}

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
      "reasoning_mode": "inferred"
    }}
  ],
  "proposed_values": [],
  "warnings": ["string"]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- Each value must have its own confidence and evidence.
- Evidence must quote or clearly reference exact phrases from the object data.
- Only include values with strong support as a primary or clearly intended use.
- Values with confidence below 0.80 must not be included.
- All returned values must be from the allowed_values list.
- Each returned value must be independently supported by its own evidence.
- If signals are ambiguous or conflicting, return:
    values = []
    warnings = ["ambiguous_evidence"]

- Else if no values are found, return:
    values = []
    warnings = ["no_supported_value_found"]

- If multiple strong values are found:
    include all of them
    add warning: "multiple_strong_values_detected"

- Confidence guidelines:
    0.90–1.00 = very strong support
    0.80–0.89 = strong inferred support
    below 0.80 = do not include

- reasoning_mode must always be "inferred"
- proposed_values must be empty

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""


def _build_compatibility_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    Infers suitability or compatibility. Avoids unsupported claims.
    Confidence must reflect strength of evidence, not assumed compatibility.
    Uses the standardised divider-based prompt format.
    """
    if attr.allowed_values:
        allowed_block = "\n".join(f"- {v}" for v in attr.allowed_values)
    else:
        allowed_block = "(none provided)"

    obj_json = json.dumps(_normalize_obj(obj), indent=2, ensure_ascii=False)

    return f"""\
You are an attribute extraction engine.

You are determining the value(s) of a single attribute for a product.

--------------------------------
ATTRIBUTE CONTEXT
--------------------------------
Attribute name: {attr.name}
Class: compatibility
Description: {attr.description}

Allowed values:
{allowed_block}

Rules:
- Explicit suitability statements have the highest priority.
- Indirect clues such as compression level, comfort language, activity type, or use context must not override an explicit suitability statement on their own.
- Treat indirect clues as supporting context, not as a stronger source of truth than an explicit suitability statement.
- Only treat the evidence as ambiguous if:
    - there are multiple explicit suitability statements pointing to different allowed values, or
    - the object data explicitly negates or directly contradicts the explicit suitability statement.
- If an explicit suitability statement is present and there is no direct contradiction, use it.

--------------------------------
CLASS BEHAVIOR
--------------------------------
- This is a suitability assessment task, not a literal extraction task.
- Judge how well the product is functionally suited to each candidate value.
- Use semantic and contextual signals from the object data, not literal-text matching alone.
- Confidence must reflect the strength of evidence, not the assumed compatibility.
- If multiple values are independently and strongly supported, you may return more than one.
- If signals are weak or ambiguous, return no values.

--------------------------------
OBJECT DATA
--------------------------------
{obj_json}

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
      "reasoning_mode": "suitability"
    }}
  ],
  "proposed_values": [],
  "warnings": ["string"]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- Each value must have its own confidence and evidence.
- Evidence must quote or clearly reference exact phrases from the object data.
- All returned values must be from the allowed_values list when provided.
- reasoning_mode must always be "suitability"
- proposed_values must be empty
- If signals are ambiguous or conflicting AND there is no usable explicit suitability statement, return:
    values = []
    warnings = ["ambiguous_evidence"]

- Else if no values are clearly supported, return:
    values = []
    warnings = ["no_supported_value_found"]

- If multiple strong values are found:
    include all of them
    add warning: "multiple_strong_values_detected"

- Confidence guidelines:
    0.80–1.00 = strong evidence supports the value
    0.50–0.79 = moderate evidence, some inference required
    below 0.50 = weak or ambiguous — do not include

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""


def _build_taxonomy_discovery_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    The value space does not yet exist. Goal is to propose values.
    proposed_values is the primary output; value is the single best proposal if confident.
    """
    lines = [
        f'You are discovering taxonomy values for the attribute "{attr.name}" on a {attr.object_type}.',
        "",
        "TASK",
        f'Determine the value of the attribute "{attr.name}" for the given {attr.object_type}.',
        "",
        "CLASS BEHAVIOR",
        "  - The value space for this attribute does not yet fully exist.",
        "  - Propose meaningful, reusable values based on the object's characteristics.",
        "  - Do not assume a fixed taxonomy.",
        "",
        "ATTRIBUTE",
        f"  Name        : {attr.name}",
        f"  Description : {attr.description}",
        f"  Evidence    : {', '.join(attr.evidence_sources)}",
    ]

    if attr.allowed_values:
        lines += [
            f"  Known values: {json.dumps(attr.allowed_values)}",
            "  When allowed_values are provided, treat them as the primary value space.",
            "  You may reuse existing values, extend them, or propose entirely new ones.",
            "  Include all relevant values (new AND reused) in proposed_values.",
        ]
    else:
        lines += [
            "  No known values exist yet. Propose values from scratch.",
            "  Proposed values should be concise, lowercase, and reusable across objects.",
        ]

    behavior_lines = _behavior_instructions(attr)
    if behavior_lines:
        lines += ["", "CONSTRAINTS"]
        lines += [f"  - {line}" for line in behavior_lines]

    lines += [
        "",
        "OUTPUT GUIDANCE",
        "  proposed_values : list all values you would recommend for this taxonomy",
        "  value           : the single best proposed value, or null if none is certain",
        "  evidence        : cite the object fields or phrases that drove your proposals",
        "  confidence      : your certainty that the proposed values are appropriate",
        "",
        "OBJECT DATA",
        json.dumps(obj, indent=2, ensure_ascii=False),
    ]
    lines += _output_section()

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Class → builder dispatch
# ---------------------------------------------------------------------------

_BUILDERS = {
    "descriptive_literal": _build_descriptive_literal_prompt,
    "contextual_semantic": _build_contextual_semantic_prompt,
    "compatibility": _build_compatibility_prompt,
    "taxonomy_discovery": _build_taxonomy_discovery_prompt,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_prompt_for_attribute(attribute: AttributeDefinition, obj: dict) -> str:
    """
    Build a Claude-ready prompt for extracting or inferring the given attribute
    from the provided object data.

    Dispatches to a class-specific builder based on attribute.class_name.
    Each builder differs in strictness, inference allowance, and taxonomy behavior.

    Returns a plain string prompt ready to be sent as a user message to Claude.
    The prompt instructs Claude to respond with a single JSON object matching
    EnrichmentResult's structure.
    """
    builder = _BUILDERS[attribute.class_name]
    return builder(attribute, obj)
