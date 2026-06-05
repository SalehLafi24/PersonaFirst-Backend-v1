"""Recommendation engine conformance + constraint checkers.

Verifies the rec engine does what its design claims: no LLM grading, no
similarity benchmarking, no synthetic-customers-as-ground-truth.

V1 scope:
  - Hard constraints (must always hold). See `check_hard_constraints`.
  - Ranking-weight conformance. See `check_weight_conformance`.
  - Future: diversity floor, complement separation, persona coverage.

Boundary: pure read. Calls the rec engine, then evaluates the response.
Never mutates the DB.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.customer import CustomerInteraction
from app.models.product import Product


# Tolerance for floating-point comparisons against manifest weights and
# scoring math. 1e-6 is sufficient for the engine's float64 arithmetic.
_FLOAT_EPS = 1e-6


@dataclass
class Violation:
    """One specific violation. `check` is the check name (e.g.
    'no_duplicates'); `severity` is 'error' (must-pass) or 'warn'."""
    check: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    """Outcome of one check on one customer's rec response.

    `passed` is True iff there are zero `error`-severity violations.
    `warn`-severity entries are surfaced but don't fail the check."""
    customer_id: str
    check: str
    passed: bool
    violations: list[Violation] = field(default_factory=list)


# --------------------------------------------------------------------------
# Hard-constraint checks (each returns a list of violations; empty == pass)
# --------------------------------------------------------------------------


def _check_no_duplicate_product_ids(response) -> list[Violation]:
    """C8: no two recs in a single response share the same product_id."""
    seen: dict[str, int] = {}
    violations: list[Violation] = []
    for i, rec in enumerate(response.recommendations):
        pid = rec.product_id
        if pid in seen:
            violations.append(Violation(
                check="no_duplicates", severity="error",
                message=f"product_id {pid!r} appears at indices {seen[pid]} and {i}",
                detail={"product_id": pid,
                        "indices": [seen[pid], i]},
            ))
        else:
            seen[pid] = i
    return violations


def _check_top_n_contract(response, requested_top_n: int) -> list[Violation]:
    """C18: response has at most `requested_top_n` recommendations."""
    n = len(response.recommendations)
    if n > requested_top_n:
        return [Violation(
            check="top_n_contract", severity="error",
            message=f"response has {n} recs but top_n={requested_top_n} requested",
            detail={"returned": n, "requested": requested_top_n},
        )]
    return []


def _check_score_descending(response) -> list[Violation]:
    """C9 (partial): recs are sorted by score, descending."""
    violations: list[Violation] = []
    prev_score: float | None = None
    prev_pid: str | None = None
    for i, rec in enumerate(response.recommendations):
        if prev_score is not None and rec.score > prev_score + 1e-9:
            violations.append(Violation(
                check="score_descending", severity="error",
                message=(f"rec[{i}] {rec.product_id!r} score={rec.score:.6f} > "
                         f"rec[{i-1}] {prev_pid!r} score={prev_score:.6f}"),
                detail={"index": i, "score": rec.score,
                        "prev_index": i - 1, "prev_score": prev_score},
            ))
        prev_score = rec.score
        prev_pid = rec.product_id
    return violations


