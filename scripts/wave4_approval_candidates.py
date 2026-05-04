"""Wave 4 approval candidate analysis (read-only).

No DB writes. No engine writes. No enrichment. Pulls pending aggregates,
filters by readiness + preferred floors, cross-references the merge
suggestion detector to flag normalization duplicates and hierarchy
candidates, and estimates coverage-gain per candidate by counting
unassigned products whose events would resolve to it.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import defaultdict

from app.api.routes.taxonomy_admin import (
    _AMBIGUOUS_AS_TARGET,
    _HIERARCHY_HEAD_NOUNS,
    _detect_merge_suggestions,
    _tokens_of,
)
from app.core.database import SessionLocal
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate as A,
    ProposedAttributeValueEvent as E,
    PROPOSAL_STATUS_PENDING,
)
from app.models.workspace import Workspace
from app.services.proposed_attribute_value_service import (
    promotion_readiness,
    PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
    PROMOTION_MIN_PROPOSAL_COUNT,
)

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"

# Preferred floors per the user's brief.
PREF_COUNT = 5
PREF_DISTINCT = 5
PREF_CONF = 0.90


def domain_for(cluster_key: str) -> str:
    """Best-guess domain bucket for the candidate. Coarse classification
    used in the report; not used for filtering."""
    k = cluster_key.lower()
    if any(t in k for t in ("toy", "puzzle", "doll", "figure")):
        return "toy"
    if any(t in k for t in ("bottle", "feeding", "pacifier", "diaper", "stroller", "nursing")):
        return "baby"
    if any(t in k for t in ("clothing", "shirt", "dress", "bathrobe", "costume", "sock", "lingerie")):
        return "apparel"
    if any(t in k for t in ("book", "stationery", "supply", "pencil", "ruler", "geometry")):
        return "stationery/learn"
    if any(t in k for t in ("skin", "hair", "makeup", "feminine", "personal_care", "grooming")):
        return "personal_care"
    if any(t in k for t in ("kitchen", "mug", "cup", "bowl", "utensil", "cutlery", "tableware",
                              "glassware", "cookware", "appliance", "food", "snack", "tea",
                              "coffee", "beverage")):
        return "home/kitchen"
    if any(t in k for t in ("furniture", "decor", "bed", "mattress", "pillow", "rug", "lamp",
                              "organizer", "storage")):
        return "home/decor"
    if any(t in k for t in ("game", "board", "card", "outdoor", "sport", "swim", "bike",
                              "scooter", "play")):
        return "outdoor/play"
    if any(t in k for t in ("bath", "towel", "robe", "shower", "tub")):
        return "bath"
    return "other"


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        # Active AAV set (active canonical taxonomy).
        active_aav = {
            v for (v,) in db.query(AttributeAllowedValue.value).filter(
                AttributeAllowedValue.workspace_id == ws_id,
                AttributeAllowedValue.attribute_name == ATTR,
                AttributeAllowedValue.is_active == True,
            ).all()
        }
        active_aav_lower = {v.lower() for v in active_aav}

        # Products that already have a PA(product_type) row.
        already_assigned_dbids = {
            pid for (pid,) in db.query(ProductAttribute.product_id)
            .join(Product, ProductAttribute.product_id == Product.id)
            .filter(Product.workspace_id == ws_id,
                    ProductAttribute.attribute_id == ATTR)
            .distinct().all()
        }

        # External product_id -> Product DB row.
        products = db.query(Product).filter(Product.workspace_id == ws_id).all()
        ext_to_dbid = {p.product_id: p.id for p in products}
        total_products = len(products)

        # Index events by their cluster_key (= normalized_value).
        # Track the set of UNASSIGNED products that have an event for each
        # cluster -- this is the coverage-gain estimate.
        events = db.query(E).filter(
            E.workspace_id == ws_id, E.attribute_name == ATTR,
        ).all()
        events_by_cluster: dict[str, list] = defaultdict(list)
        unassigned_by_cluster: dict[str, set[str]] = defaultdict(set)
        for ev in events:
            events_by_cluster[ev.normalized_value].append(ev)
            db_id = ext_to_dbid.get(ev.product_id)
            if db_id is not None and db_id not in already_assigned_dbids:
                unassigned_by_cluster[ev.normalized_value].add(ev.product_id)

        # Run the merge-suggestion detector to flag candidates that are
        # normalization duplicates / hierarchy candidates.
        merge_items, hierarchy_candidates = _detect_merge_suggestions(
            db, ws_id, ATTR
        )
        # Map cluster_key -> list of merge_type tags it appears in.
        merge_tags: dict[str, set[str]] = defaultdict(set)
        for s in merge_items:
            merge_tags[s["source_cluster"]].add(s["merge_type"])
            merge_tags[s["target_cluster"]].add(s["merge_type"])
        hierarchy_sources = {h["source_cluster"] for h in hierarchy_candidates}

        # Pull all pending aggregates.
        pending = db.query(A).filter(
            A.workspace_id == ws_id, A.attribute_name == ATTR,
            A.status == PROPOSAL_STATUS_PENDING,
        ).all()

        # Build a candidate row per pending aggregate.
        candidates = []
        for agg in pending:
            check = promotion_readiness(agg)
            if not check.ready:
                continue
            cluster = agg.cluster_key
            # Skip candidates that are already represented by an active
            # canonical -- approving them would create a near-duplicate.
            if cluster.lower() in active_aav_lower:
                continue
            # Coverage-gain: unassigned products whose events route to this
            # cluster's normalized_value.
            gain = len(unassigned_by_cluster.get(cluster, set()))
            tokens = _tokens_of(cluster)

            # Risk classification.
            risks: list[str] = []
            recommend = "approve"

            # Hard reds.
            if cluster.lower() in _AMBIGUOUS_AS_TARGET:
                risks.append("cluster_key is in ambiguous-target set")
                recommend = "defer"
            if cluster in hierarchy_sources:
                risks.append("flagged as hierarchy_candidate (subtype)")
                recommend = "defer"
            if "normalization_variant" in merge_tags.get(cluster, set()):
                risks.append("part of normalization-variant pair")
                recommend = "merge first"
            # Soft yellows.
            if "parent_child" in merge_tags.get(cluster, set()):
                risks.append("appears in parent_child suggestion")
                # don't override defer/merge if already set
                if recommend == "approve":
                    recommend = "defer"
            if "semantic_duplicate" in merge_tags.get(cluster, set()):
                risks.append("appears in semantic_duplicate suggestion")
                if recommend == "approve":
                    recommend = "defer"
            if tokens & _HIERARCHY_HEAD_NOUNS:
                risks.append("contains generic head-noun token")
                if recommend == "approve":
                    recommend = "defer"
            # Soft preference floors.
            below_floors = []
            if agg.proposal_count < PREF_COUNT:
                below_floors.append(f"count<{PREF_COUNT}")
            if agg.distinct_product_count < PREF_DISTINCT:
                below_floors.append(f"distinct<{PREF_DISTINCT}")
            if agg.avg_confidence < PREF_CONF:
                below_floors.append(f"avg_conf<{PREF_CONF}")
            if below_floors:
                risks.append("preferred floors: " + ",".join(below_floors))

            # Risk label (low/medium/high).
            if recommend != "approve":
                risk_label = "high"
            elif below_floors:
                risk_label = "medium"
            else:
                risk_label = "low"

            candidates.append({
                "cluster_key": cluster,
                "canonical": agg.canonical_value,
                "count": agg.proposal_count,
                "distinct": agg.distinct_product_count,
                "avg_conf": round(agg.avg_confidence, 3),
                "gain": gain,
                "domain": domain_for(cluster),
                "risk": risk_label,
                "risks": risks,
                "recommend": recommend,
                "sample_evidence": list(agg.sample_evidence or [])[:3],
                "sample_products": list(agg.sample_product_ids or [])[:3],
                "id": agg.id,
            })

        # Rank: low-risk first, then by gain desc, then by count desc.
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        candidates.sort(key=lambda c: (
            risk_rank[c["risk"]], -c["gain"], -c["count"]
        ))

        # ----- Print Top 15 -----
        print("=" * 92)
        print(f"WAVE 4 APPROVAL CANDIDATES  (ws_id={ws_id})  "
              f"thresholds: count>={PROMOTION_MIN_PROPOSAL_COUNT}, "
              f"distinct>={PROMOTION_MIN_DISTINCT_PRODUCTS}, "
              f"avg_conf>={PROMOTION_MIN_AVG_CONFIDENCE}")
        print("=" * 92)
        print(f"  state: {total_products} products, "
              f"{len(already_assigned_dbids)} assigned "
              f"({100*len(already_assigned_dbids)/max(total_products,1):.2f}% coverage), "
              f"{len(active_aav)} active canonicals, "
              f"{len(pending)} pending aggregates "
              f"(of which {sum(1 for a in pending if promotion_readiness(a).ready)} ready)")
        print()

        top15 = candidates[:15]
        for i, c in enumerate(top15, 1):
            print(f"-- {i:>2}. {c['cluster_key']:<24}  domain={c['domain']:<16} "
                  f"risk={c['risk']:<6} recommend={c['recommend']}")
            print(f"     count={c['count']}, distinct={c['distinct']}, "
                  f"avg_conf={c['avg_conf']}, est_gain={c['gain']}")
            print(f"     evidence : {c['sample_evidence']}")
            print(f"     products : {c['sample_products']}")
            if c["risks"]:
                print(f"     risks    : {c['risks']}")
            print()

        # ----- Coverage estimation -----
        print("=" * 92)
        print("ESTIMATED COVERAGE GAIN")
        print("=" * 92)
        approve_recs = [c for c in candidates if c["recommend"] == "approve"]
        # The "gain" sets may overlap if a product has multiple events
        # routing to different cluster_keys. To avoid double counting,
        # union them.
        union_set: set[str] = set()
        for c in approve_recs:
            union_set |= unassigned_by_cluster.get(c["cluster_key"], set())
        approve_top10 = approve_recs[:10]
        union_top10: set[str] = set()
        for c in approve_top10:
            union_top10 |= unassigned_by_cluster.get(c["cluster_key"], set())
        cov_now = len(already_assigned_dbids)
        print(f"  if ALL approve-recommended candidates ({len(approve_recs)}) approved:")
        print(f"    new assignable products (union): {len(union_set)}")
        print(f"    coverage           : "
              f"{100*cov_now/max(total_products,1):.2f}% -> "
              f"{100*(cov_now + len(union_set))/max(total_products,1):.2f}%")
        print()
        print(f"  if TOP 10 approve-recommended candidates approved:")
        print(f"    new assignable products (union): {len(union_top10)}")
        print(f"    coverage           : "
              f"{100*cov_now/max(total_products,1):.2f}% -> "
              f"{100*(cov_now + len(union_top10))/max(total_products,1):.2f}%")
        print(f"    NOTE: gain is approximate -- assumes each unassigned")
        print(f"    product would actually pick up the canonical via the")
        print(f"    same backfill rule (highest-confidence event in active AAV).")

        # ----- Required merges before approval -----
        print()
        print("=" * 92)
        print("REQUIRED MERGES BEFORE APPROVAL  (cluster appears in normalization_variant)")
        print("=" * 92)
        norm_pairs = [s for s in merge_items if s["merge_type"] == "normalization_variant"]
        for s in norm_pairs:
            print(f"  {s['recommended_source']:<22} -> {s['recommended_target']:<22} "
                  f"(executable={s['executable']}, src_status={s['source_status']})")
        if not norm_pairs:
            print("  (none active)")

        # ----- Should NOT be approved -----
        print()
        print("=" * 92)
        print("VALUES THAT SHOULD NOT BE APPROVED  (top reasons)")
        print("=" * 92)
        deferrals = [c for c in candidates if c["recommend"] != "approve"][:15]
        for c in deferrals:
            print(f"  {c['cluster_key']:<24} (count={c['count']}, gain={c['gain']}) "
                  f"-> {c['recommend']} : {c['risks'][:2]}")

        # ----- Final recommended approval list (max 10) -----
        print()
        print("=" * 92)
        print("FINAL RECOMMENDED APPROVAL LIST  (max 10)")
        print("=" * 92)
        final = approve_top10
        for i, c in enumerate(final, 1):
            print(f"  {i:>2}. {c['cluster_key']:<22} count={c['count']:<3} "
                  f"distinct={c['distinct']:<3} avg_conf={c['avg_conf']} "
                  f"gain={c['gain']:<4} risk={c['risk']:<6} domain={c['domain']}")
        print()
        print(f"  total: {len(final)} candidates")
        print(f"  combined estimated coverage gain (union): {len(union_top10)} products")
        print(f"  coverage projection: "
              f"{100*cov_now/max(total_products,1):.2f}% -> "
              f"{100*(cov_now + len(union_top10))/max(total_products,1):.2f}%")
    finally:
        db.close()


if __name__ == "__main__":
    main()
