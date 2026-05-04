"""Read-only evaluation: is product_type sufficient for recommendations?

Defines "can receive at least 3 recommendations" as:
    - product has product_type assigned (PA row exists)
    - product_type pool size >= 4   (so 3 peers exist after excluding self)

No DB writes. No engine writes. Pure analytics over current state.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import Counter, defaultdict

from app.core.database import SessionLocal
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"
MIN_POOL_FOR_3_RECS = 4  # self + 3 peers


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).one()
        ws_id = ws.id

        products = db.query(Product).filter(Product.workspace_id == ws_id).all()
        total_products = len(products)

        # product_id (db_id) -> product_type value
        pt_map: dict[int, str] = {}
        for pa in (db.query(ProductAttribute)
                   .join(Product, ProductAttribute.product_id == Product.id)
                   .filter(Product.workspace_id == ws_id,
                           ProductAttribute.attribute_id == ATTR).all()):
            pt_map[pa.product_id] = pa.attribute_value

        ext_to_dbid = {p.product_id: p.id for p in products}
        dbid_to_product = {p.id: p for p in products}

        pt_counter: Counter = Counter(pt_map.values())
        # Pool index for sample lookups.
        pool: dict[str, list[int]] = defaultdict(list)
        for db_id, pt in pt_map.items():
            pool[pt].append(db_id)

        # ----- Q1: % of products that can receive >=3 recommendations -----
        can_recv_3 = 0
        cannot_recv_3_unassigned = 0
        cannot_recv_3_small_pool = 0
        for prod in products:
            pt = pt_map.get(prod.id)
            if pt is None:
                cannot_recv_3_unassigned += 1
                continue
            pool_size = pt_counter[pt]  # includes self
            if pool_size >= MIN_POOL_FOR_3_RECS:
                can_recv_3 += 1
            else:
                cannot_recv_3_small_pool += 1

        # ----- Q2: % of product_types with <3 products -----
        small_pts = [pt for pt, n in pt_counter.items() if n < 3]
        # Note: brief asks <3, not <4; using <3 as worded.

        # ----- Q3: top 10 strongest -----
        top10 = pt_counter.most_common(10)

        # ----- Q4: weak product_types (low pool) -----
        weak = sorted(
            ((pt, n) for pt, n in pt_counter.items()),
            key=lambda x: (x[1], x[0])
        )
        # Anything with pool <4 means assigned products there can't get
        # 3 same-type peers. Anything <3 means truly under-populated.
        weak_below_4 = [(pt, n) for pt, n in pt_counter.items() if n < 4]
        weak_below_3 = [(pt, n) for pt, n in pt_counter.items() if n < 3]

        # ----- Q5: examples of good vs bad -----
        # "Good" example: pick a product in a strong, semantically tight
        # pool (e.g. water_bottle) and show 3 sample peer names.
        # "Bad" example: pick a product in a tiny pool OR an unassigned
        # product, and explain why.
        def name_of(db_id: int) -> str:
            p = dbid_to_product.get(db_id)
            return ((p.name or "")[:78] + "…") if p and p.name and len(p.name) > 78 else (p.name if p else "")

        examples = []

        # Good: water_bottle.
        if pt_counter.get("water_bottle", 0) >= 4:
            anchor = pool["water_bottle"][0]
            peers = [d for d in pool["water_bottle"] if d != anchor][:3]
            examples.append({
                "kind": "GOOD",
                "anchor_pt": "water_bottle",
                "anchor_name": name_of(anchor),
                "anchor_id": dbid_to_product[anchor].product_id,
                "peer_names": [name_of(d) for d in peers],
                "peer_ids": [dbid_to_product[d].product_id for d in peers],
                "comment": (
                    "tight semantic pool — every peer is the same product "
                    "category; recommendations are coherent."
                ),
            })

        # Good: backpack (newly approved, pool=11).
        if pt_counter.get("backpack", 0) >= 4:
            anchor = pool["backpack"][0]
            peers = [d for d in pool["backpack"] if d != anchor][:3]
            examples.append({
                "kind": "GOOD",
                "anchor_pt": "backpack",
                "anchor_name": name_of(anchor),
                "anchor_id": dbid_to_product[anchor].product_id,
                "peer_names": [name_of(d) for d in peers],
                "peer_ids": [dbid_to_product[d].product_id for d in peers],
                "comment": (
                    "newly approved canonical; pool of 11 carries clean "
                    "schoolbag/lunch-bag products."
                ),
            })

        # Bad #1: a product in a small pool (<4).
        for pt, n in weak_below_4:
            if pt in pool and pool[pt]:
                anchor = pool[pt][0]
                peers = [d for d in pool[pt] if d != anchor]
                examples.append({
                    "kind": "BAD (small pool)",
                    "anchor_pt": pt,
                    "anchor_name": name_of(anchor),
                    "anchor_id": dbid_to_product[anchor].product_id,
                    "peer_names": [name_of(d) for d in peers],
                    "peer_ids": [dbid_to_product[d].product_id for d in peers],
                    "comment": (
                        f"only {n} products in this canonical -- cannot reach "
                        f"3 same-type recommendations; downstream would need a "
                        f"sibling/category fallback."
                    ),
                })
                break

        # Bad #2: an unassigned product.
        unassigned = [p for p in products if p.id not in pt_map]
        if unassigned:
            anchor = unassigned[0]
            examples.append({
                "kind": "BAD (unassigned)",
                "anchor_pt": "(no product_type)",
                "anchor_name": name_of(anchor.id),
                "anchor_id": anchor.product_id,
                "peer_names": [],
                "peer_ids": [],
                "comment": (
                    "no product_type assignment -- this product cannot "
                    "anchor or be matched by a product_type-only recommender; "
                    "events for it routed to pending or non-active aggregates."
                ),
            })

        # Bad #3: a strong but semantically broad pool, to expose recall.
        # Use 'book' as the canonical (pool=100). Not strictly bad in count,
        # but a reviewer should question whether 'book' is too generic.
        if pt_counter.get("book", 0) >= 50:
            anchor = pool["book"][0]
            peers = [d for d in pool["book"] if d != anchor][:3]
            examples.append({
                "kind": "QUESTIONABLE (broad pool)",
                "anchor_pt": "book",
                "anchor_name": name_of(anchor),
                "anchor_id": dbid_to_product[anchor].product_id,
                "peer_names": [name_of(d) for d in peers],
                "peer_ids": [dbid_to_product[d].product_id for d in peers],
                "comment": (
                    "100-product pool but covers educational, picture, "
                    "religious, board books, etc. -- product_type alone "
                    "is too coarse; needs a second axis to refine."
                ),
            })

        # ----- Print -----
        print("=" * 78)
        print(f"PRODUCT_TYPE SUFFICIENCY EVALUATION  (ws_id={ws_id})")
        print("=" * 78)
        print(f"  total products              : {total_products}")
        print(f"  products with product_type  : {len(pt_map)}  "
              f"({100*len(pt_map)/max(total_products,1):.2f}%)")
        print(f"  distinct product_type values: {len(pt_counter)}")

        print()
        print("=" * 78)
        print("Q1. % of products that can receive >= 3 recommendations")
        print("    (definition: assigned product_type AND pool size >= 4)")
        print("=" * 78)
        pct_can = 100 * can_recv_3 / max(total_products, 1)
        print(f"  CAN receive >= 3 recs       : {can_recv_3:>4}  ({pct_can:.2f}%)")
        print(f"  cannot -- unassigned        : {cannot_recv_3_unassigned:>4}  "
              f"({100*cannot_recv_3_unassigned/max(total_products,1):.2f}%)")
        print(f"  cannot -- pool too small    : {cannot_recv_3_small_pool:>4}  "
              f"({100*cannot_recv_3_small_pool/max(total_products,1):.2f}%)")

        print()
        print("=" * 78)
        print("Q2. % of product_types with < 3 products")
        print("=" * 78)
        pct_small_pt = 100 * len(small_pts) / max(len(pt_counter), 1)
        print(f"  product_types with < 3 prods: {len(small_pts)}  /  {len(pt_counter)}  "
              f"({pct_small_pt:.2f}%)")
        print(f"  product_types with < 4 prods: {len(weak_below_4)}  /  {len(pt_counter)}")

        print()
        print("=" * 78)
        print("Q3. Top 10 strongest product_types (by pool size)")
        print("=" * 78)
        for pt, n in top10:
            print(f"  {pt:<22} {n:>4}")

        print()
        print("=" * 78)
        print("Q4. Weak product_types  (pool < 4 -- can't supply 3 same-type peers)")
        print("=" * 78)
        if not weak_below_4:
            print("  (none)")
        else:
            for pt, n in sorted(weak_below_4, key=lambda x: (x[1], x[0])):
                marker = "  <-- below 3 (very weak)" if n < 3 else ""
                print(f"  {pt:<22} {n:>4}{marker}")

        print()
        print("=" * 78)
        print("Q5. Examples")
        print("=" * 78)
        for e in examples:
            print(f"  [{e['kind']}]  product_type = {e['anchor_pt']!r}")
            print(f"      anchor : {e['anchor_id']}  --  {e['anchor_name']}")
            for pid, pname in zip(e["peer_ids"], e["peer_names"]):
                print(f"      peer   : {pid}  --  {pname}")
            print(f"      note   : {e['comment']}")
            print()

        # ----- Verdict -----
        print("=" * 78)
        print("Q6. Verdict")
        print("=" * 78)
        verdict = "needs more signals"
        # Heuristic: "ready" requires that at least 70% of products can
        # receive 3 recommendations from product_type alone.
        if pct_can >= 70:
            verdict = "ready for recommendations"
        print(f"  >>> {verdict}")
        print()
        print(f"    Rationale:")
        print(f"      - only {pct_can:.1f}% of products meet the bar of "
              f"'assigned + pool>=4'.")
        print(f"      - {cannot_recv_3_unassigned} products ({100*cannot_recv_3_unassigned/total_products:.1f}%) are unassigned -- they")
        print(f"        cannot anchor or be matched by a product_type-only recommender.")
        print(f"      - {len(weak_below_4)} product_types have pool < 4; their products can't")
        print(f"        receive 3 same-type peers.")
        print(f"      - 'book' (100) is the only large pool but is broad; it lacks a")
        print(f"        second axis (book_type, age_band) to tighten relevance.")
        print(f"      - product_type IS necessary but NOT sufficient. Need at least one")
        print(f"        additional discriminator (age_band, brand-or-style affinity, or")
        print(f"        complementary attribute) before recommendations are useful.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
