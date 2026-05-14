"""Quality evaluation framework (Phase 8).

Pure read-only functions that score the live system against hand-labeled
gold sets. Outputs are advisory; CLIs in scripts/eval_*.py handle I/O.

Today: attribute gold-set sampling.
Future: attribute_eval, recommendation_eval, signal_ablation.
"""
from app.services.eval.labeling_guide import (
    GuideAttribute,
    GuideValue,
    GuideWarning,
    LabelingGuide,
    format_warnings,
    load_labeling_guide,
    validate_guide_against_manifest,
)
from app.services.eval.sampling import (
    SampledProduct,
    SamplingConfig,
    SamplingResult,
    sample_attribute_gold,
)

__all__ = [
    "GuideAttribute",
    "GuideValue",
    "GuideWarning",
    "LabelingGuide",
    "SampledProduct",
    "SamplingConfig",
    "SamplingResult",
    "format_warnings",
    "load_labeling_guide",
    "sample_attribute_gold",
    "validate_guide_against_manifest",
]