def _check_no_purchased_in_response(
    db: Session, response, *, workspace_id: int, customer_id: str,
) -> list[Violation]:
    """C10/C11/C12 (RepurchaseSuppression contract): a previously-purchased
    product may appear in the response ONLY IF the engine's documented
    rules allow it.

    Per `intent_layer_service.RepurchaseSuppression`:
      - product.recommendation_role=='complementary' -> ALWAYS ALLOWED
        (claim C12: complementary recs bypass RepurchaseSuppression
        AND SaturationDemoter entirely; they are designed to re-surface
        as cross-sell post-purchase).
      - product.repurchase_behavior=='one_time' AND in history -> MUST drop.
      - 'repurchasable' AND last purchase <= window -> MUST drop.
      - 'repurchasable' AND last purchase > window -> ALLOWED.
      - product.repurchase_behavior is None: FallbackInteractionExclude
        applies and drops it. So allowed only via complementary bypass.

    Anything else surfacing a previously-purchased product is a violation.
    """
    from datetime import datetime, timezone

    rows = db.query(
        CustomerInteraction.product_id,
        CustomerInteraction.occurred_at,
    ).filter(
        CustomerInteraction.workspace_id == workspace_id,
        CustomerInteraction.customer_id == customer_id,
    ).all()
    last_purchase: dict[str, datetime] = {}
    for pid, occurred in rows:
        if pid not in last_purchase or (occurred and occurred > last_purchase[pid]):
            last_purchase[pid] = occurred
    if not last_purchase:
        return []

    rec_pids_in_history = [
        rec for rec in response.recommendations
        if rec.product_id in last_purchase
    ]
    if not rec_pids_in_history:
        return []

    prods = {
        p.product_id: p for p in db.query(Product).filter(
            Product.workspace_id == workspace_id,
            Product.product_id.in_([r.product_id for r in rec_pids_in_history]),
        ).all()
    }

    now = datetime.now(timezone.utc)
    violations: list[Violation] = []
    for rec in rec_pids_in_history:
        prod = prods.get(rec.product_id)
        role = getattr(prod, "recommendation_role", None) if prod else None
        behavior = getattr(prod, "repurchase_behavior", None) if prod else None
        window_days = getattr(prod, "repurchase_window_days", None) if prod else None
        last = last_purchase.get(rec.product_id)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_days = (now - last).days if last else None

        # C12: complementary bypass is intentional design.
        if role == "complementary":
            continue

        # C11: repurchasable + outside window is intentional re-suggestion.
        if (behavior == "repurchasable"
                and window_days is not None
                and elapsed_days is not None
                and elapsed_days > window_days):
            continue

        # Anything else surfacing a history product is a real violation.
        violations.append(Violation(
            check="no_purchased_in_response", severity="error",
            message=(f"rec {rec.product_id!r} in history "
                     f"(role={role!r}, behavior={behavior!r}, "
                     f"window_days={window_days}, "
                     f"elapsed_days={elapsed_days}) -- not allowed by "
                     f"complementary-bypass or window-re-suggest rules"),
            detail={"product_id": rec.product_id,
                    "customer_id": customer_id,
                    "recommendation_role": role,
                    "repurchase_behavior": behavior,
                    "repurchase_window_days": window_days,
                    "elapsed_days": elapsed_days},
        ))
    return violations


def _check_products_exist(
    db: Session, response, *, workspace_id: int,
) -> list[Violation]:
    """Every recommended product_id corresponds to a real Product in
    the workspace."""
    pids = [rec.product_id for rec in response.recommendations]
    if not pids:
        return []
    existing: set[str] = {
        pid for (pid,) in db.query(Product.product_id).filter(
            Product.workspace_id == workspace_id,
            Product.product_id.in_(pids),
        ).all()
    }
    violations: list[Violation] = []
    for rec in response.recommendations:
        if rec.product_id not in existing:
            violations.append(Violation(
                check="products_exist", severity="error",
                message=(f"rec product_id {rec.product_id!r} not found "
                         f"in workspace {workspace_id}"),
                detail={"product_id": rec.product_id,
                        "workspace_id": workspace_id},
            ))
    return violations


# --------------------------------------------------------------------------
# Aggregator: hard-constraints suite
# --------------------------------------------------------------------------


HARD_CONSTRAINT_CHECKS = (
    "no_duplicates",
    "top_n_contract",
    "score_descending",
    "no_purchased_in_response",
    "products_exist",
)


