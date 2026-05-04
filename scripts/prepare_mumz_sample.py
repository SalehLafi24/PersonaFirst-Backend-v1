"""Prepare a small, stratified sample (~800 products) from mumz_products.csv.

Pipeline:
  1. Stream the CSV.
  2. Group by group_id; per group pick the best candidate (in_stock + has
     price + has image + most complete data).
  3. Bucket by category_1; deterministic stride-sample within each bucket
     to hit per-category targets.
  4. Add a small batch of "edge case" rows (out-of-stock / missing price /
     missing image / blank category) so the sample isn't only happy-path.
  5. Transform each sampled row into the structured JSON format described
     in the task brief.

No randomness. Deterministic ordering throughout. No model calls. No
inferred attributes.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "seed_data" / "mumz_products.csv"
DST = ROOT / "seed_data" / "mumz_sample.json"

# Stratification targets (sum ~= 800 across non-edge buckets).
PER_CATEGORY_TARGETS: dict[str, int] = {
    "Toys": 130,
    "Mumz": 100,
    "Books": 100,
    "School": 85,
    "Home": 85,
    "Party": 75,
    "Kids Fashion": 75,
    "Feeding": 65,
    "Outdoor": 50,
    "Bath": 50,
    "Gear": 50,
    "Bedroom": 50,
    "Diapers": 40,
    "Pantry": 30,
    "Safety": 25,
}
EDGE_CASE_TARGET: int = 20  # extra rows pulled from unusual / low-quality buckets


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_id(s: str) -> str:
    """sku / group_id come from the CSV in scientific notation
    (e.g. '4.10304E+12') because the source spreadsheet rendered large ints
    as floats. Round-trip through float→int recovers the original digits."""
    s = (s or "").strip().strip('"').strip("'").strip()
    if not s:
        return s
    try:
        return str(int(float(s)))
    except ValueError:
        return s


def _normalize_product_type(s: str) -> str:
    s = (s or "").strip().lower()
    if not s:
        return ""
    for prefix in ("baby ", "kids ", "kid ", "boys ", "girls ",
                   "men ", "men's ", "women ", "women's "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.replace(" & ", "_").replace("&", "_").replace(" ", "_").replace("/", "_")
    s = re.sub(r"[,;]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    parts = s.split("_")
    parts = [
        p[:-1] if (len(p) > 4 and p.endswith("s") and not p.endswith("ss")) else p
        for p in parts
    ]
    return "_".join(parts)


def _parse_age_group(age_str: str) -> list[str]:
    s = (age_str or "").lower()
    out: list[str] = []
    if "newborn" in s or "0-3 m" in s or "0-3m" in s:
        out.append("newborn")
    if any(t in s for t in ("0-2", "0-12", "infant", "baby")):
        out.append("infant")
    if any(t in s for t in ("2-4", "2-3", "1-3", "toddler")):
        out.append("toddler")
    if any(t in s for t in ("4-6", "5-7", "6+", "6-8", "7+",
                            "8+", "kid", "school")):
        out.append("kids")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped = [v for v in out if not (v in seen or seen.add(v))]
    return deduped or ["universal"]


def _normalize_gender(g: str) -> str:
    g = (g or "").strip().lower()
    if g in ("boy", "boys", "male", "men", "man"):
        return "male"
    if g in ("girl", "girls", "female", "women", "woman"):
        return "female"
    return "unisex"


def _split_keywords(s: str) -> list[str]:
    if not s:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in s.split("|"):
        tok = tok.strip().lower()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _category_path(row: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for col in ("category_1", "category_2", "category_3", "category_4"):
        v = (row.get(col) or "").strip().lower()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _candidate_score(row: dict) -> int:
    """Higher = better candidate when picking among rows that share a group_id."""
    score = 0
    if (row.get("in_stock") or "").strip().upper() == "TRUE":
        score += 10
    try:
        if float(row.get("price") or 0) > 0:
            score += 5
    except (TypeError, ValueError):
        pass
    if (row.get("image_url") or "").strip():
        score += 3
    if (row.get("name") or "").strip():
        score += 2
    score += sum(1 for v in row.values() if v and str(v).strip())
    return score


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _dedupe_by_group_id(path: Path) -> dict[str, dict]:
    """Stream the CSV; keep the best candidate row per group_id.
    Rows without a group_id are kept under their sku as a synthetic key."""
    best_by_group: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            group_id = _normalize_id(row.get("group_id") or "")
            sku = _normalize_id(row.get("sku") or "")
            key = group_id or f"sku::{sku}"
            if not key:
                continue
            score = _candidate_score(row)
            existing = best_by_group.get(key)
            if existing is None or score > existing[1] or (
                score == existing[1] and sku < _normalize_id(existing[0].get("sku") or "")
            ):
                best_by_group[key] = (row, score)
    return {k: v[0] for k, v in best_by_group.items()}


def _bucket_by_category(deduped: dict[str, dict]) -> dict[str, list[tuple[str, dict]]]:
    """Group deduped rows by category_1. Sort within each bucket by sku for
    deterministic ordering."""
    buckets: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for key, row in deduped.items():
        cat1 = (row.get("category_1") or "").strip()
        buckets[cat1].append((key, row))
    for k in buckets:
        buckets[k].sort(key=lambda kv: _normalize_id(kv[1].get("sku") or "") or kv[0])
    return buckets


def _stride_sample(items: list, n: int) -> list:
    """Take n evenly-spaced items from a sorted list. No randomness."""
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _select_edge_cases(deduped: dict[str, dict], n: int) -> list[tuple[str, dict]]:
    """Pull a few rows that look low-quality on purpose: out_of_stock,
    missing price, missing image, or empty category_1. Deterministic."""
    pools: dict[str, list[tuple[str, dict]]] = {
        "out_of_stock": [],
        "no_price": [],
        "no_image": [],
        "no_category": [],
    }
    for key, row in deduped.items():
        if (row.get("in_stock") or "").strip().upper() != "TRUE":
            pools["out_of_stock"].append((key, row))
        try:
            if float(row.get("price") or 0) <= 0:
                pools["no_price"].append((key, row))
        except (TypeError, ValueError):
            pools["no_price"].append((key, row))
        if not (row.get("image_url") or "").strip():
            pools["no_image"].append((key, row))
        if not (row.get("category_1") or "").strip():
            pools["no_category"].append((key, row))
    for k in pools:
        pools[k].sort(key=lambda kv: _normalize_id(kv[1].get("sku") or "") or kv[0])
    out: list[tuple[str, dict]] = []
    seen_keys: set[str] = set()
    per_pool = max(1, n // len(pools))
    for pool_name, items in pools.items():
        picked = _stride_sample(items, per_pool)
        for key, row in picked:
            if key not in seen_keys:
                seen_keys.add(key)
                out.append((key, row))
    return out[:n]


def _transform(row: dict) -> dict:
    return {
        "product_id": _normalize_id(row.get("sku") or ""),
        "group_id": _normalize_id(row.get("group_id") or ""),
        "name": (row.get("name") or "").strip(),
        "brand": (row.get("brand") or "").strip(),
        "attributes": {
            "product_type": _normalize_product_type(
                (row.get("category_4") or "").strip()
                or (row.get("category_3") or "").strip()
                or (row.get("category_2") or "").strip()
            ),
            "category_path": _category_path(row),
            "age_group": _parse_age_group(row.get("age") or ""),
            "gender": _normalize_gender(row.get("gender") or ""),
            "color": (row.get("color") or "").strip().lower(),
            "keywords": _split_keywords(row.get("keywords") or ""),
        },
    }


def main() -> None:
    if not SRC.exists():
        print(f"Source not found: {SRC}", file=sys.stderr)
        sys.exit(1)

    print(f"Streaming and deduplicating {SRC} ...")
    deduped = _dedupe_by_group_id(SRC)
    print(f"  unique group_ids: {len(deduped)}")

    print("Bucketing by category_1 ...")
    buckets = _bucket_by_category(deduped)

    selected: list[tuple[str, dict]] = []
    selected_keys: set[str] = set()

    print(f"Stride-sampling per-category targets:")
    for cat, target in PER_CATEGORY_TARGETS.items():
        items = buckets.get(cat, [])
        picked = _stride_sample(items, target)
        added = 0
        for key, row in picked:
            if key not in selected_keys:
                selected_keys.add(key)
                selected.append((key, row))
                added += 1
        print(f"  {cat:<14} target={target:>3} available={len(items):>6} added={added}")

    edge = _select_edge_cases(deduped, EDGE_CASE_TARGET)
    edge_added = 0
    for key, row in edge:
        if key not in selected_keys:
            selected_keys.add(key)
            selected.append((key, row))
            edge_added += 1
    print(f"  edge_cases     target={EDGE_CASE_TARGET:>3} added={edge_added}")

    transformed = [_transform(row) for _, row in selected]

    # Coverage stats
    n = len(transformed)
    n_pt = sum(1 for p in transformed if p["attributes"]["product_type"])
    n_age_specific = sum(
        1 for p in transformed
        if p["attributes"]["age_group"] != ["universal"]
    )

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total products selected   : {n}")
    print(f"Deduplication confirmed   : "
          f"{n} entries each from a distinct group_id key (or sku-fallback)")
    print(f"product_type coverage     : {n_pt}/{n}  "
          f"({100.0 * n_pt / n:.1f}%)")
    print(f"age_group specific (non-universal) : "
          f"{n_age_specific}/{n}  ({100.0 * n_age_specific / n:.1f}%)")

    print()
    print("=" * 70)
    print("SAMPLE OF 10 TRANSFORMED PRODUCTS")
    print("=" * 70)
    print(json.dumps(transformed[:10], indent=2, ensure_ascii=False))

    DST.write_text(json.dumps(transformed, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"Wrote {n} products to {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
