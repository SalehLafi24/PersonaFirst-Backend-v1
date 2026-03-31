from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.attribute_value_relationship import AttributeValueRelationship
from app.models.customer_attribute_affinity import CustomerAttributeAffinity

# Structural/system attribute IDs to exclude from co-occurrence analysis
DEFAULT_EXCLUDED_ATTRIBUTES: set[str] = {"group_id"}


def run_relationship_engine(
    db: Session,
    workspace_id: int,
    min_confidence: float = 0.1,
    min_lift: float = 1.0,
    min_pair_count: int = 2,
    excluded_attribute_ids: set[str] | None = None,
) -> int:
    """
    Reads customer attribute affinities for a workspace, computes co-occurrence
    statistics, and inserts suggested complementary relationships.

    Returns the number of new relationships created.
    """
    if excluded_attribute_ids is None:
        excluded_attribute_ids = DEFAULT_EXCLUDED_ATTRIBUTES

    rows = (
        db.query(CustomerAttributeAffinity)
        .filter(CustomerAttributeAffinity.workspace_id == workspace_id)
        .all()
    )

    if not rows:
        return 0

    # Build per-customer baskets of (attribute_id, value) pairs,
    # excluding structural fields and ensuring uniqueness via set.
    baskets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        if row.attribute_id in excluded_attribute_ids:
            continue
        baskets[row.customer_id].add((row.attribute_id, row.attribute_value))

    total_customers = len(baskets)
    if total_customers == 0:
        return 0

    # Support: how many customers have each (attribute_id, value) item
    support: dict[tuple[str, str], int] = defaultdict(int)
    for basket in baskets.values():
        for item in basket:
            support[item] += 1

    # Co-occurrence: for each ordered pair (A, B) from different attributes,
    # count customers who have both. Uses sorted() for deterministic order.
    cooccur: dict[tuple[str, str], dict[tuple[str, str], int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for basket in baskets.values():
        items = sorted(basket)
        for a in items:
            for b in items:
                if a == b or a[0] == b[0]:  # same item or same attribute — skip
                    continue
                cooccur[a][b] += 1

    # Score pairs and insert new suggested relationships
    created = 0
    for a, b_counts in cooccur.items():
        for b, count in b_counts.items():
            if count < min_pair_count:
                continue

            confidence = count / support[a]
            support_b_rate = support[b] / total_customers
            lift = confidence / support_b_rate if support_b_rate > 0 else 0.0

            if confidence < min_confidence or lift < min_lift:
                continue

            # Idempotent: skip if this directed relationship already exists
            exists = (
                db.query(AttributeValueRelationship)
                .filter(
                    AttributeValueRelationship.workspace_id == workspace_id,
                    AttributeValueRelationship.source_attribute_id == a[0],
                    AttributeValueRelationship.source_value == a[1],
                    AttributeValueRelationship.target_attribute_id == b[0],
                    AttributeValueRelationship.target_value == b[1],
                )
                .first()
            )
            if exists:
                continue

            db.add(
                AttributeValueRelationship(
                    workspace_id=workspace_id,
                    source_attribute_id=a[0],
                    source_value=a[1],
                    target_attribute_id=b[0],
                    target_value=b[1],
                    confidence=round(confidence, 6),
                    strength=round(confidence, 6),
                    lift=round(lift, 6),
                    pair_count=count,
                    status="suggested",
                )
            )
            created += 1

    db.commit()
    return created