def check_hard_constraints(
    db: Session,
    response,
    *,
    workspace_id: int,
    customer_id: str,
    requested_top_n: int,
) -> dict[str, CheckResult]:
    """Run the full hard-constraint suite on one customer's response.

    Returns a dict {check_name: CheckResult}, one entry per check in
    `HARD_CONSTRAINT_CHECKS` (whether the check produced violations or
    not). The CLI uses this to build the per-customer x per-check matrix.
    """
    out: dict[str, CheckResult] = {}

    runs = (
        ("no_duplicates",
         _check_no_duplicate_product_ids(response)),
        ("top_n_contract",
         _check_top_n_contract(response, requested_top_n)),
        ("score_descending",
         _check_score_descending(response)),
        ("no_purchased_in_response",
         _check_no_purchased_in_response(
             db, response, workspace_id=workspace_id,
             customer_id=customer_id,
         )),
        ("products_exist",
         _check_products_exist(db, response, workspace_id=workspace_id)),
    )

    for check_name, violations in runs:
        out[check_name] = CheckResult(
            customer_id=customer_id,
            check=check_name,
            passed=not any(v.severity == "error" for v in violations),
            violations=list(violations),
        )
    return out


# --------------------------------------------------------------------------
# Ranking-weight conformance (C1 / C2 / C3)
# --------------------------------------------------------------------------


def _check_matched_signal_weights(
    response, manifest_weights: dict[str, float],
) -> list[Violation]:
    """C2: each matched_signal.weight equals the manifest's score_weight
    for that attribute, ±_FLOAT_EPS."""
    violations: list[Violation] = []
    for i, rec in enumerate(response.recommendations):
        for sig in rec.matched_signals or []:
            expected = manifest_weights.get(sig.attribute_name)
            if expected is None:
                violations.append(Violation(
                    check="weight_matches_manifest", severity="error",
                    message=(f"rec[{i}] {rec.product_id!r} has matched_signal "
                             f"for {sig.attribute_name!r} which is not "
                             f"persona_relevant in the manifest"),
                    detail={"index": i, "product_id": rec.product_id,
                            "attribute": sig.attribute_name},
                ))
                continue
            if abs(sig.weight - expected) > _FLOAT_EPS:
                violations.append(Violation(
                    check="weight_matches_manifest", severity="error",
                    message=(f"rec[{i}] {rec.product_id!r} weight for "
                             f"{sig.attribute_name!r} = {sig.weight!r}, "
                             f"manifest says {expected!r}"),
                    detail={"index": i, "product_id": rec.product_id,
                            "attribute": sig.attribute_name,
                            "rec_weight": sig.weight,
                            "manifest_weight": expected},
                ))
    return violations


def _check_contribution_math(response) -> list[Violation]:
    """C2: contribution = weight × persona_probability for each
    matched_signal."""
    violations: list[Violation] = []
    for i, rec in enumerate(response.recommendations):
        for sig in rec.matched_signals or []:
            expected = float(sig.weight) * float(sig.persona_probability)
            if abs(float(sig.contribution) - expected) > _FLOAT_EPS:
                violations.append(Violation(
                    check="contribution_math", severity="error",
                    message=(f"rec[{i}] {rec.product_id!r} contribution for "
                             f"{sig.attribute_name!r} = {sig.contribution!r}, "
                             f"weight*prob = {expected!r}"),
                    detail={"index": i, "product_id": rec.product_id,
                            "attribute": sig.attribute_name,
                            "contribution": sig.contribution,
                            "expected": expected},
                ))
    return violations


def _check_persona_fit_math(response) -> list[Violation]:
    """C1: persona_fit = sum(matched_signal.contribution)."""
    violations: list[Violation] = []
    for i, rec in enumerate(response.recommendations):
        expected = sum(float(s.contribution) for s in (rec.matched_signals or []))
        if abs(float(rec.persona_fit) - expected) > _FLOAT_EPS:
            violations.append(Violation(
                check="persona_fit_math", severity="error",
                message=(f"rec[{i}] {rec.product_id!r} persona_fit="
                         f"{rec.persona_fit!r}, sum(contributions)={expected!r}"),
                detail={"index": i, "product_id": rec.product_id,
                        "persona_fit": rec.persona_fit, "expected": expected},
            ))
    return violations


