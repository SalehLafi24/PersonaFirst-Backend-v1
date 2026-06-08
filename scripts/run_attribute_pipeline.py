"""Generic CLI runner for the attribute engine.

Usage:
    python scripts/run_attribute_pipeline.py --workspace <slug> --attribute <name>
    python scripts/run_attribute_pipeline.py --workspace <slug> --attribute <name> --no-llm
    python scripts/run_attribute_pipeline.py --workspace <slug> --attribute <name> --no-backfill

This script is attribute-agnostic. It reads the manifest and processes
whichever attribute is named on the command line. There are no
attribute-specific branches in this file.
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
from app.services.attribute_engine import load_manifest, run_pipeline
from app.services.attribute_normalizer_service import reload_rules


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the attribute engine pipeline for one attribute.",
    )
    parser.add_argument("--workspace", required=True,
                        help="workspace slug (e.g. mumzworld_v3_sample)")
    parser.add_argument("--attribute", required=True,
                        help="attribute name from the manifest")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip llm_evidence mode (useful when offline / cost-control)")
    parser.add_argument("--no-backfill", action="store_true",
                        help="run ingest + aggregates only; do not materialize PA rows")
    args = parser.parse_args()

    reload_rules()
    manifest = load_manifest()
    if args.attribute not in manifest.entries:
        raise SystemExit(
            f"unknown attribute {args.attribute!r}; "
            f"manifest entries: {manifest.names()}"
        )

    model_call = None
    if not args.no_llm:
        try:
            from app.services.model_client import call_model_json
            model_call = call_model_json
        except Exception as e:
            print(f"warning: model_client import failed ({e}); skipping llm_evidence")

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        result = run_pipeline(
            db, workspace_id=ws.id,
            attribute_name=args.attribute,
            manifest=manifest,
            model_call=model_call,
            do_backfill=not args.no_backfill,
        )
    finally:
        db.close()

    cb = result.coverage_before
    ca = result.coverage_after
    print(f"attribute      : {result.attribute_name}")
    print(f"workspace_id   : {result.workspace_id}")
    print(f"window         : {result.started_at}  ->  {result.ended_at}")
    print(f"coverage       : {cb.coverage_pct:.2f}%  ->  {ca.coverage_pct:.2f}%  "
          f"({cb.products_with_attribute}/{cb.total_products}  ->  "
          f"{ca.products_with_attribute}/{ca.total_products})")
    print(f"events_total   : {cb.events_total}  ->  {ca.events_total}  "
          f"(diff=+{ca.events_total - cb.events_total})")
    print(f"aggregates     : refreshed={result.aggregates_refreshed}  "
          f"by_status={ca.aggregates_by_status}  "
          f"ready_for_approval={ca.aggregates_ready_for_approval}")
    print(f"per-mode events:")
    for mode in result.dispatch.per_mode:
        m = result.dispatch.per_mode[mode]
        print(f"  {mode:<14} events={m.events_created:<5} "
              f"products_decided={len(m.products_with_event):<5} "
              f"objects_processed={m.objects_processed:<5} "
              f"errors={m.errors}")
        for note in m.notes:
            print(f"    note: {note}")
    if result.backfill is not None:
        bf = result.backfill
        print(f"backfill       : inserted={bf.inserted}  "
              f"already_assigned={bf.skipped_already_assigned}  "
              f"no_event={bf.skipped_no_event}  "
              f"no_canonical={bf.skipped_no_resolved_canonical}")
        print(f"  by canonical : {bf.inserted_by_canonical}")

    drift = ca.value_aav_drift
    if drift:
        n_events = sum(d.event_count for d in drift)
        print(f"manifest/AAV drift : {len(drift)} produced value(s) with no active AAV "
              f"({n_events} event(s) cannot be materialized):")
        for d in drift:
            agg_part = (f"aggregate={d.aggregate_status}"
                        if d.aggregate_status else "aggregate=(none)")
            print(f"  {result.attribute_name}={d.produced_value:<20} "
                  f"events={d.event_count:<5} {agg_part}")

    blocked = [pb for pb in ca.pending_blockers if pb.reasons]
    if blocked:
        print(f"pending blockers : {len(blocked)} pending aggregate(s) below promotion threshold:")
        for pb in blocked:
            print(f"  {result.attribute_name}/cluster={pb.cluster_key}  "
                  f"(proposal_count={pb.proposal_count}, "
                  f"distinct_product_count={pb.distinct_product_count}, "
                  f"avg_confidence={pb.avg_confidence:.3f})")
            for reason in pb.reasons:
                print(f"    - {reason}")


if __name__ == "__main__":
    main()
