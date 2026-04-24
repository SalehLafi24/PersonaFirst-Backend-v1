"""End-to-end mom_stage value-discovery flow using fixture data.

1. Build enrichment outputs from the fixture MODEL_OUTPUTS.
2. Record proposal events into the DB.
3. Refresh aggregates for mom_stage.
4. Show promotion readiness for each aggregate.

Rolls back all DB writes at the end.
"""
from __future__ import annotations

import json

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import (
    AttributeDefinition,
    EnrichedValue,
    EnrichmentOutput,
    EnrichmentSource,
    ProposedValue,
)
from app.services.proposed_attribute_value_service import (
    promotion_readiness,
    record_events_from_output,
    refresh_aggregates,
)
from tests.fixtures.mom_stage_test_data import MODEL_OUTPUTS, PRODUCTS

WORKSPACE_SLUG = "personafirst-starter"
ATTRIBUTE_NAME = "mom_stage"


def _load_attr_def() -> AttributeDefinition:
    from pathlib import Path
    defs = json.loads(
        (Path(__file__).resolve().parent.parent / "seed_data" / "attribute_definitions.json")
        .read_text(encoding="utf-8")
    )
    for d in defs:
        if d["name"] == ATTRIBUTE_NAME:
            return AttributeDefinition(**d)
    raise SystemExit(f"attribute {ATTRIBUTE_NAME!r} not found in seed definitions")


def _build_enrichment_output(
    attr: AttributeDefinition,
    raw: dict,
) -> EnrichmentOutput:
    values = []
    for item in raw.get("values") or []:
        values.append(
            EnrichedValue(
                value=item["value"],
                confidence=float(item["confidence"]),
                evidence=list(item.get("evidence") or []),
                reasoning_mode=item.get("reasoning_mode"),
                source=EnrichmentSource.TEXT,
                contributing_sources=[EnrichmentSource.TEXT],
            )
        )
    proposed = []
    for item in raw.get("proposed_values") or []:
        conf = float(item.get("confidence", 0))
        evidence = [e for e in (item.get("evidence") or []) if isinstance(e, str) and e.strip()]
        if conf >= 0.8 and evidence:
            proposed.append(ProposedValue(value=item["value"], confidence=conf, evidence=evidence))
    return EnrichmentOutput(
        attribute_name=raw.get("attribute_name") or attr.name,
        attribute_class=raw.get("attribute_class") or attr.class_name,
        values=values,
        proposed_values=proposed,
        warnings=list(raw.get("warnings") or []),
        source=EnrichmentSource.TEXT,
    )


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
        if ws is None:
            raise SystemExit(f"Workspace {WORKSPACE_SLUG!r} not found.")
        attr_def = _load_attr_def()

        # ==================================================================
        # Step 1 — Build enrichment outputs from fixture data
        # ==================================================================
        print("=" * 72)
        print("Step 1 — Enrichment outputs (mom_stage)")
        print("=" * 72)
        outputs_by_product: dict[str, EnrichmentOutput] = {}
        for product in PRODUCTS:
            pid = product["product_id"]
            raw = MODEL_OUTPUTS[(pid, ATTRIBUTE_NAME)]
            output = _build_enrichment_output(attr_def, raw)
            outputs_by_product[pid] = output

            pvs = output.proposed_values
            if pvs:
                pv_str = ", ".join(
                    f"{p.value} ({p.confidence:.2f})" for p in pvs
                )
            else:
                pv_str = "(none)"
            warn = output.warnings
            print(f"  {pid:6s} {product['name']:42s} proposed=[{pv_str}]"
                  + (f"  warnings={warn}" if warn else ""))

        # ==================================================================
        # Step 2 — Record proposal events
        # ==================================================================
        print()
        print("=" * 72)
        print("Step 2 — Record proposal events")
        print("=" * 72)
        total_events = 0
        for pid, output in outputs_by_product.items():
            events = record_events_from_output(
                db, workspace_id=ws.id, product_id=pid, output=output,
            )
            if events:
                for ev in events:
                    print(f"  event: product={ev.product_id:6s} "
                          f"raw={ev.proposed_value_raw!r:14s} "
                          f"norm={ev.normalized_value!r:14s} "
                          f"conf={ev.confidence:.2f}  "
                          f"evidence={ev.evidence}")
                total_events += len(events)
        print(f"\n  total events recorded: {total_events}")

        # ==================================================================
        # Step 3 — Refresh aggregates
        # ==================================================================
        print()
        print("=" * 72)
        print("Step 3 — Refresh aggregates for mom_stage")
        print("=" * 72)
        aggregates = refresh_aggregates(
            db, workspace_id=ws.id, attribute_name=ATTRIBUTE_NAME,
        )
        aggregates.sort(key=lambda a: (-a.proposal_count, a.canonical_value))

        for agg in aggregates:
            check = promotion_readiness(agg)
            print(f"\n  canonical_value      = {agg.canonical_value!r}")
            print(f"  cluster_key          = {agg.cluster_key!r}")
            print(f"  proposal_count       = {agg.proposal_count}")
            print(f"  distinct_products    = {agg.distinct_product_count}")
            print(f"  avg_confidence       = {agg.avg_confidence:.3f}")
            print(f"  max_confidence       = {agg.max_confidence:.3f}")
            print(f"  sample_products      = {agg.sample_product_ids}")
            print(f"  sample_evidence      = {agg.sample_evidence}")
            print(f"  status               = {agg.status}")
            print(f"  promotion_ready      = {check.ready}")
            if check.reasons:
                for r in check.reasons:
                    print(f"    blocked: {r}")

        # ==================================================================
        # Summary
        # ==================================================================
        print()
        print("=" * 72)
        print("Summary")
        print("=" * 72)
        ready = [a for a in aggregates if promotion_readiness(a).ready]
        blocked = [a for a in aggregates if not promotion_readiness(a).ready]
        print(f"  total aggregates : {len(aggregates)}")
        print(f"  promotion-ready  : {len(ready)} — "
              + ", ".join(f"{a.canonical_value!r} ({a.proposal_count} proposals, "
                          f"{a.distinct_product_count} products)" for a in ready))
        if blocked:
            print(f"  blocked          : {len(blocked)} — "
                  + ", ".join(f"{a.canonical_value!r} ({a.proposal_count} proposals, "
                              f"{a.distinct_product_count} products)" for a in blocked))

        # Controls
        control_pids = {"P106", "P107"}
        control_events = [
            ev for pid, output in outputs_by_product.items()
            if pid in control_pids
            for ev in (output.proposed_values or [])
        ]
        print(f"  control products : {sorted(control_pids)} ->"
              f"{'0 proposals (clean)' if not control_events else f'{len(control_events)} UNEXPECTED proposals'}")

    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
