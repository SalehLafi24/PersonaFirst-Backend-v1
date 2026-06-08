"""Import brand attribute on mumzworld_v3_sample.

Pipeline order:
  CSV mapping -> normalization (cleaning rules) -> events -> aggregates.
  Approval and backfill are NOT executed; this script reports the
  candidate aggregates and a recommended strategy.
"""
from __future__ import annotations

import csv
import logging
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
from app.schemas.attribute_enrichment import (
    AttributeBehavior, AttributeDefinition, TargetingMode,
)
from app.services.attribute_normalizer_service import reload_rules, normalize_cell
from app.services.csv_mapping_import_service import (
    MAPPING_MODE_DIRECT, MappingRule, import_csv_with_mapping,
)
from app.services.proposed_attribute_value_service import promotion_readiness

WS_SLUG = "mumzworld_v3_sample"
ATTR = "brand"
CSV_PATH = ROOT / "seed_data" / "mumz_products.csv"

# Open-taxonomy attribute definition; allowed_values left empty (brand
# is high-cardinality; canonicals come from approvals, not pre-config).
BRAND_DEF = AttributeDefinition(
    name=ATTR,
    object_type="product",
    class_name="contextual_semantic",
    value_mode="single",
    allowed_values=[],
    description="The product's brand or maker.",
    evidence_sources=["text"],
    behavior=AttributeBehavior(
        taxonomy_sensitive=True, ordered_values=False,
        can_propose_values=True, multi_value_allowed=False,
        prefer_conservative_inference=True,
    ),
    targeting_mode=TargetingMode.CATEGORICAL_AFFINITY,
)

MAPPING = [MappingRule(source_column="brand", target_attribute=ATTR, mode=MAPPING_MODE_DIRECT)]