def _check_score_math_no_intent(response, persona_confidence: float) -> list[Violation]:
    """C1: score = persona_fit × confidence_overall, BUT only when no
    intent-layer contribution modified the score for that rec.

    The intent layer is allowed to boost (additive, capped) or demote
    (multiplicative). Recs with non-empty `intent_contributions` are
    expected to deviate from the pre-intent formula; we skip those here
    rather than re-implement the intent math (that's a separate check)."""
    violations: list[Violation] = []
    for i, rec in enumerate(response.recommendations):
        if rec.intent_contributions:
            # Intent layer touched this rec; pre-intent math doesn't apply.
            continue
        expected = float(rec.persona_fit) * float(persona_confidence)
        if abs(float(rec.score) - expected) > _FLOAT_EPS:
            violations.append(Violation(
                check="score_math", severity="error",
                message=(f"rec[{i}] {rec.product_id!r} score={rec.score!r}, "
                         f"persona_fit*confidence = "
                         f"{rec.persona_fit!r} * {persona_confidence!r} = "
                         f"{expected!r} (no intent contributions)"),
                detail={"index": i, "product_id": rec.product_id,
                        "score": rec.score, "expected": expected},
            ))
    return violations


def _check_manifest_weights_sum(manifest_weights: dict[str, float]) -> list[Violation]:
    """C3: persona_relevant attribute weights sum to 1.0."""
    total = sum(manifest_weights.values())
    if abs(total - 1.0) > _FLOAT_EPS:
        return [Violation(
            check="manifest_weights_sum", severity="error",
            message=(f"persona_relevant weights sum to {total!r}, expected 1.0"),
            detail={"sum": total, "weights": dict(manifest_weights)},
        )]
    return []


WEIGHT_CONFORMANCE_CHECKS = (
    "manifest_weights_sum",
    "weight_matches_manifest",
    "contribution_math",
    "persona_fit_math",
    "score_math",
)


# --------------------------------------------------------------------------
# Diversity floor
# --------------------------------------------------------------------------
#
# Two simple, deterministic measures:
#
#   distinct_value_count    Number of unique non-null values for an
#                           attribute across the rec set.
#   top_share               Fraction of recs whose attribute value equals
#                           the most-common value. High top_share = collapse.
#
# We pick floors per attribute. product_type collapse is the dominant
# failure mode (rec set is all bibs / all pacifiers); age_group homogeneity
# is often legitimate for a coherent persona, so we apply a softer rule.


def _value_distribution(
    response, attribute_name: str,
) -> tuple[int, float, dict[str, int]]:
    """Return (distinct_count, top_share, value_counts) for the rec
    set's values on `attribute_name`. Null values are excluded from the
    count base; top_share is computed against non-null total."""
    counts: dict[str, int] = {}
    for rec in response.recommendations:
        v = (rec.attributes or {}).get(attribute_name)
        if v is None:
            continue
        counts[v] = counts.get(v, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return 0, 0.0, counts
    top = max(counts.values())
    return len(counts), top / total, counts


def _check_diversity_floor(
    response,
    *,
    attribute: str,
    min_distinct: int,
    max_top_share: float,
    severity: str = "error",
) -> list[Violation]:
    """Generic diversity check: at least `min_distinct` unique values
    AND no single value exceeds `max_top_share` of the (non-null) recs."""
    distinct, top_share, counts = _value_distribution(response, attribute)
    if not response.recommendations:
        return []
    violations: list[Violation] = []
    if distinct < min_distinct:
        violations.append(Violation(
            check=f"diversity_distinct_{attribute}",
            severity=severity,
            message=(f"only {distinct} distinct {attribute} value(s) across "
                     f"{len(response.recommendations)} recs (floor={min_distinct})"),
            detail={"attribute": attribute, "distinct": distinct,
                    "min_distinct": min_distinct, "counts": counts},
        ))
    if top_share > max_top_share + _FLOAT_EPS:
        # Find the dominant value for the message.
        dominant = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)
        violations.append(Violation(
            check=f"diversity_top_share_{attribute}",
            severity=severity,
            message=(f"{attribute}={dominant[0]!r} occupies {top_share:.0%} "
                     f"of recs (cap={max_top_share:.0%})"),
            detail={"attribute": attribute, "top_share": top_share,
                    "max_top_share": max_top_share,
                    "dominant_value": dominant[0],
                    "counts": counts},
        ))
    return violations


