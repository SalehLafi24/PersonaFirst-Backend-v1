"""CLI: engine-suggested default score_weight values.

Pure-function suggester lives in app/services/attribute_engine/weight_suggester.
This script handles the side effects: printing the table, optionally
rewriting the manifest with --apply, and appending a JSONL entry to
seed_data/weight_history.jsonl on every run.

Usage:
    python scripts/suggest_attribute_weights.py --workspace mumzworld_v3_sample
    python scripts/suggest_attribute_weights.py --workspace SLUG --apply --yes
    python scripts/suggest_attribute_weights.py --workspace SLUG --diff-only --threshold 0.05
    python scripts/suggest_attribute_weights.py --workspace SLUG --no-log
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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
from app.services.attribute_engine import (
    SuggesterConfig,
    WeightSuggestion,
    load_manifest,
    suggest_weights,
)
from app.services.attribute_engine.weight_suggester import SUGGESTER_VERSION


_HISTORY_PATH = ROOT / "seed_data" / "weight_history.jsonl"
_MANIFEST_PATH = ROOT / "seed_data" / "attribute_manifest.json"


# ---------------------------------------------------------------------------
# Table printer
# ---------------------------------------------------------------------------

def _print_table(
    workspace_slug: str, workspace_id: int,
    config: SuggesterConfig, suggestions: list[WeightSuggestion],
    threshold: float, diff_only: bool,
) -> None:
    print(f"Workspace: {workspace_slug} (id={workspace_id})")
    uf = config.usage_factors
    kf = config.kind_factors
    print("Config: defaults" if uf == SuggesterConfig().usage_factors and kf == SuggesterConfig().kind_factors else "Config: custom")
    print(f"  usage_factors  cohort_key={uf.get('cohort_key')}  "
          f"ranking_signal={uf.get('ranking_signal')}  filter={uf.get('filter')}")
    print(f"  kind_factors   closed={kf.get('closed')}  open={kf.get('open')}")
    print(f"  coverage_floor={config.coverage_floor}  coverage_ceiling={config.coverage_ceiling}")
    print()

    visible = suggestions
    if diff_only:
        visible = [s for s in suggestions if abs(s.delta) >= threshold]
    print(f"Persona-relevant attributes  ({len(suggestions)}):"
          + (f"  showing {len(visible)} with |delta| >= {threshold:.2%}" if diff_only else ""))
    print()
    if not visible:
        print("  (no rows to display)")
        return

    print(f"  {'attribute':<14} {'current':>8} {'suggested':>10} {'delta':>8}   reason / explanation")
    cur_sum = 0.0
    sug_sum = 0.0
    for s in visible:
        cur = s.current_weight if s.current_weight is not None else 0.0
        cur_sum += cur
        sug_sum += s.suggested_weight
        delta_str = f"{s.delta:+.3f}"
        rsn = s.weight_reason or "(none)"
        print(f"  {s.attribute_name:<14} {cur:>8.3f} {s.suggested_weight:>10.3f} {delta_str:>8}   "
              f"reason={rsn}")
        print(f"  {'':<14} {'':>8} {'':>10} {'':>8}   {s.explanation}")
        for w in s.warnings:
            print(f"  {'':<14} {'':>8} {'':>10} {'':>8}   warning: {w}")

    print()
    print(f"  current sum   : {cur_sum:.3f}")
    print(f"  suggested sum : {sug_sum:.3f}  (validator-compliant by construction)")
    print()

    drift = [s for s in suggestions if abs(s.delta) >= threshold]
    if drift:
        worst_over = min(drift, key=lambda s: s.delta)
        worst_under = max(drift, key=lambda s: s.delta)
        print(f"Drift summary:")
        print(f"  {len(drift)} attribute(s) drift more than {threshold:.0%} from suggested weights")
        print(f"  most over-weighted   : {worst_over.attribute_name}  ({worst_over.delta:+.3f})")
        print(f"  most under-weighted  : {worst_under.attribute_name}  ({worst_under.delta:+.3f})")
    else:
        print(f"Drift summary: all attributes within {threshold:.0%} of suggested weights.")


# ---------------------------------------------------------------------------
# Manifest writer (--apply)
# ---------------------------------------------------------------------------

def _apply_to_manifest(suggestions: list[WeightSuggestion]) -> None:
    """Rewrite seed_data/attribute_manifest.json with suggested weights.

    Sets score_weight to the suggested value and _weight_reason to
    'suggester_v1' on every persona-relevant attribute. Preserves
    everything else in the JSON. Validates by re-loading the manifest.
    """
    raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    attrs = raw.get("attributes") or {}
    for s in suggestions:
        entry = attrs.get(s.attribute_name)
        if entry is None:
            continue
        rec = entry.setdefault("recommendation", {})
        rec["score_weight"] = round(s.suggested_weight, 6)
        rec["_weight_reason"] = SUGGESTER_VERSION
    _MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    # Validate by reloading.
    load_manifest()


# ---------------------------------------------------------------------------
# History log
# ---------------------------------------------------------------------------

def _append_history_log(
    *, workspace_id: int, workspace_slug: str, action: str,
    config: SuggesterConfig, suggestions: list[WeightSuggestion],
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace_id": workspace_id,
        "workspace_slug": workspace_slug,
        "action": action,
        "suggester_version": SUGGESTER_VERSION,
        "config": config.to_json(),
        "attributes": [
            {
                "name": s.attribute_name,
                "current_weight": (
                    round(s.current_weight, 6) if s.current_weight is not None else None
                ),
                "suggested_weight": round(s.suggested_weight, 6),
                "delta": round(s.delta, 6),
                "weight_reason": s.weight_reason,
                "raw_score": round(s.raw_score, 6),
                "warnings": list(s.warnings),
            }
            for s in suggestions
        ],
    }
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute and optionally apply engine-suggested weights.",
    )
    parser.add_argument("--workspace", required=True,
                        help="workspace slug (e.g. mumzworld_v3_sample)")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite the manifest with suggested weights")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt for --apply")
    parser.add_argument("--diff-only", action="store_true",
                        help="display only attributes drifting beyond --threshold")
    parser.add_argument("--threshold", type=float, default=0.05,
                        help="diff-only threshold (default 0.05 = 5pp)")
    parser.add_argument("--no-log", action="store_true",
                        help="skip writing to seed_data/weight_history.jsonl")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        config = SuggesterConfig()
        suggestions = suggest_weights(
            db, workspace_id=ws.id, config=config,
        )
    finally:
        db.close()

    if not suggestions:
        print(f"No persona-relevant attributes in manifest. Nothing to suggest.")
        if not args.no_log:
            _append_history_log(
                workspace_id=ws.id, workspace_slug=args.workspace,
                action="advisory", config=config, suggestions=[],
            )
        return

    _print_table(args.workspace, ws.id, config, suggestions,
                 threshold=args.threshold, diff_only=args.diff_only)

    if args.apply:
        if not args.yes:
            print()
            answer = input("Apply suggested weights to the manifest? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                if not args.no_log:
                    _append_history_log(
                        workspace_id=ws.id, workspace_slug=args.workspace,
                        action="advisory", config=config, suggestions=suggestions,
                    )
                return
        _apply_to_manifest(suggestions)
        print()
        print(f"Wrote {len(suggestions)} weight(s) to {_MANIFEST_PATH.relative_to(ROOT)}.")
        print(f"_weight_reason set to {SUGGESTER_VERSION!r} on each.")
        if not args.no_log:
            _append_history_log(
                workspace_id=ws.id, workspace_slug=args.workspace,
                action="applied", config=config, suggestions=suggestions,
            )
            print(f"History entry appended to {_HISTORY_PATH.relative_to(ROOT)}.")
    else:
        print()
        print("Recommendation: review and decide.")
        print("  - to apply suggestions: rerun with --apply")
        print("  - to keep current weights: document the rationale in `_weight_reason`")
        if not args.no_log:
            _append_history_log(
                workspace_id=ws.id, workspace_slug=args.workspace,
                action="advisory", config=config, suggestions=suggestions,
            )
            print(f"  - history entry appended to {_HISTORY_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
