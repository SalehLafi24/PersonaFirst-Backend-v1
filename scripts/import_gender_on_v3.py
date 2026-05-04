"""Import the `gender` attribute on mumzworld_v3_sample.

Pipeline order (per spec; no shortcuts):
    CSV mapping  ->  normalization (closed taxonomy + synonyms)
                 ->  ProposedAttributeValueEvent
                 ->  ProposedAttributeValueAggregate

Approval and backfill are NOT performed here -- run
`scripts/approve_gender_and_backfill.py` afterwards.

Idempotent: if any gender events already exist for the workspace, the
import phase is skipped (the report still prints aggregates).
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
from app.services.attribute_normalizer_service import normalize_cell, reload_rules
from app.services.csv_mapping_import_service import (
    MAPPING_MODE_DIRECT, MappingRule, import_csv_with_mapping,
)
from app.services.proposed_attribute_value_service import promotion_readiness

WS_SLUG = "mumzworld_v3_sample"
ATTR = "gender"
CSV_PATH = ROOT / "seed_data" / "mumz_products.csv"
SOURCE_COLUMN = "gender"
PRODUCT_ID_COLUMN = "sku"

MAPPING = [MappingRule(
    source_column=SOURCE_COLUMN,
    target_attribute=ATTR,
    mode=MAPPING_MODE_DIRECT,
)]


def stream_filtered_rows(csv_path: Path, allowed_skus: set[str]):
    """Stream CSV rows whose sku exists in the v3 workspace.
    Guarantees no products are created by this import."""
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get(PRODUCT_ID_COLUMN) or "").strip()
            if sku in allowed_skus:
                yield row


def snapshot(db, ws_id: int) -> dict:
    return {
        "events_total": db.query(E).filter(E.workspace_id == ws_id).count(),
        "events_gender": db.query(E).filter(
            E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
        "agg_gender": db.query(A).filter(
            A.workspace_id == ws_id, A.attribute_name == ATTR).count(),
        "aav_gender_active": db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.is_active == True).count(),
        "pa_gender": db.query(ProductAttribute)
                       .join(Product, ProductAttribute.product_id == Product.id)
                       .filter(Product.workspace_id == ws_id,
                               ProductAttribute.attribute_id == ATTR).count(),
        "pa_total": db.query(ProductAttribute)
                      .join(Product, ProductAttribute.product_id == Product.id)
                      .filter(Product.workspace_id == ws_id).count(),
    }


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found: {CSV_PATH}")
    reload_rules()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id
        v3_skus = {p.product_id for p in db.query(Product).filter(
            Product.workspace_id == ws_id).all()}
        before = snapshot(db, ws_id)
    finally:
        db.close()

    print("=" * 80)
    print(f"GENDER IMPORT  ({WS_SLUG} ws_id={ws_id})")
    print("=" * 80)
    print(f"  v3 products              : {len(v3_skus)}")
    print(f"  PRE  gender events       : {before['events_gender']}")
    print(f"  PRE  gender aggregates   : {before['agg_gender']}")
    print(f"  PRE  gender AAV active   : {before['aav_gender_active']}")
    print(f"  PRE  gender PA rows      : {before['pa_gender']}")

    rows = list(stream_filtered_rows(CSV_PATH, v3_skus))
    cells_total = len(rows)
    cells_with_gender = sum(1 for r in rows if (r.get(SOURCE_COLUMN) or "").strip())
    cells_empty = cells_total - cells_with_gender

    print()
    print("=" * 80)
    print("1. CSV COLUMN INSPECTION")
    print("=" * 80)
    print(f"  CSV rows matched         : {cells_total}")
    print(f"  rows with gender value   : {cells_with_gender}  "
          f"({100*cells_with_gender/max(cells_total,1):.2f}%)")
    print(f"  rows with empty gender   : {cells_empty}  (will default to unisex at backfill)")

    raw_value_counts = Counter(
        (r.get(SOURCE_COLUMN) or "").strip()
        for r in rows
        if (r.get(SOURCE_COLUMN) or "").strip()
    )
    print(f"  distinct raw values      : {len(raw_value_counts)}")
    print(f"  top raw values           :")
    for v, n in raw_value_counts.most_common(15):
        print(f"    {v!r:<28} {n}")

    print()
    print("=" * 80)
    print("2. NORMALIZATION PREVIEW (raw -> canonical)")
    print("=" * 80)
    print(f"  {'raw':<28} {'decision':<12} {'canonical':<12} rule_id")
    edge_cases: list[tuple[str, str, str | None, str | None]] = []
    examples_shown = 0
    seen_raw: set[str] = set()
    for raw_value, _ in raw_value_counts.most_common():
        if raw_value in seen_raw:
            continue
        seen_raw.add(raw_value)
        results = normalize_cell(ATTR, raw_value)
        for nr in results:
            canon = nr.canonical_value or ""
            if examples_shown < 25:
                print(f"  {raw_value!r:<28} {nr.decision:<12} {canon:<12} {nr.rule_id}")
                examples_shown += 1
            if nr.decision == "discarded":
                edge_cases.append((raw_value, nr.decision, canon, nr.rule_id))

    if before["events_gender"] > 0:
        print()
        print("=" * 80)
        print("3. IMPORT  (SKIPPED -- gender events already present)")
        print("=" * 80)
        print(f"  existing events          : {before['events_gender']}")
        print(f"  Re-import would duplicate. To re-run, delete prior gender")
        print(f"  events first:")
        print(f"    DELETE FROM proposed_attribute_value_events")
        print(f"      WHERE workspace_id={ws_id} AND attribute_name='{ATTR}';")
    else:
        print()
        print("=" * 80)
        print("3. RUN IMPORT (CSV -> normalize -> events -> aggregates)")
        print("=" * 80)
        db = SessionLocal()
        try:
            result = import_csv_with_mapping(
                db=db, workspace_id=ws_id,
                rows=rows, mapping_rules=MAPPING,
                attribute_definitions={},   # direct mode needs no AttributeDefinition
                product_id_column=PRODUCT_ID_COLUMN,
                name_column="name", sku_column="sku",
                model_call=None,
            )
            print(f"  products_seen            : {result.products_seen}")
            print(f"  products_created         : {result.products_created}  (must be 0)")
            print(f"  raw_values_captured      : {result.raw_values_captured}")
            print(f"  direct_events_created    : {result.direct_events_created}")
            print(f"  matched events           : {sum(result.normalizer_matched_canonical_counts.values())}")
            print(f"    by canonical:")
            for cv, n in sorted(result.normalizer_matched_canonical_counts.items()):
                print(f"      {cv:<10} {n}")
            print(f"  passthrough events       : {result.normalizer_passthrough_events}  (must be 0; closed taxonomy)")
            print(f"  discards (normalizer)    : {result.normalizer_discarded_count}")
            print(f"  gender aggregates total  : {result.aggregates_total_after}")
            print(f"  ready for approval       : {result.aggregates_ready_after}")
            if result.products_created:
                raise SystemExit(
                    f"FATAL: {result.products_created} new products were created -- "
                    f"row filter is broken")
        finally:
            db.close()

    print()
    print("=" * 80)
    print("4. AGGREGATES  (top by proposal_count)")
    print("=" * 80)
    db = SessionLocal()
    try:
        aggs = (db.query(A)
                .filter(A.workspace_id == ws_id, A.attribute_name == ATTR)
                .order_by(A.proposal_count.desc()).all())
        print(f"  total gender aggregates : {len(aggs)}")
        print(f"  {'cluster_key':<20} {'canonical':<12} {'count':>5} "
              f"{'distinct':>8} {'avg_conf':>8} {'status':<10} ready")
        for a in aggs:
            ready = "yes" if promotion_readiness(a).ready else "no"
            print(f"  {a.cluster_key:<20} {a.canonical_value:<12} "
                  f"{a.proposal_count:>5} {a.distinct_product_count:>8} "
                  f"{a.avg_confidence:>8.3f} {a.status:<10} {ready}")
    finally:
        db.close()

    print()
    print("=" * 80)
    print("5. EDGE CASES  (raw values that the normalizer DISCARDED)")
    print("=" * 80)
    if not edge_cases:
        print("  (none -- every distinct raw value mapped to a canonical)")
    else:
        for raw_value, decision, _canon, rule_id in edge_cases:
            n = raw_value_counts.get(raw_value, 0)
            print(f"  raw={raw_value!r:<28} count={n:<5} reason={rule_id}")

    db = SessionLocal()
    try:
        after = snapshot(db, ws_id)
    finally:
        db.close()

    print()
    print("=" * 80)
    print("6. CONFIRMATIONS  (no other attributes touched)")
    print("=" * 80)
    print(f"  events total            : {before['events_total']} -> {after['events_total']}  "
          f"(diff=+{after['events_total'] - before['events_total']})")
    print(f"  events (gender)         : {before['events_gender']} -> {after['events_gender']}")
    print(f"  aggregates (gender)     : {before['agg_gender']} -> {after['agg_gender']}")
    print(f"  AAV active (gender)     : {before['aav_gender_active']} -> {after['aav_gender_active']}  (must stay 0 -- no approval yet)")
    print(f"  PA rows (gender)        : {before['pa_gender']} -> {after['pa_gender']}  (must stay 0 -- no backfill yet)")
    print(f"  PA rows total           : {before['pa_total']} -> {after['pa_total']}  (must be unchanged)")
    print()
    print("  Pipeline used           : CSV -> normalize -> events -> aggregates")
    print("  No ProductAttribute writes from this script.")
    print("  No enrichment / no LLM call.")
    print("  No recommender code touched.")


if __name__ == "__main__":
    main()
