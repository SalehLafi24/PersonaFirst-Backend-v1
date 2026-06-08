"""Run recommendations for customer C001 end-to-end.

    1. Ensure ground-truth product attributes are loaded as ProductAttribute
       rows (color/occasion/activity/support_level/fit_type) in addition to
       the category/material/fit rows already seeded.
    2. Generate customer affinities from purchases via the affinity service.
    3. Compute customer_signal_strength via the signal_strength service.
    4. Build product_enrichment_outputs from the ground-truth CSV (one
       EnrichmentOutput per product/attribute, source=TEXT).
    5. Call get_recommendations with the multi-source signal inputs
       (customer_signal_strength, product_enrichment_outputs,
       tiebreak_by_match_confidence=True) and attribute_targeting_modes /
       attribute_behaviors derived from seed_data/attribute_definitions.json.
    6. Print the top 8 results with full multi-source signal fields.

No raw SQL.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.core.database import SessionLocal
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
)
from app.services.affinity_service import generate_affinities_from_purchases
from app.services.recommendation_service import get_recommendations
from app.services.signal_strength_service import compute_customer_signal_strength

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed_data"
WORKSPACE_SLUG = "personafirst-starter"
CUSTOMER_ID = "C001"
TOP_N = 8


def _load_json(name: str):
    with (SEED_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _load_ground_truth() -> dict[str, dict[str, list[str]]]:
    """Return {product_id: {attribute_name: [values]}}."""
    out: dict[str, dict[str, list[str]]] = {}
    with (SEED_DIR / "ground_truth_product_attributes.csv").open(
        encoding="utf-8"
    ) as f:
        reader = csv.reader(f)
        next(reader)  # header
        for pid, attr, val in reader:
            out.setdefault(pid, {}).setdefault(attr, []).append(val)
    return out


def _ensure_ground_truth_attributes(db, workspace_id: int) -> int:
    """Add ground-truth attribute rows to ProductAttribute if not present.
    Returns the number of new rows inserted.
    """
    gt = _load_ground_truth()
    ground_truth_attr_ids = {"color", "occasion", "activity", "support_level", "fit_type"}

    products = {
        p.product_id: p
        for p in db.query(Product).filter(Product.workspace_id == workspace_id).all()
    }

    existing_keys: set[tuple[int, str, str]] = {
        (pa.product_id, pa.attribute_id, pa.attribute_value)
        for pa in db.query(ProductAttribute)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(
            Product.workspace_id == workspace_id,
            ProductAttribute.attribute_id.in_(ground_truth_attr_ids),
        )
        .all()
    }

    new_rows: list[ProductAttribute] = []
    for pid, attr_map in gt.items():
        if pid not in products:
            continue
        product_db_id = products[pid].id
        for attr_name, values in attr_map.items():
            for v in values:
                key = (product_db_id, attr_name, v)
                if key in existing_keys:
                    continue
                new_rows.append(
                    ProductAttribute(
                        product_id=product_db_id,
                        attribute_id=attr_name,
                        attribute_value=v,
                    )
                )
                existing_keys.add(key)

    if new_rows:
        db.add_all(new_rows)
        db.commit()
    return len(new_rows)


def _build_product_enrichment_outputs(
    gt: dict[str, dict[str, list[str]]],
    attribute_defs: list[AttributeDefinition],
) -> dict[str, dict[str, EnrichmentOutput]]:
    """Turn the ground-truth CSV into one EnrichmentOutput per
    (product, attribute) pair, tagged with source=TEXT."""
    defs_by_name = {d.name: d for d in attribute_defs}
    reasoning_by_class = {
        "descriptive_literal": "explicit",
        "contextual_semantic": "inferred",
        "compatibility": "suitability",
    }
    out: dict[str, dict[str, EnrichmentOutput]] = {}
    for pid, attr_map in gt.items():
        product_outputs: dict[str, EnrichmentOutput] = {}
        for attr_name, values in attr_map.items():
            attr_def = defs_by_name.get(attr_name)
            if attr_def is None:
                continue
            reasoning = reasoning_by_class.get(attr_def.class_name, "inferred")
            enriched_values = [
                EnrichedValue(
                    value=v,
                    confidence=0.95,
                    evidence=[f"ground_truth:{attr_name}={v}"],
                    reasoning_mode=reasoning,
                    source=EnrichmentSource.TEXT,
                    contributing_sources=[EnrichmentSource.TEXT],
                )
                for v in values
            ]
            product_outputs[attr_name] = EnrichmentOutput(
                attribute_name=attr_name,
                attribute_class=attr_def.class_name,
                values=enriched_values,
                warnings=["multiple_strong_values_detected"] if len(values) > 1 else [],
                source=EnrichmentSource.TEXT,
            )
        out[pid] = product_outputs
    return out


def _targeting_modes_from_defs(
    attribute_defs: list[AttributeDefinition],
) -> dict[str, str]:
    return {d.name: d.targeting_mode.value for d in attribute_defs}


def _behaviors_from_defs(attribute_defs: list[AttributeDefinition]) -> dict:
    return {d.name: d.behavior for d in attribute_defs}


def main():
    db = SessionLocal()
    try:
        ws = (
            db.query(Workspace)
            .filter(Workspace.slug == WORKSPACE_SLUG)
            .first()
        )
        if ws is None:
            raise SystemExit(
                f"Workspace '{WORKSPACE_SLUG}' not found. "
                "Run `python -m scripts.seed_starter_dataset` first."
            )

        # 1. Load ground-truth product attributes.
        new_attr_rows = _ensure_ground_truth_attributes(db, ws.id)

        # 2. Generate C001's affinities from purchases.
        aff_result = generate_affinities_from_purchases(
            db, ws.id, customer_id=CUSTOMER_ID
        )

        # 3. Customer signal strength from the existing service.
        sig = compute_customer_signal_strength(db, ws.id, CUSTOMER_ID)

        # 4. Attribute definitions → targeting modes, behaviors, enrichment.
        attribute_defs = [
            AttributeDefinition(**d) for d in _load_json("attribute_definitions.json")
        ]
        targeting_modes = _targeting_modes_from_defs(attribute_defs)
        # Category/material/fit fall back to categorical_affinity; the engine
        # already defaults to that when a key is missing so we leave them out.
        behaviors = _behaviors_from_defs(attribute_defs)

        gt = _load_ground_truth()
        enrichment_outputs = _build_product_enrichment_outputs(gt, attribute_defs)

        # 5. Run recommendations with multi-source signal inputs.
        results, fallback_applied = get_recommendations(
            db,
            workspace_id=ws.id,
            customer_id=CUSTOMER_ID,
            top_n=TOP_N,
            attribute_targeting_modes=targeting_modes,
            attribute_behaviors=behaviors,
            customer_signal_strength=sig.customer_signal_strength,
            product_enrichment_outputs=enrichment_outputs,
            tiebreak_by_match_confidence=True,
        )

        # 6. Report.
        out_rows = []
        for r in results:
            out_rows.append(
                {
                    "product_id": r.product_id,
                    "sku": r.sku,
                    "name": r.name,
                    "recommendation_score": round(r.recommendation_score, 6),
                    "match_confidence": (
                        round(r.match_confidence, 6)
                        if r.match_confidence is not None
                        else None
                    ),
                    "product_signal_strength": (
                        round(r.product_signal_strength, 6)
                        if r.product_signal_strength is not None
                        else None
                    ),
                    "customer_signal_strength": (
                        round(r.customer_signal_strength, 6)
                        if r.customer_signal_strength is not None
                        else None
                    ),
                    "signal_summary": (
                        r.signal_summary.model_dump()
                        if r.signal_summary is not None
                        else None
                    ),
                    "matched_attributes": [
                        {
                            "attribute_id": m.attribute_id,
                            "attribute_value": m.attribute_value,
                            "score": round(m.score, 6),
                            "weight": m.weight,
                            "targeting_mode": m.targeting_mode,
                        }
                        for m in r.matched_attributes
                    ],
                    "affinity_contribution": round(
                        r.affinity_contribution, 6
                    ),
                    "compatibility_positive_contribution": round(
                        r.compatibility_positive_contribution, 6
                    ),
                    "compatibility_negative_contribution": round(
                        r.compatibility_negative_contribution, 6
                    ),
                    "contextual_negative_contribution": round(
                        r.contextual_negative_contribution, 6
                    ),
                    "low_signal_penalty": round(
                        r.low_signal_penalty, 6
                    ),
                    "explanation": r.explanation,
                }
            )

        print(
            json.dumps(
                {
                    "workspace_id": ws.id,
                    "customer_id": CUSTOMER_ID,
                    "fallback_applied": fallback_applied,
                    "new_ground_truth_rows_inserted": new_attr_rows,
                    "affinity_summary": aff_result.model_dump(),
                    "customer_signal_strength": round(
                        sig.customer_signal_strength, 6
                    ),
                    "top_results": out_rows,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
