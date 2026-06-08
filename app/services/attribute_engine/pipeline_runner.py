"""run_pipeline(workspace_id, attribute_name) -- attribute-agnostic orchestrator.

Steps:
  1. Look up the manifest entry.
  2. Dispatch ingest modes (csv_direct, regex_extract, llm_evidence) per
     manifest precedence; emit ProposedAttributeValueEvent rows.
  3. refresh_aggregates for the attribute (idempotent).
  4. Optionally run backfill_attribute (only assigns from already-approved
     aggregates -- approval itself is an out-of-band reviewer action).

Approval is intentionally NOT performed by this runner. Reviewers approve
via the existing taxonomy_admin endpoints. This keeps "Claude is not
assigning attribute values directly" -- the LLM only proposes, humans
approve, and the engine materialises the approved decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.services.attribute_engine.backfill_service import (
    BackfillResult, backfill_attribute,
)
from app.services.attribute_engine.coverage_service import (
    CoverageReport, coverage_report,
)
from app.services.attribute_engine.ingest_dispatcher import (
    DispatcherResult, dispatch,
)
from app.services.attribute_engine.manifest import (
    AttributeManifest, load_manifest,
)
from app.services.proposed_attribute_value_service import refresh_aggregates


_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class PipelineRunResult:
    attribute_name: str
    workspace_id: int
    started_at: str
    ended_at: str
    coverage_before: CoverageReport
    coverage_after: CoverageReport
    dispatch: DispatcherResult
    aggregates_refreshed: int
    backfill: BackfillResult | None


def run_pipeline(
    db: Session,
    *,
    workspace_id: int,
    attribute_name: str,
    manifest: AttributeManifest | None = None,
    repo_root: Path | None = None,
    model_call: Callable[[str], dict] | None = None,
    do_backfill: bool = True,
) -> PipelineRunResult:
    """Run the attribute pipeline end-to-end for a single attribute.

    Idempotent and re-runnable. Each mode skips products it has already
    decided on (cardinality=single). refresh_aggregates is itself
    idempotent. Backfill only writes new ProductAttribute rows.
    """
    started = datetime.now(timezone.utc).isoformat()
    mfst = manifest or load_manifest()
    entry = mfst.get(attribute_name)
    root = repo_root or _REPO_ROOT

    cov_before = coverage_report(
        db, workspace_id=workspace_id, attribute_name=attribute_name,
        manifest_entry=entry,
    )

    # Step 1-2: ingest dispatcher emits events.
    disp = dispatch(
        db=db, workspace_id=workspace_id, entry=entry,
        repo_root=root, model_call=model_call,
    )
    db.commit()

    # Step 3: refresh aggregates (idempotent; only pending rows touched).
    aggs = refresh_aggregates(
        db, workspace_id=workspace_id, attribute_name=attribute_name,
    )
    db.commit()

    # Step 4: optional backfill from already-approved aggregates.
    bf: BackfillResult | None = None
    if do_backfill:
        bf = backfill_attribute(
            db, workspace_id=workspace_id,
            attribute_name=attribute_name,
            manifest_entry=entry,
        )

    cov_after = coverage_report(
        db, workspace_id=workspace_id, attribute_name=attribute_name,
        manifest_entry=entry,
    )
    ended = datetime.now(timezone.utc).isoformat()

    return PipelineRunResult(
        attribute_name=attribute_name,
        workspace_id=workspace_id,
        started_at=started,
        ended_at=ended,
        coverage_before=cov_before,
        coverage_after=cov_after,
        dispatch=disp,
        aggregates_refreshed=len(aggs),
        backfill=bf,
    )
