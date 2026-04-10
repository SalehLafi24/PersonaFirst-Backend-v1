import logging

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
CORE_ATTRS: frozenset[str] = frozenset({"type", "activity", "category"})
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

    # 1. Build suppression sets
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

    if affinities:
        affinity_map = {
            (a.attribute_id, a.attribute_value): a.score for a in affinities
        }

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
    candidates: list[tuple[int, RecommendationRead]] = []

    for product in products:
        # Suppression checks
        if product.product_id in suppressed_product_ids:
            continue
        group_key = product.group_id if product.group_id else product.product_id
        if group_key in suppressed_group_keys:
            continue
        if _is_functionally_suppressed(product, attrs_by_product, suppressed_functional_sigs):
            continue

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
        affinity_contribution: float = 0.0      # categorical_affinity — soft preference
        compatibility_contribution: float = 0.0  # compatibility_signal — suitability/fit
        # categorical_filter    → filter_exclusion: handled as pre-scoring gate above
        # descriptive_metadata  → metadata_ignored: not scored

        if affinity_map:
            for attr in attrs_by_product[product.id]:
                key = (attr.attribute_id, attr.attribute_value)
                if key not in affinity_map:
                    continue

                aff_score = affinity_map[key]
                weight = get_attribute_weight(attr.attribute_id)
                mode = _get_targeting_mode(attr.attribute_id, attribute_targeting_modes)

                if mode == "categorical_affinity":
                    # Soft preference signal — accumulates into direct_score
                    affinity_contribution += aff_score * weight
                    matched.append(MatchedAttribute(
                        attribute_id=attr.attribute_id,
                        attribute_value=attr.attribute_value,
                        score=aff_score,
                        weight=weight,
                        targeting_mode=mode,
                    ))
                    if attr.attribute_id in CORE_ATTRS:
                        has_core_match = True

                elif mode == "compatibility_signal":
                    # Suitability/fit — heavier weight, own contribution bucket
                    compatibility_contribution += aff_score * weight * _COMPATIBILITY_SCORE_MULTIPLIER
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

        affinity_contribution = round(affinity_contribution, 6)
        compatibility_contribution = round(compatibility_contribution, 6)

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

        # Skip if no signal at all
        if (
            direct_score == 0.0
            and compatibility_contribution == 0.0
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
        sources = []
        if (direct_score + compatibility_contribution) > 0 and direct_weight > 0:
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
                compatibility_contribution=compatibility_contribution,
                relationship_score=relationship_score,
                popularity_score=pop_score,
                behavioral_score=behavioral_score,
                recommendation_score=0.0,   # placeholder — set after weighting
                recommendation_source=recommendation_source,
                explanation=explanation,
                relationship_matches=rel_matches,
                behavioral_matches=beh_matches,
            ),
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
    scored: list[tuple[float, int, RecommendationRead]] = []
    for db_id, rec in candidates:
        final_score = round(
            (rec.direct_score + rec.compatibility_contribution) * direct_weight
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

        scored.append((eff_score, db_id, rec.model_copy(update=update)))

    logger.warning("DEBUG scored after step 8: %d", len(scored))
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

    return results, fallback_applied
