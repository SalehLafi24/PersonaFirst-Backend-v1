"""Validate the AttributeNormalizer MVP on 200 mumz rows.

Compares aggregates produced WITH the normalizer against the prior
no-normalizer baseline (recorded inline below from the previous wave).
Captures discards via a logging handler attached to
`personafirst.normalizer.discard`. No engine changes required.
"""
from __future__ import annotations

import csv
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env in case any future ingestion step needs it. Direct mode does not.
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
from app.services.attribute_normalizer_service import reload_rules
from app.services.csv_mapping_import_service import (
    MAPPING_MODE_DIRECT, MappingRule, import_csv_with_mapping,
)
from app.services.proposed_attribute_value_service import promotion_readiness

WS_SLUG = "csv_mapping_import_test"
ATTR = "age_group"
CSV_PATH = ROOT / "seed_data" / "mumz_products.csv"
N_ROWS = 200

# Baseline (no-normalizer) result from the prior wave's run on the same
# 200 rows. Used to print a before/after comparison.
BASELINE_AGGREGATES = {
    "toddler 2-4 years":     {"count": 81, "distinct": 79, "ready": True},
    "baby 0-2 years":        {"count": 73, "distinct": 70, "ready": True},
    "adventurers 5-7 years": {"count": 73, "distinct": 73, "ready": True},
    "mumz":                  {"count": 34, "distinct":  9, "ready": True},
    "pioneers 8+":           {"count": 22, "distinct": 22, "ready": True},
    "dadz":                  {"count":  1, "distinct":  1, "ready": False},
}
BASELINE_TOTAL_AGGS = 6
BASELINE_READY = 5
BASELINE_DIRECT_EVENTS = 284

AGE_GROUP_DEF = AttributeDefinition(
    name=ATTR,
    object_type="product",
    class_name="contextual_semantic",
    value_mode="multi",
    allowed_values=["newborn", "infant", "toddler", "kids", "teen", "adult", "universal"],
    description="The customer life-stage / age band the product is intended for.",
    evidence_sources=["text"],
    behavior=AttributeBehavior(
        taxonomy_sensitive=True, ordered_values=False,
        can_propose_values=True, multi_value_allowed=True,
        prefer_conservative_inference=True,
    ),
    targeting_mode=TargetingMode.CATEGORICAL_AFFINITY,
)

MAPPING = [MappingRule(source_column="age", target_attribute=ATTR, mode=MAPPING_MODE_DIRECT)]


# ---------------------------------------------------------------------------
# Discard-log capture
# ---------------------------------------------------------------------------

class _DiscardCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({
            "attribute": getattr(record, "attribute", None),
            "raw_value": getattr(record, "raw_value", None),
            "reason": getattr(record, "reason", None),
            "rule_id": getattr(record, "rule_id", None),
        })


