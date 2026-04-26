import logging
import math

logger = logging.getLogger(__name__)

from collections import defaultdict
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.attribute_value_relationship import AttributeValueRelationship
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.product_behavior_relationship import ProductBehaviorRelationship
from app.schemas.attribute_enrichment import AttributeBehavior
from app.schemas.recommendation import (
    BehavioralMatch,
    MatchedAttribute,
    RecommendationRead,
    RelationshipMatch,
    SlotFilter,
)

# ---------------------------------------------------------------------------
# Attribute taxonomy
# ---------------------------------------------------------------------------

FUNCTIONAL_SIGNATURE_ATTRS: frozenset[str] = frozenset({"type", "activity"})
CORE_ATTRS: frozenset[str] = frozenset({"type", "activity", "activity_type", "category"})
DESCRIPTIVE_ATTRS: frozenset[str] = frozenset({"color", "brand"})

# Default weights — relationship=1.0 preserves pre-V6 recommendation_score values
# exactly (direct_score + relationship_score). popularity=0.0 and behavioral=0.0
# keep those signals from competing unless the caller explicitly enables them.
_DEFAULT_DIRECT_WEIGHT: float = 1.0
_DEFAULT_RELATIONSHIP_WEIGHT: float = 1.0
_DEFAULT_POPULARITY_WEIGHT: float = 0.0
_DEFAULT_BEHAVIORAL_WEIGHT: float = 0.0

# Multiplier applied to compatibility_signal attributes.
# Makes suitability/fit matches weigh more than categorical preference matches.
_COMPATIBILITY_SCORE_MULTIPLIER: float = 1.5

# Contextual semantic mismatch penalty — applies to occasion, activity,
# and environment. Conservative multiplier: a mismatch reduces the score
# but much less aggressively than a compatibility mismatch.
_CONTEXTUAL_MISMATCH_ATTRS: frozenset[str] = frozenset({
    "occasion", "activity", "environment",
})
_CONTEXTUAL_MISMATCH_MULTIPLIER: float = 0.3

# Multi-value direct-score normalization. For attributes listed here, each
# categorical_affinity contribution from this product is divided by sqrt(N)
# where N is the count of values the product carries for that attribute.
# Dampens tag-stacking so a product tagged with all values doesn't accumulate
# a contribution proportional to its tag density. Weights and affinities are
# untouched; only the per-product accumulated contribution is normalized.
_MULTI_VALUE_NORMALIZED_ATTRS: frozenset[str] = frozenset({"activity_type"})

# Complementary compatibility attributes — inverted scoring logic.
# Normal compatibility: match = positive, mismatch = negative.
# Complementary:        match = negative (duplicate), mismatch = positive
#                       (the customer already owns this role and benefits
#                       from a different one).
# Today this covers layering_role only: a customer with a base layer
# should see mid/outer recommendations, not more base layers.
_COMPLEMENTARY_COMPAT_ATTRS: frozenset[str] = frozenset({"layering_role"})

# Low-signal penalty — demotes products with weak enrichment coverage.
# Applied after all other scoring, using product_signal_strength from the
# multi-source signal layer.  Products without signal data are unaffected.
_LOW_SIGNAL_PENALTY_WEIGHT: float = 0.1

# Diversity shaping — soft category/group re-rank applied AFTER scoring,
# penalties, halo, and suppression. Rank #1 is never touched; ranks 2..N are
# re-picked greedily from a small look-ahead window, with a multiplicative
# penalty on repeated categories. The closeness rule prevents diversity from
# overriding a clear score winner: shaping only activates for candidates
# within DIVERSITY_CLOSENESS_RATIO of the window leader.
DIVERSITY_SCAN_WINDOW: int = 5
CATEGORY_REPEAT_PENALTY: float = 0.15
MAX_CATEGORY_REPEAT_EFFECT: float = 0.45
DIVERSITY_CLOSENESS_RATIO: float = 0.85

# Similarity-halo fallback — kicks in only when purchase suppression has
# removed the strongest candidates. Suppressed products are still scored so
# the engine can detect "a much better match was removed" and boost remaining
# products that share key attributes with the top suppressed ones.
#
# HALO_ATTR_WEIGHTS gives per-attribute influence: category dominates, fit is
# only a tiebreaker. Overlap is normalized by the number of attributes the
# halo considers so a single strong match cannot dwarf the base score.
#
# The trigger is guarded by a ratio so normal ranking is untouched whenever a
# reasonably strong unsuppressed candidate already exists.
HALO_ATTR_WEIGHTS: dict[str, float] = {
    "category": 1.0,
    "occasion": 0.9,
    "activity": 0.8,
    "support_level": 0.7,
    "fit_type": 0.5,
    "fit": 0.3,
}
_SIMILARITY_FALLBACK_TRIGGER_RATIO: float = 1.25
_SIMILARITY_FALLBACK_MIN_SUPPRESSED_SCORE: float = 0.5
_SIMILARITY_HALO_TOP_K: int = 2
HALO_BASE_WEIGHT: float = 0.5
HALO_MAX_RATIO: float = 0.75

# ---------------------------------------------------------------------------
# Algorithm presets — each maps to weights + a tie-break priority.
#
# tie_break_priority: ordered list of score fields used to break ties when
# final_score is equal. After exhausting these, product PK ASC is the final
# deterministic tiebreaker.
# ---------------------------------------------------------------------------

ALGORITHM_PRESETS: dict[str, dict] = {
    "balanced": {
        "direct_weight": 1.0,
        "relationship_weight": 0.7,
        "popularity_weight": 0.0,
        "behavioral_weight": 0.5,
        "tie_break_priority": ["direct_score", "relationship_score", "behavioral_score", "product_id"],
    },
    "behavior_first": {
        "direct_weight": 0.3,
        "relationship_weight": 0.3,
        "popularity_weight": 0.0,
        "behavioral_weight": 1.0,
        "tie_break_priority": ["behavioral_score", "relationship_score", "direct_score", "product_id"],
    },
    "affinity_first": {
        "direct_weight": 1.0,
        "relationship_weight": 0.3,
        "popularity_weight": 0.0,
        "behavioral_weight": 0.2,
        "tie_break_priority": ["direct_score", "relationship_score", "behavioral_score", "product_id"],
    },
    "relationship_only": {
        "direct_weight": 0.0,
        "relationship_weight": 1.0,
        "popularity_weight": 0.0,
        "behavioral_weight": 0.0,
        "tie_break_priority": ["relationship_score", "direct_score", "behavioral_score", "product_id"],
    },
    "behavioral_only": {
        "direct_weight": 0.0,
        "relationship_weight": 0.0,
        "popularity_weight": 0.0,
        "behavioral_weight": 1.0,
        "tie_break_priority": ["behavioral_score", "relationship_score", "direct_score", "product_id"],
    },
}