# Defaults per attribute. Tuned conservatively: catch obvious collapse
# without flagging legitimately-coherent persona alignment.
DIVERSITY_FLOORS = {
    "product_type": {"min_distinct": 2, "max_top_share": 0.6, "severity": "error"},
    "age_group":    {"min_distinct": 1, "max_top_share": 1.0, "severity": "warn"},
    "use_case":     {"min_distinct": 2, "max_top_share": 0.7, "severity": "warn"},
}

DIVERSITY_CHECKS = (
    "diversity_distinct_product_type",
    "diversity_top_share_product_type",
    "diversity_distinct_age_group",
    "diversity_top_share_age_group",
    "diversity_distinct_use_case",
    "diversity_top_share_use_case",
)


def check_diversity_floor(
    response, *, customer_id: str,
    floors: dict[str, dict] | None = None,
) -> dict[str, CheckResult]:
    """Diversity floor across product_type / age_group / use_case.

    Floors are configurable but default to DIVERSITY_FLOORS. age_group
    is intentionally permissive (homogeneity is often correct for a
    coherent persona); product_type collapse is the dominant failure
    mode and is flagged as 'error'.
    """
    floors = floors or DIVERSITY_FLOORS
    out: dict[str, CheckResult] = {}
    for attr, cfg in floors.items():
        violations = _check_diversity_floor(
            response, attribute=attr,
            min_distinct=cfg["min_distinct"],
            max_top_share=cfg["max_top_share"],
            severity=cfg.get("severity", "error"),
        )
        # Each attribute produces 2 named checks; bucket violations by name.
        by_name: dict[str, list[Violation]] = {
            f"diversity_distinct_{attr}": [],
            f"diversity_top_share_{attr}": [],
        }
        for v in violations:
            by_name.setdefault(v.check, []).append(v)
        for name, vs in by_name.items():
            out[name] = CheckResult(
                customer_id=customer_id,
                check=name,
                passed=not any(v.severity == "error" for v in vs),
                violations=vs,
            )
    return out


# --------------------------------------------------------------------------
# Age-coherence floor (Patch B)
# --------------------------------------------------------------------------
#
# Targets the gift_buyer cross-age collapse: a customer whose PURCHASES
# concentrate on one age_group still receives recs that collapse to a
# DIFFERENT age (e.g. infant). We measure the share of the rec set sitting
# on age values the customer barely purchases, and flag when that share is
# high AND the customer has a clear purchase center of mass.
#
# Self-contained on the response: the customer's purchase distribution is
# read from `response.persona.attribute_affinities[attr].distribution` (the
# same normalized distribution the engine scores against); the rec
# distribution is read from `rec.attributes[attr]`. This is the eval-side
# mirror of intent_layer_service.PersonaCoherenceDemoter; the demoter
# corrects the ranking, this check confirms the correction held.

_AGE_COHERENCE_FLOOR = 0.15             # purchase share below this = the
                                        # customer barely buys this value.
_AGE_COHERENCE_DOMINANCE_MIN = 0.40     # only judge customers with a clear
                                        # purchase center of mass.
_AGE_COHERENCE_MAX_OFFDIST_SHARE = 0.5  # at most half the rec set may sit on
                                        # off-distribution values.


def _purchase_distribution(response, attribute_name: str) -> dict[str, float]:
    """The customer's purchase probability distribution for `attribute_name`,
    read from the persona attached to the response. Empty when absent."""
    persona = getattr(response, "persona", None)
    affs = getattr(persona, "attribute_affinities", None) or {}
    aff = affs.get(attribute_name)
    dist = getattr(aff, "distribution", None) if aff is not None else None
    return dict(dist) if dist else {}


