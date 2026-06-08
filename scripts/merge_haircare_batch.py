"""Controlled single-merge execution: haircare -> hair_care.

Scope is intentionally one merge. Does NOT approve any targets, does NOT
use force=true, does NOT touch lunchbox/lunch_box or bathrobe/bath_robe.

Captures full pre/post state for audit and prints the validation report
the user requested.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
)
from app.models.workspace import Workspace

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"
SOURCE = "haircare"
TARGET = "hair_care"

client = TestClient(app)


def snapshot(db, ws_id: int) -> dict:
    """Capture all state we need to verify the merge was scoped correctly."""
    aggs = (db.query(A)
            .filter(A.workspace_id == ws_id, A.attribute_name == ATTR)
            .all())
    by_status: dict[str, int] = {}
    for agg in aggs:
        by_status[agg.status] = by_status.get(agg.status, 0) + 1

    # Pull each cluster of interest by cluster_key.
    rows = {}
    for ck in (SOURCE, TARGET, "lunchbox", "lunch_box", "bathrobe", "bath_robe"):
        agg = db.query(A).filter(
            A.workspace_id == ws_id, A.attribute_name == ATTR,
            A.cluster_key == ck).first()
        rows[ck] = None if agg is None else {
            "id": agg.id, "status": agg.status,
            "promoted_to": agg.promoted_to_allowed_value,
            "merge_reason": agg.merge_reason,
            "review_note": agg.review_note,
            "count": agg.proposal_count,
            "distinct": agg.distinct_product_count,
        }

    aav_active = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).count()
    aav_total = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR).count()

    pa_total = (db.query(ProductAttribute)
                .join(Product, ProductAttribute.product_id == Product.id)
                .filter(Product.workspace_id == ws_id,
                        ProductAttribute.attribute_id == ATTR)
                .count())
    pa_source = (db.query(ProductAttribute)
                 .join(Product, ProductAttribute.product_id == Product.id)
                 .filter(Product.workspace_id == ws_id,
                         ProductAttribute.attribute_id == ATTR,
                         ProductAttribute.attribute_value == SOURCE)
                 .count())
    pa_target = (db.query(ProductAttribute)
                 .join(Product, ProductAttribute.product_id == Product.id)
                 .filter(Product.workspace_id == ws_id,
                         ProductAttribute.attribute_id == ATTR,
                         ProductAttribute.attribute_value == TARGET)
                 .count())

    events_source = db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR,
        E.normalized_value == SOURCE).count()
    events_target = db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR,
        E.normalized_value == TARGET).count()
    events_total = db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR).count()

    total_products = db.query(Product).filter(
        Product.workspace_id == ws_id).count()
    products_with_pt = (db.query(ProductAttribute.product_id)
                        .join(Product, ProductAttribute.product_id == Product.id)
                        .filter(Product.workspace_id == ws_id,
                                ProductAttribute.attribute_id == ATTR)
                        .distinct().count())

    return {
        "by_status": by_status,
        "rows": rows,
        "aav_active": aav_active,
        "aav_total": aav_total,
        "pa_total": pa_total,
        "pa_source": pa_source,
        "pa_target": pa_target,
        "events_source": events_source,
        "events_target": events_target,
        "events_total": events_total,
        "total_products": total_products,
        "products_with_pt": products_with_pt,
    }


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id
        before = snapshot(db, ws_id)
    finally:
        db.close()

    print("=" * 72)
    print(f"PRE-MERGE STATE  (workspace={WS_SLUG} ws_id={ws_id})")
    print("=" * 72)
    print(f"  aggregates by status   : {before['by_status']}")
    print(f"  AAV active / total     : {before['aav_active']} / {before['aav_total']}")
    print(f"  total ProductAttribute : {before['pa_total']}")
    print(f"  PA rows where value={SOURCE!r} : {before['pa_source']}")
    print(f"  PA rows where value={TARGET!r} : {before['pa_target']}")
    print(f"  events ({SOURCE}/{TARGET}/total): "
          f"{before['events_source']}/{before['events_target']}/{before['events_total']}")
    print(f"  coverage               : {before['products_with_pt']}/{before['total_products']} "
          f"= {100.0 * before['products_with_pt'] / max(before['total_products'], 1):.2f}%")
    print()
    print("  cluster snapshots:")
    for ck, row in before["rows"].items():
        print(f"    {ck:<12} {row}")

    print()
    print("=" * 72)
    print(f"EXECUTING: {SOURCE} -> {TARGET}")
    print("=" * 72)
    r = client.post(
        "/admin/taxonomy/api/merge_suggestions/execute",
        json={
            "workspace_id": ws_id,
            "attribute": ATTR,
            "source_cluster": SOURCE,
            "target_cluster": TARGET,
            "merge_type": "normalization_variant",
        },
    )
    print(f"  HTTP status            : {r.status_code}")
    if r.status_code != 200:
        print(f"  body                   : {r.text}")
        raise SystemExit("merge failed; aborting before snapshot")
    body = r.json()
    print(f"  response               :")
    for k, v in body.items():
        print(f"    {k:<32} {v}")

    db = SessionLocal()
    try:
        after = snapshot(db, ws_id)
    finally:
        db.close()

    print()
    print("=" * 72)
    print("POST-MERGE STATE")
    print("=" * 72)
    print(f"  aggregates by status   : {after['by_status']}")
    print(f"  AAV active / total     : {after['aav_active']} / {after['aav_total']}")
    print(f"  total ProductAttribute : {after['pa_total']}")
    print(f"  PA rows where value={SOURCE!r} : {after['pa_source']}")
    print(f"  PA rows where value={TARGET!r} : {after['pa_target']}")
    print(f"  events ({SOURCE}/{TARGET}/total): "
          f"{after['events_source']}/{after['events_target']}/{after['events_total']}")
    print(f"  coverage               : {after['products_with_pt']}/{after['total_products']} "
          f"= {100.0 * after['products_with_pt'] / max(after['total_products'], 1):.2f}%")
    print()
    print("  cluster snapshots:")
    for ck, row in after["rows"].items():
        print(f"    {ck:<12} {row}")

    print()
    print("=" * 72)
    print("DELTA")
    print("=" * 72)
    print(f"  aggregate status delta : "
          f"merged {before['by_status'].get('merged',0)} -> {after['by_status'].get('merged',0)}; "
          f"approved {before['by_status'].get('approved',0)} -> {after['by_status'].get('approved',0)}; "
          f"pending {before['by_status'].get('pending',0)} -> {after['by_status'].get('pending',0)}")
    print(f"  AAV active delta       : {before['aav_active']} -> {after['aav_active']} "
          f"(diff={after['aav_active'] - before['aav_active']})")
    print(f"  AAV total delta        : {before['aav_total']} -> {after['aav_total']} "
          f"(diff={after['aav_total'] - before['aav_total']})")
    print(f"  PA total delta         : {before['pa_total']} -> {after['pa_total']} "
          f"(diff={after['pa_total'] - before['pa_total']})")
    print(f"  events total delta     : {before['events_total']} -> {after['events_total']} "
          f"(diff={after['events_total'] - before['events_total']})")
    print(f"  coverage % delta       : "
          f"{100.0 * before['products_with_pt'] / max(before['total_products'],1):.2f}% -> "
          f"{100.0 * after['products_with_pt'] / max(after['total_products'],1):.2f}%")

    print()
    print("=" * 72)
    print("REMAINING BLOCKED NORMALIZATION VARIANTS")
    print("=" * 72)
    blocked = [
        ("lunchbox", "lunch_box", "target lunch_box AAV is_active=False (was reverted); "
                                  "approving target NOT performed"),
        ("bathrobe", "bath_robe", "target bath_robe has no AAV row and aggregate count=2 "
                                  "(below promotion threshold count>=3); approving target "
                                  "NOT performed"),
    ]
    for src, tgt, why in blocked:
        print(f"  {src:<12} -> {tgt:<12}  blocked: {why}")


if __name__ == "__main__":
    main()