def get_algorithm_preset(name: str) -> dict | None:
    """Return a copy of the named preset, or None if unknown."""
    preset = ALGORITHM_PRESETS.get(name)
    return dict(preset) if preset is not None else None


def get_attribute_weight(attr_id: str) -> float:
    """Return the scoring multiplier for an attribute."""
    if attr_id in CORE_ATTRS:
        return 1.0
    if attr_id in DESCRIPTIVE_ATTRS:
        return 0.2
    return 0.5


# ---------------------------------------------------------------------------
# Eligibility helpers
# ---------------------------------------------------------------------------

def _is_group_suppressed(
    product: Product | None,
    most_recent_order_date: date,
    reference_date: date,
) -> bool:
    if product is None:
        return True

    behavior = product.repurchase_behavior
    window = product.repurchase_window_days

    if behavior == "one_time":
        return True
    if behavior == "repurchasable":
        if window is not None:
            return (reference_date - most_recent_order_date).days <= window
        return False
    if window is not None:
        return (reference_date - most_recent_order_date).days <= window
    return True


def _build_suppression_sets(
    db: Session,
    workspace_id: int,
    customer_id: str,
    reference_date: date,
) -> tuple[set[str], set[str], set[frozenset]]:
    """
    Returns (suppressed_product_ids, suppressed_group_keys, suppressed_functional_sigs).
    See inline comments for semantics of each set.
    """
    purchases = (
        db.query(CustomerPurchase)
        .filter(
            CustomerPurchase.workspace_id == workspace_id,
            CustomerPurchase.customer_id == customer_id,
        )
        .all()
    )

    if not purchases:
        return set(), set(), set()

    suppressed_product_ids: set[str] = {p.product_id for p in purchases}

    by_group_key: dict[str, list[CustomerPurchase]] = defaultdict(list)
    for p in purchases:
        key = p.group_id if p.group_id else p.product_id
        by_group_key[key].append(p)

    purchased_db_ids = list({p.product_db_id for p in purchases})
    products_purchased = (
        db.query(Product)
        .filter(Product.workspace_id == workspace_id, Product.id.in_(purchased_db_ids))
        .all()
    )
    product_map: dict[int, Product] = {p.id: p for p in products_purchased}

    sig_attrs = (
        db.query(ProductAttribute)
        .filter(
            ProductAttribute.product_id.in_(purchased_db_ids),
            ProductAttribute.attribute_id.in_(list(FUNCTIONAL_SIGNATURE_ATTRS)),
        )
        .all()
    )
    sig_attrs_by_db_id: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for a in sig_attrs:
        sig_attrs_by_db_id[a.product_id].append((a.attribute_id, a.attribute_value))

    suppressed_group_keys: set[str] = set()
    suppressed_functional_sigs: set[frozenset] = set()

    for group_key, group_purchases in by_group_key.items():
        most_recent = max(group_purchases, key=lambda p: p.order_date)
        product = product_map.get(most_recent.product_db_id)

        if not _is_group_suppressed(product, most_recent.order_date, reference_date):
            continue

        suppressed_group_keys.add(group_key)

        for purchase in group_purchases:
            sig = frozenset(sig_attrs_by_db_id[purchase.product_db_id])
            if sig:
                suppressed_functional_sigs.add(sig)

    return suppressed_product_ids, suppressed_group_keys, suppressed_functional_sigs


def _build_popularity_scores(db: Session, workspace_id: int) -> dict[int, float]:
    """
    Returns {product_db_id → SUM(quantity)} over all customers in the workspace.
    Only products with at least one purchase are included.
    """
    rows = (
        db.query(
            CustomerPurchase.product_db_id,
            func.sum(CustomerPurchase.quantity).label("total_qty"),
        )
        .filter(CustomerPurchase.workspace_id == workspace_id)
        .group_by(CustomerPurchase.product_db_id)
        .all()
    )
    return {row.product_db_id: float(row.total_qty) for row in rows}


def _is_functionally_suppressed(
    product: Product,
    attrs_by_product: dict[int, list[ProductAttribute]],
    suppressed_functional_sigs: set[frozenset],
) -> bool:
    if not suppressed_functional_sigs:
        return False
    if product.recommendation_role == "complementary":
        return False
    candidate_sig = frozenset(
        (attr.attribute_id, attr.attribute_value)
        for attr in attrs_by_product[product.id]
        if attr.attribute_id in FUNCTIONAL_SIGNATURE_ATTRS
    )
    return bool(candidate_sig) and candidate_sig in suppressed_functional_sigs


# ---------------------------------------------------------------------------
# Targeting-mode helpers
#
# class_name    = HOW an attribute value was generated (enrichment strategy)
# targeting_mode = HOW an attribute is used during recommendation scoring
#
#   categorical_affinity  — soft preference signal; accumulates into direct_score
#   compatibility_signal  — suitability/fit; heavier weight; own contribution bucket
#   categorical_filter    — hard gate when filter_context is provided; no score otherwise
#   descriptive_metadata  — not scored; available for display / analytics only
# ---------------------------------------------------------------------------

def _get_targeting_mode(
    attribute_id: str,
    attribute_targeting_modes: dict[str, str] | None,
) -> str:
    """Return the targeting_mode for an attribute.

    Defaults to 'categorical_affinity' when no mapping is provided, preserving
    existing recommendation behaviour for all unclassified attributes.
    """
    if attribute_targeting_modes is None:
        return "categorical_affinity"
    return attribute_targeting_modes.get(attribute_id, "categorical_affinity")


def _ordered_mismatch_severity(
    product_value: str,
    customer_values: dict[str, float],
    value_order: list[str],
) -> float:
    """Severity of a mismatch on an ordered compatibility scale, in [0.0, 1.0].

    Distance is measured between the product's index in ``value_order`` and the
    closest customer signal value's index. Severity = closest_distance / max_distance,
    where max_distance is ``len(value_order) - 1``.

    Returns 0.0 (no penalty) when:
      - the ordered scale has fewer than 2 entries (no distance is meaningful),
      - the product value is not in ``value_order``, or
      - none of the customer's signal values are in ``value_order``.

    The function is intentionally generic — it knows nothing about specific
    attribute names. ``value_order`` is the only thing that defines the axis.
    """
    if len(value_order) < 2:
        return 0.0
    try:
        product_idx = value_order.index(product_value)
    except ValueError:
        return 0.0

    customer_indices: list[int] = []
    for cv in customer_values:
        try:
            customer_indices.append(value_order.index(cv))
        except ValueError:
            continue
    if not customer_indices:
        return 0.0

    closest_distance = min(abs(product_idx - ci) for ci in customer_indices)
    max_distance = len(value_order) - 1
    return closest_distance / max_distance


