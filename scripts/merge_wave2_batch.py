"""Wave 2 controlled merge batch.

Of the three requested merges, two are blocked by the user's own
precondition (target must be approved + active). Only one merge is
executed:

    PROCEED:  ride_on_toy -> ride_on   (target ride_on is approved/active;
              evidence in both clusters references ride-on cars/wagons,
              confirming `toy` is the redundant qualifier)

    BLOCKED:  bottle -> water_bottle   (water_bottle has no AAV row;
              target not approved)
    BLOCKED:  lunchbox -> lunch_box    (lunch_box AAV is_active=False;
              target not approved)

No targets are approved. No force=true. No recommendations run. No
unrelated values touched.
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


def coverage(db, ws_id):
    total = db.query(Product).filter(Product.workspace_id == ws_id).count()
    with_pt = (db.query(ProductAttribute.product_id)
               .join(Product, ProductAttribute.product_id == Product.id)
               .filter(Product.workspace_id == ws_id,
                       ProductAttribute.attribute_id == ATTR)
               .distinct().count())
    return total, with_pt


def status_counts(db, ws_id):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_counts(db, ws_id):
    active = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).count()
    total = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR).count()
    return active, total


def pa_distribution(db, ws_id, top_n: int = 15):
    rows = (db.query(ProductAttribute.attribute_value)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR).all())
    c = Counter(v for (v,) in rows)
    return c.most_common(top_n), len(c)


def events_total(db, ws_id):
    return db.query(E).filter(
        E.workspace_id == ws_id, E.attribute_name == ATTR).count()


def cluster_snapshot(db, ws_id, cluster_key: str):
    agg = db.query(A).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR,
        A.cluster_key == cluster_key).first()
    if not agg:
        return None
    return {
        "id": agg.id, "status": agg.status,
        "promoted_to": agg.promoted_to_allowed_value,
        "merge_reason": agg.merge_reason,
        "review_note": agg.review_note,
        "count": agg.proposal_count,
    }


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        before = {
            "status_counts": status_counts(db, ws_id),
            "aav": aav_counts(db, ws_id),
            "events_total": events_total(db, ws_id),
            "coverage": coverage(db, ws_id),
            "pa_top": pa_distribution(db, ws_id),
            "ride_on_toy": cluster_snapshot(db, ws_id, "ride_on_toy"),
            "ride_on": cluster_snapshot(db, ws_id, "ride_on"),
            "bottle": cluster_snapshot(db, ws_id, "bottle"),
            "water_bottle": cluster_snapshot(db, ws_id, "water_bottle"),
            "lunchbox": cluster_snapshot(db, ws_id, "lunchbox"),
            "lunch_box": cluster_snapshot(db, ws_id, "lunch_box"),
        }
    finally:
        db.close()

    print("=" * 72)
    print(f"PRE-MERGE STATE  (workspace={WS_SLUG} ws_id={ws_id})")
    print("=" * 72)
    print(f"  aggregates by status   : {before['status_counts']}")
    print(f"  AAV active / total     : {before['aav'][0]} / {before['aav'][1]}")
    print(f"  events_total           : {before['events_total']}")
    total, with_pt = before["coverage"]
    print(f"  coverage               : {with_pt}/{total} = {100*with_pt/max(total,1):.2f}%")

    # ------------------------------------------------------------------
    # Block 1 + Block 2: report skips before doing the one allowed merge.
    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    print("MERGE 1/3:  bottle -> water_bottle")
    print("=" * 72)
    print(f"  source         : {before['bottle']}")
    print(f"  target         : {before['water_bottle']}")
    print(f"  precondition   : water_bottle has no AAV row (target not approved)")
    print(f"  decision       : SKIP -- per user spec, blocked when target not approved")
    print(f"  engine called  : NO")

    print()
    print("=" * 72)
    print("MERGE 2/3:  ride_on_toy -> ride_on")
    print("=" * 72)
    print(f"  source         : {before['ride_on_toy']}")
    print(f"  target         : {before['ride_on']}")
    print(f"  precondition   : ride_on AAV is_active=True (target approved)")
    print(f"  evidence check : ride_on_toy evidence references 'Kids Ride On Car',")
    print(f"                   'powered ride-ons', 'Spring Rider'; ride_on evidence")
    print(f"                   references 'Kids Electric Ride On Car', 'ride-ons',")
    print(f"                   'Battery Operated SUV'. Same product family;")
    print(f"                   'toy' is the redundant qualifier.")
    print(f"  decision       : PROCEED")

    r = client.post(
        "/admin/taxonomy/api/merge_suggestions/execute",
        json={
            "workspace_id": ws_id,
            "attribute": ATTR,
            "source_cluster": "ride_on_toy",
            "target_cluster": "ride_on",
            "merge_type": "parent_child",
        },
    )
    print(f"  HTTP status    : {r.status_code}")
    if r.status_code != 200:
        print(f"  body           : {r.text}")
        raise SystemExit("merge_2 failed; aborting before snapshot")
    body = r.json()
    print(f"  source.status  : {body['source']['status']}")
    print(f"  source.merge_reason : {body['source']['merge_reason']}")
    print(f"  source_allowed_value_deactivated : {body['source_allowed_value_deactivated']}")
    print(f"  PA rows_updated      : {body['product_attribute']['rows_updated']}")
    print(f"  PA duplicates_dropped: {body['product_attribute']['duplicates_dropped']}")
    merge2_body = body

    print()
    print("=" * 72)
    print("MERGE 3/3:  lunchbox -> lunch_box")
    print("=" * 72)
    print(f"  source         : {before['lunchbox']}")
    print(f"  target         : {before['lunch_box']}")
    print(f"  precondition   : lunch_box AAV is_active=False (target not re-approved)")
    print(f"  decision       : SKIP -- per user spec, only proceed if lunch_box re-approved")
    print(f"  engine called  : NO")

    db = SessionLocal()
    try:
        after = {
            "status_counts": status_counts(db, ws_id),
            "aav": aav_counts(db, ws_id),
            "events_total": events_total(db, ws_id),
            "coverage": coverage(db, ws_id),
            "pa_top": pa_distribution(db, ws_id),
            "ride_on_toy": cluster_snapshot(db, ws_id, "ride_on_toy"),
            "ride_on": cluster_snapshot(db, ws_id, "ride_on"),
            "bottle": cluster_snapshot(db, ws_id, "bottle"),
            "water_bottle": cluster_snapshot(db, ws_id, "water_bottle"),
            "lunchbox": cluster_snapshot(db, ws_id, "lunchbox"),
            "lunch_box": cluster_snapshot(db, ws_id, "lunch_box"),
        }
    finally:
        db.close()

    print()
    print("=" * 72)
    print("POST-MERGE STATE")
    print("=" * 72)
    print(f"  aggregates by status   : {after['status_counts']}")
    print(f"  AAV active / total     : {after['aav'][0]} / {after['aav'][1]}")
    print(f"  events_total           : {after['events_total']}")
    total, with_pt = after["coverage"]
    print(f"  coverage               : {with_pt}/{total} = {100*with_pt/max(total,1):.2f}%")
    print()
    print(f"  ride_on_toy after      : {after['ride_on_toy']}")
    print(f"  ride_on     after      : {after['ride_on']}")
    print(f"  bottle      after      : {after['bottle']}    (untouched)")
    print(f"  water_bottle after     : {after['water_bottle']} (untouched)")
    print(f"  lunchbox    after      : {after['lunchbox']}   (untouched)")
    print(f"  lunch_box   after      : {after['lunch_box']}   (untouched)")

    print()
    print("=" * 72)
    print("DELTAS")
    print("=" * 72)
    print(f"  pending  : {before['status_counts'].get('pending',0)} -> {after['status_counts'].get('pending',0)}")
    print(f"  approved : {before['status_counts'].get('approved',0)} -> {after['status_counts'].get('approved',0)}")
    print(f"  merged   : {before['status_counts'].get('merged',0)} -> {after['status_counts'].get('merged',0)}")
    print(f"  rejected : {before['status_counts'].get('rejected',0)} -> {after['status_counts'].get('rejected',0)}")
    print(f"  AAV active   : {before['aav'][0]} -> {after['aav'][0]}")
    print(f"  AAV total    : {before['aav'][1]} -> {after['aav'][1]}")
    print(f"  events total : {before['events_total']} -> {after['events_total']}")
    print(f"  coverage %   : {100*before['coverage'][1]/max(before['coverage'][0],1):.2f}% -> "
          f"{100*after['coverage'][1]/max(after['coverage'][0],1):.2f}%")

    print()
    print("=" * 72)
    print("TOP product_type DISTRIBUTION  (before -> after)")
    print("=" * 72)
    before_map = dict(before["pa_top"])
    after_map = dict(after["pa_top"])
    keys = sorted(set(before_map) | set(after_map),
                  key=lambda k: -max(before_map.get(k, 0), after_map.get(k, 0)))[:18]
    print(f"  {'value':<22} {'before':>8} {'after':>8} {'delta':>8}")
    for k in keys:
        b = before_map.get(k, 0)
        a = after_map.get(k, 0)
        if b == 0 and a == 0:
            continue
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
        print(f"  total high-confidence (direction_confidence=high, confidence=high) : {len(high)}")
        for s in high:
            print(f"    {s['recommended_source']:<22} -> {s['recommended_target']:<22} "
                  f"({s['merge_type']:<22}) executable={s['executable']}")
        # Also show ride_on_toy is GONE from suggestions.
        residual = [s for s in items if s.get("recommended_source") == "ride_on_toy"
                    or s.get("recommended_target") == "ride_on_toy"]
        if residual:
            print(f"  WARNING: ride_on_toy still appears in suggestions: {residual}")
        else:
            print(f"  ride_on_toy no longer appears in merge suggestions  (correct)")


if __name__ == "__main__":
    main()
