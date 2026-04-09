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
# Class-specific prompt builders
# ---------------------------------------------------------------------------

def _build_descriptive_literal_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    Strictest class. Only extract values that are explicitly present in the data.
    No inference. Prefer null over speculation.
    """
    lines = [
        f'You are extracting the attribute "{attr.name}" from a {attr.object_type}.',
        "",
        "TASK",
        "Extract only values that are explicitly stated in the object data.",
        "Do NOT infer, guess, or interpret beyond what is literally written.",
        "If the value is not clearly present in the data, return null.",
        "",
        "ATTRIBUTE",
        f"  Name        : {attr.name}",
        f"  Description : {attr.description}",
        f"  Evidence    : {', '.join(attr.evidence_sources)}",
    ]

    if attr.allowed_values:
        lines += [
            f"  Allowed     : {json.dumps(attr.allowed_values)}",
            "  Only return a value from this list. Anything outside it must be null.",
        ]

    behavior_lines = _behavior_instructions(attr)
    if behavior_lines:
        lines += ["", "CONSTRAINTS"]
        lines += [f"  - {line}" for line in behavior_lines]

    lines += [
        "",
        "OBJECT DATA",
        json.dumps(obj, indent=2, ensure_ascii=False),
        "",
        "OUTPUT",
        "Respond with valid JSON only. No markdown fences. No explanation outside the JSON.",
        _OUTPUT_SCHEMA,
    ]

    return "\n".join(lines)


def _build_contextual_semantic_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    Allows semantic inference from context, descriptions, and implied meaning.
    Conservative when the flag is set; broader reasoning otherwise.
    """
    conservatism_note = (
        "Apply conservative reasoning — only infer when the evidence is reasonably strong."
        if attr.behavior.prefer_conservative_inference
        else "You may use broader semantic reasoning to infer the value from context."
    )

    lines = [
        f'You are inferring the attribute "{attr.name}" for a {attr.object_type}.',
        "",
        "TASK",
        "Read and semantically interpret the object data to determine the attribute value.",
        "You may reason from context, descriptions, synonyms, and implied meaning.",
        conservatism_note,
        "",
        "ATTRIBUTE",
        f"  Name        : {attr.name}",
        f"  Description : {attr.description}",
        f"  Evidence    : {', '.join(attr.evidence_sources)}",
    ]

    if attr.allowed_values:
        lines += [
            f"  Allowed     : {json.dumps(attr.allowed_values)}",
            "  Map your inference to the closest matching value from this list.",
        ]

    behavior_lines = _behavior_instructions(attr)
    if behavior_lines:
        lines += ["", "CONSTRAINTS"]
        lines += [f"  - {line}" for line in behavior_lines]

    lines += [
        "",
        "OBJECT DATA",
        json.dumps(obj, indent=2, ensure_ascii=False),
        "",
        "OUTPUT",
        "Respond with valid JSON only. No markdown fences. No explanation outside the JSON.",
        "Populate evidence with the specific phrases or fields that led to your inference.",
        _OUTPUT_SCHEMA,
    ]

    return "\n".join(lines)


def _build_compatibility_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    Infers suitability or compatibility. Avoids unsupported claims.
    Confidence must reflect strength of evidence, not assumed compatibility.
    """
    lines = [
        f'You are assessing compatibility for the attribute "{attr.name}" on a {attr.object_type}.',
        "",
        "TASK",
        "Determine whether this object is compatible with or suitable for the context described",
        "by this attribute. Use only available evidence to support your assessment.",
        "Do NOT claim compatibility unless the evidence clearly supports it.",
        "Partial or uncertain compatibility should be reflected in a lower confidence score.",
        "",
        "ATTRIBUTE",
        f"  Name        : {attr.name}",
        f"  Description : {attr.description}",
        f"  Evidence    : {', '.join(attr.evidence_sources)}",
    ]

    if attr.allowed_values:
        lines += [
            f"  Allowed     : {json.dumps(attr.allowed_values)}",
            "  Select the most appropriate compatibility classification from this list.",
        ]

    behavior_lines = _behavior_instructions(attr)
    if behavior_lines:
        lines += ["", "CONSTRAINTS"]
        lines += [f"  - {line}" for line in behavior_lines]

    lines += [
        "",
        "SCORING GUIDANCE",
        "  confidence >= 0.8 : strong evidence supports the value",
        "  confidence 0.5–0.79: moderate evidence, some inference required",
        "  confidence < 0.5  : weak or ambiguous — consider null instead",
        "",
        "OBJECT DATA",
        json.dumps(obj, indent=2, ensure_ascii=False),
        "",
        "OUTPUT",
        "Respond with valid JSON only. No markdown fences. No explanation outside the JSON.",
        "Populate evidence with the specific fields or statements that informed your assessment.",
        _OUTPUT_SCHEMA,
    ]

    return "\n".join(lines)


def _build_taxonomy_discovery_prompt(attr: AttributeDefinition, obj: dict) -> str:
    """
    The value space does not yet exist. Goal is to propose values.
    proposed_values is the primary output; value is the single best proposal if confident.
    """
    lines = [
        f'You are discovering taxonomy values for the attribute "{attr.name}" on a {attr.object_type}.',
        "",
        "TASK",
        "The value space for this attribute does not yet fully exist.",
        "Your primary goal is to PROPOSE appropriate values based on the object data.",
        "Do not assume a fixed taxonomy. Reason from the object's characteristics to",
        "suggest meaningful, reusable values that could apply across similar objects.",
        "",
        "ATTRIBUTE",
        f"  Name        : {attr.name}",
        f"  Description : {attr.description}",
        f"  Evidence    : {', '.join(attr.evidence_sources)}",
    ]

    if attr.allowed_values:
        lines += [
            f"  Known values: {json.dumps(attr.allowed_values)}",
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
        "",
        "OUTPUT",
        "Respond with valid JSON only. No markdown fences. No explanation outside the JSON.",
        _OUTPUT_SCHEMA,
    ]

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