def _check_age_coherence_floor(
    response,
    *,
    attribute: str,
    floor: float,
    dominance_min: float,
    max_offdist_share: float,
    severity: str = "error",
) -> list[Violation]:
    """Flag when too large a share of the rec set sits on attribute values
    the customer barely purchases, given a clear purchase center of mass.

    Skips (passes) when there's no purchase signal for the attribute or no
    dominant value — mirroring the demoter's focused-/spread-buyer guard, so
    a genuinely diverse gift-buyer or a cold-start customer is never flagged.
    """
    if not response.recommendations:
        return []
    purchase_dist = _purchase_distribution(response, attribute)
    if not purchase_dist:
        return []  # no purchase signal for this attribute -> nothing to judge
    dominant = max(purchase_dist.values())
    if dominant < dominance_min:
        return []  # no clear center of mass (spread / cold-start) -> skip

    rec_values = [
        v for v in ((rec.attributes or {}).get(attribute)
                    for rec in response.recommendations)
        if v is not None
    ]
    if not rec_values:
        return []
    off = [v for v in rec_values if purchase_dist.get(v, 0.0) < floor]
    off_share = len(off) / len(rec_values)
    if off_share <= max_offdist_share + _FLOAT_EPS:
        return []

    off_counts: dict[str, int] = {}
    for v in off:
        off_counts[v] = off_counts.get(v, 0) + 1
    return [Violation(
        check=f"age_coherence_{attribute}",
        severity=severity,
        message=(f"{off_share:.0%} of recs sit on off-distribution "
                 f"{attribute} value(s) {sorted(set(off))} (each <{floor:.0%} "
                 f"of purchases; cap={max_offdist_share:.0%}); customer's "
                 f"dominant {attribute} purchase share is {dominant:.0%}"),
        detail={"attribute": attribute, "off_share": off_share,
                "max_offdist_share": max_offdist_share, "floor": floor,
                "dominant_purchase_share": dominant,
                "off_value_counts": off_counts,
                "purchase_distribution": purchase_dist},
    )]


AGE_COHERENCE_CHECKS = ("age_coherence_age_group",)


def check_age_coherence(
    response,
    *,
    customer_id: str,
    attribute: str = "age_group",
    floor: float = _AGE_COHERENCE_FLOOR,
    dominance_min: float = _AGE_COHERENCE_DOMINANCE_MIN,
    max_offdist_share: float = _AGE_COHERENCE_MAX_OFFDIST_SHARE,
    severity: str = "error",
) -> dict[str, CheckResult]:
    """Age-coherence floor: the rec set must track the customer's purchase
    distribution on `attribute`, not collapse to an off-distribution value.

    The eval-side counterpart to PersonaCoherenceDemoter. Defaults mirror
    the demoter's knobs so a fixed rec set is judged by the same semantics
    that produced it.
    """
    violations = _check_age_coherence_floor(
        response, attribute=attribute, floor=floor,
        dominance_min=dominance_min, max_offdist_share=max_offdist_share,
        severity=severity,
    )
    name = f"age_coherence_{attribute}"
    return {name: CheckResult(
        customer_id=customer_id,
        check=name,
        passed=not any(v.severity == "error" for v in violations),
        violations=violations,
    )}


def check_weight_conformance(
    response,
    *,
    customer_id: str,
    manifest_weights: dict[str, float],
    persona_confidence: float,
) -> dict[str, CheckResult]:
    """C1/C2/C3 suite: ranking-weight conformance against the manifest.

    `manifest_weights` is the dict of persona_relevant attribute names ->
    score_weight values, read fresh from the loaded manifest. The check
    is dynamic against the manifest -- changing the manifest changes the
    expected values automatically.
    """
    runs = (
        ("manifest_weights_sum",
         _check_manifest_weights_sum(manifest_weights)),
        ("weight_matches_manifest",
         _check_matched_signal_weights(response, manifest_weights)),
        ("contribution_math",
         _check_contribution_math(response)),
        ("persona_fit_math",
         _check_persona_fit_math(response)),
        ("score_math",
         _check_score_math_no_intent(response, persona_confidence)),
    )
    out: dict[str, CheckResult] = {}
    for check_name, violations in runs:
        out[check_name] = CheckResult(
            customer_id=customer_id,
            check=check_name,
            passed=not any(v.severity == "error" for v in violations),
            violations=list(violations),
        )
    return out
