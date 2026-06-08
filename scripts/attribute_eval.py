"""CLI: evaluate attribute extraction quality vs the gold sample.

Usage:
    python scripts/attribute_eval.py --workspace mumzworld_v3_sample
    python scripts/attribute_eval.py --workspace mumzworld_v3_sample --pilot-only
    python scripts/attribute_eval.py --workspace mumzworld_v3_sample \\
        --attributes age_group gender

Output:
    - Per-attribute counts + agreement / disagreement / precision / recall
    - Top mismatch pairs (gold=X, sys=Y) so the operator can name dominant
      error patterns
    - Aggregate disagreement rate across all evaluated attributes

This is a pure read; nothing in the DB is modified.
"""
from __future__ import annotations

import argparse
import json
import sys
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
from app.services.eval.attribute_eval import (
    AttributeEvalReport,
    EvalReport,
    evaluate_attributes,
)


_DEFAULT_GOLD = ROOT / "seed_data" / "eval" / "attribute_gold_sample.json"
_DEFAULT_ATTRS = ["product_type", "age_group", "gender", "use_case"]


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:>5.1f}%" if x is not None else "  n/a"


def _fmt_v(x: str | None) -> str:
    return repr(x) if x is None else x


def _print_attribute(rep: AttributeEvalReport) -> None:
    c = rep.counts
    extr_layer = "extraction layer" if rep.has_extraction_modes else "csv-direct only (no extraction layer)"
    print(f"  {rep.attribute}  [{extr_layer}]")
    print(f"    counts        TP={c.tp:<3}  TN={c.tn:<3}  "
          f"FP={c.fp:<3}  FN={c.fn:<3}  Mismatch={c.mismatch:<3}  total={c.total}")
    print(f"    agreement     {_fmt_pct(c.agreement_rate)}    "
          f"disagreement {_fmt_pct(c.disagreement_rate)}")
    print(f"    precision     {_fmt_pct(c.precision)}    "
          f"recall       {_fmt_pct(c.recall)}")
    # Disagreement-kind breakdown: shows where the mismatches actually
    # come from. extraction_error is engineering-actionable; other kinds
    # need a different routing.
    if c.disagreement_rate and c.disagreement_rate > 0:
        print(f"    by kind       extraction_error={c.extraction_error:<3}  "
              f"policy_divergence={c.policy_divergence:<3}  "
              f"taxonomy_gap={c.taxonomy_gap:<3}")
        print(f"    extraction_disagreement_rate : "
              f"{_fmt_pct(c.extraction_disagreement_rate)}  "
              f"(engineering-actionable subset)")
    if rep.top_mismatches:
        print(f"    top mismatches (kind, gold -> system, count, sample pids):")
        for m in rep.top_mismatches:
            sample = ", ".join(m.sample_product_ids[:3])
            extra = "" if m.count <= 3 else f" (+{m.count - 3} more)"
            print(f"      [{m.count:>3}]  {m.kind:<18}  "
                  f"gold={_fmt_v(m.gold):<20}  "
                  f"sys={_fmt_v(m.system):<20}  e.g.: {sample}{extra}")


def _print_report(rep: EvalReport) -> None:
    print(f"workspace_id        : {rep.workspace_id}")
    print(f"gold sample size    : {rep.gold_total}")
    print(f"labeled (evaluated) : {rep.labeled_total}"
          f"{' (pilot only)' if rep.pilot_only else ''}")
    print(f"attributes          : {rep.attributes_evaluated}")
    print(f"aggregate disagreement_rate            : "
          f"{_fmt_pct(rep.aggregate_disagreement_rate)}")
    print(f"aggregate extraction_disagreement_rate : "
          f"{_fmt_pct(rep.aggregate_extraction_disagreement_rate)}  "
          f"(engineering-actionable; gating metric)")
    print()
    print("disagreement kinds:")
    print("  extraction_error   = a change to extraction-layer code")
    print("                       (regex / contextual_defaults / LLM) could fix it.")
    print("  policy_divergence  = system value came from csv_direct (or attribute")
    print("                       has no extraction layer); dispatcher precedence")
    print("                       prevents extraction modes from overriding. Routing")
    print("                       is upstream-data / editorial, not engineering.")
    print("  taxonomy_gap       = gold uses a value not in the workspace's active")
    print("                       AAVs. Routing is taxonomy_admin.")
    print()
    print("per-attribute:")
    for attr in rep.attributes_evaluated:
        if attr in rep.per_attribute:
            _print_attribute(rep.per_attribute[attr])
            print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate attribute extraction vs gold sample. Read-only.",
    )
    parser.add_argument("--workspace", required=True,
                        help="workspace slug (e.g. mumzworld_v3_sample)")
    parser.add_argument("--gold", type=Path, default=_DEFAULT_GOLD,
                        help=f"path to gold sample JSON (default: {_DEFAULT_GOLD})")
    parser.add_argument("--pilot-only", action="store_true",
                        help="evaluate only is_pilot=true products")
    parser.add_argument("--attributes", nargs="+", default=_DEFAULT_ATTRS,
                        help=f"attributes to evaluate (default: {_DEFAULT_ATTRS})")
    parser.add_argument("--top-n-mismatches", type=int, default=5,
                        help="how many top mismatch pairs to print per attribute")
    args = parser.parse_args()

    if not args.gold.exists():
        raise SystemExit(f"gold sample not found: {args.gold}")
    gold_data = json.loads(args.gold.read_text(encoding="utf-8"))
    gold_products = gold_data.get("products") or []

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")
        report = evaluate_attributes(
            db,
            workspace_id=ws.id,
            gold_products=gold_products,
            attributes=args.attributes,
            pilot_only=args.pilot_only,
            top_n_mismatches=args.top_n_mismatches,
        )
        _print_report(report)
    finally:
        db.close()


if __name__ == "__main__":
    main()
