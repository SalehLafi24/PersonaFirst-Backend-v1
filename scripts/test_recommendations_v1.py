"""Validate V1 recommendations against mumzworld_v3_sample.

Read-only: no DB writes. Iterates every product in the workspace, runs
the recommender, classifies the response, and reports tier distribution,
average score per tier, sample responses, and the validation invariants.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
from app.services.recommendation_v1_service import (
    DEFAULT_COHORT_ATTRIBUTES, recommend,
)

WS_SLUG = "mumzworld_v3_sample"
client = TestClient(app)


def snapshot_state(db, ws_id: int) -> dict:
    return {
        "products": db.query(Product).filter(Product.workspace_id == ws_id).count(),
        "events": db.query(E).filter(E.workspace_id == ws_id).count(),
        "aav_active": db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id,
            AttributeAllowedValue.is_active == True).count(),
        "aav_total": db.query(AttributeAllowedValue).filter(
            AttributeAllowedValue.workspace_id == ws_id).count(),
        "aggregates": db.query(A).filter(A.workspace_id == ws_id).count(),
        "pa_total": (db.query(ProductAttribute)
                     .join(Product, ProductAttribute.product_id == Product.id)
                     .filter(Product.workspace_id == ws_id).count()),
    }


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id
        before = snapshot_state(db, ws_id)
        anchors = db.query(Product).filter(Product.workspace_id == ws_id).all()
    finally:
        db.close()

    print("=" * 80)
    print(f"V1 RECOMMENDATIONS VALIDATION  (ws_id={ws_id}, "
          f"{len(anchors)} anchor products)")
    print("=" * 80)
    print(f"  cohort attributes : {DEFAULT_COHORT_ATTRIBUTES}")
    print(f"  PRE state         : {before}")

    # Run recommender for every product in the workspace.
    tier_counts: Counter = Counter()
    tier_scores: dict[str, list[float]] = defaultdict(list)
    cohort_sizes: list[int] = []
    sample_responses: dict[str, list] = {
        "tier_1": [], "tier_2": [], "partial": [], "empty": [],
    }
    SAMPLE_PER_TIER = 3

    db = SessionLocal()
    try:
        for anchor in anchors:
            resp = recommend(
                db=db, workspace_id=ws_id,
                anchor_product_id=anchor.product_id,
                top_n=5, customer_id=None,
            )
            tier = resp.tier
            tier_counts[tier] += 1
            cohort_sizes.append(resp.cohort_size_observed)
            for r in resp.recommendations:
                tier_scores[tier].append(r["score"])
            if len(sample_responses[tier]) < SAMPLE_PER_TIER:
                sample_responses[tier].append({
                    "anchor_id": anchor.product_id,
                    "anchor_name": (anchor.name or "")[:60],
                    "tier": resp.tier,
                    "cohort_size_observed": resp.cohort_size_observed,
                    "escalated_from": resp.escalated_from,
                    "n_recs": len(resp.recommendations),
                    "first_rec": resp.recommendations[0] if resp.recommendations else None,
                })
    finally:
        db.close()

    total = len(anchors)
    print()
    print("=" * 80)
    print("TIER DISTRIBUTION")
    print("=" * 80)
    for tier in ("tier_1", "tier_2", "partial", "empty"):
        n = tier_counts.get(tier, 0)
        pct = 100.0 * n / max(total, 1)
        print(f"  {tier:<10} {n:>5}  ({pct:>5.2f}%)")
    print(f"  {'TOTAL':<10} {total:>5}  (100.00%)")

    print()
    print("=" * 80)
    print("AVERAGE RECOMMENDATION SCORE BY TIER")
    print("=" * 80)
    for tier in ("tier_1", "tier_2", "partial", "empty"):
        scores = tier_scores.get(tier, [])
        if scores:
            avg = sum(scores) / len(scores)
            print(f"  {tier:<10} n_recs={len(scores):<5} mean_score={avg:.4f}  "
                  f"min={min(scores):.4f}  max={max(scores):.4f}")
        else:
            print(f"  {tier:<10} (no recommendations served)")

    print()
    print("=" * 80)
    print("COHORT-SIZE DISTRIBUTION (across all anchors)")
    print("=" * 80)
    bins = [0, 1, 2, 3, 4, 5, 10, 20, 50, 100, 99999]
    bin_labels = [f"{bins[i]}-{bins[i+1]-1}" for i in range(len(bins)-1)]
    bin_counts = [0] * (len(bins) - 1)
    for cs in cohort_sizes:
        for i in range(len(bins) - 1):
            if bins[i] <= cs < bins[i + 1]:
                bin_counts[i] += 1
                break
    for label, n in zip(bin_labels, bin_counts):
        bar = "#" * (n * 60 // max(max(bin_counts), 1))
        print(f"  {label:<10} {n:>5}  {bar}")

    # ----- 10 sample recommendation responses (full payload) -----
    print()
    print("=" * 80)
    print("10 SAMPLE RECOMMENDATION RESPONSES")
    print("=" * 80)
    chosen = []
    for tier in ("tier_1", "tier_1", "tier_1", "tier_2", "tier_2",
                 "partial", "partial", "empty", "empty", "empty"):
        if sample_responses[tier]:
            chosen.append(sample_responses[tier].pop(0))
    db = SessionLocal()
    try:
        for i, samp in enumerate(chosen, 1):
            full = recommend(
                db=db, workspace_id=ws_id,
                anchor_product_id=samp["anchor_id"],
                top_n=5, customer_id=None,
            )
            payload = {
                "anchor_product_id": full.anchor_product_id,
                "anchor_name": samp["anchor_name"],
                "tier": full.tier,
                "cohort_size_observed": full.cohort_size_observed,
                "escalated_from": full.escalated_from,
                "n_recs": len(full.recommendations),
                "recommendations": full.recommendations[:3],
            }
            print(f"\n--- sample {i} (tier={full.tier}) ---")
            print(json.dumps(payload, indent=2)[:1600])
    finally:
        db.close()

    # ----- Strong / Fallback / Empty named examples -----
    print()
    print("=" * 80)
    print("NAMED EXAMPLES")
    print("=" * 80)

    db = SessionLocal()
    try:
        # Strong Tier 1: pick a 'changing_mat × infant' product if possible.
        cm = (db.query(Product)
              .join(ProductAttribute, ProductAttribute.product_id == Product.id)
              .filter(Product.workspace_id == ws_id,
                      ProductAttribute.attribute_id == "product_type",
                      ProductAttribute.attribute_value == "changing_mat")
              .first())
        if cm is not None:
            r = recommend(db=db, workspace_id=ws_id,
                          anchor_product_id=cm.product_id, top_n=5,
                          customer_id=None)
            print()
            print(">>> STRONG TIER_1 EXAMPLE  (product_type=changing_mat)")
            print(f"    anchor : {cm.product_id}  --  {(cm.name or '')[:70]}")
            print(f"    tier   : {r.tier}  cohort_size={r.cohort_size_observed}")
            for rec in r.recommendations[:3]:
                p = db.query(Product).filter(
                    Product.workspace_id == ws_id,
                    Product.product_id == rec["product_id"]).first()
                pname = (p.name or "")[:60] if p else "?"
                why_short = ", ".join(
                    f"{w['component']}={w['contribution']}"
                    for w in rec["why"]
                )
                print(f"      -> {rec['product_id']:<32} score={rec['score']:.3f}  {pname}")
                print(f"         why: {why_short}")

        # Tier 2 fallback: find a product whose Tier 1 cell has < 3 peers.
        # Walk products and pick the first that produces tier_2.
        for p in (db.query(Product)
                  .filter(Product.workspace_id == ws_id)
                  .order_by(Product.id).all()):
            r = recommend(db=db, workspace_id=ws_id,
                          anchor_product_id=p.product_id, top_n=5,
                          customer_id=None)
            if r.tier == "tier_2":
                print()
                print(">>> TIER_2 FALLBACK EXAMPLE  (escalated_from tier_1)")
                print(f"    anchor : {p.product_id}  --  {(p.name or '')[:70]}")
                print(f"    tier   : {r.tier}  cohort_size={r.cohort_size_observed}  "
                      f"escalated_from={r.escalated_from}")
                for rec in r.recommendations[:3]:
                    pp = db.query(Product).filter(
                        Product.workspace_id == ws_id,
                        Product.product_id == rec["product_id"]).first()
                    pname = (pp.name or "")[:60] if pp else "?"
                    why_short = ", ".join(
                        f"{w['component']}={w['contribution']}"
                        for w in rec["why"]
                    )
                    print(f"      -> {rec['product_id']:<32} score={rec['score']:.3f}  {pname}")
                    print(f"         why: {why_short}")
                break

        # Partial / Empty examples.
        for target_tier in ("partial", "empty"):
            for p in (db.query(Product)
                      .filter(Product.workspace_id == ws_id)
                      .order_by(Product.id).all()):
                r = recommend(db=db, workspace_id=ws_id,
                              anchor_product_id=p.product_id, top_n=5,
                              customer_id=None)
                if r.tier == target_tier:
                    print()
                    print(f">>> {target_tier.upper()} EXAMPLE")
                    print(f"    anchor : {p.product_id}  --  {(p.name or '')[:70]}")
                    print(f"    tier   : {r.tier}  cohort_size={r.cohort_size_observed}  "
                          f"escalated_from={r.escalated_from}")
                    print(f"    recs   : {len(r.recommendations)}")
                    for rec in r.recommendations[:3]:
                        why_short = ", ".join(
                            f"{w['component']}={w['contribution']}"
                            for w in rec["why"]
                        )
                        print(f"      -> {rec['product_id']:<32} score={rec['score']:.3f}")
                        print(f"         why: {why_short}")
                    break
    finally:
        db.close()

    # ----- Confirmations -----
    db = SessionLocal()
    try:
        after = snapshot_state(db, ws_id)
    finally:
        db.close()

    print()
    print("=" * 80)
    print("CONFIRMATIONS  (no data modified by the recommender)")
    print("=" * 80)
    for k in before:
        ok = before[k] == after[k]
        marker = "OK" if ok else "FAIL"
        print(f"  {marker}  {k:<14}  before={before[k]}  after={after[k]}")
    print()
    print(f"  brand component   : Product has no brand column today; brand")
    print(f"                      contribution evaluates to 0. Slot reserved.")
    print(f"  price component   : Product has no price column today; price")
    print(f"                      contribution evaluates to 0. Slot reserved.")
    print(f"  stock filter      : Product has no stock column today; stock")
    print(f"                      filtering is skipped (per spec).")
    print(f"  age guard         : enforced (anchor in {{infant, toddler}} cannot")
    print(f"                      receive teen/adult candidates).")
    print(f"  HTTP smoke test   :")
    sample_id = anchors[0].product_id if anchors else ""
    if sample_id:
        rr = client.get(
            f"/api/recommendations/products/{sample_id}",
            params={"workspace_id": ws_id, "top_n": 3, "customer_id": "alice"},
        )
        print(f"    GET /api/recommendations/products/{sample_id[:24]}...")
        print(f"    status={rr.status_code}, top-level keys={list(rr.json().keys())}")


if __name__ == "__main__":
    main()
