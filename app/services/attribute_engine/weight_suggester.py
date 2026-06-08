"""Engine-suggested default `score_weight` values.

Pure read-only function. Given a workspace and the current manifest,
computes catalog-aware weight defaults for every persona_relevant
attribute. Output is advisory; the CLI in scripts/suggest_attribute_weights.py
is the only path that writes back to the manifest.

Formula (suggester_v2):

    raw_score(attr) = usage_factor(attr.usage)
                    × clamp(attr.coverage_pct, [floor, ceiling])
                    × kind_factor(attr.taxonomy.kind)
                    × evenness_factor(attr)

    suggested_weight(attr) = raw_score(attr) / sum(raw_score(*))

The evenness_factor is the normalised entropy of the attribute's
value distribution across populated products:

    evenness_factor(attr) = H / log2(num_distinct_values)
                          ∈ [0, 1]
        where H = -Σ p_i × log2(p_i)
              p_i = count_i / total_populated_products

It corrects the v1 flaw where attributes with high coverage but low
discriminative power (e.g., a 3-value attribute where 95% of products
land on one value) were over-weighted purely on coverage.

Defaults (config-overridable):
    usage_factor : cohort_key 1.5, ranking_signal 1.0, filter 0.3
    kind_factor  : closed 1.0, open 0.85
    coverage_floor 0.10, coverage_ceiling 1.00

Boundary: this function does NOT do I/O beyond reading coverage,
attribute-value distributions, and the manifest. It does NOT write
anything. It does NOT log. The CLI is responsible for those side effects.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.product import Product, ProductAttribute
from app.services.attribute_engine.coverage_service import coverage_report
from app.services.attribute_engine.manifest import (
    AttributeManifest,
    load_manifest,
)


SUGGESTER_VERSION = "suggester_v2"


def _compute_evenness(
    db: Session, *, workspace_id: int, attribute_name: str,
) -> tuple[float, int]:
    """Compute (evenness_factor, num_distinct_values) for one attribute.

    One DB query: the value-frequency distribution of the attribute
    across products in the workspace. Returns:
      - evenness ∈ [0, 1]: 1.0 = uniform spread, 0.0 = all on one value
      - num_distinct_values: count of distinct values seen

    Edge cases:
      - 0 distinct values (no products tagged):  (0.0, 0)
      - 1 distinct value (perfect concentration): (0.0, 1)
      - >= 2 distinct values: standard normalised-entropy formula
    """
    rows = (
        db.query(ProductAttribute.attribute_value, func.count())
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == attribute_name)
        .group_by(ProductAttribute.attribute_value)
        .all()
    )
    counts = [int(c) for _, c in rows if c]
    total = sum(counts)
    n = len(counts)
    if n <= 1 or total == 0:
        return (0.0, n)
    h = 0.0
    for c in counts:
        p = c / total
        if p > 0:
            h -= p * math.log2(p)
    h_max = math.log2(n)
    if h_max <= 0:
        return (0.0, n)
    evenness = h / h_max
    return (max(0.0, min(1.0, evenness)), n)


_DEFAULT_USAGE_FACTORS: Mapping[str, float] = {
    "cohort_key": 1.5,
    "ranking_signal": 1.0,
    "filter": 0.3,
}
_DEFAULT_KIND_FACTORS: Mapping[str, float] = {
    "closed": 1.0,
    "open": 0.85,
}


@dataclass(frozen=True)
class SuggesterConfig:
    """Tunable knobs for the formula. All factors must be non-negative."""
    usage_factors: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_USAGE_FACTORS)
    )
    kind_factors: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_KIND_FACTORS)
    )
    coverage_floor: float = 0.10
    coverage_ceiling: float = 1.00

    def __post_init__(self) -> None:
        for k, v in self.usage_factors.items():
            if v < 0:
                raise ValueError(f"usage_factors[{k!r}]={v} must be >= 0")
        for k, v in self.kind_factors.items():
            if v < 0:
                raise ValueError(f"kind_factors[{k!r}]={v} must be >= 0")
        if not (0.0 <= self.coverage_floor <= 1.0):
            raise ValueError(
                f"coverage_floor={self.coverage_floor} must be in [0, 1]"
            )
        if not (0.0 <= self.coverage_ceiling <= 1.0):
            raise ValueError(
                f"coverage_ceiling={self.coverage_ceiling} must be in [0, 1]"
            )
        if self.coverage_ceiling < self.coverage_floor:
            raise ValueError(
                f"coverage_ceiling ({self.coverage_ceiling}) must be >= "
                f"coverage_floor ({self.coverage_floor})"
            )

    def to_json(self) -> dict:
        return {
            "usage_factors": dict(self.usage_factors),
            "kind_factors": dict(self.kind_factors),
            "coverage_floor": self.coverage_floor,
            "coverage_ceiling": self.coverage_ceiling,
        }


@dataclass(frozen=True)
class WeightSuggestion:
    attribute_name: str
    suggested_weight: float
    current_weight: float | None
    delta: float
    raw_score: float
    breakdown: Mapping[str, float]
    explanation: str
    weight_reason: str | None     # current `_weight_reason` from manifest, if any
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "attribute_name": self.attribute_name,
            "suggested_weight": round(self.suggested_weight, 6),
            "current_weight": (
                round(self.current_weight, 6) if self.current_weight is not None else None
            ),
            "delta": round(self.delta, 6),
            "raw_score": round(self.raw_score, 6),
            "breakdown": {k: round(v, 6) for k, v in self.breakdown.items()},
            "explanation": self.explanation,
            "weight_reason": self.weight_reason,
            "warnings": list(self.warnings),
        }


def suggest_weights(
    db: Session,
    *,
    workspace_id: int,
    manifest: AttributeManifest | None = None,
    config: SuggesterConfig | None = None,
) -> list[WeightSuggestion]:
    """Compute suggested score_weight values for persona_relevant attributes.

    Pure read-only. Reads:
      - the manifest (or load_manifest() if None)
      - coverage_report per attribute via coverage_service

    Output is sorted by suggested_weight descending.
    """
    mfst = manifest or load_manifest()
    cfg = config or SuggesterConfig()

    persona_attrs = [
        (name, entry) for name, entry in mfst.entries.items()
        if entry.recommendation.persona_relevant
    ]
    if not persona_attrs:
        return []

    raws: list[tuple[str, float, dict[str, float], list[str]]] = []
    for name, entry in persona_attrs:
        warns: list[str] = []
        usage = entry.recommendation.usage
        if usage is None:
            warns.append("usage not declared; defaulting to ranking_signal")
            usage_factor = cfg.usage_factors.get("ranking_signal", 1.0)
        elif usage not in cfg.usage_factors:
            raise ValueError(
                f"attribute {name!r}: usage={usage!r} not in usage_factors "
                f"{sorted(cfg.usage_factors)}"
            )
        else:
            usage_factor = cfg.usage_factors[usage]

        kind = entry.taxonomy.kind
        if kind not in cfg.kind_factors:
            raise ValueError(
                f"attribute {name!r}: taxonomy.kind={kind!r} not in "
                f"kind_factors {sorted(cfg.kind_factors)}"
            )
        kind_factor = cfg.kind_factors[kind]

        cov_pct_100 = coverage_report(
            db, workspace_id=workspace_id, attribute_name=name,
            manifest_entry=entry,
        ).coverage_pct
        cov_fraction = max(0.0, min(1.0, cov_pct_100 / 100.0))
        if cov_fraction < cfg.coverage_floor:
            warns.append(
                f"coverage {cov_fraction:.2%} below floor "
                f"{cfg.coverage_floor:.0%}; using floor"
            )
        cov_used = max(cfg.coverage_floor, min(cfg.coverage_ceiling, cov_fraction))

        evenness, value_count = _compute_evenness(
            db, workspace_id=workspace_id, attribute_name=name,
        )
        if value_count == 0:
            warns.append("no populated products; evenness=0")
        elif value_count == 1:
            warns.append(
                "only 1 distinct value populated; evenness=0 "
                "(no discriminative power)"
            )

        raw = usage_factor * cov_used * kind_factor * evenness
        breakdown = {
            "usage_factor": usage_factor,
            "coverage_pct_used": cov_used,
            "coverage_pct_actual": cov_fraction,
            "kind_factor": kind_factor,
            "evenness_factor": evenness,
            "value_count": float(value_count),
        }
        raws.append((name, raw, breakdown, warns))

    raw_total = sum(r for _, r, _, _ in raws) or 1.0  # avoid divide-by-zero

    out: list[WeightSuggestion] = []
    for name, raw, breakdown, warns in raws:
        entry = mfst.entries[name]
        current = entry.recommendation.score_weight
        suggested = raw / raw_total
        delta = suggested - current
        explanation = (
            f"{entry.recommendation.usage or 'ranking_signal'} "
            f"({breakdown['usage_factor']}x) × "
            f"cov {breakdown['coverage_pct_actual']:.1%} × "
            f"{entry.taxonomy.kind} ({breakdown['kind_factor']}x) × "
            f"evenness {breakdown['evenness_factor']:.2f} "
            f"({int(breakdown['value_count'])} values) "
            f"→ raw {raw:.4f}"
        )
        out.append(WeightSuggestion(
            attribute_name=name,
            suggested_weight=suggested,
            current_weight=current,
            delta=delta,
            raw_score=raw,
            breakdown=breakdown,
            explanation=explanation,
            weight_reason=entry.recommendation.weight_reason,
            warnings=tuple(warns),
        ))

    out.sort(key=lambda s: -s.suggested_weight)
    return out
