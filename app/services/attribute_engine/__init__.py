"""Generic attribute-processing engine.

Public entry points:
    load_manifest(path=None)            -> AttributeManifest
    run_pipeline(db, ws_id, attr, ...)  -> PipelineRunResult
    backfill_attribute(db, ws_id, attr) -> BackfillResult
    coverage_report(db, ws_id, attr)    -> CoverageReport

The engine wraps existing PersonaFirst primitives:
    attribute_normalizer_service
    csv_mapping_import_service.import_csv_with_mapping
    proposed_attribute_value_service.{record_events_from_output,
                                      refresh_aggregates,
                                      promotion_readiness}
    attribute_taxonomy_service

It never bypasses them.
"""
from app.services.attribute_engine.backfill_service import (
    BackfillResult,
    backfill_attribute,
)
from app.services.attribute_engine.coverage_service import (
    CoverageReport,
    coverage_report,
)
from app.services.attribute_engine.manifest import (
    AttributeManifestEntry,
    AttributeManifest,
    load_manifest,
)
from app.services.attribute_engine.pipeline_runner import (
    PipelineRunResult,
    run_pipeline,
)
from app.services.attribute_engine.weight_suggester import (
    SuggesterConfig,
    WeightSuggestion,
    suggest_weights,
)

__all__ = [
    "AttributeManifest",
    "AttributeManifestEntry",
    "BackfillResult",
    "CoverageReport",
    "PipelineRunResult",
    "SuggesterConfig",
    "WeightSuggestion",
    "backfill_attribute",
    "coverage_report",
    "load_manifest",
    "run_pipeline",
    "suggest_weights",
]
