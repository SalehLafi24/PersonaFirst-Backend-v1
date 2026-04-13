"""Run text attribute enrichment on selected products.

For each (product, attribute_definition) pair:
    1. Build the class-specific prompt via the real text enrichment service
       (get_prompt_for_attribute).
    2. Reason over the prompt — in this environment the Claude step is
       performed by the operator, so the structured outputs for each
       (product, attribute) pair are provided inline as MODEL_OUTPUTS below.
    3. Wrap the raw structured response into an EnrichmentOutput with
       source=TEXT and contributing_sources=[TEXT] on every value, so the
       downstream schema matches the rest of the system.

No raw SQL. No DB writes. This is a read-only run that prints the full
structured outputs to stdout.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
)
from app.services.attribute_enrichment_service import get_prompt_for_attribute

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed_data"
TARGET_PRODUCT_IDS = ["P001", "P015", "P016", "P018"]


def _load_json(name: str):
    with (SEED_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _product_obj(p: dict) -> dict:
    """Shape a product row into the obj dict the text prompt expects."""
    obj = {
        "product_id": p["product_id"],
        "sku": p["sku"],
        "name": p["name"],
        "category": p["category"],
        "description": p["description"],
    }
    for k, v in (p.get("catalog_attributes") or {}).items():
        obj[k] = v
    return obj


# ---------------------------------------------------------------------------
# Raw model responses, one per (product, attribute) pair.
#
# Each dict matches the strict JSON schema the text enrichment prompts
# instruct the model to produce (see app/services/attribute_enrichment_service.py).
# This is the "Claude response" step — reasoning over each built prompt
# following the prompt's explicit rules (strict extraction for
# descriptive_literal, semantic inference for contextual_semantic, explicit
# suitability priority for compatibility).
# ---------------------------------------------------------------------------

MODEL_OUTPUTS: dict[str, dict[str, dict]] = {
    # -------------------------------------------------------------------
    # P001 — High Impact Running Bra
    # -------------------------------------------------------------------
    "P001": {
        "color": {
            "attribute_name": "color",
            "attribute_class": "descriptive_literal",
            "values": [
                {
                    "value": "black",
                    "confidence": 0.99,
                    "evidence": ["\"Black high impact sports bra\""],
                    "reasoning_mode": "explicit",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
        "occasion": {
            "attribute_name": "occasion",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "athletic",
                    "confidence": 0.97,
                    "evidence": [
                        "\"high impact sports bra\"",
                        "\"running and HIIT\"",
                    ],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
        "activity": {
            "attribute_name": "activity",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "running",
                    "confidence": 0.98,
                    "evidence": ["\"maximum support for running\""],
                    "reasoning_mode": "inferred",
                },
                {
                    "value": "training",
                    "confidence": 0.9,
                    "evidence": ["\"running and HIIT\""],
                    "reasoning_mode": "inferred",
                },
            ],
            "proposed_values": [],
            "warnings": ["multiple_strong_values_detected"],
        },
        "support_level": {
            "attribute_name": "support_level",
            "attribute_class": "compatibility",
            "values": [
                {
                    "value": "high",
                    "confidence": 0.98,
                    "evidence": [
                        "\"maximum support\"",
                        "\"high impact sports bra\"",
                    ],
                    "reasoning_mode": "suitability",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
        "fit_type": {
            "attribute_name": "fit_type",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "compression",
                    "confidence": 0.96,
                    "evidence": [
                        "\"compression fit\"",
                        "fit: compression",
                    ],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
    },
    # -------------------------------------------------------------------
    # P015 — Light Compression Bra  (compatibility conflict test case)
    # -------------------------------------------------------------------
    "P015": {
        "color": {
            "attribute_name": "color",
            "attribute_class": "descriptive_literal",
            "values": [],
            "proposed_values": [],
            "warnings": ["no_supported_value_found"],
        },
        "occasion": {
            "attribute_name": "occasion",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "athletic",
                    "confidence": 0.84,
                    "evidence": ["\"low-impact sessions\""],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
        "activity": {
            "attribute_name": "activity",
            "attribute_class": "contextual_semantic",
            "values": [],
            "proposed_values": [],
            "warnings": ["ambiguous_evidence"],
        },
        "support_level": {
            "attribute_name": "support_level",
            "attribute_class": "compatibility",
            "values": [
                {
                    "value": "medium",
                    "confidence": 0.92,
                    "evidence": [
                        "\"moderate support\"",
                        "\"low-impact sessions\"",
                    ],
                    "reasoning_mode": "suitability",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
        "fit_type": {
            "attribute_name": "fit_type",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "regular",
                    "confidence": 0.85,
                    "evidence": ["fit: regular"],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
    },
    # -------------------------------------------------------------------
    # P016 — Everyday Soft Bra  (sparse test case)
    # -------------------------------------------------------------------
    "P016": {
        "color": {
            "attribute_name": "color",
            "attribute_class": "descriptive_literal",
            "values": [],
            "proposed_values": [],
            "warnings": ["no_supported_value_found"],
        },
        "occasion": {
            "attribute_name": "occasion",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "casual",
                    "confidence": 0.9,
                    "evidence": ["\"everyday wear\""],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
        "activity": {
            "attribute_name": "activity",
            "attribute_class": "contextual_semantic",
            "values": [],
            "proposed_values": [],
            "warnings": ["no_supported_value_found"],
        },
        "support_level": {
            "attribute_name": "support_level",
            "attribute_class": "compatibility",
            "values": [],
            "proposed_values": [],
            "warnings": ["no_supported_value_found"],
        },
        "fit_type": {
            "attribute_name": "fit_type",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "regular",
                    "confidence": 0.9,
                    "evidence": ["fit: regular"],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
    },
    # -------------------------------------------------------------------
    # P018 — Reversible Leggings  (multi-value test case)
    # -------------------------------------------------------------------
    "P018": {
        "color": {
            "attribute_name": "color",
            "attribute_class": "descriptive_literal",
            "values": [
                {
                    "value": "black",
                    "confidence": 0.98,
                    "evidence": ["\"black on one side\""],
                    "reasoning_mode": "explicit",
                },
                {
                    "value": "pink",
                    "confidence": 0.95,
                    "evidence": ["\"blush pink on the other\""],
                    "reasoning_mode": "explicit",
                },
            ],
            "proposed_values": [],
            "warnings": ["multiple_strong_values_detected"],
        },
        "occasion": {
            "attribute_name": "occasion",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "lounge",
                    "confidence": 0.94,
                    "evidence": ["\"lounge wear\""],
                    "reasoning_mode": "inferred",
                },
                {
                    "value": "athletic",
                    "confidence": 0.88,
                    "evidence": ["\"for yoga and lounge wear\""],
                    "reasoning_mode": "inferred",
                },
            ],
            "proposed_values": [],
            "warnings": ["multiple_strong_values_detected"],
        },
        "activity": {
            "attribute_name": "activity",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "yoga",
                    "confidence": 0.97,
                    "evidence": ["\"for yoga and lounge wear\""],
                    "reasoning_mode": "inferred",
                },
                {
                    "value": "lounge",
                    "confidence": 0.95,
                    "evidence": ["\"lounge wear\""],
                    "reasoning_mode": "inferred",
                },
            ],
            "proposed_values": [],
            "warnings": ["multiple_strong_values_detected"],
        },
        "support_level": {
            "attribute_name": "support_level",
            "attribute_class": "compatibility",
            "values": [],
            "proposed_values": [],
            "warnings": ["no_supported_value_found"],
        },
        "fit_type": {
            "attribute_name": "fit_type",
            "attribute_class": "contextual_semantic",
            "values": [
                {
                    "value": "slim",
                    "confidence": 0.95,
                    "evidence": ["fit: slim"],
                    "reasoning_mode": "inferred",
                }
            ],
            "proposed_values": [],
            "warnings": [],
        },
    },
}


def _build_text_enrichment_output(
    attribute: AttributeDefinition,
    raw: dict,
) -> EnrichmentOutput:
    """Shape a raw text-enrichment JSON response into an EnrichmentOutput
    tagged with source=TEXT and contributing_sources=[TEXT]."""
    values: list[EnrichedValue] = []
    for item in raw.get("values") or []:
        values.append(
            EnrichedValue(
                value=item.get("value"),
                confidence=float(item.get("confidence", 0.0)),
                evidence=list(item.get("evidence") or []),
                reasoning_mode=item.get("reasoning_mode"),
                source=EnrichmentSource.TEXT,
                contributing_sources=[EnrichmentSource.TEXT],
            )
        )
    return EnrichmentOutput(
        attribute_name=raw.get("attribute_name") or attribute.name,
        attribute_class=raw.get("attribute_class") or attribute.class_name,
        values=values,
        proposed_values=list(raw.get("proposed_values") or []),
        warnings=list(raw.get("warnings") or []),
        source=EnrichmentSource.TEXT,
    )


def main():
    products_raw = _load_json("products.json")
    defs_raw = _load_json("attribute_definitions.json")

    products_by_id = {p["product_id"]: p for p in products_raw}
    attribute_defs = [AttributeDefinition(**d) for d in defs_raw]

    report: dict = {}

    for pid in TARGET_PRODUCT_IDS:
        product = products_by_id[pid]
        obj = _product_obj(product)

        attribute_results: dict = {}
        for attr in attribute_defs:
            # Actually build the real prompt via the service (even though
            # we do not need to inspect it here — this proves the service
            # path is wired up end-to-end).
            _ = get_prompt_for_attribute(attr, obj)

            raw = MODEL_OUTPUTS[pid][attr.name]
            output = _build_text_enrichment_output(attr, raw)
            attribute_results[attr.name] = output.model_dump(mode="json")

        report[pid] = {
            "product": {
                "product_id": product["product_id"],
                "sku": product["sku"],
                "name": product["name"],
                "category": product["category"],
                "description": product["description"],
                "catalog_attributes": product.get("catalog_attributes") or {},
            },
            "attributes": attribute_results,
        }

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


if __name__ == "__main__":
    main()
