"""Cluster-validation run: feed ~1000 mumz products through the existing
PersonaFirst proposed-value pipeline for `product_type` and inspect the
natural aggregate distribution at scale.

Engine path used (unchanged):
    attribute_enrichment_service.get_prompt_for_attribute
        ↓
    model_client.call_model_json
        ↓
    proposed_value_normalizer.normalize_proposed_value      (via record_events_from_output)
        ↓
    proposed_attribute_value_service.record_events_from_output
        ↓
    proposed_attribute_value_service.refresh_aggregates

This script does NOT:
    - approve any aggregate
    - write ProductAttribute rows for product_type
    - modify thresholds
    - run recommendations
    - post-process aggregate values outside the engine
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
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
from app.services.model_client import ModelClientError, call_model_json
from app.services.proposed_attribute_value_service import (
    PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
    PROMOTION_MIN_PROPOSAL_COUNT,
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)

SAMPLE_PATH = ROOT / "seed_data" / "mumz_sample.json"
WORKSPACE_SLUG = "mumzworld_v1_sample"
WORKSPACE_NAME = "Mumzworld v1 sample"
ATTRIBUTE = "product_type"

PRODUCT_TYPE_DEF = AttributeDefinition(
    name="product_type",
    object_type="product",
    class_name="compatibility",
    value_mode="single",
    allowed_values=[],   # discovery — workspace allowed_values is empty for product_type
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


def _ensure_workspace(db) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
    if ws is None:
        ws = Workspace(slug=WORKSPACE_SLUG, name=WORKSPACE_NAME)
        db.add(ws)
        db.flush()
    return ws


def _wipe_workspace(db, ws_id: int) -> None:
    db.query(ProposedAttributeValueEvent).filter(
        ProposedAttributeValueEvent.workspace_id == ws_id
    ).delete(synchronize_session=False)
    db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == ws_id
    ).delete(synchronize_session=False)
    db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTRIBUTE,
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


def _build_obj(p: dict) -> dict:
    attrs = p.get("attributes") or {}
    return {
        "product_id": p["product_id"],
        "name": p["name"],
        "brand": p.get("brand", ""),
        "description": p["name"],
        "functional_categories": attrs.get("category_path") or [],
        "keywords": attrs.get("keywords") or [],
        "colors": [attrs["color"]] if attrs.get("color") else [],
    }


def _parse_proposed(raw: list) -> list[ProposedValue]:
    out: list[ProposedValue] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        v = item.get("value")
        if not isinstance(v, str) or not v.strip():
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


def _build_output(raw: dict) -> EnrichmentOutput:
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
    return EnrichmentOutput(
        attribute_name=ATTRIBUTE,
        attribute_class=raw.get("attribute_class") or "compatibility",
        values=values,
        proposed_values=_parse_proposed(raw.get("proposed_values") or []),
        warnings=list(raw.get("warnings") or []),
        source=EnrichmentSource.TEXT,
    )


def main() -> None:
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    print(f"[{datetime.utcnow().isoformat()}Z] sample loaded: {len(sample)} products")

    db = SessionLocal()
    try:
        ws = _ensure_workspace(db)
        _wipe_workspace(db, ws.id)
        # Seed Product rows only (no ProductAttribute writes — this
        # validation does not assign canonical product_type).
        for p in sample:
            db.add(Product(
                workspace_id=ws.id,
                product_id=p["product_id"],
                sku=p["product_id"],
                name=(p["name"] or p["product_id"])[:255],
                group_id=p.get("group_id") or None,
            ))
        db.flush()
        db.commit()

        success = failed = zero = 0
        events_recorded = 0
        t0 = time.time()
        for i, p in enumerate(sample, 1):
            obj = _build_obj(p)
            prompt = get_prompt_for_attribute(
                PRODUCT_TYPE_DEF, obj, db=db, workspace_id=ws.id,
            )
            try:
                raw = call_model_json(prompt)
            except ModelClientError as e:
                failed += 1
                if i % 25 == 0:
                    elapsed = time.time() - t0
                    print(f"[{i:>4}/{len(sample)}] success={success} failed={failed} "
                          f"zero={zero} events={events_recorded} elapsed={elapsed:.0f}s")
                continue
            output = _build_output(raw)
            evs = record_events_from_output(
                db, workspace_id=ws.id, product_id=p["product_id"], output=output,
            )
            events_recorded += len(evs)
            if not evs:
                zero += 1
            else:
                success += 1
            # Commit periodically so a crash mid-batch doesn't lose progress.
            if i % 50 == 0:
                db.commit()
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                eta = (len(sample) - i) / rate if rate else 0
                print(f"[{i:>4}/{len(sample)}] success={success} failed={failed} "
                      f"zero={zero} events={events_recorded} "
                      f"elapsed={elapsed:.0f}s rate={rate:.2f}/s eta={eta:.0f}s")
        db.commit()
        print(f"[done] success={success} failed={failed} zero_proposal={zero} "
              f"events_recorded={events_recorded} elapsed={time.time() - t0:.0f}s")

        # ------- refresh aggregates -------
        aggs = refresh_aggregates(
            db, workspace_id=ws.id, attribute_name=ATTRIBUTE,
        )
        aggs.sort(key=lambda a: (-a.proposal_count, a.canonical_value))
        db.commit()
        print(f"[aggregates] total: {len(aggs)}")

        # ------- OUTPUT 1: top 50 -------
        print()
        print("=" * 110)
        print("OUTPUT 1 — Top 50 product_type aggregates by proposal_count")
        print("=" * 110)
        print(f"  {'rank':<5}{'cluster_key':<32}{'count':>6}{'distinct':>10}"
              f"{'avg_conf':>10}{'status':>10}{'ready':>7}")
        for rank, agg in enumerate(aggs[:50], 1):
            check = promotion_readiness(agg)
            print(f"  {rank:<5}{agg.cluster_key[:30]:<32}{agg.proposal_count:>6}"
                  f"{agg.distinct_product_count:>10}"
                  f"{agg.avg_confidence:>10.3f}{agg.status:>10}"
                  f"{str(check.ready):>7}")

        # ------- OUTPUT 2: promotion readiness summary -------
        n_count_ok = sum(1 for a in aggs
                         if a.proposal_count >= PROMOTION_MIN_PROPOSAL_COUNT)
        n_distinct_ok = sum(1 for a in aggs
                            if a.distinct_product_count >= PROMOTION_MIN_DISTINCT_PRODUCTS)
        n_conf_ok = sum(1 for a in aggs
                        if a.avg_confidence >= PROMOTION_MIN_AVG_CONFIDENCE)
        n_all_ok = sum(1 for a in aggs if promotion_readiness(a).ready)

        print()
        print("=" * 110)
        print("OUTPUT 2 — Promotion readiness summary")
        print("=" * 110)
        print(f"  total aggregates                                   : {len(aggs)}")
        print(f"  meeting count >= {PROMOTION_MIN_PROPOSAL_COUNT:<30}: {n_count_ok}")
        print(f"  meeting distinct_products >= {PROMOTION_MIN_DISTINCT_PRODUCTS:<18}: {n_distinct_ok}")
        print(f"  meeting avg_conf >= {PROMOTION_MIN_AVG_CONFIDENCE:<27}: {n_conf_ok}")
        print(f"  fully ready (all three thresholds met)             : {n_all_ok}")

        # ------- OUTPUT 3: distribution -------
        total_props = sum(a.proposal_count for a in aggs)
        top10_props = sum(a.proposal_count for a in aggs[:10])
        n_singletons = sum(1 for a in aggs if a.proposal_count == 1)
        coverage_top10 = (100.0 * top10_props / total_props) if total_props else 0.0

        print()
        print("=" * 110)
        print("OUTPUT 3 — Distribution insights")
        print("=" * 110)
        print(f"  unique cluster_keys                                : {len(aggs)}")
        print(f"  total proposal events                              : {total_props}")
        print(f"  proposals captured by top 10 clusters              : {top10_props} "
              f"({coverage_top10:.1f}% of all proposals)")
        print(f"  long-tail (clusters with proposal_count == 1)      : {n_singletons} "
              f"({(100.0 * n_singletons / len(aggs) if aggs else 0):.1f}% of clusters)")
        # distinct_products top-10 coverage
        total_distinct = sum(a.distinct_product_count for a in aggs)
        top10_distinct = sum(a.distinct_product_count for a in aggs[:10])
        cov10_d = (100.0 * top10_distinct / total_distinct) if total_distinct else 0.0
        print(f"  distinct_product coverage by top 10 clusters       : "
              f"{top10_distinct}/{total_distinct} ({cov10_d:.1f}%)")

        # ------- OUTPUT 4: quality check -------
        print()
        print("=" * 110)
        print("OUTPUT 4 — Quality check (engine output, not curated)")
        print("=" * 110)
        # 10 most granular / suspicious values: long, multi-word, count==1
        granular_candidates = [
            a for a in aggs
            if a.proposal_count == 1 and (
                len(a.cluster_key) > 14 or " " in a.cluster_key
            )
        ]
        granular_candidates.sort(key=lambda a: (-len(a.cluster_key), a.cluster_key))
        print()
        print("  10 most granular / specific (count == 1, longer or multi-word):")
        for a in granular_candidates[:10]:
            print(f"    - {a.cluster_key!r:<36}  count={a.proposal_count} "
                  f"distinct={a.distinct_product_count} avg_conf={a.avg_confidence:.2f}")

        # 10 well-formed category-level values: short single-token with high count
        category_candidates = [
            a for a in aggs
            if " " not in a.cluster_key and a.proposal_count >= 3
        ]
        category_candidates.sort(key=lambda a: (-a.proposal_count, a.cluster_key))
        print()
        print("  10 well-formed category-level values (single-token, count >= 3):")
        for a in category_candidates[:10]:
            print(f"    - {a.cluster_key!r:<24}  count={a.proposal_count} "
                  f"distinct={a.distinct_product_count} avg_conf={a.avg_confidence:.2f}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
