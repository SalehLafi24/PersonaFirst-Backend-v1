"""Wave 3 approvals: water_bottle, lunch_box (skip bath_robe).

Uses the engine approve_aggregate flow via the taxonomy admin POST
endpoint. No force=true. No merges. No recommendations.

water_bottle  : count=10, distinct=10, avg_conf=0.962  -> ready
lunch_box     : count=6,  distinct=6,  avg_conf=0.942  -> ready
                AAV row exists with is_active=False (from prior revert);
                upsert_allowed_value re-activates it.
bath_robe     : count=2  -- below readiness threshold; SKIP.
                User said "optional" and "do not use force=true unless
                explicitly required"; both gate this approval.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
)
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.services.proposed_attribute_value_service import promotion_readiness

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"
client = TestClient(app)


def aav_state(db, ws_id: int, value: str):
    row = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.value == value).first()
    if row is None:
        return None
    return {"id": row.id, "is_active": row.is_active,
            "created_at": str(row.created_at)}


def status_counts(db, ws_id: int):
    out = {}
    for s, in db.query(A.status).filter(
        A.workspace_id == ws_id, A.attribute_name == ATTR
    ).all():
        out[s] = out.get(s, 0) + 1
    return out


def aav_active_count(db, ws_id: int):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR,
        AttributeAllowedValue.is_active == True).count()


def aav_total(db, ws_id: int):
    return db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == ws_id,
        AttributeAllowedValue.attribute_name == ATTR).count()


def approve(ws_id: int, agg_id: int, cluster: str):
    r = client.post(
        f"/admin/taxonomy/api/aggregates/{agg_id}/approve",
        params={"workspace_id": ws_id, "attribute": ATTR},
    )
    return r


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        before = {
            "status_counts": status_counts(db, ws_id),
            "aav_active": aav_active_count(db, ws_id),
            "aav_total": aav_total(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
            "merged_count": status_counts(db, ws_id).get("merged", 0),
        }

        # Snapshots of the 3 candidate aggregates.
        clusters = ("water_bottle", "lunch_box", "bath_robe")
        agg_rows = {}
        for ck in clusters:
            agg = db.query(A).filter(
                A.workspace_id == ws_id, A.attribute_name == ATTR,
                A.cluster_key == ck).first()
            agg_rows[ck] = agg
            if agg:
                check = promotion_readiness(agg)
                aav = aav_state(db, ws_id, agg.canonical_value)
                print(f"  {ck:<14} agg.id={agg.id} status={agg.status} "
                      f"count={agg.proposal_count} distinct={agg.distinct_product_count} "
                      f"avg_conf={agg.avg_confidence:.3f} ready={check.ready} "
                      f"aav={aav}")
                if not check.ready:
                    print(f"                    readiness gaps: {check.reasons}")
    finally:
        db.close()

    print()
    print("=" * 72)
    print(f"PRE-APPROVAL STATE  (ws_id={ws_id})")
    print("=" * 72)
    print(f"  aggregates by status   : {before['status_counts']}")
    print(f"  AAV active / total     : {before['aav_active']} / {before['aav_total']}")
    print(f"  events_total           : {before['events_total']}")
    print(f"  PA rows total          : {before['pa_total']}")

    print()
    print("=" * 72)
    print("APPROVAL 1/3:  water_bottle")
    print("=" * 72)
    agg = agg_rows["water_bottle"]
    if agg is None:
        print("  SKIP  : aggregate not found")
        wb_result = None
    else:
        print(f"  precondition : ready=True (count={agg.proposal_count}, "
              f"distinct={agg.distinct_product_count}, avg_conf={agg.avg_confidence:.3f})")
        print(f"  aav before   : {aav_state(SessionLocal(), ws_id, 'water_bottle')}")
        r = approve(ws_id, agg.id, "water_bottle")
        print(f"  HTTP status  : {r.status_code}")
        wb_result = r.json() if r.status_code == 200 else None
        if wb_result is None:
            print(f"  body         : {r.text}")
        else:
            print(f"  status after : {wb_result['status']}")
            print(f"  promoted_to  : {wb_result['promoted_to_allowed_value']}")
            print(f"  review_note  : {wb_result['review_note']}")

    print()
    print("=" * 72)
    print("APPROVAL 2/3:  lunch_box")
    print("=" * 72)
    agg = agg_rows["lunch_box"]
    if agg is None:
        print("  SKIP  : aggregate not found")
        lb_result = None
    else:
        print(f"  precondition : ready=True (count={agg.proposal_count}, "
              f"distinct={agg.distinct_product_count}, avg_conf={agg.avg_confidence:.3f})")
        print(f"  note         : AAV row exists with is_active=False (from prior revert);")
        print(f"                 upsert_allowed_value will re-activate it.")
        r = approve(ws_id, agg.id, "lunch_box")
        print(f"  HTTP status  : {r.status_code}")
        lb_result = r.json() if r.status_code == 200 else None
        if lb_result is None:
            print(f"  body         : {r.text}")
        else:
            print(f"  status after : {lb_result['status']}")
            print(f"  promoted_to  : {lb_result['promoted_to_allowed_value']}")
            print(f"  review_note  : {lb_result['review_note']}")

    print()
    print("=" * 72)
    print("APPROVAL 3/3:  bath_robe   -- SKIPPED")
    print("=" * 72)
    agg = agg_rows["bath_robe"]
    if agg is None:
        print("  reason : aggregate not found")
    else:
        print(f"  precondition : count={agg.proposal_count}, distinct={agg.distinct_product_count}")
        check = promotion_readiness(agg)
        print(f"  ready        : {check.ready}")
        if not check.ready:
            print(f"  gaps         : {check.reasons}")
        print(f"  user spec    : 'Only approve if comfortable with low-count canonical;")
        print(f"                  otherwise skip' AND 'Do NOT use force=true unless")
        print(f"                  explicitly required'.")
        print(f"  decision     : SKIP (count below readiness threshold; force=true not used)")

    db = SessionLocal()
    try:
        after = {
            "status_counts": status_counts(db, ws_id),
            "aav_active": aav_active_count(db, ws_id),
            "aav_total": aav_total(db, ws_id),
            "events_total": db.query(E).filter(
                E.workspace_id == ws_id, E.attribute_name == ATTR).count(),
            "pa_total": (db.query(ProductAttribute)
                         .join(Product, ProductAttribute.product_id == Product.id)
                         .filter(Product.workspace_id == ws_id,
                                 ProductAttribute.attribute_id == ATTR).count()),
        }
        # Updated AAV state for the two approval targets.
        wb_aav = aav_state(db, ws_id, "water_bottle")
        lb_aav = aav_state(db, ws_id, "lunch_box")
        br_aav = aav_state(db, ws_id, "bath_robe")
        # Allowed-values list (active only).
        from app.services.attribute_taxonomy_service import get_allowed_values
        allowed = get_allowed_values(db, ws_id, ATTR)
    finally:
        db.close()

    print()
    print("=" * 72)
    print("POST-APPROVAL STATE")
    print("=" * 72)
    print(f"  aggregates by status   : {after['status_counts']}")
    print(f"  AAV active / total     : {after['aav_active']} / {after['aav_total']}")
    print(f"  events_total           : {after['events_total']}")
    print(f"  PA rows total          : {after['pa_total']}")
    print()
    print(f"  water_bottle AAV       : {wb_aav}")
    print(f"  lunch_box AAV          : {lb_aav}")
    print(f"  bath_robe AAV          : {br_aav}")

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

    print()
    print("=" * 72)
    print(f"UPDATED ALLOWED VALUES  ({len(allowed)} active)")
    print("=" * 72)
    for v in sorted(allowed):
        marker = ""
        if v == "water_bottle":
            marker = "  <-- newly approved"
        elif v == "lunch_box":
            marker = "  <-- re-activated"
        print(f"  {v}{marker}")


if __name__ == "__main__":
    main()