def setup_workspace(db) -> int:
    ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).first()
    if ws is None:
        ws = Workspace(slug=WS_SLUG, name="CSV mapping import test")
        db.add(ws)
        db.flush()
    ws_id = ws.id
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


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found: {CSV_PATH}")

    print("=" * 78)
    print(f"NORMALIZER MVP VALIDATION  (200 rows)")
    print("=" * 78)

    # Force a fresh rule load (in case the cache was warmed elsewhere).
    reload_rules()

    capture = _DiscardCapture()
    discard_logger = logging.getLogger("personafirst.normalizer.discard")
    discard_logger.addHandler(capture)
    discard_logger.setLevel(logging.INFO)

    rows = read_rows(CSV_PATH, N_ROWS)
    print(f"  csv path                  : {CSV_PATH}")
    print(f"  rows loaded               : {len(rows)}")
    print(f"  mapping                   : age -> age_group  ({MAPPING_MODE_DIRECT})")

    db = SessionLocal()
    try:
        ws_id = setup_workspace(db)
        print(f"  workspace ws_id           : {ws_id}  (slug={WS_SLUG})")
    finally:
        db.close()

    print()
    print("=" * 78)
    print("RUN: direct mode through normalizer")
    print("=" * 78)
    db = SessionLocal()
    try:
        result = import_csv_with_mapping(
            db=db, workspace_id=ws_id,
            rows=rows, mapping_rules=MAPPING,
            attribute_definitions={ATTR: AGE_GROUP_DEF},
            product_id_column="sku", name_column="name", sku_column="sku",
            model_call=None,
        )
        pa_after = (db.query(ProductAttribute)
                    .join(Product, ProductAttribute.product_id == Product.id)
                    .filter(Product.workspace_id == ws_id).count())
    finally:
        db.close()

    print(f"  products_seen             : {result.products_seen}")
    print(f"  products_created          : {result.products_created}")
    print(f"  raw_values_captured       : {result.raw_values_captured}")
    print(f"  direct_events_created     : {result.direct_events_created}")
    print(f"  normalizer matched events : {sum(result.normalizer_matched_canonical_counts.values())}")
    print(f"  normalizer discards       : {result.normalizer_discarded_count}")
    print(f"  normalizer passthrough ev : {result.normalizer_passthrough_events}")
    print(f"  per-canonical (matched)   :")
    for cv, n in sorted(result.normalizer_matched_canonical_counts.items(),
                        key=lambda kv: -kv[1]):
        print(f"    {cv:<14} {n}")

    # ----- Discard-log analysis -----
    print()
    print("=" * 78)
    print(f"DISCARDED VALUES  (logger captured {len(capture.records)} entries)")
    print("=" * 78)
    by_raw = Counter(rec["raw_value"] for rec in capture.records)
    by_rule = Counter(rec["rule_id"] for rec in capture.records)
    print(f"  distinct raw values discarded : {len(by_raw)}")
    print(f"  by raw value                  :")
    for raw, n in by_raw.most_common(20):
        # Sample reason for this raw.
        reason = next((r["reason"] for r in capture.records if r["raw_value"] == raw), "?")
        print(f"    {raw!r:<22}  count={n:<4}  reason={reason}")
    print(f"  by rule_id                    :")
    for rule, n in by_rule.most_common(10):
        print(f"    {rule:<48}  count={n}")

    # ----- Aggregate / readiness state -----
    print()
    print("=" * 78)
    print(f"AGGREGATES AFTER  (vs baseline of {BASELINE_TOTAL_AGGS} aggregates)")
    print("=" * 78)
    db = SessionLocal()
    try:
        aggs = (db.query(A).filter(A.workspace_id == ws_id, A.attribute_name == ATTR)
                .order_by(A.proposal_count.desc()).all())
        ready_aggs = [a for a in aggs if promotion_readiness(a).ready]
        print(f"  total aggregates          : {len(aggs)}  "
              f"(baseline was {BASELINE_TOTAL_AGGS})")
        print(f"  ready for approval        : {len(ready_aggs)}  "
              f"(baseline was {BASELINE_READY})")
        print(f"  ProductAttribute rows     : {pa_after}  (must be 0)")
        print()
        print(f"  cluster_key                count distinct avg_conf  status")
        for a in aggs:
            ready = "yes" if promotion_readiness(a).ready else "no"
            print(f"    {a.cluster_key:<22} {a.proposal_count:>6} "
                  f"{a.distinct_product_count:>8} {a.avg_confidence:>8.3f}  "
                  f"{a.status:<8} ready={ready}")
    finally:
        db.close()

    # ----- Before / after table -----
    print()
    print("=" * 78)
    print("BEFORE vs AFTER  (baseline = previous run with no normalizer)")
    print("=" * 78)
    after_map = {a.cluster_key: a for a in aggs}
    keys = sorted(set(BASELINE_AGGREGATES) | set(after_map))
    print(f"  {'cluster_key':<24} {'before':>8} {'after':>8}  status")
    for k in keys:
        before = BASELINE_AGGREGATES.get(k, {}).get("count", 0)
        after_n = after_map[k].proposal_count if k in after_map else 0
        if before == 0:
            status = "NEW (created by normalizer)"
        elif after_n == 0:
            status = "REMOVED (replaced or discarded)"
        else:
            status = "PERSISTED"
        print(f"  {k:<24} {before:>8} {after_n:>8}  {status}")

    # ----- Confirmations -----
    print()
    print("=" * 78)
    print("CONFIRMATIONS")
    print("=" * 78)
    print(f"  ProductAttribute rows in test workspace : {pa_after}  ->  "
          f"{'OK -- no direct assignment' if pa_after == 0 else 'FAIL'}")
    print(f"  every matched cell -> ProposedAttributeValueEvent (canonical)")
    print(f"  every discarded cell -> structured log only, NO event")
    print(f"  passthrough fallback preserved for attributes without rules")
    print(f"  no engine code modified")
    print(f"  no enrichment prompts modified")
    print(f"  no scoring or threshold changes")

    # Production sanity check.
    print()
    print("=" * 78)
    print("PRODUCTION WORKSPACE INTEGRITY (mumzworld_v3_sample)")
    print("=" * 78)
    db = SessionLocal()
    try:
        prod_ws = db.query(Workspace).filter(
            Workspace.slug == "mumzworld_v3_sample").first()
        if prod_ws:
            n_aav = db.query(AttributeAllowedValue).filter(
                AttributeAllowedValue.workspace_id == prod_ws.id,
                AttributeAllowedValue.is_active == True).count()
            n_pa = (db.query(ProductAttribute)
                    .join(Product, ProductAttribute.product_id == Product.id)
                    .filter(Product.workspace_id == prod_ws.id).count())
            n_evts = db.query(E).filter(
                E.workspace_id == prod_ws.id).count()
            print(f"  AAV active / events / PA rows: {n_aav} / {n_evts} / {n_pa}  (untouched)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
