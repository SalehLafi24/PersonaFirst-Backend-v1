"""Generate the Phase 8 attribute-gold sample.

Pure four-layer sampling lives in app/services/eval/sampling.py; this
script is the I/O boundary: it loads the workspace, calls the sampler,
prints a summary, and writes the JSON sample file.

Usage:
    python scripts/sample_attribute_gold.py --workspace mumzworld_v3_sample
    python scripts/sample_attribute_gold.py --workspace SLUG --output PATH
    python scripts/sample_attribute_gold.py --workspace SLUG --per-type 3 --hard-cases 16

Output: seed_data/eval/attribute_gold_sample.json (see schema in scope doc).
The labeler subsequently fills in the `labels` field for each product;
the result is the gold set itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.services.eval.sampling import (
    DEFAULT_LABELED_ATTRIBUTES,
    SamplingConfig,
    SamplingResult,
    sample_attribute_gold,
)


_DEFAULT_OUTPUT = ROOT / "seed_data" / "eval" / "attribute_gold_sample.json"


def _build_payload(
    workspace_slug: str, workspace_id: int, result: SamplingResult,
) -> dict:
    return {
        "version": "1.0",
        "sampled_at": date.today().isoformat(),
        "workspace_slug": workspace_slug,
        "workspace_id": workspace_id,
        "labeled_attributes": list(result.labeled_attributes),
        "sampling_strategy": {
            "layers": [
                "1_stratified_by_product_type",
                "2_rare_value_topup",
                "3_hard_cases",
                "3_5_untyped",
            ],
            "products_per_type": result.config.products_per_type,
            "rare_value_floor": result.config.rare_value_floor,
            "hard_cases_count": result.config.hard_cases_count,
            "untyped_products_count": result.config.untyped_products_count,
            "seed": result.config.seed,
            "ordering": "hash(product_id, seed)",
            "layer_counts": result.layer_counts,
            "total_selected": len(result.products),
        },
        "value_distribution_in_sample": result.value_distribution,
        "warnings": list(result.warnings),
        "products": [
            {
                "product_id": p.product_id,
                "name": p.name,
                "selection_reason": p.selection_reason,
                "current_system_values": p.current_system_values,
                "current_confidences": {
                    a: round(c, 4) for a, c in p.current_confidences.items()
                },
                # Pilot annotations (set by scripts/pick_pilot_products.py;
                # default false for the rest of the sample). The pilot
                # produces ~12 stress-test labels first to validate the
                # guide before the full labeling pass.
                "is_pilot": False,
                "pilot_stress_category": None,
                # Labeling fields (filled in by the human labeler).
                "labels": None,
                "labeled_by": None,
                "labeled_at": None,
                # Timing + friction (Phase 8 / pilot tracking).
                # label_time_seconds: how long the labeler spent on this
                # product. friction_notes: free-form bullet points the
                # labeler writes when they hit ambiguity. Both populated
                # during labeling; aggregate stats inform guide
                # iteration.
                "label_time_seconds": None,
                "friction_notes": [],
                # Blind double-label fields (10% subset).
                "second_label": None,
                "second_labeled_by": None,
                "second_labeled_at": None,
                "notes": None,
            }
            for p in result.products
        ],
    }


def _print_summary(result: SamplingResult) -> None:
    print(f"  layer breakdown:")
    for layer, n in result.layer_counts.items():
        print(f"    {layer:<28} {n}")
    print(f"  total_selected               {len(result.products)}")
    print()
    print(f"  value distribution in sample:")
    for attr, dist in result.value_distribution.items():
        print(f"    {attr}:")
        for v, n in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"      {v:<20} {n}")
    if result.warnings:
        print()
        print(f"  warnings:")
        for w in result.warnings:
            print(f"    - {w}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sample products for the Phase 8 attribute gold set.",
    )
    parser.add_argument("--workspace", required=True,
                        help="workspace slug (e.g. mumzworld_v3_sample)")
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT),
                        help="output JSON path "
                             "(default: seed_data/eval/attribute_gold_sample.json)")
    parser.add_argument("--per-type", type=int, default=2)
    parser.add_argument("--rare-floor", type=int, default=3)
    parser.add_argument("--hard-cases", type=int, default=12)
    parser.add_argument("--untyped", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing output file")
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    if output_path.exists() and not args.force:
        raise SystemExit(
            f"output already exists: {output_path}\n"
            f"Pass --force to overwrite. (Refusing to clobber labeled gold sets.)"
        )

    config = SamplingConfig(
        products_per_type=args.per_type,
        rare_value_floor=args.rare_floor,
        hard_cases_count=args.hard_cases,
        untyped_products_count=args.untyped,
        seed=args.seed,
    )

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")
        result = sample_attribute_gold(
            db, workspace_id=ws.id,
            labeled_attributes=DEFAULT_LABELED_ATTRIBUTES,
            config=config,
        )
    finally:
        db.close()

    payload = _build_payload(args.workspace, ws.id, result)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Workspace: {args.workspace} (id={ws.id})")
    print(f"Config: per_type={config.products_per_type}  "
          f"rare_floor={config.rare_value_floor}  "
          f"hard_cases={config.hard_cases_count}  "
          f"untyped={config.untyped_products_count}  "
          f"seed={config.seed}")
    print()
    _print_summary(result)
    print()
    print(f"Wrote {len(result.products)} products to "
          f"{output_path.relative_to(ROOT)}")
    print()
    print("Next step: a human labeller fills in the `labels` field for "
          "each product per the labeling guide. The result is the gold set.")


if __name__ == "__main__":
    main()
