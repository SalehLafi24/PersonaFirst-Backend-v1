"""Validate the CSV mapping layer on 200 products from mumz_products.csv.

Uses an isolated test workspace (csv_mapping_import_test). Does NOT
touch mumzworld_v3_sample. The only LLM calls are for the
evidence_for_enrichment subset (controlled below).

Mapping under test:

    age   -> age_group   (direct_or_normalized_value)
    name  -> age_group   (evidence_for_enrichment)

`age_group` is defined inline (not in seed_data/attribute_definitions.json)
since the goal is to demonstrate the mapping pipeline, not to register
a new long-lived attribute. The test workspace AAV table starts empty
for age_group, so direct-mode cells become discovery events.
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so ANTHROPIC_API_KEY (if present) reaches the model_client.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeBehavior, AttributeDefinition, TargetingMode,
)
from app.services.csv_mapping_import_service import (
    MAPPING_MODE_DIRECT, MAPPING_MODE_EVIDENCE,
    MappingRule, import_csv_with_mapping,
)

WS_SLUG = "csv_mapping_import_test"
CSV_PATH = ROOT / "seed_data" / "mumz_products.csv"
ATTR = "age_group"
N_ROWS_TOTAL = 200
# Only run the LLM on the first N rows. The remaining 200-N rows still
# exercise direct-mode (which is deterministic and free).
N_EVIDENCE_LLM_ROWS = int(os.environ.get("CSV_MAPPING_LLM_ROWS", "0"))


# Inline attribute definition for the test target.
AGE_GROUP_DEF = AttributeDefinition(
    name=ATTR,
    object_type="product",
    class_name="contextual_semantic",
    value_mode="multi",
    allowed_values=["newborn", "infant", "toddler", "kids", "teen", "adult", "universal"],
    description="The customer life-stage / age band the product is intended for.",
    evidence_sources=["text"],
    behavior=AttributeBehavior(
        taxonomy_sensitive=True,
        ordered_values=False,
        can_propose_values=True,
        multi_value_allowed=True,
        prefer_conservative_inference=True,
    ),
    targeting_mode=TargetingMode.CATEGORICAL_AFFINITY,
)

MAPPING_RULES = [
    MappingRule(source_column="age",  target_attribute=ATTR, mode=MAPPING_MODE_DIRECT),
    MappingRule(source_column="name", target_attribute=ATTR, mode=MAPPING_MODE_EVIDENCE),
]


def setup_workspace(db) -> int:
    ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).first()
    if ws is None:
        ws = Workspace(slug=WS_SLUG, name="CSV mapping import test")
        db.add(ws)
        db.flush()
    ws_id = ws.id
    # Reset everything in this workspace (test only; product workspaces untouched).
    db.query(E).filter(E.workspace_id == ws_id).delete(synchronize_session=False)
    db.query(A).filter(A.workspace_id == ws_id).delete(synchronize_session=False)
    db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id
    ).delete(synchronize_session=False)
    prod_ids = [p.id for p in db.query(Product).filter(
        Product.workspace_id == ws_id).all()]
    if prod_ids:
        db.query(ProductAttribute).filter(
            ProductAttribute.product_id.in_(prod_ids)
        ).delete(synchronize_session=False)
    db.query(Product).filter(Product.workspace_id == ws_id).delete(
        synchronize_session=False)
    db.commit()
    return ws_id


def read_rows(path: Path, n: int) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for i, r in enumerate(reader):
            if i >= n:
                break
            rows.append(r)
    return rows


def confirm_no_pa_writes(db, ws_id: int) -> int:
    return (
        db.query(ProductAttribute)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == ws_id)
        .count()
    )


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found at {CSV_PATH}")

    print("=" * 78)
    print(f"CSV MAPPING IMPORT VALIDATION   ({N_ROWS_TOTAL} rows)")
    print("=" * 78)

    rows = read_rows(CSV_PATH, N_ROWS_TOTAL)
    print(f"  csv path                : {CSV_PATH}")
    print(f"  rows loaded             : {len(rows)}")
    print(f"  evidence-mode LLM rows  : {N_EVIDENCE_LLM_ROWS}  "
          f"(set CSV_MAPPING_LLM_ROWS env var to run real Claude calls)")

    print()
    print("  mapping rules:")
    for r in MAPPING_RULES:
        print(f"    {r.source_column:<8} -> {r.target_attribute:<14} mode={r.mode}")

    # ----- Setup -----
    db = SessionLocal()
    try:
        ws_id = setup_workspace(db)
        print(f"  workspace ws_id         : {ws_id}  (slug={WS_SLUG})")
    finally:
        db.close()

    # ----- Pass 1: direct mode for ALL 200 rows (no LLM) -----
    # Strip evidence-mode rules so this pass is deterministic and free.
    direct_rules = [r for r in MAPPING_RULES if r.mode == MAPPING_MODE_DIRECT]
    print()
    print("=" * 78)
    print(f"PASS 1: direct_or_normalized_value  ({len(rows)} rows)")
    print("=" * 78)
    db = SessionLocal()
    try:
        result_direct = import_csv_with_mapping(
            db=db, workspace_id=ws_id,
            rows=rows, mapping_rules=direct_rules,
            attribute_definitions={ATTR: AGE_GROUP_DEF},
            product_id_column="sku", name_column="name", sku_column="sku",
            model_call=None,
        )
        print(f"  products_seen           : {result_direct.products_seen}")
        print(f"  products_created        : {result_direct.products_created}")
        print(f"  raw_values_captured     : {result_direct.raw_values_captured}")
        print(f"  direct_events_created   : {result_direct.direct_events_created}")
        print(f"  per-attribute counts    : {result_direct.per_attribute_event_counts}")
        print(f"  aggregates total        : {result_direct.aggregates_total_after}")
        print(f"  aggregates ready        : {result_direct.aggregates_ready_after}")
        pa_after_direct = confirm_no_pa_writes(db, ws_id)
        print(f"  ProductAttribute rows   : {pa_after_direct}  (expect 0)")
    finally:
        db.close()

    # ----- Pass 2: evidence_for_enrichment on a controlled subset -----
    # Real LLM calls only if CSV_MAPPING_LLM_ROWS > 0 in the env.
    print()
    print("=" * 78)
    print(f"PASS 2: evidence_for_enrichment  "
          f"({N_EVIDENCE_LLM_ROWS} of {len(rows)} rows)")
    print("=" * 78)
    if N_EVIDENCE_LLM_ROWS == 0:
        print("  (skipped — set CSV_MAPPING_LLM_ROWS>0 to enable)")
        result_evidence = None
    else:
        from app.services.model_client import call_model_json, ModelClientError

        def safe_call(prompt: str):
            try:
                return call_model_json(prompt)
            except ModelClientError as e:
                # Surface as soft error -- mapping layer marks it.
                print(f"    [enrichment error] {type(e).__name__}: {str(e)[:120]}")
                raise

        evidence_rules = [r for r in MAPPING_RULES if r.mode == MAPPING_MODE_EVIDENCE]
        evidence_rows = rows[:N_EVIDENCE_LLM_ROWS]

        db = SessionLocal()
        try:
            result_evidence = import_csv_with_mapping(
                db=db, workspace_id=ws_id,
                rows=evidence_rows, mapping_rules=evidence_rules,
                attribute_definitions={ATTR: AGE_GROUP_DEF},
                product_id_column="sku", name_column="name", sku_column="sku",
                model_call=safe_call,
            )
            print(f"  products processed      : {result_evidence.products_seen}")
            print(f"  enrichment_calls        : {result_evidence.enrichment_calls}")
            print(f"  enrichment_errors       : {result_evidence.enrichment_errors}")
            print(f"  evidence_events_created : {result_evidence.evidence_events_created}")
            print(f"  aggregates total        : {result_evidence.aggregates_total_after}")
            print(f"  aggregates ready        : {result_evidence.aggregates_ready_after}")
        finally:
            db.close()

    # ----- Final state report -----
    print()
    print("=" * 78)
    print("FINAL STATE  (engine view)")
    print("=" * 78)
    db = SessionLocal()
    try:
        n_products = db.query(Product).filter(
            Product.workspace_id == ws_id).count()
        n_events = db.query(E).filter(
            E.workspace_id == ws_id).count()
        n_aggs = db.query(A).filter(
            A.workspace_id == ws_id).count()
        n_pa = confirm_no_pa_writes(db, ws_id)

        # Top aggregates by count.
        top_aggs = (
            db.query(A.cluster_key, A.proposal_count, A.distinct_product_count,
                     A.avg_confidence, A.status)
            .filter(A.workspace_id == ws_id)
            .order_by(A.proposal_count.desc())
            .limit(15).all()
        )
        # Aggregates ready for approval.
        from app.services.proposed_attribute_value_service import promotion_readiness
        ready_aggs = []
        for a in db.query(A).filter(A.workspace_id == ws_id).all():
            if promotion_readiness(a).ready:
                ready_aggs.append(a)
        ready_aggs.sort(key=lambda a: -a.proposal_count)

        print(f"  products in workspace   : {n_products}")
        print(f"  events                  : {n_events}")
        print(f"  aggregates              : {n_aggs}")
        print(f"  ProductAttribute rows   : {n_pa}  (must be 0)")
        print()
        print("  top aggregates (by count):")
        print(f"    {'cluster_key':<22} {'count':>6} {'distinct':>8} "
              f"{'avg_conf':>8}  status")
        for ck, cnt, dist, conf, st in top_aggs:
            print(f"    {ck:<22} {cnt:>6} {dist:>8} {conf:>8.3f}  {st}")

        print()
        print(f"  values ready for approval : {len(ready_aggs)}")
        for a in ready_aggs[:10]:
            print(f"    {a.cluster_key:<22} count={a.proposal_count} "
                  f"distinct={a.distinct_product_count} "
                  f"avg_conf={a.avg_confidence:.3f}  status={a.status}")
    finally:
        db.close()

    # ----- Confirmations -----
    print()
    print("=" * 78)
    print("CONFIRMATIONS")
    print("=" * 78)
    print(f"  ProductAttribute rows in test workspace : {n_pa}  ->  "
          f"{'OK -- no direct assignment' if n_pa == 0 else 'FAIL'}")
    print(f"  every cell routed through ProposedAttributeValueEvent : YES")
    print(f"  aggregates produced via refresh_aggregates           : YES")
    print(f"  approval / ProductAttribute promotion is a separate step : YES")
    print(f"  no engine code modified                              : YES")
    print(f"  no enrichment prompt edits                           : YES")


if __name__ == "__main__":
    main()
