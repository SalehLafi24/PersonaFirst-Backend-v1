"""Read-only analysis of mumzworld_v3_sample after the 1019-product run.

Reads ProposedAttributeValueEvent directly to compute live per-cluster
stats (the engine deliberately freezes already-approved aggregates, so
the aggregate table cannot be trusted for current counts on approved
clusters). Produces the READY / ALMOST READY / long-tail breakdown the
brief asked for. No writes.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import Counter, defaultdict
from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.models.product import Product
from app.models.proposed_attribute_value import (
    ProposedAttributeValueEvent as E,
)
from app.services.attribute_taxonomy_service import get_allowed_values
from app.services.proposed_attribute_value_service import (
    PROMOTION_MIN_PROPOSAL_COUNT, PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
)

MERGE_FLAGGED = {
    "educational_toy", "haircare", "hair_product", "card_game", "arcade_game",
    "craft_kit", "dollhouse", "toy_gun", "collectible", "figurine",
    "collectible_toy", "maternity_dress", "bra", "pregnancy_pillow",
    "nursing_pillow",
}
TOO_BROAD = {"accessory", "bag", "clothing", "toy", "beauty", "product"}
AMBIGUOUS = {"activity_toy", "play_kitchen", "personal_care_appliance",
             "inflatable_toy", "ball_pit", "science_kit", "personal_care"}
EXCLUSIONS = MERGE_FLAGGED | TOO_BROAD | AMBIGUOUS

DOMAIN_BY_PREFIX = [
    ("plush", "toys"), ("puzzle", "toys"), ("game", "toys"),
    ("playset", "toys"), ("doll", "toys"), ("art_", "toys"),
    ("craft", "toys"), ("ride_", "toys"), ("walker", "toys"),
    ("rattle", "toys"), ("vehicle", "toys"), ("construction_", "toys"),
    ("sensory", "toys"), ("learning", "toys"), ("musical", "toys"),
    ("action_", "toys"), ("outdoor", "toys"), ("toy", "toys"),
    ("makeup", "beauty"), ("skincare", "beauty"),
    ("bath_product", "beauty"), ("hair_", "beauty"), ("nail", "beauty"),
    ("perfume", "beauty"), ("deodorant", "beauty"),
    ("feminine", "beauty"), ("oral", "beauty"),
    ("toothpaste", "beauty"), ("mouthwash", "beauty"),
    ("clothing", "apparel"), ("dress", "apparel"),
    ("activewear", "apparel"), ("shapewear", "apparel"),
    ("lingerie", "apparel"), ("eyewear", "apparel"),
    ("costume", "apparel"),
    ("nursing", "maternity"), ("pregnancy", "maternity"),
    ("maternity", "maternity"), ("breastfeeding", "maternity"),
    ("supplement", "health_wellness"), ("vitamin", "health_wellness"),
    ("fitness", "health_wellness"), ("sleep_", "health_wellness"),
    ("baby_food", "feeding"), ("bottle", "feeding"),
    ("pacifier", "feeding"), ("teether", "feeding"), ("formula", "feeding"),
    ("wipe", "diapers"), ("diaper", "diapers"),
    ("napkin", "party"), ("party", "party"), ("balloon", "party"),
    ("furniture", "home"), ("crib", "home"), ("decor", "home"),
    ("blanket", "home"), ("pillow", "home"), ("kitchen", "home"),
    ("book", "books"),
    ("backpack", "school"), ("lunchbox", "school"),
    ("bag", "other"), ("accessory", "other"), ("keychain", "other"),
]


def domain_of(key):
    for prefix, dom in DOMAIN_BY_PREFIX:
        if key.startswith(prefix):
            return dom
    return "unmapped"


def main():
    db = SessionLocal()
    try:
        ws = (db.query(Workspace)
              .filter(Workspace.slug == "mumzworld_v3_sample").first())
        approved_set = {v.lower() for v in get_allowed_values(db, ws.id, "product_type")}

        rows = (
            db.query(
                E.normalized_value.label("ck"),
                func.count(E.id).label("count"),
                func.count(func.distinct(E.product_id)).label("distinct"),
                func.avg(E.confidence).label("avg_conf"),
            )
            .filter(E.workspace_id == ws.id, E.attribute_name == "product_type")
            .group_by(E.normalized_value)
            .all()
        )
        cluster_stats = [
            {"cluster_key": r.ck, "count": int(r.count),
             "distinct": int(r.distinct),
             "avg_conf": float(r.avg_conf or 0.0)}
            for r in rows
        ]
        cluster_stats.sort(key=lambda x: (-x["count"], x["cluster_key"]))

        total_events = sum(c["count"] for c in cluster_stats)
        total_products = (db.query(Product)
                          .filter(Product.workspace_id == ws.id).count())
        products_with_event = (
            db.query(func.count(func.distinct(E.product_id)))
            .filter(E.workspace_id == ws.id,
                    E.attribute_name == "product_type")
            .scalar()
        )

        ready, almost, excluded, longtail = [], [], [], []
        for c in cluster_stats:
            c["domain"] = domain_of(c["cluster_key"])
            c["is_approved"] = c["cluster_key"].lower() in approved_set
            full = (c["count"] >= PROMOTION_MIN_PROPOSAL_COUNT and
                    c["distinct"] >= PROMOTION_MIN_DISTINCT_PRODUCTS and
                    c["avg_conf"] >= PROMOTION_MIN_AVG_CONFIDENCE)
            near = (c["count"] == 2 and c["distinct"] == 2 and
                    c["avg_conf"] >= PROMOTION_MIN_AVG_CONFIDENCE)
            if c["cluster_key"] in EXCLUSIONS:
                excluded.append(c)
            elif full:
                ready.append(c)
            elif near:
                almost.append(c)
            else:
                longtail.append(c)

        # Coverage
        pids_per_cluster = defaultdict(set)
        for ev in (db.query(E)
                   .filter(E.workspace_id == ws.id,
                           E.attribute_name == "product_type").all()):
            pids_per_cluster[ev.normalized_value].add(ev.product_id)

        ready_pids = set()
        for c in ready:
            ready_pids |= pids_per_cluster.get(c["cluster_key"], set())
        ready_almost_pids = set(ready_pids)
        for c in almost:
            ready_almost_pids |= pids_per_cluster.get(c["cluster_key"], set())
        approved_pids = set()
        for c in cluster_stats:
            if c["is_approved"]:
                approved_pids |= pids_per_cluster.get(c["cluster_key"], set())

        print("=" * 100)
        print("SCALED PIPELINE RESULTS - workspace=mumzworld_v3_sample")
        print("=" * 100)
        print(f"  total products in workspace        : {total_products}")
        print(f"  products with >=1 event            : {products_with_event}")
        print(f"  total proposal events              : {total_events}")
        print(f"  unique cluster_keys                : {len(cluster_stats)}")
        print(f"  approved canonical values          : {len(approved_set)}")
        print()
        print(f"  READY candidates (excl. flagged)   : {len(ready)}")
        print(f"  ALMOST READY candidates            : {len(almost)}")
        print(f"  Excluded (merge/too-broad/ambig.)  : {len(excluded)}")
        print(f"  Long tail (below thresholds)       : {len(longtail)}")
        print()
        print(f"  product coverage IF READY approved : {len(ready_pids)}/{total_products} = {100 * len(ready_pids) / total_products:.1f}%")
        print(f"  product coverage IF READY+ALMOST   : {len(ready_almost_pids)}/{total_products} = {100 * len(ready_almost_pids) / total_products:.1f}%")
        print(f"  product coverage from current 17 approved canonicals: "
              f"{len(approved_pids)}/{total_products} = "
              f"{100 * len(approved_pids) / total_products:.1f}%")
        print()
        print("=" * 100)
        print("TOP 50 cluster_keys by count")
        print("=" * 100)
        print(f"  {'rank':<5}{'cluster_key':<30}{'count':>6}{'distinct':>10}"
              f"{'avg_conf':>10}{'approved':>10}{'domain':>14}{'gate':>14}")
        for r, c in enumerate(cluster_stats[:50], 1):
            if c["cluster_key"] in EXCLUSIONS:
                gate = "EXCLUDED"
            elif c in ready:
                gate = "READY"
            elif c in almost:
                gate = "ALMOST_READY"
            else:
                gate = "long_tail"
            print(f"  {r:<5}{c['cluster_key'][:28]:<30}{c['count']:>6}"
                  f"{c['distinct']:>10}{c['avg_conf']:>10.3f}"
                  f"{str(c['is_approved']):>10}{c['domain']:>14}{gate:>14}")

        print()
        print("=" * 100)
        print("READY (full list, sorted by count desc)")
        print("=" * 100)
        for r, c in enumerate(sorted(ready, key=lambda x: -x["count"]), 1):
            marker = "[approved]" if c["is_approved"] else "[NEW]"
            print(f"  {r:<3}{c['cluster_key'][:28]:<30} count={c['count']:>4} "
                  f"distinct={c['distinct']:>4} avg_conf={c['avg_conf']:.3f} "
                  f"domain={c['domain']:<14} {marker}")

        print()
        print("=" * 100)
        print("ALMOST READY (full list, sorted by count desc)")
        print("=" * 100)
        for r, c in enumerate(sorted(almost, key=lambda x: -x["count"]), 1):
            print(f"  {r:<3}{c['cluster_key'][:28]:<30} count={c['count']:>4} "
                  f"distinct={c['distinct']:>4} avg_conf={c['avg_conf']:.3f} "
                  f"domain={c['domain']:<14}")

        print()
        print("=" * 100)
        print("Domain distribution (READY)")
        print("=" * 100)
        cnt = Counter(c["domain"] for c in ready)
        evt = Counter()
        for c in ready:
            evt[c["domain"]] += c["count"]
        for d, n in cnt.most_common():
            print(f"  {d:<18} clusters={n:>3}  events={evt[d]:>5}")

        print()
        print("=" * 100)
        print("Domain distribution (READY + ALMOST)")
        print("=" * 100)
        cnt2 = Counter(c["domain"] for c in ready + almost)
        evt2 = Counter()
        for c in ready + almost:
            evt2[c["domain"]] += c["count"]
        for d, n in cnt2.most_common():
            print(f"  {d:<18} clusters={n:>3}  events={evt2[d]:>5}")

        print()
        print("=" * 100)
        print("Comparison vs 200-product run")
        print("=" * 100)
        prev = {"products": 200, "events": 200, "clusters": 69,
                "ready": 17, "almost": 10, "excluded": 25, "longtail": 17,
                "ready_cov_pct": 63.0, "ready_almost_cov_pct": 73.0}
        print(f"  {'metric':<32}{'200':>14}{'1019':>14}{'delta':>14}")
        print(f"  {'products processed':<32}"
              f"{prev['products']:>14}{products_with_event:>14}"
              f"{products_with_event - prev['products']:>+14d}")
        print(f"  {'total events':<32}"
              f"{prev['events']:>14}{total_events:>14}"
              f"{total_events - prev['events']:>+14d}")
        print(f"  {'unique cluster_keys':<32}"
              f"{prev['clusters']:>14}{len(cluster_stats):>14}"
              f"{len(cluster_stats) - prev['clusters']:>+14d}")
        print(f"  {'READY':<32}"
              f"{prev['ready']:>14}{len(ready):>14}"
              f"{len(ready) - prev['ready']:>+14d}")
        print(f"  {'ALMOST READY':<32}"
              f"{prev['almost']:>14}{len(almost):>14}"
              f"{len(almost) - prev['almost']:>+14d}")
        print(f"  {'excluded':<32}"
              f"{prev['excluded']:>14}{len(excluded):>14}"
              f"{len(excluded) - prev['excluded']:>+14d}")
        print(f"  {'long tail':<32}"
              f"{prev['longtail']:>14}{len(longtail):>14}"
              f"{len(longtail) - prev['longtail']:>+14d}")
        print(f"  {'READY product coverage':<32}"
              f"{prev['ready_cov_pct']:>13.1f}%"
              f"{100 * len(ready_pids) / total_products:>13.1f}%")
        print(f"  {'READY+ALMOST coverage':<32}"
              f"{prev['ready_almost_cov_pct']:>13.1f}%"
              f"{100 * len(ready_almost_pids) / total_products:>13.1f}%")
    finally:
        db.close()


if __name__ == "__main__":
    main()
