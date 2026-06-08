"""Explicit operator repair tool: sync manifest allowed_values into AAV.

Usage (dry-run, no writes):
    python scripts/sync_manifest_aav.py --workspace mumzworld_v3_sample

Usage (apply, writes inactive AAV rows):
    python scripts/sync_manifest_aav.py --workspace mumzworld_v3_sample --apply

What it does:
  - For each closed-taxonomy attribute in the manifest, diff
    manifest.allowed_values against the workspace's AAV rows.
  - Print: missing / already-active / already-inactive per attribute.
  - On --apply: insert missing rows as is_active=False.
  - Open taxonomies are skipped (governed by clustering, not manifest).

What it does NOT do:
  - Auto-activate any AAV row (activation is a per-workspace governance
    decision).
  - Update or deactivate existing rows.
  - Run on manifest load or as part of the pipeline. This is operator-
    invoked only.
"""
from __future__ import annotations

import argparse
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
from app.services.attribute_engine import load_manifest
from app.services.attribute_engine.aav_sync import (
    SyncPlan,
    apply_sync_plan,
    compute_sync_plan,
)


def _print_plan(plan: SyncPlan, *, apply_mode: bool) -> None:
    header = "apply" if apply_mode else "dry-run"
    print(f"workspace_id : {plan.workspace_id}")
    print(f"mode         : {header}")
    print()

    for ap in plan.attributes:
        if ap.kind == "open":
            print(f"attribute: {ap.attribute_name}  (open) -- skipped")
            print(f"  reason: {ap.skipped_reason}")
            print()
            continue

        print(f"attribute: {ap.attribute_name}  ({ap.kind})")
        print(f"  manifest values  : {len(ap.manifest_values)}  "
              f"({', '.join(ap.manifest_values) or '(none)'})")
        print(f"  already active   : {len(ap.already_active)}  "
              f"({', '.join(ap.already_active) or '(none)'})")
        print(f"  already inactive : {len(ap.already_inactive)}  "
              f"({', '.join(ap.already_inactive) or '(none)'})")
        verb = "inserted (inactive)" if apply_mode else "would insert (inactive)"
        print(f"  missing in AAV   : {len(ap.missing_in_aav)}  -> {verb}")
        for v in ap.missing_in_aav:
            print(f"    + {v}")
        print()

    print("== summary ==")
    print(f"closed attributes : {len(plan.closed_attributes)}")
    print(f"open attributes   : {len(plan.open_attributes)} (skipped)")
    print(f"missing total     : {plan.total_missing}")
    if not apply_mode and plan.total_missing > 0:
        print(f"(use --apply to insert {plan.total_missing} inactive AAV row(s))")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync manifest.allowed_values into AttributeAllowedValue "
                    "(closed taxonomies only). Dry-run by default.",
    )
    parser.add_argument("--workspace", required=True,
                        help="workspace slug (e.g. mumzworld_v3_sample)")
    parser.add_argument("--apply", action="store_true",
                        help="actually write inactive AAV rows. Default is dry-run.")
    args = parser.parse_args()

    manifest = load_manifest()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        plan = compute_sync_plan(
            db, manifest=manifest, workspace_id=ws.id,
        )
        _print_plan(plan, apply_mode=args.apply)

        if args.apply and plan.total_missing > 0:
            result = apply_sync_plan(db, plan=plan)
            db.commit()
            print()
            print(f"inserted {result.inserted} inactive AAV row(s).")
            print("Operators must explicitly activate values they accept "
                  "via the workspace AAV catalog.")
        elif args.apply:
            print()
            print("nothing to insert.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
