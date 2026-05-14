"""Internal read-only taxonomy admin UI.

Adds:
    GET /admin/taxonomy/                  → HTML page (4 tabs, vanilla JS)
    GET /admin/taxonomy/api/workspaces
    GET /admin/taxonomy/api/aggregates
    GET /admin/taxonomy/api/allowed_values
    GET /admin/taxonomy/api/products
    GET /admin/taxonomy/api/coverage

All endpoints are READ-ONLY. No approval, merge, edit, or write actions.
PersonaFirst remains the system of record; this layer only inspects it.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.product import Product, ProductAttribute
from app.models.proposed_attribute_value import (
    MERGE_REASON_FLATTENED_CHILD,
    MERGE_REASON_NORMALIZED_DUPLICATE,
    MERGE_REASON_SYNONYM,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_MERGED,
    PROPOSAL_STATUS_PENDING,
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.workspace import Workspace
from app.services.attribute_taxonomy_service import get_allowed_values
from app.services.attribute_taxonomy_service import (
    deactivate_allowed_value as _deactivate_allowed_value_service,
)
from app.services.proposed_attribute_value_service import (
    PROMOTION_MIN_AVG_CONFIDENCE,
    PROMOTION_MIN_DISTINCT_PRODUCTS,
    PROMOTION_MIN_PROPOSAL_COUNT,
    approve_aggregate,
    list_review_queue,
    merge_aggregate,
    promotion_readiness,
    queue_health_snapshot,
    reject_aggregate,
)

router = APIRouter(prefix="/admin/taxonomy", tags=["admin"])


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/workspaces")
def list_workspaces(db: Session = Depends(get_db)):
    rows = db.query(Workspace).order_by(Workspace.id).all()
    return [{"id": w.id, "slug": w.slug, "name": w.name} for w in rows]


# ---------------------------------------------------------------------------
# Reviewer queue (Phase 8.5a)
#
# Read-only endpoints exposing the prioritised queue and its health.
# CLI tooling under scripts/review_queue_report.py is the recommended
# entry point; these endpoints are for an admin UI / external dashboard
# integration.
# ---------------------------------------------------------------------------

@router.get("/api/queue")
def get_review_queue(
    workspace_id: int,
    attribute: Optional[str] = None,
    sort_by: str = "priority",
    stale_only: bool = False,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    if sort_by not in {"priority", "age", "count"}:
        raise HTTPException(
            status_code=400,
            detail=f"sort_by must be one of priority/age/count; got {sort_by!r}",
        )
    items = list_review_queue(
        db, workspace_id=workspace_id, attribute_name=attribute,
        sort_by=sort_by, stale_only=stale_only, limit=limit,
    )
    # Each ReviewQueueItem is a plain dataclass; serialise by reading vars().
    return [vars(i) for i in items]


@router.get("/api/queue_health")
def get_queue_health(
    workspace_id: int,
    db: Session = Depends(get_db),
):
    snap = queue_health_snapshot(db, workspace_id=workspace_id)
    return vars(snap)


# ---------------------------------------------------------------------------
# Recommended-for-approval helpers (read-only; never auto-approves).
# ---------------------------------------------------------------------------

# Hardcoded "obviously too broad" set per the brief. These can never be
# recommended even if they meet the count/distinct/confidence thresholds.
_OVERLY_BROAD: frozenset[str] = frozenset({
    "accessory", "clothing", "product", "item", "thing", "stuff",
    "object", "general", "misc", "other", "various",
})

# Recommendation thresholds (separate from engine promotion thresholds —
# a stricter, opinionated bar for what shows up as "recommended").
_REC_MIN_COUNT = 10
_REC_MIN_DISTINCT = 8
_REC_MIN_AVG_CONF = 0.92

_TOKEN_SPLIT_RE = re.compile(r"[_\s\-/]+")
_NORMALIZATION_RE = re.compile(r"[^a-z0-9]+")


def _normalization_signature(cluster_key: str) -> str:
    """Strip non-alphanumeric characters and lowercase. Two cluster_keys
    that produce the same signature are normalization variants of each
    other (e.g. 'lunch_box' and 'lunchbox' both → 'lunchbox')."""
    return _NORMALIZATION_RE.sub("", cluster_key.lower())


def _build_recommendations_index(
    db: Session, workspace_id: int, attribute: str,
    approved_lower: set[str],
) -> tuple[dict[str, list[str]], set[str]]:
    """Return (signature -> [cluster_keys], approved_token_set).
    The signature index is workspace-wide so a paginated response can
    still detect normalization variants that fall outside the page."""
    all_keys = [
        k for (k,) in db.query(ProposedAttributeValueAggregate.cluster_key)
        .filter(
            ProposedAttributeValueAggregate.workspace_id == workspace_id,
            ProposedAttributeValueAggregate.attribute_name == attribute,
        ).all()
    ]
    by_sig: dict[str, list[str]] = {}
    for k in all_keys:
        by_sig.setdefault(_normalization_signature(k), []).append(k)
    return by_sig, approved_lower


def _is_recommended(
    agg: ProposedAttributeValueAggregate,
    sig_index: dict[str, list[str]],
    approved_set: set[str],
    approved_token_sets: list[tuple[str, frozenset[str]]],
) -> bool:
    if agg.status != "pending":
        return False
    if agg.proposal_count < _REC_MIN_COUNT:
        return False
    if agg.distinct_product_count < _REC_MIN_DISTINCT:
        return False
    if agg.avg_confidence < _REC_MIN_AVG_CONF:
        return False
    key_lower = agg.cluster_key.lower()
    if key_lower in _OVERLY_BROAD:
        return False
    # Normalization variant of any other cluster_key in the workspace.
    sig = _normalization_signature(agg.cluster_key)
    if len(sig_index.get(sig, [])) > 1:
        return False
    # Multi-token subtype-of-approved check: cluster is a subtype if every
    # token of any approved canonical also appears in this cluster's token
    # set. Catches single-token cases like 'outdoor_playset' (contains
    # approved 'playset') AND multi-token cases like 'ride_on_toy'
    # (contains approved 'ride_on' = {'ride', 'on'}).
    key_tokens = frozenset(t for t in _TOKEN_SPLIT_RE.split(key_lower) if t)
    for approved_value, approved_tokens in approved_token_sets:
        if (approved_value != key_lower
                and approved_tokens
                and approved_tokens.issubset(key_tokens)):
            return False
    return True


def _approved_token_sets_for(approved: set[str]) -> list[tuple[str, frozenset[str]]]:
    """Pre-tokenise each approved canonical for the multi-token subtype check."""
    out = []
    for v in approved:
        toks = frozenset(t for t in _TOKEN_SPLIT_RE.split(v.lower()) if t)
        out.append((v.lower(), toks))
    return out


# ---------------------------------------------------------------------------
# Merge-suggestion detection. Read-only: every helper here only reads
# existing aggregate / event rows and returns suggestion records. None of
# them touch the database.
# ---------------------------------------------------------------------------

_MERGE_TYPE_NORM = "normalization_variant"
_MERGE_TYPE_PARENT_CHILD = "parent_child"
_MERGE_TYPE_SEMANTIC = "semantic_duplicate"

_VALID_MERGE_TYPES = frozenset({
    _MERGE_TYPE_NORM, _MERGE_TYPE_PARENT_CHILD, _MERGE_TYPE_SEMANTIC,
})

_MERGE_REASON_BY_TYPE = {
    _MERGE_TYPE_NORM: MERGE_REASON_NORMALIZED_DUPLICATE,
    _MERGE_TYPE_PARENT_CHILD: MERGE_REASON_FLATTENED_CHILD,
    _MERGE_TYPE_SEMANTIC: MERGE_REASON_SYNONYM,
}

# Token Jaccard / evidence Jaccard thresholds for semantic_duplicate.
# These were tuned to surface real duplicates (e.g. bottle vs water_bottle)
# without firing on weakly-related families (e.g. art_supply vs office_supply).
_SEMANTIC_TOKEN_JACCARD_MEDIUM = 0.50
_SEMANTIC_EVIDENCE_JACCARD_MEDIUM = 0.40
_SEMANTIC_BOTH_HIGH_FLOOR = 0.50  # both signals above this → confidence=high

# Generic "head noun" parents whose child clusters represent meaningful
# specializations rather than redundant variants. When a parent/child
# detection has the parent matching one of these, the relationship is
# reclassified as a `hierarchy_candidate` (kept for future hierarchy
# modeling) and is NOT surfaced as a merge suggestion.
#
# Why: child values like `tech_accessory`, `gift_bag`, `swim_accessory`
# drive distinct product recommendations from their broad parent. Merging
# them collapses real product-level signal. They should remain separate
# canonicals or eventually become children in a real hierarchy.
#
# Why these specific tokens: this is the closed list of "kitchen-sink"
# umbrella categories observed in the v3 catalog. Tokens like `game`,
# `toy`, `playset`, `ride_on` are intentionally NOT here -- their
# qualifiers (card_game, ride_on_toy) are usually redundant rather than
# semantically distinct.
_HIERARCHY_HEAD_NOUNS: frozenset[str] = frozenset({
    "accessory", "accessories",
    "bag", "bags",
    "supply", "supplies",
    "gear",
    "equipment",
})

# ---------------------------------------------------------------------------
# Direction scoring. For every merge-suggestion pair, both directions
# (A -> B and B -> A) are scored and the higher-scoring direction is
# recommended. The scorer favours specific, evidence-aligned, non-ambiguous
# targets and penalises generic head nouns even when they are already
# approved -- approval is a tie-breaker, not an override.
# ---------------------------------------------------------------------------

# Cluster keys treated as semantically ambiguous when used as a merge
# target. Hard penalty on direction score. `bottle` is here because the
# v3 catalog evidence shows the same canonical covers feeding, water, and
# storage bottles -- merging into `bottle` collapses real product-level
# signal. `accessory`, `bag`, etc. carry the same problem.
_AMBIGUOUS_AS_TARGET: frozenset[str] = frozenset({
    "bottle",
    "accessory", "accessories",
    "bag", "bags",
    "clothing",
    "item", "items",
    "product", "products",
    "thing", "things",
    "stuff",
    "general",
    "misc",
    "other",
})

# Tokens that, when they are the ONLY extra token a source carries over a
# target, mark the source as a redundant variant of the target rather than
# a meaningful subtype. Example: ride_on_toy -> ride_on (extra token = toy).
# Distinct from _AMBIGUOUS_AS_TARGET: a token can be an OK target on its
# own (e.g. `set` is fine as a category) but adding it as a suffix to an
# existing canonical typically does not change meaning.
_REDUNDANT_QUALIFIER_TOKENS: frozenset[str] = frozenset({
    "toy", "toys",
    "item", "items",
    "product", "products",
    "thing", "things",
    "type", "types",
    "general",
})

# Direction score thresholds for `direction_confidence`.
_DIRECTION_HIGH_GAP = 2.0
_DIRECTION_MEDIUM_GAP = 0.5


def _direction_score(
    *,
    target: ProposedAttributeValueAggregate,
    source: ProposedAttributeValueAggregate,
    target_tokens: frozenset[str],
    source_tokens: frozenset[str],
    target_evid: frozenset[str],
    source_evid: frozenset[str],
    approved_lower: set[str],
) -> tuple[float, list[str]]:
    """Score one merge direction (source -> target). Higher = better target.

    Returns (score, notes). Notes are short human-readable factor labels
    so the API caller can see exactly why a direction was preferred.
    """
    notes: list[str] = []
    score = 0.0
    target_key = target.cluster_key.lower()

    # 1. Specificity penalty: target is a generic/ambiguous head noun.
    if target_key in _AMBIGUOUS_AS_TARGET:
        score -= 3.0
        notes.append(f"target {target.cluster_key!r} is generic/ambiguous (-3.0)")

    # 2. Multi-token target carries more semantic information.
    extra_specificity = max(0, len(target_tokens) - 1)
    if extra_specificity > 0:
        bonus = 0.5 * extra_specificity
        score += bonus
        notes.append(f"target is multi-token (+{bonus:.1f})")

    # 2b. Normalization-variant override. When source and target collapse
    # to the same alphanumeric signature (e.g. `bathrobe` vs `bath_robe`,
    # `lunchbox` vs `lunch_box`), prefer the form that preserves word
    # boundaries -- it is the cleaner canonical spelling regardless of
    # how the raw evidence text happens to tokenise. Without this rule,
    # a one-word evidence corpus would unfairly penalise the underscored
    # form via the evidence-alignment factor.
    if (_normalization_signature(target.cluster_key)
            == _normalization_signature(source.cluster_key)):
        token_diff = len(target_tokens) - len(source_tokens)
        if token_diff > 0:
            score += 3.0
            notes.append(
                "normalization variant: target preserves word boundaries (+3.0)"
            )
        elif token_diff < 0:
            score -= 3.0
            notes.append(
                "normalization variant: target lacks word boundaries (-3.0)"
            )

    # 3. Evidence alignment: how many target tokens appear in the
    #    combined evidence vocabulary of the pair. A target whose name
    #    matches dominant evidence terms is the right canonical.
    combined_evid = (source_evid or frozenset()) | (target_evid or frozenset())
    if target_tokens and combined_evid:
        align = sum(1 for t in target_tokens if t in combined_evid) / len(target_tokens)
        if align > 0:
            score += 2.0 * align
            notes.append(f"evidence alignment {align:.2f} (+{2*align:.2f})")

    # 4. If source's extra tokens are all redundant qualifiers, this
    #    direction is the safer one (source collapses cleanly into target).
    extra = source_tokens - target_tokens
    if extra and extra.issubset(_REDUNDANT_QUALIFIER_TOKENS):
        score += 1.5
        notes.append(
            f"source adds only redundant qualifier {sorted(extra)} (+1.5)"
        )

    # 5. Count tiebreaker (small).
    if target.proposal_count > source.proposal_count:
        score += 0.2
    # 6. Confidence tiebreaker (very small).
    if target.avg_confidence >= source.avg_confidence:
        score += 0.1

    # 7. Approved bonus -- ONLY when target is not also flagged ambiguous.
    #    A previously-approved generic target should not be auto-recommended
    #    just because it exists; the specificity penalty must dominate.
    if (target.canonical_value.lower() in approved_lower
            and target_key not in _AMBIGUOUS_AS_TARGET):
        score += 0.5
        notes.append("target already approved (+0.5)")

    return score, notes


def _pick_direction(
    *,
    agg_a: ProposedAttributeValueAggregate,
    agg_b: ProposedAttributeValueAggregate,
    approved_lower: set[str],
) -> dict:
    """Score both directions and return the recommended one + the runner-up.

    Returned keys: source, target, alt_source, alt_target, score, alt_score,
    notes (chosen direction's factor list), alt_notes.
    """
    tokens_a = _tokens_of(agg_a.cluster_key)
    tokens_b = _tokens_of(agg_b.cluster_key)
    evid_a = _evidence_words(agg_a)
    evid_b = _evidence_words(agg_b)

    score_ab, notes_ab = _direction_score(
        target=agg_b, source=agg_a,
        target_tokens=tokens_b, source_tokens=tokens_a,
        target_evid=evid_b, source_evid=evid_a,
        approved_lower=approved_lower,
    )
    score_ba, notes_ba = _direction_score(
        target=agg_a, source=agg_b,
        target_tokens=tokens_a, source_tokens=tokens_b,
        target_evid=evid_a, source_evid=evid_b,
        approved_lower=approved_lower,
    )
    if score_ab >= score_ba:
        return {
            "source": agg_a, "target": agg_b,
            "alt_source": agg_b, "alt_target": agg_a,
            "score": score_ab, "alt_score": score_ba,
            "notes": notes_ab, "alt_notes": notes_ba,
        }
    return {
        "source": agg_b, "target": agg_a,
        "alt_source": agg_a, "alt_target": agg_b,
        "score": score_ba, "alt_score": score_ab,
        "notes": notes_ba, "alt_notes": notes_ab,
    }


def _direction_confidence_label(score: float, alt_score: float) -> str:
    gap = score - alt_score
    if gap >= _DIRECTION_HIGH_GAP:
        return "high"
    if gap >= _DIRECTION_MEDIUM_GAP:
        return "medium"
    return "low"


def _alt_risk_summary(
    alt_target: ProposedAttributeValueAggregate,
    alt_notes: list[str],
    score: float,
    alt_score: float,
    approved_lower: set[str],
) -> str:
    """Short single-line explanation of why the alternative direction is
    not the recommendation."""
    parts: list[str] = []
    alt_key = alt_target.cluster_key.lower()
    if alt_key in _AMBIGUOUS_AS_TARGET:
        parts.append(
            f"alternative target {alt_target.cluster_key!r} is generic/ambiguous"
        )
    if alt_target.canonical_value.lower() not in approved_lower:
        parts.append("alternative target is not approved")
    parts.append(f"direction score gap = {score - alt_score:+.2f}")
    return "; ".join(parts)

# Common short words filtered when building evidence vocabularies.
_EVIDENCE_STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "into", "this", "that", "are",
    "was", "but", "not", "you", "your", "all", "any", "has", "had",
    "have", "use", "off", "out", "via", "per", "etc",
})


def _tokens_of(s: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_SPLIT_RE.split((s or "").lower()) if t)


def _evidence_words(agg: ProposedAttributeValueAggregate) -> frozenset[str]:
    words: set[str] = set()
    for ev in (agg.sample_evidence or []):
        for w in re.findall(r"[a-z]{3,}", (ev or "").lower()):
            if w in _EVIDENCE_STOPWORDS:
                continue
            words.add(w)
    return frozenset(words)


def _suggestion_record(
    *, source: ProposedAttributeValueAggregate,
    target: ProposedAttributeValueAggregate,
    merge_type: str, confidence: str, reason: str,
    risk_notes: list[str], approved_lower: set[str],
) -> dict:
    """Build a merge-suggestion record. The (source, target) passed in is
    the *initially detected* direction. This function re-scores both
    directions via `_pick_direction` and may flip them so the recommended
    canonical target is the one that survives specificity / evidence /
    redundancy checks. The runner-up is exposed as `alternative_direction`.
    """
    direction = _pick_direction(
        agg_a=source, agg_b=target, approved_lower=approved_lower,
    )
    chosen_source = direction["source"]
    chosen_target = direction["target"]
    alt_source = direction["alt_source"]
    alt_target = direction["alt_target"]

    target_approved = chosen_target.canonical_value.lower() in approved_lower

    # Carry over the lexical risk notes the detector already produced, but
    # re-derive direction-specific risks against the (possibly flipped)
    # final target so the UI never shows stale "target is not approved"
    # text against the wrong cluster.
    risks: list[str] = []
    direction_specific = {"target is not approved", "approve target first"}
    for note in risk_notes:
        nl = note.lower()
        if any(d in nl for d in direction_specific):
            continue
        risks.append(note)
    if not target_approved:
        risks.append("target is not approved — approve target first")

    direction_confidence = _direction_confidence_label(
        direction["score"], direction["alt_score"]
    )
    direction_reason = "; ".join(direction["notes"]) or (
        f"direction score {direction['score']:+.2f}"
    )
    alt_risk = _alt_risk_summary(
        alt_target=alt_target,
        alt_notes=direction["alt_notes"],
        score=direction["score"],
        alt_score=direction["alt_score"],
        approved_lower=approved_lower,
    )

    return {
        "source_cluster": chosen_source.cluster_key,
        "target_cluster": chosen_target.cluster_key,
        "source_aggregate_id": chosen_source.id,
        "target_aggregate_id": chosen_target.id,
        "merge_type": merge_type,
        "confidence": confidence,
        "reason": reason,
        "source_status": chosen_source.status,
        "target_status": chosen_target.status,
        "source_count": chosen_source.proposal_count,
        "target_count": chosen_target.proposal_count,
        "combined_count": chosen_source.proposal_count + chosen_target.proposal_count,
        "sample_source_products": list(chosen_source.sample_product_ids or [])[:5],
        "sample_target_products": list(chosen_target.sample_product_ids or [])[:5],
        "sample_source_evidence": list(chosen_source.sample_evidence or [])[:3],
        "sample_target_evidence": list(chosen_target.sample_evidence or [])[:3],
        "risk_notes": risks,
        "executable": target_approved,
        # Direction-scoring fields (see `_direction_score`).
        "recommended_source": chosen_source.cluster_key,
        "recommended_target": chosen_target.cluster_key,
        "direction_confidence": direction_confidence,
        "direction_reason": direction_reason,
        "direction_score": round(direction["score"], 3),
        "alternative_direction": {
            "source": alt_source.cluster_key,
            "target": alt_target.cluster_key,
            "risk": alt_risk,
            "score": round(direction["alt_score"], 3),
        },
    }


def _detect_merge_suggestions(
    db: Session, workspace_id: int, attribute: str,
) -> tuple[list[dict], list[dict]]:
    """Return (merge_suggestions, hierarchy_candidates).

    Merge suggestions are surfaced in the UI for reviewer action.
    Hierarchy candidates are parent/child detections that have been
    reclassified because the parent is a generic head noun (see
    `_HIERARCHY_HEAD_NOUNS`) and the child likely represents a meaningful
    subtype rather than a redundant variant. They are returned to the
    caller for future hierarchy work but NOT shown as merge candidates.
    """
    aggs = (db.query(ProposedAttributeValueAggregate)
            .filter(ProposedAttributeValueAggregate.workspace_id == workspace_id,
                    ProposedAttributeValueAggregate.attribute_name == attribute)
            .all())
    if not aggs:
        return [], []
    approved_lower = {v.lower() for v in get_allowed_values(db, workspace_id, attribute)}

    # Track pairs we've already emitted so a higher-confidence detector
    # wins over a lower-confidence one for the same (source, target) pair.
    seen_pairs: dict[tuple[str, str], dict] = {}

    def remember(rec: dict) -> None:
        key = tuple(sorted((rec["source_cluster"], rec["target_cluster"])))
        prev = seen_pairs.get(key)
        if prev is None:
            seen_pairs[key] = rec
            return
        # Priority: normalization_variant > parent_child > semantic_duplicate
        priority = {_MERGE_TYPE_NORM: 0, _MERGE_TYPE_PARENT_CHILD: 1, _MERGE_TYPE_SEMANTIC: 2}
        if priority[rec["merge_type"]] < priority[prev["merge_type"]]:
            seen_pairs[key] = rec

    # 1. Normalization variants: clusters sharing the same alphanumeric signature.
    sig_groups: dict[str, list[ProposedAttributeValueAggregate]] = {}
    for a in aggs:
        sig_groups.setdefault(_normalization_signature(a.cluster_key), []).append(a)
    for sig, group in sig_groups.items():
        if len(group) < 2:
            continue
        # Pick target preferring approved; tie-break on count.
        ranked = sorted(group, key=lambda a: (
            0 if a.canonical_value.lower() in approved_lower else 1,
            -a.proposal_count,
            a.canonical_value,
        ))
        target = ranked[0]
        for source in ranked[1:]:
            risks: list[str] = []
            if target.canonical_value.lower() not in approved_lower:
                risks.append("target is not yet approved — approve target first")
            reason = (
                f"Same alphanumeric signature {sig!r}; "
                f"target {target.cluster_key!r} preferred ("
                f"{'approved' if target.canonical_value.lower() in approved_lower else 'higher count'}"
                f", {target.proposal_count} vs {source.proposal_count})."
            )
            remember(_suggestion_record(
                source=source, target=target,
                merge_type=_MERGE_TYPE_NORM, confidence="high",
                reason=reason, risk_notes=risks, approved_lower=approved_lower,
            ))

    # 2. Parent/child: tokens(parent) ⊊ tokens(child).
    # When the parent's cluster_key is a generic head noun (accessory,
    # bag, supply, ...), reclassify as `hierarchy_candidate` instead of
    # emitting a merge suggestion -- the child almost always represents
    # meaningful product specialization that drives distinct
    # recommendations (e.g. tech_accessory, swim_accessory, diaper_bag).
    hierarchy_seen: set[tuple[str, str]] = set()
    hierarchy_candidates: list[dict] = []
    by_id_tokens = [(a, _tokens_of(a.cluster_key)) for a in aggs]
    for child, c_toks in by_id_tokens:
        if not c_toks:
            continue
        for parent, p_toks in by_id_tokens:
            if parent.id == child.id or not p_toks:
                continue
            if p_toks == c_toks:
                continue
            if not p_toks.issubset(c_toks):
                continue
            # Suppress when the child cluster contains any generic
            # head-noun token (accessory, bag, supply, ...). Such children
            # represent meaningful "X-related Y" categories regardless of
            # which subset parent we'd merge into. Examples reclassified
            # to hierarchy_candidate:
            #   tech_accessory       -> accessory
            #   stroller_accessory   -> stroller   (child still has 'accessory')
            #   diaper_bag           -> diaper     (child still has 'bag')
            #   gift_bag             -> bag
            # Examples NOT suppressed (no head-noun token in child):
            #   ride_on_toy          -> ride_on
            #   outdoor_playset      -> playset
            #   card_game            -> game
            #   water_bottle         -> bottle
            if c_toks & _HIERARCHY_HEAD_NOUNS:
                pair_key = (child.cluster_key, parent.cluster_key)
                if pair_key in hierarchy_seen:
                    continue
                hierarchy_seen.add(pair_key)
                hierarchy_candidates.append({
                    "relationship_type": "hierarchy_candidate",
                    "source_cluster": child.cluster_key,
                    "parent_cluster": parent.cluster_key,
                    "reason": "Meaningful subtype; should not be merged automatically",
                })
                continue
            # Default direction: child → parent.
            risks: list[str] = []
            if child.status != PROPOSAL_STATUS_PENDING:
                risks.append(
                    f"source aggregate is {child.status!r}; expected pending."
                )
            if parent.canonical_value.lower() not in approved_lower:
                risks.append("parent is not approved — approve target first")
            if child.proposal_count > parent.proposal_count:
                risks.append(
                    f"child count ({child.proposal_count}) exceeds parent ({parent.proposal_count});"
                    " consider whether the parent is still the right canonical."
                )
            reason = (
                f"Token set {{{', '.join(sorted(c_toks))}}} is a strict superset of "
                f"{{{', '.join(sorted(p_toks))}}}; child collapses into parent."
            )
            remember(_suggestion_record(
                source=child, target=parent,
                merge_type=_MERGE_TYPE_PARENT_CHILD, confidence="high",
                reason=reason, risk_notes=risks, approved_lower=approved_lower,
            ))

    # 3. Semantic duplicates: token Jaccard + evidence-word Jaccard.
    n = len(aggs)
    for i in range(n):
        a = aggs[i]
        a_toks = _tokens_of(a.cluster_key)
        a_words = _evidence_words(a)
        if not a_toks:
            continue
        for j in range(i + 1, n):
            b = aggs[j]
            b_toks = _tokens_of(b.cluster_key)
            if not b_toks:
                continue
            # Subset relation is parent/child territory (already emitted above).
            if a_toks.issubset(b_toks) or b_toks.issubset(a_toks):
                continue
            inter = a_toks & b_toks
            if not inter:
                # No shared token in cluster_key — also need evidence overlap
                # to even consider semantic duplicate.
                pass
            union = a_toks | b_toks
            tok_j = len(inter) / len(union) if union else 0.0
            b_words = _evidence_words(b)
            if a_words or b_words:
                ev_inter = len(a_words & b_words)
                ev_union = len(a_words | b_words)
                ev_j = ev_inter / ev_union if ev_union else 0.0
            else:
                ev_j = 0.0
            qualifies = (
                tok_j >= _SEMANTIC_TOKEN_JACCARD_MEDIUM
                or ev_j >= _SEMANTIC_EVIDENCE_JACCARD_MEDIUM
            )
            if not qualifies:
                continue
            confidence = (
                "high" if (tok_j >= _SEMANTIC_BOTH_HIGH_FLOOR
                           and ev_j >= _SEMANTIC_BOTH_HIGH_FLOOR)
                else "medium"
            )
            # Pick target: approved over pending; tie on count.
            a_app = a.canonical_value.lower() in approved_lower
            b_app = b.canonical_value.lower() in approved_lower
            if a_app and not b_app:
                source, target = b, a
            elif b_app and not a_app:
                source, target = a, b
            elif a.proposal_count >= b.proposal_count:
                source, target = b, a
            else:
                source, target = a, b
            risks: list[str] = []
            if target.canonical_value.lower() not in approved_lower:
                risks.append("target is not approved — approve target first")
            if confidence == "medium":
                risks.append(
                    "medium-confidence semantic match — verify evidence side-by-side before merging"
                )
            reason = (
                f"Token Jaccard={tok_j:.2f}, evidence Jaccard={ev_j:.2f} "
                f"(shared tokens: {sorted(inter) or '[]'})."
            )
            remember(_suggestion_record(
                source=source, target=target,
                merge_type=_MERGE_TYPE_SEMANTIC, confidence=confidence,
                reason=reason, risk_notes=risks, approved_lower=approved_lower,
            ))

    # Sort: high-conf first, then by combined_count desc, then by merge_type priority.
    type_priority = {_MERGE_TYPE_NORM: 0, _MERGE_TYPE_PARENT_CHILD: 1, _MERGE_TYPE_SEMANTIC: 2}
    out = list(seen_pairs.values())
    out.sort(key=lambda r: (
        0 if r["confidence"] == "high" else 1,
        type_priority[r["merge_type"]],
        -r["combined_count"],
        r["source_cluster"],
    ))
    hierarchy_candidates.sort(key=lambda r: (r["parent_cluster"], r["source_cluster"]))
    return out, hierarchy_candidates


@router.get("/api/merge_suggestions")
def list_merge_suggestions(
    workspace_id: int,
    attribute: str = "product_type",
    db: Session = Depends(get_db),
):
    """Read-only merge-suggestion list. No DB writes.

    Returns suggestions grouped by detector type (normalization_variant,
    parent_child, semantic_duplicate) with all the structural fields the
    UI needs to render side-by-side review and a single "Approve Merge"
    click.

    `hierarchy_candidates` is a separate list of parent/child pairs that
    were intentionally suppressed from `items` because the parent is a
    generic umbrella category (accessory, bag, supply, ...) and the child
    likely represents meaningful product specialization. The UI does not
    render these today; they are exposed for future hierarchy work.
    """
    suggestions, hierarchy_candidates = _detect_merge_suggestions(
        db, workspace_id, attribute
    )
    by_type: dict[str, list[dict]] = {
        _MERGE_TYPE_NORM: [], _MERGE_TYPE_PARENT_CHILD: [], _MERGE_TYPE_SEMANTIC: [],
    }
    for s in suggestions:
        by_type[s["merge_type"]].append(s)
    return {
        "total": len(suggestions),
        "by_type_count": {k: len(v) for k, v in by_type.items()},
        "items": suggestions,
        "hierarchy_candidates": hierarchy_candidates,
        "hierarchy_candidate_count": len(hierarchy_candidates),
    }


@router.get("/api/aggregates")
def list_aggregates(
    workspace_id: int,
    attribute: str = "product_type",
    status: Optional[str] = None,
    search: Optional[str] = None,
    recommended_only: bool = False,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    # Pre-compute the workspace-wide variant index + approved set so the
    # recommendation flag is consistent regardless of pagination.
    approved_set = {v.lower() for v in get_allowed_values(db, workspace_id, attribute)}
    sig_index, _ = _build_recommendations_index(db, workspace_id, attribute, approved_set)
    approved_token_sets = _approved_token_sets_for(approved_set)

    q = db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id,
        ProposedAttributeValueAggregate.attribute_name == attribute,
    )
    if status:
        q = q.filter(ProposedAttributeValueAggregate.status == status)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            ProposedAttributeValueAggregate.cluster_key.ilike(like),
            ProposedAttributeValueAggregate.canonical_value.ilike(like),
        ))
    if recommended_only:
        # Apply the threshold floors at the SQL layer so we don't paginate
        # through thousands of low-count aggregates first.
        q = q.filter(
            ProposedAttributeValueAggregate.status == "pending",
            ProposedAttributeValueAggregate.proposal_count >= _REC_MIN_COUNT,
            ProposedAttributeValueAggregate.distinct_product_count >= _REC_MIN_DISTINCT,
            ProposedAttributeValueAggregate.avg_confidence >= _REC_MIN_AVG_CONF,
        )
    rows = (q.order_by(ProposedAttributeValueAggregate.proposal_count.desc(),
                       ProposedAttributeValueAggregate.canonical_value.asc())
              .all())
    if recommended_only:
        rows = [a for a in rows if _is_recommended(a, sig_index, approved_set, approved_token_sets)]
    total = len(rows) if recommended_only else q.count()
    page = rows[offset: offset + limit] if recommended_only else (
        # When not filtering, page from the original query (more efficient).
        q.order_by(ProposedAttributeValueAggregate.proposal_count.desc(),
                   ProposedAttributeValueAggregate.canonical_value.asc())
         .offset(offset).limit(limit).all()
    )
    items = []
    for a in page:
        check = promotion_readiness(a)
        items.append({
            "id": a.id,
            "cluster_key": a.cluster_key,
            "canonical_value": a.canonical_value,
            "count": a.proposal_count,
            "distinct_products": a.distinct_product_count,
            "avg_conf": round(a.avg_confidence, 3),
            "status": a.status,
            "promoted_to_allowed_value": a.promoted_to_allowed_value,
            "ready": check.ready,
            "recommended_for_approval": _is_recommended(a, sig_index, approved_set, approved_token_sets),
            "sample_evidence": list(a.sample_evidence or [])[:3],
            "sample_product_ids": list(a.sample_product_ids or [])[:5],
        })
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "thresholds": {
            "min_count": PROMOTION_MIN_PROPOSAL_COUNT,
            "min_distinct": PROMOTION_MIN_DISTINCT_PRODUCTS,
            "min_avg_conf": PROMOTION_MIN_AVG_CONFIDENCE,
        },
        "items": items,
    }


@router.get("/api/allowed_values")
def list_allowed_values(
    workspace_id: int,
    attribute: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == workspace_id,
    )
    if attribute:
        q = q.filter(AttributeAllowedValue.attribute_name == attribute)
    rows = q.order_by(AttributeAllowedValue.attribute_name,
                      AttributeAllowedValue.value).all()
    return [
        {
            "attribute_name": r.attribute_name,
            "value": r.value,
            "is_active": getattr(r, "is_active", True),
            "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
        }
        for r in rows
    ]


@router.get("/api/products")
def list_products(
    workspace_id: int,
    attribute: str = "product_type",
    status: Optional[str] = None,   # assigned | pending | missing
    search: Optional[str] = None,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    # Subquery: assigned value (ProductAttribute).
    assigned_subq = (
        db.query(ProductAttribute.product_id, ProductAttribute.attribute_value)
        .filter(ProductAttribute.attribute_id == attribute)
        .subquery()
    )

    # Build base list of products in workspace.
    base = db.query(Product).filter(Product.workspace_id == workspace_id)
    if search:
        like = f"%{search}%"
        base = base.filter(or_(
            Product.product_id.ilike(like),
            Product.name.ilike(like),
            Product.sku.ilike(like),
        ))
    products = base.order_by(Product.product_id).all()

    # Per-product join to assigned value + most-confident event.
    assigned_map = {
        pid: val for pid, val in db.query(
            assigned_subq.c.product_id, assigned_subq.c.attribute_value
        ).all()
    }
    # Map ProductAttribute.product_id (db_id) → Product.product_id (external).
    db_to_ext = {p.id: p.product_id for p in products}

    # Best (highest-confidence) event per external product_id.
    event_q = (
        db.query(
            ProposedAttributeValueEvent.product_id,
            ProposedAttributeValueEvent.normalized_value,
            ProposedAttributeValueEvent.confidence,
        )
        .filter(
            ProposedAttributeValueEvent.workspace_id == workspace_id,
            ProposedAttributeValueEvent.attribute_name == attribute,
        )
        .all()
    )
    proposed_map: dict[str, tuple[str, float]] = {}
    for ext_pid, normalized, conf in event_q:
        prev = proposed_map.get(ext_pid)
        if prev is None or float(conf or 0) > prev[1]:
            proposed_map[ext_pid] = (normalized, float(conf or 0))

    rows = []
    for p in products:
        assigned = assigned_map.get(p.id)  # uses db_id key
        proposed = proposed_map.get(p.product_id)
        if assigned:
            st = "assigned"
        elif proposed:
            st = "pending"
        else:
            st = "missing"
        if status and st != status:
            continue
        rows.append({
            "product_id": p.product_id,
            "name": p.name,
            "brand": None,  # not stored on Product (raw_metadata only)
            "assigned_value": assigned,
            "proposed_value": proposed[0] if proposed else None,
            "proposed_confidence": round(proposed[1], 3) if proposed else None,
            "status": st,
        })

    total = len(rows)
    page = rows[offset: offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "items": page}


# ---------------------------------------------------------------------------
# Mutation endpoints — read the safety-check rules in the docstring before
# changing. All writes flow through proposed_attribute_value_service; this
# router never touches AttributeAllowedValue, ProposedAttributeValueAggregate,
# or ProductAttribute directly.
# ---------------------------------------------------------------------------

def _load_pending_aggregate(
    db: Session, aggregate_id: int, workspace_id: int, attribute: str,
) -> ProposedAttributeValueAggregate:
    """Shared safety wrapper. Raises HTTPException for every disallowed
    state — the caller never has to interpret None or sentinel values."""
    agg = db.get(ProposedAttributeValueAggregate, aggregate_id)
    if agg is None:
        raise HTTPException(404, f"aggregate {aggregate_id} not found")
    if agg.workspace_id != workspace_id:
        raise HTTPException(
            400,
            f"aggregate workspace mismatch: aggregate.workspace_id={agg.workspace_id}, "
            f"requested workspace_id={workspace_id}",
        )
    if agg.attribute_name != attribute:
        raise HTTPException(
            400,
            f"aggregate attribute mismatch: aggregate.attribute_name="
            f"{agg.attribute_name!r}, requested attribute={attribute!r}",
        )
    if agg.status != PROPOSAL_STATUS_PENDING:
        raise HTTPException(
            409,
            f"aggregate {agg.cluster_key!r} is not pending "
            f"(status={agg.status!r}); approve/reject is only valid on pending rows.",
        )
    return agg


@router.post("/api/aggregates/{aggregate_id}/approve")
def approve_aggregate_action(
    aggregate_id: int,
    workspace_id: int,
    attribute: str = "product_type",
    force: bool = False,
    synonym_additions: Optional[list[dict]] = Body(default=None),
    db: Session = Depends(get_db),
):
    """Promote a pending aggregate via the engine's approve_aggregate
    service. force=False by default — the readiness gate is enforced
    unless the caller explicitly opts out.

    Engine path:
        proposed_attribute_value_service.approve_aggregate
            → flips aggregate.status to 'approved'
            → calls attribute_taxonomy_service.upsert_allowed_value
            → optionally writes synonym overrides (Phase 8.5b)
            → returns extended allowed_values list + ApprovalReport

    Audit: review_note is recorded on the aggregate.
    Phase 8.5b: response now carries `raw_variants_seen` and
    `suggested_synonym_additions` so a caller can decide which raw forms
    to harden as synonyms. The body field `synonym_additions` accepts
    the reviewer's choice; matching rows are written to
    `attribute_synonym_overrides`.
    """
    agg = _load_pending_aggregate(db, aggregate_id, workspace_id, attribute)
    if not force:
        check = promotion_readiness(agg)
        if not check.ready:
            raise HTTPException(
                422,
                "aggregate is not ready for promotion: " + "; ".join(check.reasons)
                + " (pass force=true to override)",
            )

    current_allowed = list(get_allowed_values(db, workspace_id, attribute))
    try:
        updated_agg, new_allowed, report = approve_aggregate(
            db,
            aggregate_id=agg.id,
            current_allowed_values=current_allowed,
            force=force,
            review_note="Approved from taxonomy admin UI",
            synonym_additions=synonym_additions,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {
        "aggregate_id": updated_agg.id,
        "cluster_key": updated_agg.cluster_key,
        "canonical_value": updated_agg.canonical_value,
        "status": updated_agg.status,
        "promoted_to_allowed_value": updated_agg.promoted_to_allowed_value,
        "review_note": updated_agg.review_note,
        "allowed_values_after": list(get_allowed_values(db, workspace_id, attribute)),
        "raw_variants_seen": report.raw_variants_seen,
        "suggested_synonym_additions": report.suggested_synonym_additions,
        "synonyms_added": report.synonyms_added,
    }


@router.post("/api/aggregates/{aggregate_id}/reject")
def reject_aggregate_action(
    aggregate_id: int,
    workspace_id: int,
    attribute: str = "product_type",
    merge_reason: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Mark a pending aggregate as rejected via the engine's
    reject_aggregate service. merge_reason is optional metadata
    (e.g. 'noise') for the audit trail; it does not perform a merge.

    Engine path:
        proposed_attribute_value_service.reject_aggregate
            → flips aggregate.status to 'rejected'
            → clears promoted_to_allowed_value
            → preserves underlying ProposedAttributeValueEvent rows

    Audit: review_note is recorded on the aggregate.
    """
    agg = _load_pending_aggregate(db, aggregate_id, workspace_id, attribute)
    try:
        updated_agg = reject_aggregate(
            db,
            aggregate_id=agg.id,
            merge_reason=merge_reason,
            review_note="Rejected from taxonomy admin UI",
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {
        "aggregate_id": updated_agg.id,
        "cluster_key": updated_agg.cluster_key,
        "canonical_value": updated_agg.canonical_value,
        "status": updated_agg.status,
        "merge_reason": updated_agg.merge_reason,
        "review_note": updated_agg.review_note,
    }


# ---------------------------------------------------------------------------
# Merge execution. POST endpoint that re-validates everything and routes
# the actual write through the engine's existing `merge_aggregate` service.
# ProductAttribute backfill is run inline so a single reviewer click leaves
# the workspace in a consistent state.
# ---------------------------------------------------------------------------

class _MergePayload:
    """Lightweight typed accessor over the JSON body. Avoids pydantic
    coupling for one endpoint while keeping a single source of truth for
    field names + validation messages."""
    def __init__(self, body: dict):
        self.workspace_id: int = int(body.get("workspace_id", 0) or 0)
        self.attribute: str = (body.get("attribute") or "product_type").strip()
        self.source_cluster: str = (body.get("source_cluster") or "").strip()
        self.target_cluster: str = (body.get("target_cluster") or "").strip()
        self.merge_type: str = (body.get("merge_type") or "").strip()
        self.review_note: str = (
            body.get("review_note") or "Merged from taxonomy admin UI"
        ).strip()


@router.post("/api/merge_suggestions/execute")
def execute_merge_suggestion(payload: dict, db: Session = Depends(get_db)):
    """Execute one merge suggestion. Re-validates everything from scratch
    (the suggestion list is advisory, not a contract) and routes the
    actual aggregate flip through the engine's `merge_aggregate` service.

    Steps:
        1. Validate inputs (workspace, attribute, merge_type, source/target).
        2. Load source + target aggregates; verify status.
        3. Verify target is approved (target_allowed_value must already be
           in current_allowed_values for merge_aggregate to accept it).
        4. Call merge_aggregate (engine).
        5. If source was approved, deactivate its AttributeAllowedValue
           (so the canonical disappears from get_allowed_values).
        6. Update ProductAttribute rows: source.canonical_value →
           target.canonical_value, dropping rows that would create a
           per-product duplicate of attribute_id="product_type".
    """
    p = _MergePayload(payload or {})

    # Step 1 — input validation.
    if p.workspace_id <= 0:
        raise HTTPException(400, "workspace_id is required")
    if not p.source_cluster or not p.target_cluster:
        raise HTTPException(400, "source_cluster and target_cluster are required")
    if p.source_cluster == p.target_cluster:
        raise HTTPException(400, "source_cluster and target_cluster must differ")
    if p.merge_type not in _VALID_MERGE_TYPES:
        raise HTTPException(
            400,
            f"merge_type must be one of {sorted(_VALID_MERGE_TYPES)}; got {p.merge_type!r}",
        )

    # Step 2 — load aggregates.
    source = (
        db.query(ProposedAttributeValueAggregate)
        .filter(ProposedAttributeValueAggregate.workspace_id == p.workspace_id,
                ProposedAttributeValueAggregate.attribute_name == p.attribute,
                ProposedAttributeValueAggregate.cluster_key == p.source_cluster)
        .first()
    )
    if source is None:
        raise HTTPException(404, f"source aggregate not found for cluster_key={p.source_cluster!r}")
    if source.status not in (PROPOSAL_STATUS_PENDING, PROPOSAL_STATUS_APPROVED):
        raise HTTPException(
            409,
            f"source aggregate must be 'pending' or 'approved'; got {source.status!r}",
        )
    target = (
        db.query(ProposedAttributeValueAggregate)
        .filter(ProposedAttributeValueAggregate.workspace_id == p.workspace_id,
                ProposedAttributeValueAggregate.attribute_name == p.attribute,
                ProposedAttributeValueAggregate.cluster_key == p.target_cluster)
        .first()
    )
    if target is None:
        raise HTTPException(404, f"target aggregate not found for cluster_key={p.target_cluster!r}")

    # Step 3 — target must already be approved (engine merge_aggregate
    # requires target_allowed_value ∈ current_allowed_values).
    current_allowed = list(get_allowed_values(db, p.workspace_id, p.attribute))
    target_canonical = target.canonical_value
    if target_canonical.lower() not in {v.lower() for v in current_allowed}:
        raise HTTPException(
            422,
            f"target aggregate {target.cluster_key!r} is not approved; "
            "approve the target first before merging.",
        )

    source_was_approved = (source.status == PROPOSAL_STATUS_APPROVED)
    source_canonical = source.canonical_value

    # Step 4 — engine merge.
    merge_reason = _MERGE_REASON_BY_TYPE[p.merge_type]
    audit_note = f"{p.review_note} (type={p.merge_type})"
    try:
        merge_aggregate(
            db,
            aggregate_id=source.id,
            target_allowed_value=target_canonical,
            current_allowed_values=current_allowed,
            merge_reason=merge_reason,
            review_note=audit_note,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    # Step 5 — if source was approved, deactivate its AttributeAllowedValue
    # so future enrichment + get_allowed_values stop returning it.
    deactivated = False
    if source_was_approved:
        deactivated = _deactivate_allowed_value_service(
            db, p.workspace_id, p.attribute, source_canonical
        )

    # Step 6 — ProductAttribute backfill.
    pa_updated = 0
    pa_dropped_duplicates = 0
    pa_skipped_no_target = 0

    target_lower = target_canonical.lower()
    # Walk products in this workspace that currently carry the source value.
    rows = (
        db.query(ProductAttribute, Product)
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == p.workspace_id,
                ProductAttribute.attribute_id == p.attribute,
                ProductAttribute.attribute_value == source_canonical)
        .all()
    )
    for pa, prod in rows:
        # If the product already has a row for the target value, drop the
        # source-valued row (no duplicates allowed; one product_type per product).
        existing_target = (
            db.query(ProductAttribute)
            .filter(ProductAttribute.product_id == prod.id,
                    ProductAttribute.attribute_id == p.attribute,
                    func.lower(ProductAttribute.attribute_value) == target_lower)
            .first()
        )
        if existing_target is not None and existing_target.id != pa.id:
            db.delete(pa)
            pa_dropped_duplicates += 1
        else:
            pa.attribute_value = target_canonical
            pa_updated += 1
    db.flush()
    db.commit()

    # Re-read final state for the response.
    updated_source = db.get(ProposedAttributeValueAggregate, source.id)
    updated_target = db.get(ProposedAttributeValueAggregate, target.id)
    return {
        "merge_type": p.merge_type,
        "merge_reason": merge_reason,
        "source": {
            "cluster_key": updated_source.cluster_key,
            "status": updated_source.status,
            "promoted_to_allowed_value": updated_source.promoted_to_allowed_value,
            "merge_reason": updated_source.merge_reason,
            "review_note": updated_source.review_note,
        },
        "target": {
            "cluster_key": updated_target.cluster_key,
            "status": updated_target.status,
            "canonical_value": updated_target.canonical_value,
        },
        "source_allowed_value_deactivated": deactivated,
        "product_attribute": {
            "rows_updated": pa_updated,
            "duplicates_dropped": pa_dropped_duplicates,
            "skipped_target_not_approved": pa_skipped_no_target,
        },
        "allowed_values_after": list(get_allowed_values(db, p.workspace_id, p.attribute)),
    }


@router.get("/api/coverage")
def coverage(
    workspace_id: int,
    attribute: str = "product_type",
    db: Session = Depends(get_db),
):
    total_products = db.query(Product).filter(
        Product.workspace_id == workspace_id
    ).count()
    products_with_attr = (
        db.query(func.count(func.distinct(ProductAttribute.product_id)))
        .join(Product, ProductAttribute.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id,
                ProductAttribute.attribute_id == attribute)
        .scalar() or 0
    )
    approved_value_count = db.query(AttributeAllowedValue).filter(
        AttributeAllowedValue.workspace_id == workspace_id,
        AttributeAllowedValue.attribute_name == attribute,
    ).count()
    pending_agg_count = db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id,
        ProposedAttributeValueAggregate.attribute_name == attribute,
        ProposedAttributeValueAggregate.status == "pending",
    ).count()
    # Long-tail = pending aggregates with proposal_count == 1.
    long_tail_count = db.query(ProposedAttributeValueAggregate).filter(
        ProposedAttributeValueAggregate.workspace_id == workspace_id,
        ProposedAttributeValueAggregate.attribute_name == attribute,
        ProposedAttributeValueAggregate.status == "pending",
        ProposedAttributeValueAggregate.proposal_count == 1,
    ).count()
    missing_count = total_products - products_with_attr
    pct = (100.0 * products_with_attr / total_products) if total_products else 0.0
    return {
        "total_products": total_products,
        "products_with_attribute": products_with_attr,
        "coverage_pct": round(pct, 2),
        "approved_value_count": approved_value_count,
        "pending_aggregate_count": pending_agg_count,
        "long_tail_count": long_tail_count,
        "missing_count": missing_count,
        "attribute": attribute,
    }


# ---------------------------------------------------------------------------
# HTML UI (single page, vanilla JS)
# ---------------------------------------------------------------------------

_HTML = """<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<title>PersonaFirst - Taxonomy Admin</title>
<style>
  body { font-family: -apple-system, system-ui, Arial, sans-serif; margin: 0; padding: 0;
         background: #f6f6f8; color: #1d1d1f; font-size: 13px; }
  header { background: #20232a; color: #fff; padding: 10px 16px; }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  .controls { background: #fff; padding: 10px 16px; border-bottom: 1px solid #ddd;
              display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .controls label { font-size: 12px; color: #555; }
  select, input { padding: 5px 8px; font-size: 13px; border: 1px solid #ccc; border-radius: 3px; }
  .tabs { display: flex; gap: 0; background: #fff; border-bottom: 1px solid #ddd; padding: 0 16px; }
  .tab { padding: 10px 16px; cursor: pointer; border-bottom: 2px solid transparent;
         user-select: none; }
  .tab.active { border-bottom-color: #20232a; font-weight: 600; }
  .tab-content { display: none; padding: 16px; }
  .tab-content.active { display: block; }
  .filters { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; flex-wrap: wrap; }
  table { border-collapse: collapse; width: 100%; background: #fff; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee;
           font-size: 12px; vertical-align: top; }
  th { background: #f0f0f3; font-weight: 600; position: sticky; top: 0; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.code { font-family: ui-monospace, Menlo, Consolas, monospace; }
  .pill { display: inline-block; padding: 2px 6px; border-radius: 8px;
          font-size: 11px; font-weight: 600; }
  .pill.assigned { background: #d1f0d6; color: #15601e; }
  .pill.pending  { background: #ffe6b3; color: #8a4a00; }
  .pill.missing  { background: #ffd6d6; color: #9b1c1c; }
  .pill.approved { background: #d1f0d6; color: #15601e; }
  .pill.merged   { background: #ddd; color: #555; }
  .pill.rejected { background: #ffd6d6; color: #9b1c1c; }
  .pill.ready    { background: #cfe7ff; color: #14457e; }
  .pill.recommended { background: #ffe9b3; color: #6e4400; border: 1px solid #d2a040; }
  .pill.normalization_variant { background: #d6e7ff; color: #1c4e9b; }
  .pill.parent_child          { background: #e0d4f7; color: #4a2a8a; }
  .pill.semantic_duplicate    { background: #ffe0d6; color: #8a3b14; }
  .pill.high   { background: #d1f0d6; color: #15601e; }
  .pill.medium { background: #ffe6b3; color: #8a4a00; }
  .pager { margin-top: 8px; display: flex; gap: 8px; align-items: center; }
  .metric { display: inline-block; background: #fff; padding: 12px 16px; margin: 6px;
            border-radius: 4px; border: 1px solid #e5e5ea; min-width: 160px; }
  .metric .label { font-size: 11px; color: #666; }
  .metric .value { font-size: 22px; font-weight: 600; margin-top: 2px;
                   font-variant-numeric: tabular-nums; }
  small.dim { color: #888; }
  small.evid { color: #555; font-style: italic; }
  #errbar { display: none; background: #ffd6d6; color: #9b1c1c;
            padding: 8px 16px; font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 12px; white-space: pre-wrap; border-bottom: 1px solid #d49a9a; }
  #errbar.visible { display: block; }
  #status { color: #888; font-size: 11px; margin-left: auto; }
  td.actions { white-space: nowrap; }
  button.act { font-size: 11px; padding: 3px 8px; margin-right: 4px;
               border: 1px solid #ccc; background: #fff; cursor: pointer;
               border-radius: 3px; }
  button.act:disabled { opacity: 0.35; cursor: not-allowed; }
  button.act-approve:not(:disabled) { background: #d1f0d6; border-color: #99c79f; color: #15601e; }
  button.act-reject:not(:disabled)  { background: #ffd6d6; border-color: #d49a9a; color: #9b1c1c; }
</style>
</head>
<body>
<header>
  <h1>PersonaFirst - Taxonomy Admin (read-only)</h1>
</header>
<div id=errbar></div>
<div class=controls>
  <label>Workspace</label>
  <select id=ws><option value="">(loading...)</option></select>
  <label>Attribute</label>
  <input id=attr value="product_type" size=18>
  <button onclick="refreshAll()">Refresh</button>
  <small class=dim id=ctxinfo></small>
  <small id=status></small>
</div>
<div class=tabs>
  <div class="tab active" data-tab=aggregates>Aggregates</div>
  <div class=tab data-tab=allowed>Allowed Values</div>
  <div class=tab data-tab=products>Products</div>
  <div class=tab data-tab=coverage>Coverage</div>
  <div class=tab data-tab=merges>Merge Suggestions</div>
</div>

<div id=aggregates class="tab-content active">
  <div class=filters>
    <label>Status</label>
    <select id=agg-status>
      <option value=>(any)</option>
      <option>pending</option>
      <option>approved</option>
      <option>merged</option>
      <option>rejected</option>
    </select>
    <label>Search</label>
    <input id=agg-search placeholder="cluster_key / canonical_value" size=22>
    <label><input type=checkbox id=agg-recommended-only> Recommended only</label>
    <button onclick="loadAggregates()">Apply</button>
    <small class=dim id=agg-count></small>
  </div>
  <table id=agg-table>
    <thead><tr>
      <th>cluster_key</th><th>canonical</th><th class=num>count</th>
      <th class=num>distinct</th><th class=num>avg_conf</th>
      <th>status</th><th>promoted_to</th><th>ready</th><th>rec.</th>
      <th>sample evidence</th><th>sample products</th><th>actions</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <div class=pager>
    <button onclick="aggPage(-1)">&laquo; prev</button>
    <span id=agg-pageinfo></span>
    <button onclick="aggPage(1)">next &raquo;</button>
  </div>
</div>

<div id=allowed class=tab-content>
  <table id=allowed-table>
    <thead><tr>
      <th>attribute_name</th><th>value</th><th>is_active</th><th>created_at</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<div id=products class=tab-content>
  <div class=filters>
    <label>Status</label>
    <select id=prod-status>
      <option value=>(any)</option>
      <option>assigned</option>
      <option>pending</option>
      <option>missing</option>
    </select>
    <label>Search</label>
    <input id=prod-search placeholder="product_id / name / sku" size=22>
    <button onclick="loadProducts()">Apply</button>
    <small class=dim id=prod-count></small>
  </div>
  <table id=prod-table>
    <thead><tr>
      <th>product_id</th><th>name</th>
      <th>assigned product_type</th>
      <th>proposed product_type</th>
      <th>conf</th>
      <th>status</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <div class=pager>
    <button onclick="prodPage(-1)">&laquo; prev</button>
    <span id=prod-pageinfo></span>
    <button onclick="prodPage(1)">next &raquo;</button>
  </div>
</div>

<div id=coverage class=tab-content>
  <div id=cov-metrics></div>
</div>

<div id=merges class=tab-content>
  <div class=filters>
    <small class=dim id=merges-count></small>
  </div>
  <table id=merges-table>
    <thead><tr>
      <th>source</th><th>target</th><th>merge_type</th><th>conf</th>
      <th class=num>src ct</th><th class=num>tgt ct</th><th class=num>combined</th>
      <th>reason</th><th>evidence preview</th><th>risk notes</th><th>action</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<script>
// Immediate beacon: if you see "[js started]" in the status bar but the
// workspace dropdown stays at "(loading...)", the script is running but
// something failed during loadWorkspaces -- check the red banner / console.
// If you DON'T see "[js started]" at all, the script isn't being parsed
// (browser blocked it, stale cache, or uvicorn not serving the new code).
try {
  const _s = document.getElementById("status");
  if (_s) _s.textContent = "[js started] @ " + new Date().toISOString();
} catch (e) {}

const PAGE = 50;
let aggOffset = 0, prodOffset = 0;

function showError(msg) {
  const el = document.getElementById("errbar");
  el.textContent = "[error] " + msg;
  el.classList.add("visible");
  console.error(msg);
}
function clearError() {
  const el = document.getElementById("errbar");
  el.textContent = "";
  el.classList.remove("visible");
}
function setStatus(msg) {
  document.getElementById("status").textContent = msg || "";
}

// Surface any uncaught JS errors visibly so they don't fail silently.
window.addEventListener("error", e => showError("JS: " + (e.message || e)));
window.addEventListener("unhandledrejection",
  e => showError("Promise: " + (e.reason && e.reason.message || e.reason || "unknown")));

async function api(path, params = {}) {
  const url = new URL("/admin/taxonomy/api/" + path, window.location.origin);
  for (const k in params) if (params[k] !== "" && params[k] != null) url.searchParams.set(k, params[k]);
  let r;
  try {
    r = await fetch(url);
  } catch (e) {
    throw new Error("network error fetching " + url.pathname + url.search + ": " + e.message);
  }
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch {}
    throw new Error("HTTP " + r.status + " " + url.pathname + url.search + " -- " + body.slice(0, 200));
  }
  return r.json();
}

function ws()   { return document.getElementById("ws").value; }
function attr() { return document.getElementById("attr").value; }

async function loadWorkspaces() {
  setStatus("loading workspaces...");
  try {
    const list = await api("workspaces");
    const sel = document.getElementById("ws");
    sel.innerHTML = "";
    if (!list.length) {
      sel.innerHTML = '<option value="">(no workspaces)</option>';
      showError("workspaces endpoint returned an empty list");
      return;
    }
    for (const w of list) {
      const opt = document.createElement("option");
      opt.value = w.id;
      opt.textContent = `${w.slug} (id=${w.id})`;
      sel.appendChild(opt);
    }
    // Default to mumzworld_v3_sample if present, else first.
    const v3 = list.find(w => w.slug === "mumzworld_v3_sample");
    sel.value = (v3 || list[0]).id;
    setStatus(`${list.length} workspaces loaded`);
    clearError();
    refreshAll();
  } catch (e) {
    showError("loadWorkspaces failed: " + e.message);
    setStatus("");
  }
}

async function loadAggregates() {
  if (!ws()) return;
  let data;
  try {
    data = await api("aggregates", {
      workspace_id: ws(), attribute: attr(),
      status: document.getElementById("agg-status").value,
      search: document.getElementById("agg-search").value,
      recommended_only: document.getElementById("agg-recommended-only").checked ? "true" : "",
      offset: aggOffset, limit: PAGE,
    });
  } catch (e) { showError("aggregates: " + e.message); return; }
  const tbody = document.querySelector("#agg-table tbody");
  tbody.innerHTML = "";
  for (const a of data.items) {
    const tr = document.createElement("tr");
    const evid = (a.sample_evidence || []).map(e => `* ${e}`).join("<br>");
    const samp = (a.sample_product_ids || []).slice(0,3).join(", ");
    const readyPill = a.ready ? '<span class="pill ready">ready</span>' : '';
    const recPill = a.recommended_for_approval ? '<span class="pill recommended">recommended</span>' : '';
    const isPending = a.status === "pending";
    const approveDisabled = !(isPending && a.ready);
    const rejectDisabled  = !isPending;
    const approveBtn = `<button class="act act-approve" data-id="${a.id}"
        data-key="${a.cluster_key}" data-count="${a.count}"
        data-dist="${a.distinct_products}" data-conf="${a.avg_conf}"
        ${approveDisabled ? "disabled" : ""}>Approve</button>`;
    const rejectBtn = `<button class="act act-reject" data-id="${a.id}"
        data-key="${a.cluster_key}" data-count="${a.count}"
        data-dist="${a.distinct_products}" data-conf="${a.avg_conf}"
        ${rejectDisabled ? "disabled" : ""}>Reject</button>`;
    tr.innerHTML = `
      <td class=code>${a.cluster_key}</td>
      <td class=code>${a.canonical_value}</td>
      <td class=num>${a.count}</td>
      <td class=num>${a.distinct_products}</td>
      <td class=num>${a.avg_conf.toFixed(3)}</td>
      <td><span class="pill ${a.status}">${a.status}</span></td>
      <td class=code>${a.promoted_to_allowed_value || ""}</td>
      <td>${readyPill}</td>
      <td>${recPill}</td>
      <td><small class=evid>${evid}</small></td>
      <td><small class=dim>${samp}</small></td>
      <td class=actions>${approveBtn} ${rejectBtn}</td>`;
    tbody.appendChild(tr);
  }
  // Wire up newly-rendered action buttons.
  tbody.querySelectorAll(".act-approve").forEach(b =>
    b.addEventListener("click", () => onApprove(b.dataset)));
  tbody.querySelectorAll(".act-reject").forEach(b =>
    b.addEventListener("click", () => onReject(b.dataset)));
  document.getElementById("agg-count").textContent =
    `${data.total} aggregates (thresholds: count>=${data.thresholds.min_count}, ` +
    `distinct>=${data.thresholds.min_distinct}, avg_conf>=${data.thresholds.min_avg_conf})`;
  document.getElementById("agg-pageinfo").textContent =
    `${aggOffset + 1}-${Math.min(aggOffset + PAGE, data.total)} of ${data.total}`;
}
function aggPage(dir) {
  aggOffset = Math.max(0, aggOffset + dir * PAGE);
  loadAggregates();
}

async function postAction(verb, ds) {
  // Browser confirm with the cluster details -- required by the spec.
  const lines = [
    `${verb.toUpperCase()} aggregate?`,
    `cluster_key      : ${ds.key}`,
    `count            : ${ds.count}`,
    `distinct_products: ${ds.dist}`,
    `avg_conf         : ${(+ds.conf).toFixed(3)}`,
    `workspace        : id=${ws()}`,
    `attribute        : ${attr()}`,
  ];
  if (!confirm(lines.join("\\n"))) return;
  const url = new URL(
    `/admin/taxonomy/api/aggregates/${ds.id}/${verb}`,
    window.location.origin
  );
  url.searchParams.set("workspace_id", ws());
  url.searchParams.set("attribute", attr());
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const body = await r.text();
    alert(`Action failed (${r.status}): ${body}`);
    return;
  }
  // Refresh ALL four tabs so the user sees the consequence everywhere.
  refreshAll();
}
function onApprove(ds) { postAction("approve", ds); }
function onReject(ds)  { postAction("reject",  ds); }

async function loadAllowed() {
  if (!ws()) return;
  let data;
  try {
    data = await api("allowed_values", {
      workspace_id: ws(), attribute: attr(),
    });
  } catch (e) { showError("allowed_values: " + e.message); return; }
  const tbody = document.querySelector("#allowed-table tbody");
  tbody.innerHTML = "";
  for (const r of data) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class=code>${r.attribute_name}</td>
      <td class=code>${r.value}</td>
      <td>${r.is_active}</td>
      <td><small>${r.created_at || ""}</small></td>`;
    tbody.appendChild(tr);
  }
}

async function loadProducts() {
  if (!ws()) return;
  let data;
  try {
    data = await api("products", {
      workspace_id: ws(), attribute: attr(),
      status: document.getElementById("prod-status").value,
      search: document.getElementById("prod-search").value,
      offset: prodOffset, limit: PAGE,
    });
  } catch (e) { showError("products: " + e.message); return; }
  const tbody = document.querySelector("#prod-table tbody");
  tbody.innerHTML = "";
  for (const p of data.items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class=code>${p.product_id}</td>
      <td>${(p.name || "").slice(0,80)}</td>
      <td class=code>${p.assigned_value || ""}</td>
      <td class=code>${p.proposed_value || ""}</td>
      <td class=num>${p.proposed_confidence != null ? p.proposed_confidence.toFixed(2) : ""}</td>
      <td><span class="pill ${p.status}">${p.status}</span></td>`;
    tbody.appendChild(tr);
  }
  document.getElementById("prod-count").textContent = `${data.total} products`;
  document.getElementById("prod-pageinfo").textContent =
    `${prodOffset + 1}-${Math.min(prodOffset + PAGE, data.total)} of ${data.total}`;
}
function prodPage(dir) {
  prodOffset = Math.max(0, prodOffset + dir * PAGE);
  loadProducts();
}

async function loadCoverage() {
  if (!ws()) return;
  let c;
  try {
    c = await api("coverage", {
      workspace_id: ws(), attribute: attr(),
    });
  } catch (e) { showError("coverage: " + e.message); return; }
  const el = document.getElementById("cov-metrics");
  const fmt = (label, val) =>
    `<div class=metric><div class=label>${label}</div><div class=value>${val}</div></div>`;
  el.innerHTML =
    fmt("total products", c.total_products) +
    fmt("with " + c.attribute, c.products_with_attribute) +
    fmt("coverage %", c.coverage_pct + "%") +
    fmt("approved values", c.approved_value_count) +
    fmt("pending aggregates", c.pending_aggregate_count) +
    fmt("long tail (count==1)", c.long_tail_count) +
    fmt("missing", c.missing_count);
  document.getElementById("ctxinfo").textContent =
    `${c.total_products} products | ${c.products_with_attribute} assigned | ` +
    `${c.coverage_pct}% coverage`;
}

async function loadMerges() {
  if (!ws()) return;
  let data;
  try {
    data = await api("merge_suggestions", { workspace_id: ws(), attribute: attr() });
  } catch (e) { showError("merge_suggestions: " + e.message); return; }
  const tbody = document.querySelector("#merges-table tbody");
  tbody.innerHTML = "";
  for (const s of data.items) {
    const tr = document.createElement("tr");
    const sevid = (s.sample_source_evidence || []).slice(0,2).map(x => "* " + x).join("<br>");
    const tevid = (s.sample_target_evidence || []).slice(0,2).map(x => "* " + x).join("<br>");
    const risks = (s.risk_notes || []).map(r => "&#9888; " + r).join("<br>");
    const disabled = s.executable ? "" : "disabled";
    const btn = `<button class="act act-merge" data-source="${s.source_cluster}"
        data-target="${s.target_cluster}" data-type="${s.merge_type}"
        data-srcct="${s.source_count}" data-tgtct="${s.target_count}"
        data-srcst="${s.source_status}" data-tgtst="${s.target_status}"
        ${disabled}>Approve Merge</button>`;
    tr.innerHTML = `
      <td class=code>${s.source_cluster} <small class=dim>(${s.source_status})</small></td>
      <td class=code>${s.target_cluster} <small class=dim>(${s.target_status})</small></td>
      <td><span class="pill ${s.merge_type}">${s.merge_type.replace("_"," ")}</span></td>
      <td><span class="pill ${s.confidence}">${s.confidence}</span></td>
      <td class=num>${s.source_count}</td>
      <td class=num>${s.target_count}</td>
      <td class=num>${s.combined_count}</td>
      <td><small>${s.reason}</small></td>
      <td><small class=evid><b>src:</b><br>${sevid}<br><b>tgt:</b><br>${tevid}</small></td>
      <td><small class=evid>${risks}</small></td>
      <td class=actions>${btn}</td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".act-merge").forEach(b =>
    b.addEventListener("click", () => onApproveMerge(b.dataset)));
  document.getElementById("merges-count").textContent =
    `${data.total} suggestions  (norm=${data.by_type_count.normalization_variant}, ` +
    `parent_child=${data.by_type_count.parent_child}, ` +
    `semantic=${data.by_type_count.semantic_duplicate})`;
}

async function onApproveMerge(ds) {
  const lines = [
    "MERGE aggregate?",
    `source           : ${ds.source} (status=${ds.srcst}, count=${ds.srcct})`,
    `target           : ${ds.target} (status=${ds.tgtst}, count=${ds.tgtct})`,
    `merge_type       : ${ds.type}`,
    "",
    "ProductAttribute rows on the source value will be updated to point to",
    "the target. Per-product duplicates will be dropped (one product_type",
    "per product). Underlying ProposedAttributeValueEvent rows are preserved.",
  ];
  if (!confirm(lines.join("\\n"))) return;
  const url = new URL(
    "/admin/taxonomy/api/merge_suggestions/execute",
    window.location.origin
  );
  const body = {
    workspace_id: parseInt(ws(), 10),
    attribute: attr(),
    source_cluster: ds.source,
    target_cluster: ds.target,
    merge_type: ds.type,
    review_note: "Merged from taxonomy admin UI",
  };
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    alert(`Merge failed (${r.status}): ${text}`);
    return;
  }
  refreshAll();
}

function refreshAll() {
  aggOffset = 0; prodOffset = 0;
  loadAggregates(); loadAllowed(); loadProducts(); loadCoverage(); loadMerges();
}

document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  document.getElementById(t.dataset.tab).classList.add("active");
}));

document.getElementById("ws").addEventListener("change", refreshAll);
document.getElementById("attr").addEventListener("change", refreshAll);

loadWorkspaces();
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def admin_ui():
    return HTMLResponse(_HTML)
