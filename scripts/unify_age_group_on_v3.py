"""Unify age_group onto mumzworld_v3_sample via the existing pipeline.

Steps (no new code — orchestrates already-built services):
  1. Stream mumz_products.csv, keep only rows whose `sku` matches an
     existing v3 Product.product_id (so no new products are created).
  2. Run csv_mapping_import_service with one mapping rule:
        age (column)  ->  age_group (attribute)  in direct mode.
     Per-cell behaviour comes from attribute_normalizer_service:
        matched   -> 0.98-confidence ProposedAttributeValueEvent on canonical
        discarded -> structured discard log only, no event
        passthrough -> existing fallback (no rule matches today for the
                       known age tokens, so this branch is unused)
  3. refresh_aggregates(attribute=age_group) is called inside the import.
  4. Approve 4 canonicals (infant, toddler, kids, teen) via the taxonomy
     admin endpoint; readiness gate enforced (no force=true).
  5. Backfill ProductAttribute(age_group) for every product that has an
     event resolving into the active AAV set; one row per product.

Constraints honoured:
  - product_type aggregates / approvals / PA rows are not touched.
  - No engine code modified.
  - No new attributes introduced.
  - No recommendations executed.
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

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
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

WS_SLUG = "mumzworld_v3_sample"
ATTR = "age_group"
APPROVE_TARGETS = ["infant", "toddler", "kids", "teen"]
CSV_PATH = ROOT / "seed_data" / "mumz_products.csv"

# Attribute definition reused from the test workspace setup. allowed_values
# matches the normalizer's synonym canonicals so promote-readiness checks
# behave identically.
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

client = TestClient(app)


# ---------------------------------------------------------------------------
# Discard-log capture (re-used pattern from the MVP validator).
# ---------------------------------------------------------------------------

class _DiscardCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({
            "raw_value": getattr(record, "raw_value", None),
            "reason": getattr(record, "reason", None),
            "rule_id": getattr(record, "rule_id", None),
        })


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def stream_filtered_rows(csv_path: Path, allowed_skus: set[str]):
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get("sku") or "").strip()
            if sku in allowed_skus:
                yield row


def status_counts(db, ws_id, attribute):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == attribute
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_active_count(db, ws_id, attribute):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == attribute,
        AttributeAllowedValue.is_active == True).count()


def coverage(db, ws_id, attribute):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_attr = (db.query(ProductAttribute.product_id)
                 .join(Product, ProductAttribute.product_id == Product.id)
                 .filter(Product.workspace_id == ws_id,
                         ProductAttribute.attribute_id == attribute)
                 .distinct().count())
    return total, with_attr


def run_age_group_backfill(db, ws_id):
    """Identical logic to scripts/approve_age_group_and_backfill.py:
    pick the highest-confidence event per product whose normalized_value
    resolves to an active AAV. Skip products that already have an
    age_group PA row. One PA row per product. Idempotent."""
    active_aav = {
        v for (v,) in db.query(AttributeAllowedValue.value).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.attribute_name == ATTR,
            AttributeAllowedValue.is_active == True,
        ).all()
    }
    active_lower = {v.lower() for v in active_aav}
    cluster_to_canonical: dict[str, str] = {}
    for agg in db.query(A).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        if agg.status == PROPOSAL_STATUS_APPROVED:
            canonical = agg.canonical_value
        elif agg.status == PROPOSAL_STATUS_MERGED:
            canonical = agg.promoted_to_allowed_value
        else:
            continue
        if canonical and canonical.lower() in active_lower:
            cluster_to_canonical[agg.cluster_key] = next(
                (v for v in active_aav if v.lower() == canonical.lower()),
                canonical,
            )

    products = db.query(Product).filter(Product.workspace_id == ws_id).all()
    already = {
        pid for (pid,) in db.query(ProductAttribute.product_id)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == ws_id,
                ProductAttribute.attribute_id == ATTR)
        .distinct().all()
    }
    events_by_pid: dict[str, list] = defaultdict(list)
    for ev in db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR
    ).all():
        events_by_pid[ev.product_id].append(ev)

    inserted = 0
    inserted_by_canonical: Counter = Counter()
    for prod in products:
        if prod.id in already:
            continue
        evs = events_by_pid.get(prod.product_id, [])
        if not evs:
            continue
        assignable = []
        for ev in evs:
            canonical = cluster_to_canonical.get(ev.normalized_value)
            if canonical is None:
                continue
            assignable.append((ev, canonical))
        if not assignable:
            continue
        ev, canonical = max(
            assignable,
            key=lambda x: (float(x[0].confidence or 0), x[0].created_at),
        )
        db.add(ProductAttribute(
            product_id=prod.id, attribute_id=ATTR, attribute_value=canonical,
        ))
        inserted += 1
        inserted_by_canonical[canonical] += 1
    if inserted:
        db.commit()
    return inserted, inserted_by_canonical


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found: {CSV_PATH}")
    reload_rules()

    capture = _DiscardCapture()
    discard_logger = logging.getLogger("personafirst.normalizer.discard")
    discard_logger.addHandler(capture)
    discard_logger.setLevel(logging.INFO)

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        # -------- Pre-state snapshot --------
        before = {
            "agg_age_group_status": status_counts(db, ws_id, ATTR),
            "agg_product_type_status": status_counts(db, ws_id, "product_type"),
            "aav_age_group_active": aav_active_count(db, ws_id, ATTR),
            "aav_product_type_active": aav_active_count(db, ws_id, "product_type"),
            "events_age_group": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "events_product_type": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == "product_type").count(),
            "pa_age_group": (db.query(ProductAttribute)
                             .join(Product, ProductAttribute.product_id == Product.id)
                             .filter(Product.workspace_id == ws_id,
                                     ProductAttribute.attribute_id == ATTR).count()),
            "pa_product_type": (db.query(ProductAttribute)
                                .join(Product, ProductAttribute.product_id == Product.id)
                                .filter(Product.workspace_id == ws_id,
                                        ProductAttribute.attribute_id == "product_type").count()),
        }
        v3_skus = {p.product_id for p in db.query(Product).filter(
            Product.workspace_id == ws_id).all()}
        total_products = len(v3_skus)
    finally:
        db.close()

    print("=" * 80)
    print(f"UNIFY age_group ON {WS_SLUG}  (ws_id={ws_id})")
    print("=" * 80)
    print(f"  total v3 products       : {total_products}")
    print(f"  PRE  age_group aggregates  : {before['agg_age_group_status']}")
    print(f"  PRE  product_type aggregates: {before['agg_product_type_status']}")
    print(f"  PRE  AAV active (age_group/product_type): "
          f"{before['aav_age_group_active']} / {before['aav_product_type_active']}")
    print(f"  PRE  events     (age_group/product_type): "
          f"{before['events_age_group']} / {before['events_product_type']}")
    print(f"  PRE  PA rows    (age_group/product_type): "
          f"{before['pa_age_group']} / {before['pa_product_type']}")

    # -------- Step 1+2+3+4: import filtered CSV through mapping layer --------
    print()
    print("=" * 80)
    print("STEP 1-4: filtered CSV -> normalize -> events -> aggregates")
    print("=" * 80)
    rows = list(stream_filtered_rows(CSV_PATH, v3_skus))
    print(f"  CSV rows matched against v3 product_id : {len(rows)}")

    db = SessionLocal()
    try:
        result = import_csv_with_mapping(
            db=db, workspace_id=ws_id,
            rows=rows, mapping_rules=MAPPING,
            attribute_definitions={ATTR: AGE_GROUP_DEF},
            product_id_column="sku", name_column="name", sku_column="sku",
            model_call=None,
        )
        # Sanity: confirm products_created == 0 (existing products only).
        print(f"  products_seen          : {result.products_seen}")
        print(f"  products_created       : {result.products_created}  (must be 0)")
        print(f"  raw_values_captured    : {result.raw_values_captured}")
        print(f"  direct_events_created  : {result.direct_events_created}")
        print(f"  matched events         : "
              f"{sum(result.normalizer_matched_canonical_counts.values())}")
        print(f"  per-canonical (matched):")
        for cv, n in sorted(result.normalizer_matched_canonical_counts.items(),
                            key=lambda kv: -kv[1]):
            print(f"    {cv:<14} {n}")
        print(f"  discards               : {result.normalizer_discarded_count}")
        print(f"  passthrough events     : {result.normalizer_passthrough_events}")
        print(f"  age_group aggregates   : {result.aggregates_total_after}")
        print(f"  ready for approval     : {result.aggregates_ready_after}")
    finally:
        db.close()

    if result.products_created != 0:
        raise SystemExit(
            f"unexpected: products_created={result.products_created} "
            f"(should be 0 because we filtered to existing v3 SKUs)"
        )

    # Show a quick discard summary.
    if capture.records:
        by_raw = Counter(r["raw_value"] for r in capture.records)
        print(f"  discarded raw values   :")
        for raw, n in by_raw.most_common(8):
            print(f"    {raw!r:<22} count={n}")

    # -------- Step 5: approve 4 canonicals --------
    print()
    print("=" * 80)
    print("STEP 5: approve infant / toddler / kids / teen")
    print("=" * 80)
    db = SessionLocal()
    try:
        agg_by_cluster: dict[str, A] = {}
        for ck in APPROVE_TARGETS:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            if agg is None:
                raise SystemExit(f"aggregate not found: {ck!r}")
            check = promotion_readiness(agg)
            if not check.ready:
                raise SystemExit(
                    f"{ck} not ready ({check.reasons}); aborting before any write"
                )
            if agg.status != "pending":
                raise SystemExit(
                    f"{ck} not pending (status={agg.status}); aborting"
                )
            agg_by_cluster[ck] = agg
            print(f"  pre-check  {ck:<10} agg.id={agg.id} count={agg.proposal_count} "
                  f"distinct={agg.distinct_product_count} avg_conf={agg.avg_confidence:.3f}")
    finally:
        db.close()

    for ck, agg in agg_by_cluster.items():
        r = client.post(
            f"/admin/taxonomy/api/aggregates/{agg.id}/approve",
            params={"workspace_id": ws_id, "attribute": ATTR},
        )
        if r.status_code != 200:
            print(f"  {ck:<10} HTTP {r.status_code}  body={r.text[:160]}")
            raise SystemExit(f"approval for {ck} failed")
        body = r.json()
        print(f"  {ck:<10} agg.id={agg.id}  HTTP 200  -> "
              f"status={body['status']} promoted_to={body['promoted_to_allowed_value']!r}")

    # -------- Step 6: backfill --------
    print()
    print("=" * 80)
    print("STEP 6: backfill ProductAttribute(age_group)")
    print("=" * 80)
    db = SessionLocal()
    try:
        inserted, by_canonical = run_age_group_backfill(db, ws_id)
    finally:
        db.close()
    print(f"  PA(age_group) rows inserted : {inserted}")
    for v, n in sorted(by_canonical.items(), key=lambda x: -x[1]):
        print(f"    {v:<14} {n}")

    # -------- Output 1-4 --------
    db = SessionLocal()
    try:
        # 1. age_group coverage.
        total, with_age = coverage(db, ws_id, ATTR)
        # 2. products with BOTH product_type AND age_group.
        pt_pids = {pid for (pid,) in db.query(ProductAttribute.product_id)
                   .join(Product, ProductAttribute.product_id == Product.id)
                   .filter(Product.workspace_id == ws_id,
                           ProductAttribute.attribute_id == "product_type")
                   .distinct().all()}
        ag_pids = {pid for (pid,) in db.query(ProductAttribute.product_id)
                   .join(Product, ProductAttribute.product_id == Product.id)
                   .filter(Product.workspace_id == ws_id,
                           ProductAttribute.attribute_id == ATTR)
                   .distinct().all()}
        both_count = len(pt_pids & ag_pids)
        # 3. (product_type x age_group) distribution.
        pa_rows = (db.query(ProductAttribute.product_id, ProductAttribute.attribute_id,
                            ProductAttribute.attribute_value)
                   .join(Product, ProductAttribute.product_id == Product.id)
                   .filter(Product.workspace_id == ws_id,
                           ProductAttribute.attribute_id.in_([ATTR, "product_type"]))
                   .all())
        per_product: dict[int, dict[str, str]] = defaultdict(dict)
        for pid, attr, val in pa_rows:
            per_product[pid][attr] = val
        cell_counter: Counter = Counter()
        for pid, m in per_product.items():
            pt = m.get("product_type")
            ag = m.get(ATTR)
            if pt and ag:
                cell_counter[(pt, ag)] += 1

        # 4. confirmation queries.
        after = {
            "agg_age_group_status": status_counts(db, ws_id, ATTR),
            "agg_product_type_status": status_counts(db, ws_id, "product_type"),
            "aav_age_group_active": aav_active_count(db, ws_id, ATTR),
            "aav_product_type_active": aav_active_count(db, ws_id, "product_type"),
            "events_age_group": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "events_product_type": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == "product_type").count(),
            "pa_age_group": (db.query(ProductAttribute)
                             .join(Product, ProductAttribute.product_id == Product.id)
                             .filter(Product.workspace_id == ws_id,
                                     ProductAttribute.attribute_id == ATTR).count()),
            "pa_product_type": (db.query(ProductAttribute)
                                .join(Product, ProductAttribute.product_id == Product.id)
                                .filter(Product.workspace_id == ws_id,
                                        ProductAttribute.attribute_id == "product_type").count()),
        }
        # Per-product max age_group rows (must be 1).
        ag_per_product = Counter()
        for pid, attr, _ in pa_rows:
            if attr == ATTR:
                ag_per_product[pid] += 1
        max_ag_per_product = max(ag_per_product.values()) if ag_per_product else 0
    finally:
        db.close()

    # -------- Print outputs --------
    print()
    print("=" * 80)
    print("OUTPUT 1: age_group coverage")
    print("=" * 80)
    print(f"  products with age_group : {with_age}/{total} = "
          f"{100*with_age/max(total,1):.2f}%")
    print(f"  max age_group rows / product : {max_ag_per_product}  (must be 1)")

    print()
    print("=" * 80)
    print("OUTPUT 2: products with BOTH product_type + age_group")
    print("=" * 80)
    print(f"  product_type-tagged products : {len(pt_pids)}")
    print(f"  age_group-tagged products    : {len(ag_pids)}")
    print(f"  intersection (both signals)  : {both_count}  "
          f"({100*both_count/max(total,1):.2f}% of catalog)")
    print(f"  product_type only            : {len(pt_pids - ag_pids)}")
    print(f"  age_group only               : {len(ag_pids - pt_pids)}")
    print(f"  neither                      : "
          f"{total - len(pt_pids | ag_pids)}")

    print()
    print("=" * 80)
    print("OUTPUT 3: (product_type x age_group) distribution")
    print("=" * 80)
    print(f"  total cells populated : {len(cell_counter)}")
    print(f"  total products        : {sum(cell_counter.values())}")
    # Marginal distributions for context.
    by_pt = Counter()
    by_ag = Counter()
    for (pt, ag), n in cell_counter.items():
        by_pt[pt] += n
        by_ag[ag] += n
    print()
    print(f"  Top product_type rows :")
    for pt, n in by_pt.most_common(15):
        infant = cell_counter.get((pt, "infant"), 0)
        toddler = cell_counter.get((pt, "toddler"), 0)
        kids = cell_counter.get((pt, "kids"), 0)
        teen = cell_counter.get((pt, "teen"), 0)
        print(f"    {pt:<22} total={n:>4}  "
              f"infant={infant:<4} toddler={toddler:<4} "
              f"kids={kids:<4} teen={teen:<4}")
    print()
    print(f"  age_group totals      :")
    for ag, n in by_ag.most_common():
        print(f"    {ag:<14} {n}")

    # Strong cells (>=4 = >=3 same-cell peers).
    strong = sum(1 for n in cell_counter.values() if n >= 4)
    sparse = sum(1 for n in cell_counter.values() if 1 <= n < 4)
    print()
    print(f"  cells with >=4 (>=3 peers possible) : {strong}")
    print(f"  cells with 1-3 (sparse)             : {sparse}")
    print(f"  cells with 0 (empty)                : "
          f"{4 * len(by_pt) - len(cell_counter)} (if 4 age bands x {len(by_pt)} pts)")

    print()
    print("=" * 80)
    print("OUTPUT 4: confirmation no other data modified")
    print("=" * 80)

    def cmp(label, b, a, expect_unchanged=False):
        if isinstance(b, dict):
            ok = b == a if expect_unchanged else True
            print(f"  {label:<32} BEFORE={b}  AFTER={a}  "
                  f"{'unchanged' if ok else 'CHANGED'}")
        else:
            d = a - b
            sign = "+" if d > 0 else ""
            tag = "unchanged" if d == 0 else f"diff={sign}{d}"
            if expect_unchanged and d != 0:
                tag = f"CHANGED diff={sign}{d}"
            print(f"  {label:<32} {b}  ->  {a}    ({tag})")

    cmp("age_group aggregates",       before["agg_age_group_status"],   after["agg_age_group_status"])
    cmp("product_type aggregates",    before["agg_product_type_status"], after["agg_product_type_status"], expect_unchanged=True)
    cmp("AAV active age_group",       before["aav_age_group_active"],   after["aav_age_group_active"])
    cmp("AAV active product_type",    before["aav_product_type_active"], after["aav_product_type_active"], expect_unchanged=True)
    cmp("events age_group",           before["events_age_group"],       after["events_age_group"])
    cmp("events product_type",        before["events_product_type"],    after["events_product_type"], expect_unchanged=True)
    cmp("PA rows age_group",          before["pa_age_group"],           after["pa_age_group"])
    cmp("PA rows product_type",       before["pa_product_type"],        after["pa_product_type"], expect_unchanged=True)


if __name__ == "__main__":
    main()
