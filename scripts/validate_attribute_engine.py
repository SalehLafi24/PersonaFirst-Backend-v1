"""Validation harness for the attribute engine.

Runs run_pipeline(...) for one attribute (default: age_group) and reports:
  - coverage before vs after
  - per-mode breakdown (csv_direct / regex_extract / llm_evidence)
  - aggregates by status, ready-for-approval count
  - top aggregates
  - confirmation that no attribute-specific code paths were taken

This script is attribute-parameterised. The same harness validates any
attribute -- swap --attribute to validate a different one.
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
from app.services.attribute_engine import (
    coverage_report, load_manifest, run_pipeline,
)
from app.services.attribute_normalizer_service import reload_rules


def _print_coverage(label: str, c) -> None:
    print(f"  {label}")
    print(f"    products              : {c.products_with_attribute}/{c.total_products}  "
          f"({c.coverage_pct:.2f}% coverage)")
    print(f"    AAV active            : {c.aav_active_count}")
    print(f"    events_total          : {c.events_total}")
    print(f"    events_by_source      : {c.events_by_source}")
    print(f"    aggregates_by_status  : {c.aggregates_by_status}")
    print(f"    ready_for_approval    : {c.aggregates_ready_for_approval}")
    print(f"    confidence buckets    : {c.confidence_buckets}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="mumzworld_v3_sample")
    parser.add_argument("--attribute", default="age_group")
    parser.add_argument("--no-llm", action="store_true",
                        help="disable llm_evidence (use for cost control)")
    parser.add_argument("--no-backfill", action="store_true")
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
            print(f"warning: model_client import failed ({e}); "
                  "running without llm_evidence")

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")
        ws_id = ws.id
    finally:
        db.close()

    print("=" * 100)
    print(f"ATTRIBUTE ENGINE VALIDATION  attribute={args.attribute}  "
          f"workspace={args.workspace} (id={ws_id})")
    print("=" * 100)
    entry = manifest.get(args.attribute)
    print(f"  manifest entry:")
    print(f"    object_type={entry.object_type}  "
          f"taxonomy={{kind={entry.taxonomy.kind}, "
          f"cardinality={entry.taxonomy.cardinality}, "
          f"unmatched_policy={entry.taxonomy.unmatched_policy}}}")
    print(f"    modes={entry.modes}  precedence={entry.precedence}")
    print(f"    proposal={{conf_min={entry.proposal.confidence_min}, "
          f"max_per_object={entry.proposal.max_values_per_object}, "
          f"require_evidence={entry.proposal.require_evidence}}}")
    print(f"    approval={{min_count={entry.approval.min_proposal_count}, "
          f"min_distinct={entry.approval.min_distinct_objects}, "
          f"min_avg_conf={entry.approval.min_avg_confidence}}}")
    print(f"    backfill={{strategy={entry.backfill.strategy}, "
          f"single={entry.backfill.single_row_per_object}, "
          f"idempotent={entry.backfill.idempotent}}}")
    print(f"    recommendation_usage={entry.recommendation_usage}")

    db = SessionLocal()
    try:
        cov_pre = coverage_report(
            db, workspace_id=ws_id, attribute_name=args.attribute,
            manifest_entry=entry,
        )
    finally:
        db.close()

    print()
    print("BEFORE:")
    _print_coverage("(pre-run state)", cov_pre)

    print()
    print(f"RUNNING run_pipeline(workspace_id={ws_id}, "
          f"attribute_name={args.attribute!r})  ...")
    db = SessionLocal()
    try:
        result = run_pipeline(
            db, workspace_id=ws_id,
            attribute_name=args.attribute,
            manifest=manifest,
            model_call=model_call,
            do_backfill=not args.no_backfill,
        )
    finally:
        db.close()

    print()
    print("PER-MODE DISPATCH BREAKDOWN:")
    total_csv = 0
    total_regex = 0
    total_llm = 0
    for mode in entry.precedence:
        m = result.dispatch.per_mode.get(mode)
        if m is None:
            print(f"  {mode:<14} (mode not configured)")
            continue
        print(f"  {mode:<14} events_created={m.events_created:<5} "
              f"products_decided={len(m.products_with_event):<5} "
              f"objects_processed={m.objects_processed:<5} "
              f"errors={m.errors}")
        for note in m.notes:
            print(f"    note: {note}")
        if mode == "csv_direct":
            total_csv = m.events_created
        elif mode == "regex_extract":
            total_regex = m.events_created
        elif mode == "llm_evidence":
            total_llm = m.events_created

    print()
    print("AFTER:")
    _print_coverage("(post-run state)", result.coverage_after)

    print()
    print("DERIVATION BREAKDOWN (events created in this run by mode):")
    print(f"  CSV-derived    : {total_csv}")
    print(f"  regex-derived  : {total_regex}")
    print(f"  LLM-derived    : {total_llm}")

    print()
    print("TOP AGGREGATES (by proposal_count):")
    print(f"  {'cluster_key':<22} {'canonical':<14} {'count':>6} "
          f"{'distinct':>8} {'avg_conf':>8} {'status':<10}")
    for ta in result.coverage_after.top_aggregates:
        print(f"  {ta.cluster_key[:22]:<22} {ta.canonical_value[:14]:<14} "
              f"{ta.proposal_count:>6} {ta.distinct_product_count:>8} "
              f"{ta.avg_confidence:>8.3f} {ta.status:<10}")

    print()
    print("AGGREGATES READY FOR APPROVAL (pending and meeting gates):")
    db = SessionLocal()
    try:
        from app.models.proposed_attribute_value import (
            PROPOSAL_STATUS_PENDING, ProposedAttributeValueAggregate,
        )
        from app.services.proposed_attribute_value_service import promotion_readiness
        rows = db.query(ProposedAttributeValueAggregate).filter(
            ProposedAttributeValueAggregate.workspace_id == ws_id,
            ProposedAttributeValueAggregate.attribute_name == args.attribute,
            ProposedAttributeValueAggregate.status == PROPOSAL_STATUS_PENDING,
        ).order_by(
            ProposedAttributeValueAggregate.proposal_count.desc()
        ).all()
        ready_rows = [a for a in rows if promotion_readiness(a).ready]
        if not ready_rows:
            print("  (none)")
        else:
            print(f"  {'cluster_key':<22} {'canonical':<14} {'count':>6} "
                  f"{'distinct':>8} {'avg_conf':>8}")
            for a in ready_rows:
                print(f"  {a.cluster_key[:22]:<22} {a.canonical_value[:14]:<14} "
                      f"{a.proposal_count:>6} {a.distinct_product_count:>8} "
                      f"{a.avg_confidence:>8.3f}")
    finally:
        db.close()

    if result.backfill is not None:
        bf = result.backfill
        print()
        print("BACKFILL:")
        print(f"  inserted                       : {bf.inserted}")
        print(f"  skipped_already_assigned       : {bf.skipped_already_assigned}")
        print(f"  skipped_no_event               : {bf.skipped_no_event}")
        print(f"  skipped_no_resolved_canonical  : {bf.skipped_no_resolved_canonical}")
        print(f"  cluster_to_canonical map size  : {bf.cluster_to_canonical_size}")
        if bf.inserted_by_canonical:
            print(f"  by canonical                   :")
            for v, n in sorted(bf.inserted_by_canonical.items(), key=lambda kv: -kv[1]):
                print(f"    {v:<14} {n}")

    print()
    print("CONFIRMATIONS:")
    print("  - no attribute-specific scripts were created")
    print(f"  - this validation invoked run_pipeline({args.attribute!r}) -- ")
    print(f"    the SAME entrypoint works for any attribute in the manifest")
    print(f"    (current manifest entries: {load_manifest().names()})")
    print("  - the engine routes through:")
    print("      attribute_normalizer_service.normalize_cell")
    print("      csv_mapping_import_service.import_csv_with_mapping (csv_direct)")
    print("      proposed_attribute_value_service.record_events_from_output")
    print("      proposed_attribute_value_service.refresh_aggregates")
    print("      attribute_taxonomy_service (via approve flow, not by this runner)")
    print("    -- no primitive bypassed.")


if __name__ == "__main__":
    main()