def _apply_categorical_filters(
    product_db_id: int,
    attrs_by_product: dict[int, list],
    filter_context: dict[str, list[str]],
    attribute_targeting_modes: dict[str, str],
) -> bool:
    """Return True if the product passes all categorical_filter constraints.

    For each attribute whose targeting_mode is 'categorical_filter', if
    filter_context specifies allowed values for that attribute the product must
    carry a matching value.  Attributes with no filter_context entry are skipped
    (no constraint imposed).  This function is a no-op when filter_context is
    None — callers must guard before calling.
    """
    for attr in attrs_by_product.get(product_db_id, []):
        if _get_targeting_mode(attr.attribute_id, attribute_targeting_modes) != "categorical_filter":
            continue
        allowed = filter_context.get(attr.attribute_id)
        if allowed is None:
            continue  # No constraint specified for this attribute
        if attr.attribute_value not in allowed:
            return False
    return True


# ---------------------------------------------------------------------------
# Main recommendation logic
# ---------------------------------------------------------------------------

def _product_passes_filters(
    db_id: int,
    attrs_by_product: dict[int, list],
    filters: list[SlotFilter],
) -> bool:
    """Return True if the product satisfies ALL filters (AND logic)."""
    product_attrs = attrs_by_product.get(db_id, [])
    for f in filters:
        matched = False
        for attr in product_attrs:
            if attr.attribute_id != f.attribute_id:
                continue
            if f.operator == "eq" and attr.attribute_value == f.value:
                matched = True
                break
            if f.operator == "in" and attr.attribute_value in f.value:
                matched = True
                break
        if not matched:
            return False
    return True


def _compute_effective_score(
    rec: RecommendationRead,
    final_score: float,
    algorithm: str | None,
    fallback_behavior: str,
) -> tuple[float, str]:
    """Return (effective_score, fallback_explanation).

    When the algorithm's primary signal is absent and a fallback policy is set,
    returns an alternative score for ranking/thresholding.  Otherwise returns
    (final_score, "").
    """
    if algorithm in ("behavior_first", "behavioral_only") and rec.behavioral_score <= 0:
        if fallback_behavior == "direct":
            return rec.direct_score, "Behavioral signal unavailable; fell back to direct score."
        if fallback_behavior == "balanced":
            eff = (0.5 * rec.direct_score
                   + 0.3 * rec.relationship_score
                   + 0.2 * rec.behavioral_score)
            return eff, "Behavioral signal unavailable; fell back to balanced fallback score."

    if algorithm == "relationship_only" and rec.relationship_score <= 0:
        if fallback_behavior == "direct":
            return rec.direct_score, "Relationship signal unavailable; fell back to direct score."
        if fallback_behavior == "balanced":
            eff = (0.5 * rec.direct_score
                   + 0.3 * rec.relationship_score
                   + 0.2 * rec.behavioral_score)
            return eff, "Relationship signal unavailable; fell back to balanced fallback score."

    return final_score, ""


def _diversity_key(rec: RecommendationRead) -> str:
    """Pick the key used to detect category repetition during diversity shaping.

    Prefers an explicit `category` matched attribute (what the engine scored
    against); falls back to `group_id` (populated for all products in the
    starter dataset); finally uses the product_id so a missing group never
    causes false repeats.
    """
    for m in rec.matched_attributes:
        if m.attribute_id == "category":
            return f"cat::{m.attribute_value}"
    if rec.group_id:
        return f"grp::{rec.group_id}"
    return f"pid::{rec.product_id}"


def _apply_diversity_shaping(
    results: list[RecommendationRead],
) -> tuple[list[RecommendationRead], list[tuple[str, int, int]]]:
    """Soft-diversity re-rank the top-N list.

    - Rank #1 is locked in place.
    - For ranks 2..N, greedily pick from a look-ahead window of the next
      DIVERSITY_SCAN_WINDOW candidates. Within the window, candidates whose
      raw score is >= window_top * DIVERSITY_CLOSENESS_RATIO are "close
      enough" for shaping; they get a multiplicative penalty based on how
      many times their category has already appeared in the selected list.
      Out-of-band candidates are ranked by raw score.
    - `recommendation_score` is never mutated; only list order changes.

    Returns the re-ranked list AND a debug log of (product_id, old_rank,
    new_rank) tuples for movements (old_rank != new_rank).
    """
    if len(results) <= 1:
        return results, []

    original_order = {rec.product_id: i for i, rec in enumerate(results)}

    locked = [results[0]]
    remaining: list[RecommendationRead] = list(results[1:])
    seen_keys: dict[str, int] = {_diversity_key(results[0]): 1}

    while remaining:
        window = remaining[:DIVERSITY_SCAN_WINDOW]
        window_top = max(r.recommendation_score for r in window)
        threshold = window_top * DIVERSITY_CLOSENESS_RATIO

        best_idx = 0
        best_adjusted = float("-inf")
        for i, rec in enumerate(window):
            repeats = seen_keys.get(_diversity_key(rec), 0)
            if rec.recommendation_score >= threshold:
                factor = 1.0 - min(
                    repeats * CATEGORY_REPEAT_PENALTY, MAX_CATEGORY_REPEAT_EFFECT
                )
                adjusted = rec.recommendation_score * factor
            else:
                adjusted = rec.recommendation_score
            # Stable tiebreak: earlier original position wins when adjusted
            # scores tie, preserving raw-score order for equal candidates.
            if adjusted > best_adjusted:
                best_adjusted = adjusted
                best_idx = i

        chosen = remaining.pop(best_idx)
        locked.append(chosen)
        key = _diversity_key(chosen)
        seen_keys[key] = seen_keys.get(key, 0) + 1

    movements: list[tuple[str, int, int]] = []
    for new_idx, rec in enumerate(locked):
        old_idx = original_order[rec.product_id]
        if new_idx != old_idx:
            movements.append((rec.product_id, old_idx, new_idx))
    return locked, movements


