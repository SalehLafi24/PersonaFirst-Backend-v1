"""Annotate the gold sample with a 12-product pilot subset (Phase 8 pilot).

The pilot's role is to STRESS TEST the labeling guide before the full
~120-product pass commits to it. Six categories, 2 products each, picked
deterministically (hash-ordered by product_id + seed) so the run is
reproducible.

Categories:
    clear_infant_care    -- unambiguous baby items; the calibration baseline
    bundle               -- multi-component names; tests edge_cases rules
    apparel_no_context   -- shirts/bodysuits/socks; tests use_case=null discipline
    toy_play_vs_learning -- puzzles/learning toys; tests value discrimination
    untyped_oov          -- no product_type; tests open-vocab OOV behaviour
    niche_low_volume     -- mug/bouncer/snack-style rarer types

Idempotent: re-running on a file that already has pilot picks resets and
re-picks them deterministically (same selections under the same seed,
unless the underlying sample changed).

Also migrates older sample files to the v2 schema by ensuring every
entry has `is_pilot`, `pilot_stress_category`, `label_time_seconds`,
and `friction_notes` fields. Existing labels (if any) are preserved.

Usage:
    python scripts/pick_pilot_products.py
    python scripts/pick_pilot_products.py --sample-path PATH
    python scripts/pick_pilot_products.py --seed 42 --per-category 2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DEFAULT_PATH = ROOT / "seed_data" / "eval" / "attribute_gold_sample.json"


# ---------------------------------------------------------------------------
# Category detectors
# ---------------------------------------------------------------------------

# `+` is required to be a real separator (whitespace either side) so we
# don't false-match codes like "Pa+++" or sizes like "6m+".
_BUNDLE_RE = re.compile(
    r"(\s\+\s|\bset\b|\bkit\b|\bwith\s+\w|\bbundle\b|\bcombo\b|\bgift\s+set\b)",
    re.IGNORECASE,
)

_CLEAR_INFANT_TYPES = frozenset({
    "pacifier", "teether", "bib", "changing_mat", "diaper", "wipes",
})

_APPAREL_NO_CONTEXT_TYPES = frozenset({
    "shirt", "bodysuit", "socks", "costume",
})

_TOY_PLAY_VS_LEARNING_TYPES = frozenset({
    "puzzle", "learning_toy", "sensory_toy", "musical_toy",
    "construction_toy",
})

_NICHE_LOW_VOLUME_TYPES = frozenset({
    "mug", "bouncer", "walker_toy", "action_figure", "supplement",
    "feminine_care", "snack", "bath_toy", "tea", "coffee", "blanket",
})


def _vals(entry: dict) -> dict:
    return entry.get("current_system_values") or {}


def _is_clear_infant_care(entry: dict) -> bool:
    """Calibration baseline: clearly-infant care product with the
    canonical attributes set. We deliberately do NOT require
    gender=unisex -- many infant products are labelled with a specific
    gender for legitimate reasons (Disney brand, Princess theme).
    Requirement: infant + a clear-infant product_type + use_case set.
    """
    v = _vals(entry)
    if v.get("product_type") not in _CLEAR_INFANT_TYPES:
        return False
    if v.get("age_group") != "infant":
        return False
    if not v.get("use_case"):
        return False
    return True


def _is_bundle(entry: dict) -> bool:
    name = entry.get("name") or ""
    if not _BUNDLE_RE.search(name):
        return False
    # Skip products with no product_type — those go to the untyped bucket
    # for clearer category attribution.
    if not _vals(entry).get("product_type"):
        return False
    return True


def _is_apparel_no_context(entry: dict) -> bool:
    v = _vals(entry)
    if v.get("product_type") not in _APPAREL_NO_CONTEXT_TYPES:
        return False
    return True


def _is_toy_play_vs_learning(entry: dict) -> bool:
    return _vals(entry).get("product_type") in _TOY_PLAY_VS_LEARNING_TYPES


def _is_untyped_oov(entry: dict) -> bool:
    return _vals(entry).get("product_type") is None


def _is_niche_low_volume(entry: dict) -> bool:
    return _vals(entry).get("product_type") in _NICHE_LOW_VOLUME_TYPES


_CATEGORIES: "OrderedDict[str, Callable[[dict], bool]]" = OrderedDict([
    ("clear_infant_care",      _is_clear_infant_care),
    ("bundle",                 _is_bundle),
    ("apparel_no_context",     _is_apparel_no_context),
    ("toy_play_vs_learning",   _is_toy_play_vs_learning),
    ("untyped_oov",            _is_untyped_oov),
    ("niche_low_volume",       _is_niche_low_volume),
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_order_key(product_id: str, seed: int) -> str:
    return hashlib.sha256(f"{product_id}|{seed}".encode("utf-8")).hexdigest()


def _ensure_v2_fields(entry: dict) -> None:
    """Migrate older sample entries in place to the v2 schema."""
    entry.setdefault("is_pilot", False)
    entry.setdefault("pilot_stress_category", None)
    entry.setdefault("label_time_seconds", None)
    entry.setdefault("friction_notes", [])


def _reset_pilot_flags(products: list[dict]) -> None:
    """Clear pilot annotations so the picker is idempotent."""
    for p in products:
        p["is_pilot"] = False
        p["pilot_stress_category"] = None


def _short(name: str, n: int = 60) -> str:
    return (name or "")[:n]


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

def pick_pilot(
    products: list[dict],
    *,
    seed: int = 42,
    per_category: int = 2,
) -> list[dict]:
    """Apply each category filter; pick `per_category` from each by hash
    ordering; mark them in place. Returns the picked entries (ordered by
    category, then hash). Each product is assigned to AT MOST ONE
    category."""
    used: set[str] = set()
    picked_in_order: list[dict] = []

    for category, predicate in _CATEGORIES.items():
        candidates = [p for p in products
                      if p["product_id"] not in used and predicate(p)]
        candidates.sort(key=lambda p: _hash_order_key(p["product_id"], seed))
        chosen = candidates[:per_category]
        for c in chosen:
            c["is_pilot"] = True
            c["pilot_stress_category"] = category
            used.add(c["product_id"])
            picked_in_order.append(c)
    return picked_in_order


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Annotate the gold sample with a 12-product pilot subset.",
    )
    parser.add_argument("--sample-path", default=str(_DEFAULT_PATH))
    parser.add_argument("--seed", type=int, default=42,
                        help="hash-ordering seed (default 42)")
    parser.add_argument("--per-category", type=int, default=2,
                        help="products per stress-test category (default 2)")
    args = parser.parse_args()

    path = Path(args.sample_path).resolve()
    if not path.exists():
        raise SystemExit(
            f"sample not found: {path}\n"
            f"Run scripts/sample_attribute_gold.py first."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    products = payload.get("products") or []
    if not products:
        raise SystemExit(f"no products in sample: {path}")

    # Migrate v1 -> v2 schema in place.
    for p in products:
        _ensure_v2_fields(p)

    _reset_pilot_flags(products)
    picked = pick_pilot(
        products, seed=args.seed, per_category=args.per_category,
    )

    # Record the pilot strategy in the top-level metadata for audit.
    payload["pilot_strategy"] = {
        "seed": args.seed,
        "per_category": args.per_category,
        "categories": list(_CATEGORIES.keys()),
        "total_pilot_products": len(picked),
    }

    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # ---- print summary ----
    print(f"Sample file: {path.relative_to(ROOT)}")
    print(f"Total products: {len(products)}")
    print(f"Pilot products picked: {len(picked)}")
    print(f"Seed: {args.seed}  per_category: {args.per_category}")
    print()
    print(f"{'category':<22}  {'product_id':<32}  current_system_values  /  name")
    print("─" * 100)
    for c in picked:
        cat = c["pilot_stress_category"] or "?"
        v = _vals(c)
        v_compact = (
            f"pt={v.get('product_type') or '-'},"
            f"age={v.get('age_group') or '-'},"
            f"gen={v.get('gender') or '-'},"
            f"uc={v.get('use_case') or '-'}"
        )
        print(f"  {cat:<20}  {c['product_id']:<32}  {v_compact}")
        print(f"  {'':<20}  {'':<32}  {_short(c.get('name'), 80)!r}")
        print()

    # Per-category coverage check
    by_cat: dict[str, int] = {}
    for c in picked:
        by_cat[c["pilot_stress_category"]] = by_cat.get(c["pilot_stress_category"], 0) + 1
    missing = [k for k in _CATEGORIES if by_cat.get(k, 0) < args.per_category]
    if missing:
        print("WARNING: under-filled categories (catalog had too few candidates):")
        for k in missing:
            print(f"  {k}: filled {by_cat.get(k, 0)} / {args.per_category} requested")
    else:
        print("All 6 categories filled to target.")

    print()
    print("Next: a labeller opens the sample file, finds entries with")
    print("`is_pilot: true`, fills in `labels`, `label_time_seconds`, and")
    print("`friction_notes` for each. Review notes; refine the guide;")
    print("then proceed with the remaining ~108 non-pilot products.")


if __name__ == "__main__":
    main()
