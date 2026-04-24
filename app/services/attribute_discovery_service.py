"""Attribute discovery prompt builder.

Separate from normal enrichment. This service builds a prompt that asks
the model to propose *new taxonomy dimensions* — not new values for
existing attributes. The output feeds into the proposed-attribute
pipeline (propose -> aggregate -> review -> promote).

Discovery scope (enforced via prompt rules):
    DO propose:
        - usage context, environment, climate suitability, layering role,
          body-area targeting, care/maintenance needs — high-value
          dimensions that drive recommendation, filtering, compatibility,
          or customer segmentation.
    DO NOT propose:
        - new values for existing attributes (that's the proposed-value
          pipeline's job)
        - product-specific low-level features (e.g. "has_zipper")
        - vague or stylistic attributes (e.g. "aesthetic", "vibe")
        - attributes that overlap with existing ones
"""
from __future__ import annotations

import json

from app.schemas.attribute_enrichment import AttributeDefinition


def build_attribute_discovery_prompt(
    obj: dict,
    existing_attributes: list[AttributeDefinition],
) -> str:
    """Build a prompt that asks the model to discover new taxonomy dimensions.

    *existing_attributes* is the full list of currently defined attributes
    so the model can avoid proposing duplicates or value expansions.
    """
    existing_names = sorted(a.name for a in existing_attributes)
    existing_block = "\n".join(
        f"- {a.name} ({a.class_name}): {a.description}" for a in existing_attributes
    )
    obj_json = json.dumps(obj, indent=2, ensure_ascii=False)

    return f"""\
You are a taxonomy discovery engine.

Your goal is to identify missing attribute dimensions that would be useful
for product recommendation, filtering, compatibility scoring, or customer
segmentation — but that are NOT already covered by the existing attribute
definitions listed below.

--------------------------------
EXISTING ATTRIBUTES (do NOT propose these or overlapping concepts)
--------------------------------
{existing_block}

Existing attribute names for reference: {json.dumps(existing_names)}

--------------------------------
STRICT SCOPE RULES
--------------------------------
1. Do NOT propose attributes that are just new values for existing
   attributes. For example, if "activity" already exists, do not propose
   "hiking_activity" — that is a value expansion, not a new dimension.

2. Do NOT propose attributes that overlap with or duplicate existing
   attributes. For example, do not propose "exercise_type" if
   "activity_type" already exists.

3. Do NOT propose product-specific low-level features such as
   "has_zipper", "has_pockets", "seam_type". These are not useful
   taxonomy dimensions.

4. Do NOT propose vague or stylistic attributes such as "aesthetic",
   "vibe", "style_mood". These are not actionable for recommendation.

5. ONLY propose attributes that represent a distinct, useful dimension
   for recommendation, filtering, compatibility, or segmentation.

6. Focus discovery on high-value missing dimensions such as:
   - usage context or environment (e.g. indoor vs outdoor)
   - climate or weather suitability
   - layering role (base, mid, outer)
   - body coverage or target area
   - care requirements (machine-washable, delicate)

7. Be conservative: propose nothing rather than propose weak candidates.

8. Evidence must be explicit or strongly implied in the product data.

--------------------------------
OBJECT DATA
--------------------------------
{obj_json}

--------------------------------
OUTPUT FORMAT (STRICT)
--------------------------------
Return valid JSON only. No markdown fences. No explanation.

{{
  "proposed_attributes": [
    {{
      "attribute_name": "string (lowercase, underscore-separated)",
      "confidence": 0.0,
      "description": "string (what this attribute represents)",
      "evidence": ["string (quoted phrase from object data)"],
      "suggested_values": ["string", "string"],
      "suggested_class_name": "contextual_semantic | compatibility | descriptive_literal",
      "suggested_targeting_mode": "categorical_affinity | compatibility_signal | categorical_filter"
    }}
  ]
}}

--------------------------------
OUTPUT RULES
--------------------------------
- confidence must be >= 0.85 to include. Below that, do not include.
- evidence must quote exact phrases from the object data.
- suggested_values must be concrete and reusable across products.
- suggested_class_name must be one of: contextual_semantic,
  compatibility, descriptive_literal.
- suggested_targeting_mode must be one of: categorical_affinity,
  compatibility_signal, categorical_filter.
- If no new attributes are justified, return:
  {{"proposed_attributes": []}}

--------------------------------
FINAL RULE
--------------------------------
Return JSON only. No explanation."""