def stream_filtered_rows(csv_path: Path, allowed_skus: set[str]):
    """Stream CSV rows matching v3 SKUs (so no new products are created)."""
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get("sku") or "").strip()
            if sku in allowed_skus:
                yield row


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
        # Pre-state.
        before = {
            "products": len(v3_skus),
            "events_total": db.query(E).filter(E.workspace_id == ws_id).count(),
            "events_brand": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "agg_brand": db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR).count(),
            "aav_brand_active": db.query(AttributeAllowedValue).filter(
                AttributeAllowedValue.workspace_id == ws_id,
                AttributeAllowedValue.attribute_name == ATTR,
                AttributeAllowedValue.is_active == True).count(),
            "pa_brand": db.query(ProductAttribute)
                          .join(Product, ProductAttribute.product_id == Product.id)
                          .filter(Product.workspace_id == ws_id,
                                  ProductAttribute.attribute_id == ATTR).count(),
            "pa_total": db.query(ProductAttribute)
                          .join(Product, ProductAttribute.product_id == Product.id)
                          .filter(Product.workspace_id == ws_id).count(),
        }
    finally:
        db.close()

    print("=" * 80)
    print(f"BRAND IMPORT  ({WS_SLUG} ws_id={ws_id})")
    print("=" * 80)
    print(f"  v3 products              : {before['products']}")
    print(f"  PRE  brand events        : {before['events_brand']}")
    print(f"  PRE  brand aggregates    : {before['agg_brand']}")
    print(f"  PRE  brand AAV active    : {before['aav_brand_active']}")
    print(f"  PRE  brand PA rows       : {before['pa_brand']}")

    # ----- 1. Brand coverage in CSV -----
    rows = list(stream_filtered_rows(CSV_PATH, v3_skus))
    cells_total = len(rows)
    cells_with_brand = sum(1 for r in rows if (r.get("brand") or "").strip())
    cells_empty = cells_total - cells_with_brand
    print()
    print("=" * 80)
    print("1. BRAND COVERAGE IN CSV (filtered to v3 SKUs)")
    print("=" * 80)
    print(f"  CSV rows matched         : {cells_total}")
    print(f"  rows with brand value    : {cells_with_brand}  "
          f"({100*cells_with_brand/max(cells_total,1):.2f}%)")
    print(f"  rows with empty brand    : {cells_empty}")

    # ----- 4. Normalization examples (preview before importing) -----
    print()
    print("=" * 80)
    print("4. NORMALIZATION EXAMPLES (preview)")
    print("=" * 80)
    examples = []
    seen_raw: set[str] = set()
    for r in rows:
        raw = (r.get("brand") or "").strip()
        if not raw or raw in seen_raw:
            continue
        seen_raw.add(raw)
        if len(examples) >= 12:
            break
        results = normalize_cell(ATTR, raw)
        for nr in results:
            cleaned = nr.proposed_value or nr.canonical_value or "(unchanged)"
            examples.append((raw, nr.decision, cleaned, nr.rule_id))
    print(f"  {'raw':<32} {'decision':<12} {'cleaned':<24}")
    for raw, decision, cleaned, _ in examples:
        print(f"  {raw!r:<32} {decision:<12} {cleaned!r}")

    # ----- 2. Run import -----
    print()
    print("=" * 80)
    print("2. RUN IMPORT (CSV mapping -> normalize -> events -> aggregates)")
    print("=" * 80)
    db = SessionLocal()
    try:
        result = import_csv_with_mapping(
            db=db, workspace_id=ws_id,
            rows=rows, mapping_rules=MAPPING,
            attribute_definitions={ATTR: BRAND_DEF},
            product_id_column="sku", name_column="name", sku_column="sku",
            model_call=None,
        )
        print(f"  products_seen            : {result.products_seen}")
        print(f"  products_created         : {result.products_created}  (must be 0)")
        print(f"  raw_values_captured      : {result.raw_values_captured}")
        print(f"  direct_events_created    : {result.direct_events_created}")
        print(f"  matched events           : {sum(result.normalizer_matched_canonical_counts.values())}")
        print(f"  passthrough events       : {result.normalizer_passthrough_events}")
        print(f"  discards                 : {result.normalizer_discarded_count}")
        print(f"  brand aggregates total   : {result.aggregates_total_after}")
        print(f"  ready for approval       : {result.aggregates_ready_after}")
    finally:
        db.close()

    # ----- 3. Top brand aggregates + 5. duplicate/variant clusters -----
    print()
    print("=" * 80)
    print("3. TOP 50 BRAND AGGREGATES")
    print("=" * 80)
    db = SessionLocal()
    try:
        aggs = (db.query(A)
                .filter(A.workspace_id == ws_id, A.attribute_name == ATTR)
                .order_by(A.proposal_count.desc()).all())
        print(f"  total brand aggregates : {len(aggs)}")
        print(f"  {'cluster_key':<32} {'count':>5} {'distinct':>8} {'avg_conf':>8}")
        for a in aggs[:50]:
            ready = "yes" if promotion_readiness(a).ready else "no"
            print(f"  {a.cluster_key:<32} {a.proposal_count:>5} "
                  f"{a.distinct_product_count:>8} {a.avg_confidence:>8.3f}  ready={ready}")

        # ----- 5. duplicate / variant detection -----
        # Group by alphanumeric signature (lowercase + only [a-z0-9]) to
        # surface near-duplicates the cleaning didn't catch.
        import re
        sig_groups: dict[str, list[A]] = defaultdict(list)
        for a in aggs:
            sig = re.sub(r"[^a-z0-9]", "", a.cluster_key.lower())
            sig_groups[sig].append(a)
        dup_groups = [(sig, group) for sig, group in sig_groups.items() if len(group) > 1]

        # ----- 6. values ready for approval -----
        ready_aggs = [a for a in aggs if promotion_readiness(a).ready]
    finally:
        db.close()

    print()
    print("=" * 80)
    print("5. DUPLICATE / VARIANT CLUSTERS (same alphanumeric signature)")
    print("=" * 80)
    if not dup_groups:
        print("  (none -- cleaning collapsed all variants to a canonical form)")
    else:
        print(f"  {len(dup_groups)} signature groups with multiple cluster_keys:")
        for sig, group in dup_groups[:15]:
            keys = sorted([(g.cluster_key, g.proposal_count) for g in group],
                          key=lambda x: -x[1])
            print(f"    sig={sig!r}: " + ", ".join(f"{k}({n})" for k, n in keys))

    # ----- 6. values ready for approval -----
    print()
    print("=" * 80)
    print("6. VALUES READY FOR APPROVAL  (engine readiness gate)")
    print("=" * 80)
    print(f"  total ready (count>=3, distinct>=2, avg_conf>=0.85): {len(ready_aggs)}")
    by_count_buckets = Counter()
    for a in ready_aggs:
        if   a.proposal_count >= 50: by_count_buckets[">=50"] += 1
        elif a.proposal_count >= 20: by_count_buckets["20-49"] += 1
        elif a.proposal_count >= 10: by_count_buckets["10-19"] += 1
        elif a.proposal_count >= 5:  by_count_buckets["5-9"] += 1
        else:                         by_count_buckets["3-4"] += 1
    for bucket in (">=50", "20-49", "10-19", "5-9", "3-4"):
        print(f"    {bucket:<8} : {by_count_buckets.get(bucket, 0)}")
    print()
    if ready_aggs:
        print(f"  top 15 ready brands by count :")
        for a in sorted(ready_aggs, key=lambda x: -x.proposal_count)[:15]:
            print(f"    {a.cluster_key:<32} count={a.proposal_count:>4} "
                  f"distinct={a.distinct_product_count:>4} avg_conf={a.avg_confidence:.3f}")

    # ----- 7. Recommended approval strategy -----
    print()
    print("=" * 80)
    print("7. RECOMMENDED APPROVAL STRATEGY")
    print("=" * 80)
    print(f"""
  Brand is open-taxonomy and high-cardinality, with {len(aggs)} distinct
  cluster_keys discovered. Approving all {len(ready_aggs)} ready aggregates
  in one batch is technically fine (every event is already cleaned and
  every cluster_key is a normalized brand string), but the approval set
  is large enough to warrant a tiered, controlled batch:

    Wave A  (highest signal, count >= 10)
        -- brands with >= 10 products. Approval is unambiguous; these
           dominate the catalog and unlock the most recommendation
           value immediately.
        -- count: {by_count_buckets.get('10-19', 0) + by_count_buckets.get('20-49', 0) + by_count_buckets.get('>=50', 0)}

    Wave B  (medium signal, count 5-9)
        -- approve after a quick eyeball of the cluster_keys; some may
           be near-duplicates of Wave A canonicals (cleaning may have
           missed an edge case like a UK/US spelling variant).
        -- count: {by_count_buckets.get('5-9', 0)}

    Wave C  (long tail, count 3-4)
        -- approve in bulk only after Wave A + B; many of these will be
           legitimate small brands. Rejecting noisy ones (e.g. retailer
           house labels accidentally tagged) in this wave is expected.
        -- count: {by_count_buckets.get('3-4', 0)}

  After each wave, run the existing brand backfill (one PA(brand) row per
  product, single-value attribute). The recommender will start using the
  brand-match signal as soon as Wave A is backfilled.

  No automatic approval was performed by this script.
    """)

    # ----- 8. Confirmations -----
    db = SessionLocal()
    try:
        after = {
            "events_total": db.query(E).filter(E.workspace_id == ws_id).count(),
            "events_brand": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "agg_brand": db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR).count(),
            "aav_brand_active": db.query(AttributeAllowedValue).filter(
                AttributeAllowedValue.workspace_id == ws_id,
                AttributeAllowedValue.attribute_name == ATTR,
                AttributeAllowedValue.is_active == True).count(),
            "pa_brand": db.query(ProductAttribute)
                          .join(Product, ProductAttribute.product_id == Product.id)
                          .filter(Product.workspace_id == ws_id,
                                  ProductAttribute.attribute_id == ATTR).count(),
            "pa_total": db.query(ProductAttribute)
                          .join(Product, ProductAttribute.product_id == Product.id)
                          .filter(Product.workspace_id == ws_id).count(),
        }
    finally:
        db.close()

    print("=" * 80)
    print("8. CONFIRMATIONS  (no recommendations re-run; no other data touched)")
    print("=" * 80)
    print(f"  events total            : {before['events_total']} -> {after['events_total']}  "
          f"(diff=+{after['events_total'] - before['events_total']})")
    print(f"  events (brand)          : {before['events_brand']} -> {after['events_brand']}")
    print(f"  aggregates (brand)      : {before['agg_brand']} -> {after['agg_brand']}")
    print(f"  AAV active (brand)      : {before['aav_brand_active']} -> {after['aav_brand_active']}  (must stay 0 -- no approval)")
    print(f"  PA rows (brand)         : {before['pa_brand']} -> {after['pa_brand']}  (must stay 0 -- no backfill)")
    print(f"  PA rows total           : {before['pa_total']} -> {after['pa_total']}  "
          f"(should equal -- only brand events were added; PA layer untouched)")
    print()
    print("  No recommendations re-run.")
    print("  No approval performed.")
    print("  No backfill performed.")
    print("  No engine code modified.")
    print("  No ProductAttribute rows written from this CSV import.")


if __name__ == "__main__":
    main()
