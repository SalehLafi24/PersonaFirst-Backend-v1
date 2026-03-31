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
    diversity_enabled: bool = False,
    excluded_product_ids: set[str] | None = None,
) -> tuple[list[RecommendationRead], bool]:
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

    # 2. Load all products and attributes
    products = db.query(Product).filter(Product.workspace_id == workspace_id).all()
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

        # Direct + relationship scoring
        matched: list[MatchedAttribute] = []
        has_core_match = False

        if affinity_map:
            for attr in attrs_by_product[product.id]:
                key = (attr.attribute_id, attr.attribute_value)
                if key in affinity_map:
                    weight = get_attribute_weight(attr.attribute_id)
                    matched.append(
                        MatchedAttribute(
                            attribute_id=attr.attribute_id,
                            attribute_value=attr.attribute_value,
                            score=affinity_map[key],
                            weight=weight,
                        )
                    )
                    if attr.attribute_id in CORE_ATTRS:
                        has_core_match = True

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
        direct_score = round(sum(m.score * m.weight for m in matched), 6)

        # Popularity for this product (workspace-wide)
        pop_score = popularity.get(product.id, 0.0)

        # Behavioral score for this product
        beh_matches = list(behavioral_contributions.get(product.id, []))
        beh_matches.sort(key=lambda m: (-m.contribution, m.source_product_id))
        behavioral_score = round(sum(m.contribution for m in beh_matches), 6)

        # Skip if no signal at all
        if (
            direct_score == 0.0
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
        if direct_score > 0 and direct_weight > 0:
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

    if not candidates:
        return [], False

    # 8. Apply weighted scoring to every candidate
    scored: list[tuple[float, int, RecommendationRead]] = []
    for db_id, rec in candidates:
        final_score = round(
            rec.direct_score * direct_weight
            + rec.relationship_score * relationship_weight
            + rec.popularity_score * popularity_weight
            + rec.behavioral_score * behavioral_weight,
            6,
        )
        if final_score <= 0:
            continue
        scored.append((
            final_score,
            db_id,
            rec.model_copy(update={"recommendation_score": final_score}),
        ))

    # 9. Deduplicate by group — keep highest final_score per group.
    #    When diversity_enabled, skip dedup here; the diversity-aware selection
    #    in step 11 enforces max-1-per-group from the full ranked pool instead.
    if diversity_enabled:
        by_group: dict[str, tuple[float, int, RecommendationRead]] = {
            f"{db_id}": (final_score, db_id, rec)
            for final_score, db_id, rec in scored
        }
    else:
        by_group: dict[str, tuple[float, int, RecommendationRead]] = {}
        for final_score, db_id, rec in scored:
            key = rec.group_id if rec.group_id else rec.product_id
            if key not in by_group or final_score > by_group[key][0]:
                by_group[key] = (final_score, db_id, rec)

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

    # 12. Selection with refill — single pass over the ranked pool.
    #     Skips excluded products (cross-slot) and duplicate groups (diversity).
    #     Continues scanning past skipped candidates until top_n or pool exhausted.
    _excluded = excluded_product_ids or set()
    seen_groups: set[str] = set()
    results: list[RecommendationRead] = []

    for _, _, rec in ranked:
        if rec.product_id in _excluded:
            continue
        if diversity_enabled:
            gkey = rec.group_id if rec.group_id else rec.product_id
            if gkey in seen_groups:
                continue
            seen_groups.add(gkey)
        results.append(rec)
        if len(results) >= top_n:
            break

    return results, fallback_applied
