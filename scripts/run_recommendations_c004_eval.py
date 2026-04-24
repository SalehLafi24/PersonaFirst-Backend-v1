"""Run C004 recommendations in production mode and evaluation mode.

Evaluation mode disables purchase-based suppression so the engine is scored on
raw relevance and previously purchased items are allowed to surface. Production
mode keeps all existing suppression behavior exactly the same.
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
CUSTOMER_ID = "C004"
TOP_N = 8


def _load_json(name: str):
    with (SEED_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _load_ground_truth() -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    with (SEED_DIR / "ground_truth_product_attributes.csv").open(encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for pid, attr, val in reader:
            out.setdefault(pid, {}).setdefault(attr, []).append(val)
    return out


def _ensure_ground_truth_attributes(db, workspace_id: int) -> int:
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


def _run_mode(
    db,
    workspace_id: int,
    targeting_modes: dict[str, str],
    behaviors: dict,
    customer_signal_strength: float,
    enrichment_outputs: dict[str, dict[str, EnrichmentOutput]],
    disable_suppression: bool,
    disable_diversity: bool = False,
):
    results, fallback_applied = get_recommendations(
        db,
        workspace_id=workspace_id,
        customer_id=CUSTOMER_ID,
        top_n=TOP_N,
        attribute_targeting_modes=targeting_modes,
        attribute_behaviors=behaviors,
        customer_signal_strength=customer_signal_strength,
        product_enrichment_outputs=enrichment_outputs,
        tiebreak_by_match_confidence=True,
        disable_purchase_suppression_for_eval=disable_suppression,
        disable_diversity_shaping=disable_diversity,
    )
    rows = []
    for r in results:
        rows.append(
            {
                "product_id": r.product_id,
                "name": r.name,
                "recommendation_score": round(r.recommendation_score, 4),
                "match_confidence": (
                    round(r.match_confidence, 4)
                    if r.match_confidence is not None
                    else None
                ),
                "matched_attributes": [
                    {
                        "attribute_id": m.attribute_id,
                        "attribute_value": m.attribute_value,
                        "score": round(m.score, 4),
                        "targeting_mode": m.targeting_mode,
                    }
                    for m in r.matched_attributes
                ],
                "explanation": r.explanation,
            }
        )
    return {"fallback_applied": fallback_applied, "top": rows}


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

        _ensure_ground_truth_attributes(db, ws.id)
        generate_affinities_from_purchases(db, ws.id, customer_id=CUSTOMER_ID)
        sig = compute_customer_signal_strength(db, ws.id, CUSTOMER_ID)

        attribute_defs = [
            AttributeDefinition(**d) for d in _load_json("attribute_definitions.json")
        ]
        targeting_modes = {d.name: d.targeting_mode.value for d in attribute_defs}
        behaviors = {d.name: d.behavior for d in attribute_defs}

        gt = _load_ground_truth()
        enrichment_outputs = _build_product_enrichment_outputs(gt, attribute_defs)

        normal_no_diversity = _run_mode(
            db, ws.id, targeting_modes, behaviors,
            sig.customer_signal_strength, enrichment_outputs,
            disable_suppression=False,
            disable_diversity=True,
        )
        normal_with_diversity = _run_mode(
            db, ws.id, targeting_modes, behaviors,
            sig.customer_signal_strength, enrichment_outputs,
            disable_suppression=False,
            disable_diversity=False,
        )
        evaluation = _run_mode(
            db, ws.id, targeting_modes, behaviors,
            sig.customer_signal_strength, enrichment_outputs,
            disable_suppression=True,
            disable_diversity=True,
        )

        print(json.dumps(
            {
                "workspace_id": ws.id,
                "customer_id": CUSTOMER_ID,
                "customer_signal_strength": round(sig.customer_signal_strength, 4),
                "A_normal_no_diversity": normal_no_diversity,
                "A_normal_with_diversity": normal_with_diversity,
                "B_evaluation_mode": evaluation,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ))
    finally:
        db.close()


if __name__ == "__main__":
    main()
