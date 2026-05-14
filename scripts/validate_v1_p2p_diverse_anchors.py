"""V1 product-to-product validation across 20 diverse anchors.

Read-only. Uses existing recommendation_v1_service. No scoring, taxonomy,
enrichment, or fake-customer changes.

Output:
  - per-anchor: id, name, product_type, age_group, gender, tier, top-5 recs
    with score and matched components, plus a heuristic judgment
  - summary: tier mix, good/acceptable/bad %, strongest/weakest types,
    common failure patterns, demo-readiness verdict
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.services.recommendation_v1_service import recommend


WS_SLUG = "mumzworld_v3_sample"

# 20 product_types spanning the catalog: high-volume (book, party_supply),
# mid-volume (changing_mat, makeup), and lower-volume (stroller, pacifier,
# teether). Picked for diversity, not curated by name.
TARGET_PRODUCT_TYPES: list[str] = [
    "book", "party_supply", "water_bottle", "bedding", "skincare",
    "changing_mat", "makeup", "art_supply", "towel", "ride_on",
    "furniture", "vehicle_toy", "puzzle", "bath_product", "playset",
    "backpack", "shirt", "stroller", "pacifier", "teether",
]


def fetch_attrs(db, workspace_id: int, product_db_id: int) -> dict[str, str]:
    rows = (db.query(ProductAttribute.attribute_id,
                     ProductAttribute.attribute_value)
            .filter(ProductAttribute.product_id == product_db_id)
            .all())
    return {a: v for a, v in rows}


def pick_anchor(db, workspace_id: int, product_type: str) -> Product | None:
    """First (lowest-id) product whose product_type attribute matches."""
    return (db.query(Product)
            .join(ProductAttribute, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == workspace_id,
                    ProductAttribute.attribute_id == "product_type",
                    ProductAttribute.attribute_value == product_type)
            .order_by(Product.id)
            .first())


def judge(tier: str, recs: list[dict], anchor_attrs: dict[str, str]) -> tuple[str, str | None]:
    """Heuristic per-anchor verdict.

    good        tier_1 with mean_score >= 0.30 and >=3 recs with name_overlap>0
    bad         tier == 'empty', OR partial/tier_2 dominated by
                age_non_adjacent_penalty, OR fewer than 3 recs returned
    acceptable  everything else
    """
    if tier == "empty" or len(recs) == 0:
        return ("bad", "no recommendations served")
    if len(recs) < 3:
        return ("bad", f"thin result set: only {len(recs)} recs")

    scores = [r["score"] for r in recs]
    mean_score = mean(scores) if scores else 0.0

    def has_component(rec: dict, name: str) -> bool:
        return any(w["component"] == name for w in rec.get("why", []))

    name_overlap_count = sum(1 for r in recs if has_component(r, "name_overlap"))
    non_adj_count = sum(1 for r in recs if has_component(r, "age_non_adjacent_penalty"))

    if non_adj_count >= 3:
        return ("bad", f"{non_adj_count}/5 recs cross non-adjacent age band")

    if tier == "tier_1" and mean_score >= 0.30 and name_overlap_count >= 3:
        return ("good", None)

    if tier == "tier_1" and mean_score < 0.05 and name_overlap_count == 0:
        return ("bad", "tier_1 cohort but zero discriminating signal "
                       "(no brand/gender/name overlap on any rec)")

    return ("acceptable", None)


def short(s: str | None, n: int = 60) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def fmt_components(why: list[dict]) -> str:
    parts = []
    for w in why:
        contrib = w.get("contribution", 0.0)
        parts.append(f"{w['component']}={contrib:+.3f}" if contrib else f"{w['component']}")
    return ", ".join(parts)


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        anchors: list[tuple[str, Product]] = []
        for pt in TARGET_PRODUCT_TYPES:
            p = pick_anchor(db, ws_id, pt)
            if p is not None:
                anchors.append((pt, p))

        if len(anchors) < 20:
            print(f"warning: only found {len(anchors)} anchors "
                  f"out of {len(TARGET_PRODUCT_TYPES)} requested types")

        print("=" * 100)
        print(f"V1 PRODUCT-TO-PRODUCT VALIDATION  (ws={WS_SLUG}, ws_id={ws_id})")
        print(f"Anchors: {len(anchors)} across {len(set(pt for pt, _ in anchors))} product_types")
        print("=" * 100)

        per_anchor_records = []  # (anchor_pt, judgment, mean_score, tier, reason)

        for i, (pt, anchor) in enumerate(anchors, 1):
            a_attrs = fetch_attrs(db, ws_id, anchor.id)
            resp = recommend(
                db=db, workspace_id=ws_id,
                anchor_product_id=anchor.product_id,
                top_n=5, customer_id=None,
            )
            verdict, reason = judge(resp.tier, resp.recommendations, a_attrs)
            mean_score = mean([r["score"] for r in resp.recommendations]) \
                if resp.recommendations else 0.0
            per_anchor_records.append(
                (pt, verdict, mean_score, resp.tier, reason)
            )

            print()
            print(f"--- {i:>2}. product_type={pt} | age_group={a_attrs.get('age_group') or '-'} "
                  f"| gender={a_attrs.get('gender') or '-'} ---")
            print(f"    anchor       : {anchor.product_id}")
            print(f"    name         : {short(anchor.name, 90)}")
            print(f"    tier         : {resp.tier}  "
                  f"(cohort_size={resp.cohort_size_observed}, "
                  f"escalated_from={resp.escalated_from})")
            print(f"    n_recs       : {len(resp.recommendations)}  "
                  f"mean_score={mean_score:.3f}")
            print(f"    judgment     : {verdict.upper()}"
                  + (f"   reason: {reason}" if reason else ""))
            print(f"    top-5:")
            if not resp.recommendations:
                print(f"      (no recommendations)")
            else:
                for r in resp.recommendations:
                    rp = (db.query(Product)
                          .filter(Product.workspace_id == ws_id,
                                  Product.product_id == r["product_id"]).first())
                    rp_attrs = fetch_attrs(db, ws_id, rp.id) if rp else {}
                    rname = short(rp.name, 80) if rp else "?"
                    print(f"      -> {r['product_id']:<28} score={r['score']:+.3f}  "
                          f"pt={rp_attrs.get('product_type','-')[:14]:<14} "
                          f"age={rp_attrs.get('age_group','-')[:8]:<8} "
                          f"gen={rp_attrs.get('gender','-')[:6]:<6}")
                    print(f"         name: {rname}")
                    print(f"         why : {fmt_components(r['why'])}")

        # ---------------- Summary ----------------
        print()
        print("=" * 100)
        print("SUMMARY")
        print("=" * 100)

        verdict_counts: dict[str, int] = defaultdict(int)
        for _, v, _, _, _ in per_anchor_records:
            verdict_counts[v] += 1
        total = len(per_anchor_records)

        print(f"  Anchors evaluated : {total}")
        for v in ("good", "acceptable", "bad"):
            n = verdict_counts.get(v, 0)
            pct = 100.0 * n / max(total, 1)
            print(f"  {v.upper():<11}      : {n:>2}  ({pct:>5.1f}%)")

        # tier distribution
        tier_dist: dict[str, int] = defaultdict(int)
        for _, _, _, t, _ in per_anchor_records:
            tier_dist[t] += 1
        print()
        print("  Tier mix:")
        for t in ("tier_1", "tier_2", "partial", "empty"):
            n = tier_dist.get(t, 0)
            pct = 100.0 * n / max(total, 1)
            print(f"    {t:<10}     : {n:>2}  ({pct:>5.1f}%)")

        # strongest / weakest by mean score
        by_pt_score = [(pt, ms) for pt, _, ms, _, _ in per_anchor_records]
        by_pt_score.sort(key=lambda x: -x[1])
        print()
        print("  Strongest product_types (by mean rec score on the anchor):")
        for pt, ms in by_pt_score[:5]:
            print(f"    {pt:<22} mean_score={ms:.3f}")
        print()
        print("  Weakest product_types (by mean rec score on the anchor):")
        for pt, ms in by_pt_score[-5:]:
            print(f"    {pt:<22} mean_score={ms:.3f}")

        # failure patterns
        print()
        print("  Failure / weakness reasons (anchors marked bad or acceptable):")
        for pt, v, ms, t, reason in per_anchor_records:
            if v != "good":
                print(f"    [{v:<10}] {pt:<22} tier={t:<8} mean={ms:.3f}  "
                      f"{reason or '(no specific failure reason)'}")

        # demo-ready verdict
        good_pct = 100.0 * verdict_counts.get("good", 0) / max(total, 1)
        bad_pct = 100.0 * verdict_counts.get("bad", 0) / max(total, 1)
        print()
        print("  DEMO-READINESS:")
        if good_pct >= 60 and bad_pct <= 15:
            verdict = "READY -- strong majority of anchors produce coherent top-5"
        elif good_pct >= 40 and bad_pct <= 25:
            verdict = "DEMO-CAPABLE WITH CURATION -- pick anchors from strongest "\
                      "product_types; avoid weakest"
        else:
            verdict = "NOT DEMO-READY -- too many empty/weak results; revisit "\
                      "cohort coverage or scoring signals"
        print(f"    good={good_pct:.0f}%, bad={bad_pct:.0f}%  -->  {verdict}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
