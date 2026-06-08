"""Wave 4 controlled merge batch.

    bottle    -> water_bottle   (Wave 3 just approved water_bottle)
    lunchbox  -> lunch_box      (Wave 3 just re-activated lunch_box AAV)

Both targets must be approved + active before any engine call. No
force=true. No recommendations. Only these two merges.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import Counter

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
client = TestClient(app)

MERGES = [
    {"source": "bottle",   "target": "water_bottle", "merge_type": "parent_child"},
    {"source": "lunchbox", "target": "lunch_box",    "merge_type": "normalization_variant"},
]


def status_counts(db, ws_id):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_active_count(db, ws_id):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).count()


def aav_total(db, ws_id):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR).count()


def coverage(db, ws_id):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_pt = (db.query(ProductAttribute.product_id)
               .join(Product, ProductAttribute.product_id == Product.id)
               .filter(Product.workspace_id == ws_id,
                       ProductAttribute.attribute_id == ATTR)
               .distinct().count())
    return total, with_pt


def pa_distribution(db, ws_id):
    rows = (db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all())
    return Counter(v for (v,) in rows)


def cluster_state(db, ws_id, ck):
    agg = db.query(A).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR,
        A.cluster_key == ck).first()
    if not agg:
        return None
    aav = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.value == agg.canonical_value).first()
    pa = (db.query(ProductAttribute)
          .join(Product, ProductAttribute.product_id == Product.id)
          .filter(Product.workspace_id == ws_id,
                  ProductAttribute.attribute_id == ATTR,
                  ProductAttribute.attribute_value == agg.canonical_value)
          .count())
    return {
        "id": agg.id, "status": agg.status, "count": agg.proposal_count,
        "aav_active": (aav.is_active if aav else None),
        "aav_id": (aav.id if aav else None),
        "pa_rows": pa,
    }


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        # ----- Preconditions -----
        print("=" * 72)
        print("PRECONDITIONS")
        print("=" * 72)
        targets = ("water_bottle", "lunch_box")
        ok = True
        for t in targets:
            st = cluster_state(db, ws_id, t)
            tag = "OK" if (st and st["status"] == "approved" and st["aav_active"]) else "BAD"
            print(f"  [{tag}] {t:<14} {st}")
            if tag == "BAD":
                ok = False
        if not ok:
            print("Preconditions failed; aborting before any engine call.")
            return

        before = {
            "status_counts": status_counts(db, ws_id),
            "aav_active": aav_active_count(db, ws_id),
            "aav_total": aav_total(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "coverage": coverage(db, ws_id),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "pa_dist": pa_distribution(db, ws_id),
            "clusters": {ck: cluster_state(db, ws_id, ck)
                         for ck in ("bottle", "water_bottle", "lunchbox", "lunch_box")},
        }
    finally:
        db.close()

    print()
    print("=" * 72)
    print(f"PRE-MERGE STATE  (ws_id={ws_id})")
    print("=" * 72)
    print(f"  aggregates by status   : {before['status_counts']}")
    print(f"  AAV active / total     : {before['aav_active']} / {before['aav_total']}")
    print(f"  events_total           : {before['events_total']}")
    total, with_pt = before["coverage"]
    print(f"  coverage               : {with_pt}/{total} = {100*with_pt/max(total,1):.2f}%")
    print(f"  PA total               : {before['pa_total']}")
    for ck, st in before["clusters"].items():
        print(f"    {ck:<14} {st}")

    # ----- Execute merges -----
    merge_results = []
    for i, m in enumerate(MERGES, start=1):
        print()
        print("=" * 72)
        print(f"MERGE {i}/{len(MERGES)}:  {m['source']} -> {m['target']}  "
              f"(merge_type={m['merge_type']})")
        print("=" * 72)
        r = client.post(
            "/admin/taxonomy/api/merge_suggestions/execute",
            json={
                "workspace_id": ws_id,
                "attribute": ATTR,
                "source_cluster": m["source"],
                "target_cluster": m["target"],
                "merge_type": m["merge_type"],
            },
        )
        print(f"  HTTP status         : {r.status_code}")
        if r.status_code != 200:
            print(f"  body                : {r.text}")
            raise SystemExit(f"merge {i} failed; aborting before snapshot")
        body = r.json()
        merge_results.append({"input": m, "response": body})
        print(f"  source.status       : {body['source']['status']}")
        print(f"  source.merge_reason : {body['source']['merge_reason']}")
        print(f"  source_aav_deactivated : {body['source_allowed_value_deactivated']}")
        print(f"  PA rows_updated     : {body['product_attribute']['rows_updated']}")
        print(f"  PA duplicates_dropped : {body['product_attribute']['duplicates_dropped']}")

    # ----- Post snapshot -----
    db = SessionLocal()
    try:
        after = {
            "status_counts": status_counts(db, ws_id),
            "aav_active": aav_active_count(db, ws_id),
            "aav_total": aav_total(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "coverage": coverage(db, ws_id),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "pa_dist": pa_distribution(db, ws_id),
            "clusters": {ck: cluster_state(db, ws_id, ck)
                         for ck in ("bottle", "water_bottle", "lunchbox", "lunch_box")},
        }
    finally:
        db.close()

    print()
    print("=" * 72)
    print("POST-MERGE STATE")
    print("=" * 72)
    print(f"  aggregates by status   : {after['status_counts']}")
    print(f"  AAV active / total     : {after['aav_active']} / {after['aav_total']}")
    print(f"  events_total           : {after['events_total']}")
    total, with_pt = after["coverage"]
    print(f"  coverage               : {with_pt}/{total} = {100*with_pt/max(total,1):.2f}%")
    print(f"  PA total               : {after['pa_total']}")
    for ck, st in after["clusters"].items():
        print(f"    {ck:<14} {st}")

    print()
    print("=" * 72)
    print("DELTAS")
    print("=" * 72)
    print(f"  pending  : {before['status_counts'].get('pending',0)} -> {after['status_counts'].get('pending',0)}")
    print(f"  approved : {before['status_counts'].get('approved',0)} -> {after['status_counts'].get('approved',0)}")
    print(f"  merged   : {before['status_counts'].get('merged',0)} -> {after['status_counts'].get('merged',0)}")
    print(f"  rejected : {before['status_counts'].get('rejected',0)} -> {after['status_counts'].get('rejected',0)}")
    print(f"  AAV active   : {before['aav_active']} -> {after['aav_active']} "
          f"(diff={after['aav_active'] - before['aav_active']})")
    print(f"  AAV total    : {before['aav_total']} -> {after['aav_total']} "
          f"(diff={after['aav_total'] - before['aav_total']})")
    print(f"  events total : {before['events_total']} -> {after['events_total']}")
    print(f"  PA total     : {before['pa_total']} -> {after['pa_total']}")
    print(f"  coverage %   : {100*before['coverage'][1]/max(before['coverage'][0],1):.2f}% -> "
          f"{100*after['coverage'][1]/max(after['coverage'][0],1):.2f}%")

    print()
    print("=" * 72)
    print("TOP product_type DISTRIBUTION (before -> after, top 18)")
    print("=" * 72)
    keys = sorted(set(before["pa_dist"]) | set(after["pa_dist"]),
                  key=lambda k: -max(before["pa_dist"].get(k, 0), after["pa_dist"].get(k, 0)))[:18]
    print(f"  {'value':<22} {'before':>8} {'after':>8} {'delta':>8}")
    for k in keys:
        b = before["pa_dist"].get(k, 0)
        a = after["pa_dist"].get(k, 0)
        delta = a - b
        marker = "+" if delta > 0 else ""
        print(f"  {k:<22} {b:>8} {a:>8} {marker}{delta:>7}")

    print()
    print("=" * 72)
    print("REMAINING HIGH-CONFIDENCE MERGE CANDIDATES")
    print("=" * 72)
    r = client.get("/admin/taxonomy/api/merge_suggestions",
                   params={"workspace_id": ws_id, "attribute": ATTR})
    if r.status_code == 200:
        items = r.json().get("items", [])
        high = [s for s in items
                if s.get("direction_confidence") == "high"
                and s.get("confidence") == "high"]
        print(f"  count : {len(high)}")
        for s in high:
            # filter out cosmetic stale entries (source already merged)
            print(f"    {s['recommended_source']:<22} -> {s['recommended_target']:<22} "
                  f"({s['merge_type']:<22}) executable={s['executable']} "
                  f"src_status={s['source_status']}")


if __name__ == "__main__":
    main()
