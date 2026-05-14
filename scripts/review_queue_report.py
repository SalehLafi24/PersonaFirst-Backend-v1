"""Phase 8.5a: prioritised reviewer queue report.

Pure listing logic lives in app/services/proposed_attribute_value_service.py;
this script handles I/O: prints a formatted table, appends a queue health
snapshot to seed_data/queue_health_history.jsonl.

Usage:
    python scripts/review_queue_report.py --workspace mumzworld_v3_sample
    python scripts/review_queue_report.py --workspace SLUG --attribute use_case
    python scripts/review_queue_report.py --workspace SLUG --top 10 --stale-only
    python scripts/review_queue_report.py --workspace SLUG --no-log
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
from app.services.proposed_attribute_value_service import (
    QUEUE_STALE_THRESHOLD_DAYS,
    list_review_queue,
    queue_health_snapshot,
)


_HISTORY_PATH = ROOT / "seed_data" / "queue_health_history.jsonl"


def _print_health(snap, workspace_slug: str, workspace_id: int) -> None:
    print(f"Workspace: {workspace_slug} (id={workspace_id})")
    print(f"Pending aggregates: {snap.pending_total}   "
          f"stale (>={QUEUE_STALE_THRESHOLD_DAYS}d): {snap.stale_count}   "
          f"oldest: {snap.oldest_age_days}d   "
          f"avg age: {snap.avg_age_days}d")
    if snap.by_attribute:
        per_attr = "  ".join(
            f"{attr}={n}" for attr, n in sorted(snap.by_attribute.items())
        )
        print(f"By attribute: {per_attr}")
    if snap.by_attribute_stale:
        per_attr = "  ".join(
            f"{attr}={n}" for attr, n in sorted(snap.by_attribute_stale.items())
        )
        print(f"By attribute (stale): {per_attr}")
    print()


def _print_table(items: list, top: int) -> None:
    if not items:
        print("(no items)")
        return
    print(f"  {'prio':>5}  {'age':>4}  {'cnt':>4}  {'dist':>4}  {'conf':>4}  "
          f"{'attribute':<14}  {'cluster_key':<22}  notes")
    for it in items[:top]:
        prio = f"{it.priority_score:>5.2f}"
        age = f"{it.age_days}d"
        cnt = f"{it.proposal_count}"
        dist = f"{it.distinct_product_count}"
        conf = f"{it.avg_confidence:.2f}"
        attr = it.attribute_name[:14]
        cluster = it.cluster_key[:22]
        notes = []
        if it.is_stale:
            notes.append("STALE")
        if it.raw_variants_seen and len(it.raw_variants_seen) > 1:
            notes.append(
                f"raw variants: {it.raw_variants_seen[:3]}"
                + ("..." if len(it.raw_variants_seen) > 3 else "")
            )
        note_text = "  ".join(notes)
        print(f"  {prio:>5}  {age:>4}  {cnt:>4}  {dist:>4}  {conf:>4}  "
              f"{attr:<14}  {cluster:<22}  {note_text}")


def _append_history(snap, workspace_id: int, workspace_slug: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace_id": workspace_id,
        "workspace_slug": workspace_slug,
        "pending_total": snap.pending_total,
        "stale_count": snap.stale_count,
        "stale_threshold_days": QUEUE_STALE_THRESHOLD_DAYS,
        "oldest_age_days": snap.oldest_age_days,
        "avg_age_days": snap.avg_age_days,
        "by_attribute": dict(snap.by_attribute),
        "by_attribute_stale": dict(snap.by_attribute_stale),
    }
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prioritised review queue + queue health snapshot.",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--attribute", default=None,
                        help="filter to a single attribute (e.g. use_case)")
    parser.add_argument("--sort-by", choices=("priority", "age", "count"),
                        default="priority")
    parser.add_argument("--stale-only", action="store_true",
                        help="only show items aged >= stale threshold")
    parser.add_argument("--top", type=int, default=20,
                        help="rows to print (default 20)")
    parser.add_argument("--limit", type=int, default=200,
                        help="server-side limit on items pulled")
    parser.add_argument("--no-log", action="store_true",
                        help="skip appending to queue_health_history.jsonl")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        snap = queue_health_snapshot(db, workspace_id=ws.id)
        items = list_review_queue(
            db, workspace_id=ws.id,
            attribute_name=args.attribute,
            sort_by=args.sort_by,
            stale_only=args.stale_only,
            limit=args.limit,
        )
    finally:
        db.close()

    _print_health(snap, args.workspace, ws.id)

    if args.attribute:
        print(f"Top {args.top} pending [{args.attribute}] by {args.sort_by}"
              + ("  (stale only)" if args.stale_only else "")
              + ":")
    else:
        print(f"Top {args.top} pending by {args.sort_by}"
              + ("  (stale only)" if args.stale_only else "")
              + ":")
    _print_table(items, top=args.top)

    if not args.no_log:
        _append_history(snap, workspace_id=ws.id, workspace_slug=args.workspace)
        print()
        print(f"Appended health snapshot to "
              f"{_HISTORY_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