def get_recommendations(
    db: Session,
    workspace_id: int,
    customer_id: str,
    min_score: float | None = None,
    top_n: int = 10,
    reference_date: date | None = None,
    direct_weight: float = _DEFAULT_DIRECT_WEIGHT,
    relationship_weight: float = _DEFAULT_RELATIONSHIP_WEIGHT,
    popularity_weight: float = _DEFAULT_POPULARITY_WEIGHT,
    behavioral_weight: float = _DEFAULT_BEHAVIORAL_WEIGHT,
    tie_break_priority: list[str] | None = None,
    slot_filters: list[SlotFilter] | None = None,
    fallback_mode: str = "strict",
    algorithm: str | None = None,
    fallback_behavior: str = "none",
    max_per_group: int | None = None,
    max_scan_depth: int | None = None,
    min_score_threshold: float | None = None,
    excluded_product_ids: set[str] | None = None,
    excluded_group_ids: set[str] | None = None,
    attribute_targeting_modes: dict[str, str] | None = None,
    filter_context: dict[str, list[str]] | None = None,
    attribute_behaviors: dict[str, AttributeBehavior] | None = None,
    customer_signal_strength: float | None = None,
    product_enrichment_outputs: dict | None = None,
    tiebreak_by_match_confidence: bool = False,
    disable_purchase_suppression_for_eval: bool = False,
    disable_diversity_shaping: bool = False,
) -> tuple[list[RecommendationRead], bool]:
    logger.warning("DEBUG get_recommendations called: workspace=%s customer=%s", workspace_id, customer_id)
    if reference_date is None:
        reference_date = date.today()

    # All-zero weights → silently restore defaults so scoring is meaningful
    if (
        direct_weight == 0.0
        and relationship_weight == 0.0
        and popularity_weight == 0.0
        and behavioral_weight == 0.0
    ):
        direct_weight = _DEFAULT_DIRECT_WEIGHT
        relationship_weight = _DEFAULT_RELATIONSHIP_WEIGHT
        popularity_weight = _DEFAULT_POPULARITY_WEIGHT
        # behavioral_weight stays 0.0

    # 1. Build suppression sets.
    # Evaluation mode: skip purchase-based suppression entirely so the engine
    # can be scored on raw relevance. Production default keeps all existing
    # suppression behavior unchanged.
    if disable_purchase_suppression_for_eval:
        suppressed_product_ids: set[str] = set()
        suppressed_group_keys: set[str] = set()
        suppressed_functional_sigs: set[frozenset] = set()
        logger.warning("DEBUG suppression disabled (evaluation mode)")
    else:
        suppressed_product_ids, suppressed_group_keys, suppressed_functional_sigs = (
            _build_suppression_sets(db, workspace_id, customer_id, reference_date)
        )
        logger.warning("DEBUG suppressed: products=%d groups=%d sigs=%d",
                       len(suppressed_product_ids), len(suppressed_group_keys), len(suppressed_functional_sigs))

    # 2. Load all products and attributes
    products = db.query(Product).filter(Product.workspace_id == workspace_id).all()
    logger.warning("DEBUG products loaded: %d", len(products))
    if not products:
        return [], False

    product_db_ids = [p.id for p in products]
    raw_attrs = (
        db.query(ProductAttribute)
        .filter(ProductAttribute.product_id.in_(product_db_ids))
        .all()
    )
    attrs_by_product: dict[int, list[ProductAttribute]] = defaultdict(list)
    for attr in raw_attrs:
        attrs_by_product[attr.product_id].append(attr)

    # 3. Load affinities
    affinities_q = db.query(CustomerAttributeAffinity).filter(
        CustomerAttributeAffinity.workspace_id == workspace_id,
        CustomerAttributeAffinity.customer_id == customer_id,
    )
    if min_score is not None:
        affinities_q = affinities_q.filter(CustomerAttributeAffinity.score >= min_score)
    affinities = affinities_q.order_by(CustomerAttributeAffinity.score.desc()).all()
    logger.warning("DEBUG affinities loaded: %d (min_score filter=%s)", len(affinities), min_score)

    # 4. Build affinity map and relationship contributions
    affinity_map: dict[tuple[str, str], float] = {}
    rel_contributions: dict[tuple[str, str], list[RelationshipMatch]] = defaultdict(list)
    # customer_signals_by_attr inverts affinity_map by attribute_id so the
    # negative compatibility pass can look up "what value(s) does this customer
    # signal for attribute X?" without scanning the full affinity list per
    # product. Empty when no affinities exist.
    customer_signals_by_attr: dict[str, dict[str, float]] = defaultdict(dict)

    if affinities:
        affinity_map = {
            (a.attribute_id, a.attribute_value): a.score for a in affinities
        }
        for (attr_id, attr_val), score in affinity_map.items():
            customer_signals_by_attr[attr_id][attr_val] = score

        approved_rels = (
            db.query(AttributeValueRelationship)
            .filter(
                AttributeValueRelationship.workspace_id == workspace_id,
                AttributeValueRelationship.status == "approved",
            )
            .all()
        )
        rel_lookup: dict[tuple[str, str], list[AttributeValueRelationship]] = defaultdict(list)
        for rel in approved_rels:
            rel_lookup[(rel.source_attribute_id, rel.source_value)].append(rel)

        for (src_attr, src_val), aff_score in affinity_map.items():
            for rel in rel_lookup.get((src_attr, src_val), []):
                target_key = (rel.target_attribute_id, rel.target_value)
                contribution = round(aff_score * rel.strength, 6)
                rel_contributions[target_key].append(
                    RelationshipMatch(
                        source_attribute_id=src_attr,
                        source_attribute_value=src_val,
                        target_attribute_id=rel.target_attribute_id,
                        target_attribute_value=rel.target_value,
                        source_score=aff_score,
                        relationship_strength=rel.strength,
                        contribution=contribution,
                    )
                )

    # 5. Compute workspace-wide popularity for ALL products once
    popularity = _build_popularity_scores(db, workspace_id)

    # 6. Load behavioral contributions for this customer
    #    Maps target_product_db_id → list[BehavioralMatch]
    behavioral_contributions: dict[int, list[BehavioralMatch]] = defaultdict(list)

    # Load the customer's purchased product db_ids and their external product_ids
    customer_purchase_rows = (
        db.query(CustomerPurchase.product_db_id, CustomerPurchase.product_id)
        .filter(
            CustomerPurchase.workspace_id == workspace_id,
            CustomerPurchase.customer_id == customer_id,
        )
        .distinct()
        .all()
    )
    # product_id here is the denormalized external id stored on CustomerPurchase
    purchased_ext_id_map: dict[int, str] = {
        row.product_db_id: row.product_id for row in customer_purchase_rows
    }
    customer_purchased_db_ids = list(purchased_ext_id_map.keys())

    if customer_purchased_db_ids:
        behavior_rels = (
            db.query(ProductBehaviorRelationship)
            .filter(
                ProductBehaviorRelationship.workspace_id == workspace_id,
                ProductBehaviorRelationship.source_product_db_id.in_(
                    customer_purchased_db_ids
                ),
            )
            .all()
        )
        for rel in behavior_rels:
            src_ext_id = purchased_ext_id_map.get(
                rel.source_product_db_id, str(rel.source_product_db_id)
            )
            behavioral_contributions[rel.target_product_db_id].append(
                BehavioralMatch(
                    source_product_id=src_ext_id,
                    strength=rel.strength,
                    contribution=rel.strength,
                )
            )

    # 7. Single loop — score every product
    # Suppressed products are still scored (and tagged) so the similarity-halo
    # fallback in step 8b can mine their attributes.  They are dropped from
    # the pipeline before ranking, so suppression semantics are unchanged.
    candidates: list[tuple[int, RecommendationRead, bool]] = []

    for product in products:
        # Purchase-based suppression: tag but keep scoring.
        is_suppressed = False
        if product.product_id in suppressed_product_ids:
            is_suppressed = True
        else:
            group_key = product.group_id if product.group_id else product.product_id
            if group_key in suppressed_group_keys:
                is_suppressed = True
            elif _is_functionally_suppressed(product, attrs_by_product, suppressed_functional_sigs):
                is_suppressed = True

        # categorical_filter pre-check: hard-exclude products that fail filter
        # constraints when filter_context is explicitly provided.
        # Without filter_context, categorical_filter attributes are silent (no gate,
        # no score) — they do not act as affinity boosts.
        if filter_context and attribute_targeting_modes:
            if not _apply_categorical_filters(
                product.id, attrs_by_product, filter_context, attribute_targeting_modes
            ):
                continue

        # Direct + relationship scoring
        # Contributions are separated by targeting_mode before being combined.
        matched: list[MatchedAttribute] = []
        has_core_match = False
        affinity_contribution: float = 0.0                  # categorical_affinity — soft preference
        compatibility_positive_contribution: float = 0.0    # compatibility_signal — match
        compatibility_negative_contribution: float = 0.0    # compatibility_signal — mismatch penalty
        # Track which compatibility-signal attribute_ids were matched positively
        # on this product, so the negative pass can skip them — a positive match
        # always wins over any concurrent mismatch on the same attribute.
        positively_matched_compat_attrs: set[str] = set()
        positively_matched_contextual_attrs: set[str] = set()
        # categorical_filter    → filter_exclusion: handled as pre-scoring gate above
        # descriptive_metadata  → metadata_ignored: not scored

        if affinity_map:
            # Per-attribute value counts on this product. Used to divide
            # multi-value direct contributions by sqrt(N) for attributes in
            # _MULTI_VALUE_NORMALIZED_ATTRS.
            value_counts_by_attr: dict[str, int] = defaultdict(int)
            for attr in attrs_by_product[product.id]:
                value_counts_by_attr[attr.attribute_id] += 1

            for attr in attrs_by_product[product.id]:
                key = (attr.attribute_id, attr.attribute_value)
                if key not in affinity_map:
                    continue

                aff_score = affinity_map[key]
                weight = get_attribute_weight(attr.attribute_id)
                mode = _get_targeting_mode(attr.attribute_id, attribute_targeting_modes)

                if mode == "categorical_affinity":
                    # Soft preference signal — accumulates into direct_score
                    contribution = aff_score * weight
                    if attr.attribute_id in _MULTI_VALUE_NORMALIZED_ATTRS:
                        n = value_counts_by_attr[attr.attribute_id]
                        if n > 1:
                            contribution /= math.sqrt(n)
                    affinity_contribution += contribution
                    matched.append(MatchedAttribute(
                        attribute_id=attr.attribute_id,
                        attribute_value=attr.attribute_value,
                        score=aff_score,
                        weight=weight,
                        targeting_mode=mode,
                    ))
                    if attr.attribute_id in CORE_ATTRS:
                        has_core_match = True
                    if attr.attribute_id in _CONTEXTUAL_MISMATCH_ATTRS:
                        positively_matched_contextual_attrs.add(attr.attribute_id)

                elif mode == "compatibility_signal":
                    contribution = aff_score * weight * _COMPATIBILITY_SCORE_MULTIPLIER
                    if attr.attribute_id in _COMPLEMENTARY_COMPAT_ATTRS:
                        # Complementary: same value = duplicate = penalty.
                        compatibility_negative_contribution += contribution
                    else:
                        # Normal: same value = good match = positive.
                        compatibility_positive_contribution += contribution
                    positively_matched_compat_attrs.add(attr.attribute_id)
                    matched.append(MatchedAttribute(
                        attribute_id=attr.attribute_id,
                        attribute_value=attr.attribute_value,
                        score=aff_score,
                        weight=weight,
                        targeting_mode=mode,
                    ))
                    if attr.attribute_id in CORE_ATTRS:
                        has_core_match = True

                elif mode == "categorical_filter":
                    pass  # filter_exclusion — no scoring contribution

                # descriptive_metadata: metadata_ignored — not scored

        # Negative compatibility pass — opt-in via attribute_behaviors.
        # For each attribute the customer has a compatibility signal for, if
        # the product carries explicit value(s) for that attribute and none of
        # them match any customer signal value, accumulate a penalty into
        # compatibility_negative_contribution.
        #
        # Generic by design: nothing here references specific attribute names.
        # Only attribute_targeting_modes (which attribute is a compatibility
        # signal) and attribute_behaviors (whether to penalise + how to weight
        # the penalty) drive the behaviour.
        #
        # Skipped automatically when:
        #   - attribute_behaviors is None (backward compat — original positive-only path)
        #   - the attribute has no AttributeBehavior entry
        #   - negative_scoring_enabled is False on that behavior
        #   - the product carries no explicit value for the attribute (insufficient data → neutral)
        #   - the product already produced a positive match on the same attribute
        if attribute_behaviors and customer_signals_by_attr:
            for attr_id, customer_signals in customer_signals_by_attr.items():
                if attr_id in positively_matched_compat_attrs:
                    continue
                behavior = attribute_behaviors.get(attr_id)
                if behavior is None or not behavior.negative_scoring_enabled:
                    continue
                if _get_targeting_mode(attr_id, attribute_targeting_modes) != "compatibility_signal":
                    continue

                product_values_for_attr = [
                    a.attribute_value
                    for a in attrs_by_product[product.id]
                    if a.attribute_id == attr_id
                ]
                if not product_values_for_attr:
                    continue  # neutral — insufficient data, do not penalise
                # If any product value is in the customer signal set, this is
                # really a positive match the upstream loop should have caught.
                # Skip to be safe.
                if any(pv in customer_signals for pv in product_values_for_attr):
                    continue

                weight = get_attribute_weight(attr_id)
                # Use the strongest customer signal score as the penalty basis,
                # so the magnitude of the penalty scales with how strongly the
                # customer signalled the attribute in the first place.
                basis = max(customer_signals.values())

                if attr_id in _COMPLEMENTARY_COMPAT_ATTRS:
                    # Complementary: mismatch = the product has a DIFFERENT
                    # role than what the customer already owns = positive.
                    compatibility_positive_contribution += (
                        basis * weight * _COMPATIBILITY_SCORE_MULTIPLIER
                    )
                elif behavior.ordered_values and behavior.value_order:
                    # Distance-based: take the smallest mismatch distance
                    # across the product's values. The product is "as close
                    # as possible" to the customer's preferred value.
                    severities = [
                        _ordered_mismatch_severity(pv, customer_signals, behavior.value_order)
                        for pv in product_values_for_attr
                    ]
                    severity = min(severities) if severities else 0.0
                    if severity > 0.0:
                        compatibility_negative_contribution += (
                            basis * weight * _COMPATIBILITY_SCORE_MULTIPLIER * severity
                        )
                else:
                    # Unordered: flat penalty for any explicit mismatch.
                    compatibility_negative_contribution += (
                        basis * weight * _COMPATIBILITY_SCORE_MULTIPLIER
                    )

        # Contextual semantic mismatch pass — occasion / activity only.
        # If the customer has affinity signal for an attribute and the product
        # carries explicit value(s) for it but NONE of them match, apply a
        # conservative penalty proportional to the customer's strongest signal.
        # Skipped when the attribute already matched positively on this product.
        contextual_negative_contribution: float = 0.0
        if customer_signals_by_attr:
            for attr_id in _CONTEXTUAL_MISMATCH_ATTRS:
                if attr_id in positively_matched_contextual_attrs:
                    continue
                customer_signals = customer_signals_by_attr.get(attr_id)
                if not customer_signals:
                    continue
                if _get_targeting_mode(attr_id, attribute_targeting_modes) != "categorical_affinity":
                    continue

                product_values_for_attr = [
                    a.attribute_value
                    for a in attrs_by_product[product.id]
                    if a.attribute_id == attr_id
                ]
                if not product_values_for_attr:
                    continue  # no data → neutral, do not penalise
                if any(pv in customer_signals for pv in product_values_for_attr):
                    continue  # at least one value matches — not a mismatch

                weight = get_attribute_weight(attr_id)
                basis = max(customer_signals.values())
                contextual_negative_contribution += basis * weight * _CONTEXTUAL_MISMATCH_MULTIPLIER

        affinity_contribution = round(affinity_contribution, 6)
        compatibility_positive_contribution = round(compatibility_positive_contribution, 6)
        compatibility_negative_contribution = round(compatibility_negative_contribution, 6)
        contextual_negative_contribution = round(contextual_negative_contribution, 6)

        attr_set: frozenset[tuple[str, str]] = frozenset(
            (a.attribute_id, a.attribute_value) for a in attrs_by_product[product.id]
        )
        rel_matches: list[RelationshipMatch] = []
        for target_key in attr_set:
            rel_matches.extend(rel_contributions.get(target_key, []))
        rel_matches.sort(key=lambda m: (
            m.source_attribute_id, m.source_attribute_value,
            m.target_attribute_id, m.target_attribute_value,
        ))

        relationship_score = round(sum(m.contribution for m in rel_matches), 6)

        matched.sort(key=lambda m: (-(m.score * m.weight), m.attribute_id, m.attribute_value))
        direct_score = affinity_contribution  # categorical_affinity contributions only

        # Popularity for this product (workspace-wide)
        pop_score = popularity.get(product.id, 0.0)

        # Behavioral score for this product
        beh_matches = list(behavioral_contributions.get(product.id, []))
        beh_matches.sort(key=lambda m: (-m.contribution, m.source_product_id))
        behavioral_score = round(sum(m.contribution for m in beh_matches), 6)

        # Skip if no positive signal at all. The negative compatibility bucket
        # is intentionally NOT included here — a product with only a penalty
        # and no positive signal has nothing to recommend it, and the
        # meaningfulness gate below would filter it anyway.
        if (
            direct_score == 0.0
            and compatibility_positive_contribution == 0.0
            and relationship_score == 0.0
            and pop_score == 0.0
            and behavioral_score == 0.0
        ):
            continue

        # Meaningfulness gate for non-complementary products:
        # must have a core direct match, relationship signal, popularity, OR behavioral signal
        if product.recommendation_role != "complementary":
            if (
                not has_core_match
                and relationship_score == 0.0
                and pop_score == 0.0
                and behavioral_score == 0.0
            ):
                continue

        # Determine source label — only include a signal if it actually contributed
        # (score > 0 AND weight > 0). "popular" is a fallback when nothing else fires.
        # Note: only the positive compatibility bucket counts as a "direct"
        # source — a pure penalty doesn't surface a product, it only modifies
        # how an already-surfaced product ranks.
        sources = []
        if (direct_score + compatibility_positive_contribution) > 0 and direct_weight > 0:
            sources.append("direct")
        if relationship_score > 0 and relationship_weight > 0:
            sources.append("relationship")
        if behavioral_score > 0 and behavioral_weight > 0:
            sources.append("behavioral")
        if not sources:
            sources.append("popular")
        recommendation_source = "+".join(sources)

        # Build explanation
        if matched:
            direct_explanation = "Matched on " + ", ".join(
                f"{m.attribute_id}={m.attribute_value} (affinity={m.score}, weight={m.weight})"
                for m in matched
            )
        else:
            direct_explanation = ""

        if rel_matches:
            rel_explanation = "Relationships: " + ", ".join(
                f"{m.source_attribute_id}={m.source_attribute_value}"
                f"→{m.target_attribute_id}={m.target_attribute_value}"
                f" (contribution={m.contribution})"
                for m in rel_matches
            )
        else:
            rel_explanation = ""

        if beh_matches and behavioral_weight > 0:
            beh_explanation = "Behavioral: " + ", ".join(
                f"bought {m.source_product_id}→this (strength={m.strength})"
                for m in beh_matches
            )
        else:
            beh_explanation = ""

        parts = [p for p in [direct_explanation, rel_explanation, beh_explanation] if p]
        if recommendation_source == "popular" and not parts:
            explanation = f"Popular in this workspace (popularity_score={pop_score})"
        else:
            explanation = "; ".join(parts)

        candidates.append((
            product.id,
            RecommendationRead(
                product_id=product.product_id,
                sku=product.sku,
                name=product.name,
                group_id=product.group_id,
                matched_attributes=matched,
                direct_score=direct_score,
                affinity_contribution=affinity_contribution,
                compatibility_positive_contribution=compatibility_positive_contribution,
                compatibility_negative_contribution=compatibility_negative_contribution,
                contextual_negative_contribution=contextual_negative_contribution,
                relationship_score=relationship_score,
                popularity_score=pop_score,
                behavioral_score=behavioral_score,
                recommendation_score=0.0,   # placeholder — set after weighting
                recommendation_source=recommendation_source,
                explanation=explanation,
                relationship_matches=rel_matches,
                behavioral_matches=beh_matches,
            ),
            is_suppressed,
        ))

    logger.warning("DEBUG candidates after step 7: %d", len(candidates))
    if not candidates:
        return [], False

    # 8. Apply weighted scoring to every candidate.
    #    When fallback_behavior is active, effective_score may differ from
    #    final_score for candidates missing the algorithm's primary signal.
    #    effective_score is stored as the tuple's first element and used for
    #    ranking (step 11) and threshold (step 12).  recommendation_score in
    #    the response always reflects the original weighted final_score.
    scored_all: list[tuple[float, int, RecommendationRead, bool]] = []
    for db_id, rec, is_suppressed in candidates:
        # Final score: positive direct + positive compatibility contributions,
        # MINUS the compatibility and contextual penalty buckets. The negative
        # buckets are stored as non-negative magnitudes on RecommendationRead
        # and subtracted here.
        final_score = round(
            (
                rec.direct_score
                + rec.compatibility_positive_contribution
                - rec.compatibility_negative_contribution
                - rec.contextual_negative_contribution
            ) * direct_weight
            + rec.relationship_score * relationship_weight
            + rec.popularity_score * popularity_weight
            + rec.behavioral_score * behavioral_weight,
            6,
        )

        eff_score = final_score
        fallback_explanation = ""
        if fallback_behavior != "none":
            eff_score, fallback_explanation = _compute_effective_score(
                rec, final_score, algorithm, fallback_behavior,
            )

        if eff_score <= 0:
            continue

        update: dict = {"recommendation_score": final_score}
        if fallback_explanation:
            update["explanation"] = (
                f"{rec.explanation}; {fallback_explanation}" if rec.explanation else fallback_explanation
            )
            update["recommendation_source"] = f"fallback_{fallback_behavior}"

        scored_all.append((eff_score, db_id, rec.model_copy(update=update), is_suppressed))

    # 8b. Similarity-halo fallback.
    #
    # Split into unsuppressed (what we actually rank) and suppressed (what was
    # removed).  When the top suppressed score meaningfully beats the top
    # unsuppressed score, mine the top-K suppressed products for key attribute
    # values and add a per-attribute boost to unsuppressed candidates that
    # share them.  Normal ranking is unaffected whenever the trigger doesn't
    # fire — so this is inert unless suppression actually removed strong
    # candidates.
    scored: list[tuple[float, int, RecommendationRead]] = [
        (s, d, r) for s, d, r, sup in scored_all if not sup
    ]
    scored_suppressed: list[tuple[float, int, RecommendationRead]] = [
        (s, d, r) for s, d, r, sup in scored_all if sup
    ]

    similarity_fallback_triggered = False
    if scored and scored_suppressed:
        top_unsup = max(s for s, _, _ in scored)
        top_sup = max(s for s, _, _ in scored_suppressed)
        if (
            top_sup >= _SIMILARITY_FALLBACK_MIN_SUPPRESSED_SCORE
            and top_sup > top_unsup * _SIMILARITY_FALLBACK_TRIGGER_RATIO
        ):
            similarity_fallback_triggered = True
            top_sup_sorted = sorted(scored_suppressed, key=lambda t: -t[0])[
                :_SIMILARITY_HALO_TOP_K
            ]
            # Pre-enrich the top suppressed recs with multi-source signals so
            # match_confidence / product_signal_strength are populated at halo
            # time. Step 13 runs after this on the unsuppressed list; we need
            # the values earlier, only for the halo sources.
            if customer_signal_strength is not None or product_enrichment_outputs is not None:
                from app.services.multi_source_signal_service import (
                    apply_multi_source_signals,
                )
                enriched = apply_multi_source_signals(
                    [r for _s, _d, r in top_sup_sorted],
                    customer_signal_strength=customer_signal_strength,
                    product_enrichment_outputs=product_enrichment_outputs,
                    tiebreak_by_match_confidence=False,
                )
                top_sup_sorted = [
                    (s, d, er)
                    for (s, d, _orig), er in zip(top_sup_sorted, enriched)
                ]
            # Weight each suppressed source by its score relative to the top,
            # so the strongest removed match dominates the halo. Per-attribute
            # HALO_ATTR_WEIGHTS scale contribution by how diagnostic each
            # attribute is (category dominates, fit is a tiebreaker).
            norm = top_sup_sorted[0][0] or 1.0
            halo_weights: dict[tuple[str, str], float] = {}
            halo_source_products: list[str] = []
            weighted_conf_sum = 0.0
            src_weight_sum = 0.0
            for s_score, _s_db, s_rec in top_sup_sorted:
                halo_source_products.append(s_rec.product_id)
                src_weight = s_score / norm
                src_conf = s_rec.match_confidence
                if src_conf is None:
                    src_conf = s_rec.product_signal_strength
                if src_conf is None:
                    src_conf = 0.5
                weighted_conf_sum += src_weight * src_conf
                src_weight_sum += src_weight
                for m in s_rec.matched_attributes:
                    attr_w = HALO_ATTR_WEIGHTS.get(m.attribute_id)
                    if attr_w is None:
                        continue
                    key = (m.attribute_id, m.attribute_value)
                    halo_weights[key] = halo_weights.get(key, 0.0) + src_weight * attr_w

            suppressed_confidence = (
                weighted_conf_sum / src_weight_sum if src_weight_sum > 0 else 0.5
            )
            num_halo_attrs = len(HALO_ATTR_WEIGHTS)

            logger.warning(
                "DEBUG similarity fallback triggered: top_sup=%.4f top_unsup=%.4f"
                " sources=%s halo_pairs=%d suppressed_confidence=%.4f",
                top_sup, top_unsup, halo_source_products, len(halo_weights),
                suppressed_confidence,
            )

            boosted: list[tuple[float, int, RecommendationRead]] = []
            for eff_score, db_id, rec in scored:
                candidate_pairs = {
                    (m.attribute_id, m.attribute_value) for m in rec.matched_attributes
                }
                overlap_sum = sum(
                    halo_weights.get(pair, 0.0) for pair in candidate_pairs
                )
                if overlap_sum <= 0:
                    boosted.append((eff_score, db_id, rec))
                    continue
                overlap_weight = overlap_sum / num_halo_attrs
                base_score = eff_score
                raw_boost = overlap_weight * HALO_BASE_WEIGHT * suppressed_confidence
                capped_boost = round(min(raw_boost, base_score * HALO_MAX_RATIO), 6)
                logger.warning(
                    "DEBUG halo candidate=%s base_score=%.4f overlap_weight=%.4f"
                    " raw_boost=%.4f capped_boost=%.4f suppressed_confidence=%.4f"
                    " sources=%s",
                    rec.product_id, base_score, overlap_weight, raw_boost,
                    capped_boost, suppressed_confidence, halo_source_products,
                )
                new_final = round(rec.recommendation_score + capped_boost, 6)
                new_eff = round(eff_score + capped_boost, 6)
                suffix = (
                    f"similarity_fallback_boost={capped_boost}"
                    f" (from {','.join(halo_source_products)}"
                    f" conf={round(suppressed_confidence, 4)})"
                )
                new_explanation = (
                    f"{rec.explanation}; {suffix}" if rec.explanation else suffix
                )
                boosted.append((
                    new_eff,
                    db_id,
                    rec.model_copy(update={
                        "recommendation_score": new_final,
                        "explanation": new_explanation,
                    }),
                ))
            scored = boosted

    logger.warning(
        "DEBUG scored after step 8: unsuppressed=%d suppressed=%d halo=%s",
        len(scored), len(scored_suppressed), similarity_fallback_triggered,
    )
    for es, _did, r in scored[:5]:
        delta = round(es - r.recommendation_score, 6)
        logger.warning("  scored: product=%s direct=%.4f rec=%.4f eff=%.4f delta=%.4f fallback_used=%s",
                       r.product_id, r.direct_score, r.recommendation_score, es, delta, delta > 0)

    # 9. Build candidate pool — all scored candidates, keyed by db_id.
    #    Group uniqueness is NOT enforced here; it is only enforced during
    #    final selection (step 12) when max_per_group is set.
    by_group: dict[str, tuple[float, int, RecommendationRead]] = {
        f"{db_id}": (final_score, db_id, rec)
        for final_score, db_id, rec in scored
    }

    # 10. Apply slot filters — remove candidates that don't match all filters.
    #     If filtering empties the set and fallback_mode is "relax_filters",
    #     fall back to the unfiltered candidate pool (no score recomputation).
    fallback_applied = False
    if slot_filters:
        filtered = {
            k: v for k, v in by_group.items()
            if _product_passes_filters(v[1], attrs_by_product, slot_filters)
        }
        if filtered or fallback_mode == "strict":
            by_group = filtered
        else:
            # fallback_mode == "relax_filters" — keep by_group as-is
            fallback_applied = True

    # 11. Sort by final_score DESC, then algorithm-specific tie-break fields,
    #     then internal product PK ASC (deterministic fallback).
    #     Numeric fields sort DESC (negated); string fields sort ASC naturally.
    def _sort_key(t: tuple[float, int, RecommendationRead]):
        final_score, db_id, rec = t
        key: list = [-final_score]
        for field in (tie_break_priority or []):
            val = getattr(rec, field, 0.0)
            if isinstance(val, str):
                key.append(val)
            else:
                key.append(-val)
        key.append(float(db_id))
        return key

    ranked = sorted(by_group.values(), key=_sort_key)
    logger.warning("DEBUG ranked count: %d (max_scan_depth=%s, min_score_threshold=%s)",
                   len(ranked), max_scan_depth, min_score_threshold)

    # 12. Selection with scan cap — single pass over the ranked pool.
    #     Skips excluded products (cross-slot) and over-represented groups (diversity).
    #     Continues scanning past skipped candidates until top_n results are
    #     selected OR max_scan_depth candidates have been inspected.
    _excluded_pids = excluded_product_ids or set()
    _excluded_gids = excluded_group_ids or set()
    selected_product_ids: set[str] = set()
    group_usage: dict[str, int] = {}
    results: list[RecommendationRead] = []
    scanned_count = 0

    for eff_score, _, rec in ranked:
        if max_scan_depth is not None and scanned_count >= max_scan_depth:
            logger.warning("DEBUG scan depth limit hit at %d", scanned_count)
            break

        scanned_count += 1

        fallback_delta = round(eff_score - rec.recommendation_score, 6)
        logger.warning(
            "DEBUG candidate #%d product=%s direct=%.4f rec=%.4f eff=%.4f threshold=%s delta=%.4f",
            scanned_count, rec.product_id, rec.direct_score, rec.recommendation_score,
            eff_score, min_score_threshold, fallback_delta,
        )

        if min_score_threshold is not None and eff_score < min_score_threshold:
            continue
        if rec.product_id in selected_product_ids:
            continue
        if rec.product_id in _excluded_pids:
            continue
        if rec.group_id and rec.group_id in _excluded_gids:
            continue
        if max_per_group is not None and rec.group_id is not None:
            if group_usage.get(rec.group_id, 0) >= max_per_group:
                continue
            group_usage[rec.group_id] = group_usage.get(rec.group_id, 0) + 1
        selected_product_ids.add(rec.product_id)
        results.append(rec)
        if len(results) >= top_n:
            break

    # 13. Starter-mode multi-source signal layer. Additive and opt-in:
    #     only runs when the caller passes signal inputs. When nothing is
    #     passed, `results` is returned exactly as built above.
    if (
        customer_signal_strength is not None
        or product_enrichment_outputs is not None
        or tiebreak_by_match_confidence
    ):
        from app.services.multi_source_signal_service import apply_multi_source_signals

        results = apply_multi_source_signals(
            results,
            customer_signal_strength=customer_signal_strength,
            product_enrichment_outputs=product_enrichment_outputs,
            tiebreak_by_match_confidence=tiebreak_by_match_confidence,
        )

    # 14. Low-signal penalty — demote products with weak enrichment coverage.
    #     Uses product_signal_strength populated by step 13.  Products with
    #     no enrichment data (None) are treated as 0.0 — maximum penalty —
    #     so unenriched products never outrank enriched ones by default.
    #     Re-sorts afterward so rankings reflect the adjusted scores.
    any_penalty = False
    for i, rec in enumerate(results):
        effective_strength = rec.product_signal_strength if rec.product_signal_strength is not None else 0.0
        penalty = round(
            (1.0 - effective_strength) * _LOW_SIGNAL_PENALTY_WEIGHT, 6
        )
        if penalty > 0:
            new_score = round(rec.recommendation_score - penalty, 6)
            results[i] = rec.model_copy(update={
                "low_signal_penalty": penalty,
                "recommendation_score": new_score,
            })
            any_penalty = True

    if any_penalty:
        results.sort(
            key=lambda r: (
                -r.recommendation_score,
                -(r.match_confidence if r.match_confidence is not None else -1.0),
            ),
        )

    # 15. Soft diversity shaping. Runs last so it operates on the final
    #     ranked list, after scoring, halo, suppression, and penalties.
    #     Rank #1 is preserved; ranks 2..N are re-picked with a category
    #     repetition penalty, gated by a closeness band.
    if not disable_diversity_shaping:
        results, diversity_movements = _apply_diversity_shaping(results)
        if diversity_movements:
            logger.warning(
                "DEBUG diversity shaping moved %d products: %s",
                len(diversity_movements), diversity_movements,
            )

    return results, fallback_applied
