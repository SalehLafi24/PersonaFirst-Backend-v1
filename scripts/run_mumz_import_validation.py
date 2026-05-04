"""Mumz import → engine pipeline validation.

Validates that the existing PersonaFirst pipeline can ingest a real,
arbitrary product feed via a column-mapping layer:

    raw CSV row
       │
       ▼
    column mapping (per-column mode declaration — script-level)
       │
       ├── direct_value              → ProductAttribute (only if matches approved allowed_value)
       ├── direct_or_normalized_value → match approved → ProductAttribute,
       │                                  else ProposedAttributeValueEvent
       ├── evidence_for_enrichment   → get_prompt_for_attribute + call_model_json
       │                                  → ProposedAttributeValueEvent
       └── raw_metadata              → debug only, not stored as engine attribute

Engine services exercised end-to-end:
    - attribute_taxonomy_service.upsert_allowed_value / get_allowed_values
    - attribute_enrichment_service.get_prompt_for_attribute
    - model_client.call_model_json
    - proposed_attribute_value_service.record_events_from_output
    - proposed_attribute_value_service.refresh_aggregates
    - proposed_attribute_value_service.promotion_readiness
    - proposed_attribute_value_service.approve_aggregate

Models written / read:
    Workspace, Product, ProductAttribute, AttributeAllowedValue,
    ProposedAttributeValueEvent, ProposedAttributeValueAggregate.

This script does NOT modify any engine code. It is the upstream wrapper
that prepares EnrichmentOutputs the engine already knows how to ingest.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeBehavior,
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.attribute_enrichment_service import get_prompt_for_attribute
from app.services.attribute_taxonomy_service import (
    get_allowed_values,
    upsert_allowed_value,
)
from app.services.model_client import (
    ApiKeyMissingError,
    ModelClientError,
    call_model_json,
)
from app.services.proposed_attribute_value_service import (
    PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
    PROMOTION_MIN_PROPOSAL_COUNT,
    approve_aggregate,
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)
from app.services.proposed_value_normalizer import normalize_proposed_value

CSV_PATH = ROOT / "seed_data" / "mumz_products.csv"
SAMPLE_PATH = ROOT / "seed_data" / "mumz_sample.json"
WORKSPACE_SLUG = "mumzworld_v1_sample"
WORKSPACE_NAME = "Mumzworld v1 sample"
SAMPLE_SIZE = 10  # small slice; pipeline correctness > scale
TODAY = date.today()


# ---------------------------------------------------------------------------
# Column mapping (declared at script level; engine has no column-mapping
# abstraction today — see "missing integration points" in the validation
# output). Each column maps to exactly one mode.
# ---------------------------------------------------------------------------

COLUMN_MAPPING: list[dict] = [
    # IDs / metadata — never engine attributes.
    {"source_column": "sku",          "target_attribute": None,             "mode": "raw_metadata", "meaning": "external SKU; used as Product.product_id and Product.sku"},
    {"source_column": "group_id",     "target_attribute": None,             "mode": "raw_metadata", "meaning": "variant grouping key; used as Product.group_id"},
    {"source_column": "name",         "target_attribute": None,             "mode": "raw_metadata", "meaning": "stored on Product.name; also feeds product_type evidence"},
    {"source_column": "brand",        "target_attribute": None,             "mode": "raw_metadata", "meaning": "displayed; not used in scoring; may inform product_type evidence when categorical signal is weak"},
    {"source_column": "image_url",    "target_attribute": None,             "mode": "raw_metadata", "meaning": "display only"},
    {"source_column": "url",          "target_attribute": None,             "mode": "raw_metadata", "meaning": "display only"},
    {"source_column": "price",        "target_attribute": None,             "mode": "raw_metadata", "meaning": "display only"},
    {"source_column": "in_stock",     "target_attribute": None,             "mode": "raw_metadata", "meaning": "filter signal at request time, not an attribute"},
    {"source_column": "color",        "target_attribute": None,             "mode": "raw_metadata", "meaning": "free-text in source; could be promoted to direct_or_normalized once a color taxonomy exists"},
    # Direct-or-normalized: the source is already in the canonical vocabulary
    # we expect, but we still run it through approved-allowed-values matching.
    {"source_column": "gender",       "target_attribute": "gender",         "mode": "direct_or_normalized_value", "meaning": "male / female / unisex; values pre-normalized in mumz_sample.json"},
    {"source_column": "age",          "target_attribute": "age_group",      "mode": "direct_or_normalized_value", "meaning": "newborn / infant / toddler / kids / universal; multi-value"},
    # Evidence for enrichment: feed text columns to the engine's prompt;
    # LLM output goes through the proposed-value pipeline.
    {"source_column": "categories",   "target_attribute": "product_type",   "mode": "evidence_for_enrichment", "meaning": "category breadcrumb is the strongest signal for product_type"},
    {"source_column": "keywords",     "target_attribute": "product_type",   "mode": "evidence_for_enrichment", "meaning": "secondary signal; reinforces or disambiguates the category"},
    # category_1..4 are decomposed into category_path on the structured
    # object; treated as evidence_for_enrichment (above) via `categories`.
    {"source_column": "category_1",   "target_attribute": "product_type",   "mode": "evidence_for_enrichment", "meaning": "top-level taxonomy bucket"},
    {"source_column": "category_2",   "target_attribute": "product_type",   "mode": "evidence_for_enrichment", "meaning": "intermediate taxonomy"},
    {"source_column": "category_3",   "target_attribute": "product_type",   "mode": "evidence_for_enrichment", "meaning": "specific taxonomy; usually the strongest single column"},
    {"source_column": "category_4",   "target_attribute": "product_type",   "mode": "evidence_for_enrichment", "meaning": "leaf-level taxonomy when populated (~31% coverage)"},
    {"source_column": "entity_id",    "target_attribute": None,             "mode": "raw_metadata", "meaning": "internal export sequence number; unused"},
    {"source_column": "dy_display_price", "target_attribute": None,         "mode": "raw_metadata", "meaning": "display-only price field"},
]


# Approved allowed_values for the workspace. These are seeded as
# AttributeAllowedValue rows so direct_or_normalized_value mode can match
# against them. product_type intentionally has NO seeded values — the
# engine should discover them via the proposed-value pipeline.
APPROVED_ALLOWED_VALUES: dict[str, list[str]] = {
    "gender":    ["male", "female", "unisex"],
    "age_group": ["newborn", "infant", "toddler", "kids", "universal"],
    # "product_type": []  -- discovered, not seeded
}


# Attribute definitions used by the LLM prompt. Engine reads
# allowed_values at runtime from the workspace-scoped table when
# db + workspace_id are passed to get_prompt_for_attribute.
PRODUCT_TYPE_DEF = AttributeDefinition(
    name="product_type",
    object_type="product",
    class_name="compatibility",
    value_mode="single",
    allowed_values=[],          # populated dynamically from DB at prompt time
    description="The garment / item type the product represents (e.g. bottle, plush, stroller, hoodie).",
    evidence_sources=["text"],
    behavior=AttributeBehavior(
        taxonomy_sensitive=True,
        ordered_values=False,
        can_propose_values=True,
        multi_value_allowed=False,
        prefer_conservative_inference=True,
        value_order=None,
        negative_scoring_enabled=True,
    ),
    targeting_mode="compatibility_signal",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_workspace(db) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
    if ws is None:
        ws = Workspace(slug=WORKSPACE_SLUG, name=WORKSPACE_NAME)
        db.add(ws)
        db.flush()
    return ws


def _wipe_workspace(db, ws_id: int) -> None:
    """Idempotent wipe so re-runs are clean. Delete in FK-safe order."""
    db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == ws_id
    ).delete(synchronize_session=False)
    db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == ws_id
    ).delete(synchronize_session=False)
    db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id
    ).delete(synchronize_session=False)
    prod_db_ids = [p.id for p in db.query(Product).filter(
        Product.workspace_id == ws_id
    ).all()]
    if prod_db_ids:
        db.query(ProductAttribute).filter(
            ProductAttribute.product_id.in_(prod_db_ids)
        ).delete(synchronize_session=False)
    db.query(Product).filter(Product.workspace_id == ws_id).delete(
        synchronize_session=False
    )
    db.flush()


def _seed_approved_allowed_values(db, ws_id: int) -> None:
    for attr_name, values in APPROVED_ALLOWED_VALUES.items():
        for v in values:
            upsert_allowed_value(db, ws_id, attr_name, v)
    db.flush()


def _load_sample(limit: int) -> list[dict]:
    with SAMPLE_PATH.open(encoding="utf-8") as f:
        all_products = json.load(f)
    # Choose 10 across diverse buckets: pick stride from the (already-sorted) list.
    if limit >= len(all_products):
        return all_products
    step = len(all_products) // limit
    return [all_products[i * step] for i in range(limit)]


def _seed_product(db, ws_id: int, p: dict) -> Product:
    prod = Product(
        workspace_id=ws_id,
        product_id=p["product_id"],
        sku=p["product_id"],
        name=p["name"][:255] or p["product_id"],
        group_id=p["group_id"] or None,
    )
    db.add(prod)
    db.flush()
    return prod


def _build_product_obj_for_prompt(p: dict) -> dict:
    """Shape the structured product into the dict shape the engine's
    enrichment prompt expects (matches `_product_obj` in
    run_real_text_enrichment.py)."""
    attrs = p.get("attributes", {}) or {}
    return {
        "product_id": p["product_id"],
        "name": p["name"],
        "brand": p.get("brand", ""),
        "description": p["name"],   # mumz feed has no long description column
        "functional_categories": attrs.get("category_path") or [],
        "keywords": attrs.get("keywords") or [],
        "colors": [attrs["color"]] if attrs.get("color") else [],
    }


def _parse_proposed_values(raw: list, allowed_values: list[str]) -> list[ProposedValue]:
    allowed_set = {v.lower() for v in (allowed_values or [])}
    out: list[ProposedValue] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        v = item.get("value")
        if not isinstance(v, str) or not v.strip():
            continue
        if v.lower() in allowed_set:
            continue
        if v in seen:
            continue
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf < 0.8:
            continue
        evidence = [e for e in (item.get("evidence") or []) if isinstance(e, str) and e.strip()]
        if not evidence:
            continue
        seen.add(v)
        out.append(ProposedValue(value=v, confidence=conf, evidence=evidence))
    return out


def _build_enrichment_output(attr_def: AttributeDefinition, raw: dict) -> EnrichmentOutput:
    values: list[EnrichedValue] = []
    for item in raw.get("values") or []:
        try:
            values.append(EnrichedValue(
                value=item.get("value"),
                confidence=float(item.get("confidence", 0.0)),
                evidence=list(item.get("evidence") or []),
                reasoning_mode=item.get("reasoning_mode"),
                source=EnrichmentSource.TEXT,
                contributing_sources=[EnrichmentSource.TEXT],
            ))
        except Exception:
            continue
    proposed = _parse_proposed_values(raw.get("proposed_values") or [], attr_def.allowed_values)
    return EnrichmentOutput(
        attribute_name=attr_def.name,
        attribute_class=raw.get("attribute_class") or attr_def.class_name,
        values=values,
        proposed_values=proposed,
        warnings=list(raw.get("warnings") or []),
        source=EnrichmentSource.TEXT,
    )


# ---------------------------------------------------------------------------
# Per-mode appliers
# ---------------------------------------------------------------------------

def _apply_direct_or_normalized(
    db, ws_id: int, prod: Product, *,
    attr_name: str, raw_values: list[str],
    direct_writes: list[tuple[str, str, str]],
    proposed_events: list[tuple[str, str, str, float, list[str]]],
) -> None:
    approved = {v.lower() for v in get_allowed_values(db, ws_id, attr_name)}
    for raw_val in raw_values:
        if not raw_val:
            continue
        if raw_val.lower() in approved:
            db.add(ProductAttribute(
                product_id=prod.id,
                attribute_id=attr_name,
                attribute_value=raw_val,
            ))
            direct_writes.append((prod.product_id, attr_name, raw_val))
        else:
            # Source carries a value that's not yet approved → submit as a
            # proposal via a synthetic single-value EnrichmentOutput so the
            # engine's pipeline records it consistently.
            output = EnrichmentOutput(
                attribute_name=attr_name,
                attribute_class="contextual_semantic",
                values=[],
                proposed_values=[
                    ProposedValue(
                        value=raw_val,
                        confidence=0.95,
                        evidence=[f"source column literal: {raw_val!r}"],
                    )
                ],
                warnings=[],
                source=EnrichmentSource.TEXT,
            )
            events = record_events_from_output(
                db, workspace_id=ws_id, product_id=prod.product_id, output=output,
            )
            for ev in events:
                proposed_events.append((
                    prod.product_id, attr_name, ev.normalized_value,
                    ev.confidence, list(ev.evidence),
                ))


def _apply_evidence_for_enrichment(
    db, ws_id: int, prod: Product, p_obj: dict, *,
    attr_def: AttributeDefinition,
    proposed_events: list[tuple[str, str, str, float, list[str]]],
) -> EnrichmentOutput | None:
    prompt = get_prompt_for_attribute(attr_def, p_obj, db=db, workspace_id=ws_id)
    try:
        raw = call_model_json(prompt)
    except ApiKeyMissingError:
        print(f"  [skip] {prod.product_id}: ANTHROPIC_API_KEY not set")
        return None
    except ModelClientError as e:
        print(f"  [skip] {prod.product_id}: model error {type(e).__name__}: {e}")
        return None
    output = _build_enrichment_output(attr_def, raw)
    events = record_events_from_output(
        db, workspace_id=ws_id, product_id=prod.product_id, output=output,
    )
    for ev in events:
        proposed_events.append((
            prod.product_id, attr_def.name, ev.normalized_value,
            ev.confidence, list(ev.evidence),
        ))
    return output


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def _csv_inspection_block() -> None:
    if not CSV_PATH.exists():
        print(f"  CSV not found at {CSV_PATH}")
        return
    rows: list[dict] = []
    nulls: dict[str, int] = {}
    cols: list[str] = []
    n = 0
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        cols = list(r.fieldnames or [])
        for row in r:
            n += 1
            for c in cols:
                v = (row.get(c) or "").strip()
                if not v:
                    nulls[c] = nulls.get(c, 0) + 1
            if len(rows) < 20:
                rows.append(row)
    print(f"  total rows : {n}")
    print(f"  columns ({len(cols)}): {cols}")
    print()
    print(f"  per-column coverage:")
    for c in cols:
        pct_null = 100.0 * nulls.get(c, 0) / n if n else 0
        print(f"    {c:<22} coverage={100 - pct_null:5.1f}%")
    print()
    print(f"  20 sample rows (showing 6 columns: sku, name, category_3, age, gender, color):")
    for row in rows:
        print(f"    {row.get('sku','')[:18]:<18}  "
              f"{(row.get('name') or '')[:42]:<42}  "
              f"cat3={(row.get('category_3') or '')[:16]:<16}  "
              f"age={(row.get('age') or '')[:14]:<14}  "
              f"g={row.get('gender') or '':<7}  "
              f"col={row.get('color') or '':<10}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sample = _load_sample(SAMPLE_SIZE)
    print(f"Loaded sample: {len(sample)} products from {SAMPLE_PATH.name}")

    # ------- step 1: schema inspection of raw CSV -------
    _print_section("STEP 1 — Source CSV schema inspection")
    _csv_inspection_block()

    # ------- step 2: column mapping table -------
    _print_section("STEP 2 — Column mapping for mumz_products")
    print(f"  {'source_column':<22} {'target_attribute':<18} {'mode':<28} meaning")
    print(f"  {'-'*22} {'-'*18} {'-'*28} {'-'*60}")
    for m in COLUMN_MAPPING:
        print(f"  {m['source_column']:<22} {str(m['target_attribute'] or '—'):<18} "
              f"{m['mode']:<28} {m['meaning']}")

    # ------- step 3: 5 structured product objects (before engine writes) -------
    _print_section("STEP 3 — 5 structured product objects (before any engine writes)")
    for p in sample[:5]:
        print(json.dumps({
            "product_id": p["product_id"],
            "name": p["name"],
            "brand": p.get("brand"),
            "raw_metadata": {
                "color": (p["attributes"] or {}).get("color"),
                "keywords": (p["attributes"] or {}).get("keywords"),
                "image_url": p.get("image_url"),  # not present in sample.json — illustrative
            },
            "direct_or_normalized_inputs": {
                "gender": (p["attributes"] or {}).get("gender"),
                "age_group": (p["attributes"] or {}).get("age_group"),
            },
            "evidence_for_enrichment": {
                "product_type_evidence": {
                    "category_path": (p["attributes"] or {}).get("category_path"),
                    "keywords": (p["attributes"] or {}).get("keywords"),
                    "name": p["name"],
                }
            },
        }, indent=2, ensure_ascii=False))

    # ------- step 4–6: run the actual engine pipeline -------
    db = SessionLocal()
    try:
        ws = _ensure_workspace(db)
        _wipe_workspace(db, ws.id)
        _seed_approved_allowed_values(db, ws.id)

        _print_section("STEP 4 — Workspace + approved allowed_values seeded")
        print(f"  workspace.slug        = {ws.slug}")
        for an in APPROVED_ALLOWED_VALUES:
            allowed = get_allowed_values(db, ws.id, an)
            print(f"  allowed_values[{an}] = {allowed}")
        print(f"  allowed_values[product_type] = "
              f"{get_allowed_values(db, ws.id, 'product_type')}  # empty by design")

        # Apply mapping per product
        direct_writes: list[tuple[str, str, str]] = []
        proposed_events: list[tuple[str, str, str, float, list[str]]] = []

        _print_section("STEP 5 — Applying column mapping per product (small sample)")
        print(f"  Sample size: {len(sample)} products. Each product:")
        print(f"    - direct_or_normalized: gender, age_group  →  match approved or proposed event")
        print(f"    - evidence_for_enrichment: product_type    →  LLM call → proposed events")
        print()
        for p in sample:
            prod = _seed_product(db, ws.id, p)
            attrs = p.get("attributes", {}) or {}
            print(f"  • {prod.product_id}  {prod.name[:60]}")

            # gender
            g = (attrs.get("gender") or "").strip()
            _apply_direct_or_normalized(
                db, ws.id, prod, attr_name="gender", raw_values=[g] if g else [],
                direct_writes=direct_writes, proposed_events=proposed_events,
            )
            # age_group (multi-value list)
            ages = attrs.get("age_group") or []
            _apply_direct_or_normalized(
                db, ws.id, prod, attr_name="age_group", raw_values=ages,
                direct_writes=direct_writes, proposed_events=proposed_events,
            )
            # product_type (LLM)
            p_obj = _build_product_obj_for_prompt(p)
            _apply_evidence_for_enrichment(
                db, ws.id, prod, p_obj,
                attr_def=PRODUCT_TYPE_DEF,
                proposed_events=proposed_events,
            )

        print()
        print(f"  direct_value writes (ProductAttribute) so far: {len(direct_writes)}")
        print(f"  proposed events recorded:                      {len(proposed_events)}")

        # ------- step 7: refresh aggregates -------
        _print_section("STEP 6 — refresh_aggregates(product_type)")
        aggs_pt = refresh_aggregates(db, workspace_id=ws.id, attribute_name="product_type")
        aggs_pt.sort(key=lambda a: (-a.proposal_count, a.canonical_value))
        print(f"  aggregates produced: {len(aggs_pt)}")
        print(f"  promotion thresholds: count>={PROMOTION_MIN_PROPOSAL_COUNT}, "
              f"avg_conf>={PROMOTION_MIN_AVG_CONFIDENCE}, "
              f"distinct_products>={PROMOTION_MIN_DISTINCT_PRODUCTS}")

        # ------- step 8: example outputs -------
        _print_section("STEP 7 — Example ProposedAttributeValueEvent rows (first 12)")
        print(f"  {'product_id':<22} {'attribute':<14} {'cluster_key':<28} "
              f"{'conf':>6}  evidence")
        for pid, attr, cluster, conf, evs in proposed_events[:12]:
            ev_str = (evs[0] if evs else "")[:60]
            print(f"  {pid:<22} {attr:<14} {cluster:<28} {conf:>6.2f}  {ev_str!r}")

        _print_section("STEP 8 — Example ProposedAttributeValueAggregate rows (top 15)")
        print(f"  {'cluster_key':<26} {'count':>5} {'distinct_products':>18} "
              f"{'avg_conf':>8} {'status':<10} ready")
        for agg in aggs_pt[:15]:
            ready = promotion_readiness(agg)
            print(f"  {agg.cluster_key[:26]:<26} {agg.proposal_count:>5} "
                  f"{agg.distinct_product_count:>18} {agg.avg_confidence:>8.3f} "
                  f"{agg.status:<10} {ready.ready}")

        # ------- taxonomy proposals (top 30 by count) -------
        _print_section("STEP 9 — Reviewable taxonomy proposals (top 30)")
        print(json.dumps([
            {
                "canonical_value": agg.canonical_value,
                "raw_values_merged": [agg.canonical_value],   # single key per cluster pre-review
                "product_count": agg.distinct_product_count,
                "confidence": round(agg.avg_confidence, 3),
                "examples": agg.sample_product_ids[:5],
            }
            for agg in aggs_pt[:30]
        ], indent=2, ensure_ascii=False))

        # ------- step 10: approve a representative aggregate (force=True
        #         since count<3 in this small sample). Demonstrates the
        #         approval flow that backfills ProductAttribute rows. -------
        _print_section("STEP 10 — Approve representative aggregates (force=True for sample size)")
        approved_count = 0
        current_allowed = list(get_allowed_values(db, ws.id, "product_type"))
        # Approve up to 3 highest-count aggregates so we can demo
        # ProductAttribute backfill.
        for agg in aggs_pt[:3]:
            try:
                _, current_allowed = approve_aggregate(
                    db,
                    aggregate_id=agg.id,
                    current_allowed_values=current_allowed,
                    force=True,  # below auto-promotion threshold for this small sample
                    review_note=(
                        "Approved during pipeline validation; in production this "
                        "would require the ≥3 / ≥0.85 / ≥2 thresholds."
                    ),
                )
                print(f"  approved (force=True): {agg.canonical_value}")
                approved_count += 1
            except ValueError as e:
                print(f"  approve failed for {agg.canonical_value}: {e}")
        print(f"  approved this run: {approved_count}")
        print(f"  product_type allowed_values now: "
              f"{get_allowed_values(db, ws.id, 'product_type')}")

        # ------- backfill ProductAttribute for approved canonical values -------
        approved_set = {v.lower() for v in get_allowed_values(db, ws.id, "product_type")}
        backfilled: list[tuple[str, str]] = []
        # Walk events for product_type whose normalized_value is now approved.
        events = (
            db.query(ProposedAttributeValueEvent)
            .filter(
                ProposedAttributeValueEvent.workspace_id == ws.id,
                ProposedAttributeValueEvent.attribute_name == "product_type",
            )
            .all()
        )
        # Keep one ProductAttribute row per (product, value).
        seen_pa: set[tuple[str, str, str]] = set()
        for ev in events:
            if ev.normalized_value.lower() not in approved_set:
                continue
            prod = (
                db.query(Product)
                .filter(Product.workspace_id == ws.id, Product.product_id == ev.product_id)
                .first()
            )
            if prod is None:
                continue
            key = (str(prod.id), "product_type", ev.normalized_value)
            if key in seen_pa:
                continue
            seen_pa.add(key)
            db.add(ProductAttribute(
                product_id=prod.id,
                attribute_id="product_type",
                attribute_value=ev.normalized_value,
            ))
            backfilled.append((prod.product_id, ev.normalized_value))
        db.flush()

        _print_section("STEP 11 — Sample ProductAttribute rows AFTER approval")
        all_pa = (
            db.query(ProductAttribute, Product)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws.id)
            .order_by(Product.product_id, ProductAttribute.attribute_id,
                      ProductAttribute.attribute_value)
            .all()
        )
        print(f"  total ProductAttribute rows: {len(all_pa)}")
        print(f"  {'product_id':<22} {'attribute_id':<14} {'attribute_value':<22} source")
        for pa, prod in all_pa[:30]:
            src = (
                "direct_or_normalized" if pa.attribute_id in APPROVED_ALLOWED_VALUES
                else "approved_proposal_backfill"
            )
            print(f"  {prod.product_id:<22} {pa.attribute_id:<14} {pa.attribute_value:<22} {src}")

        db.commit()
        print()
        print("Committed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
