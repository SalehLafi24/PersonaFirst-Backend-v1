from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.customer_purchase import CustomerPurchase
from app.models.product_behavior_relationship import ProductBehaviorRelationship


def run_behavior_engine(db: Session, workspace_id: int) -> int:
    """
    Compute directional product co-purchase relationships for a workspace.

    strength(A → B) = customers_who_bought_both_A_and_B / customers_who_bought_A

    Performs a full refresh: deletes all existing rows for the workspace, then
    inserts freshly computed ones.

    Returns the number of relationships created.
    """
    purchases = (
        db.query(CustomerPurchase)
        .filter(CustomerPurchase.workspace_id == workspace_id)
        .all()
    )

    if not purchases:
        return 0

    # Group by customer: customer_id → set of distinct product_db_ids
    customer_products: dict[str, set[int]] = defaultdict(set)
    for p in purchases:
        customer_products[p.customer_id].add(p.product_db_id)

    # source_count[A]      = number of distinct customers who bought A
    # overlap_count[(A,B)] = number of customers who bought both A and B (A≠B)
    source_count: dict[int, int] = defaultdict(int)
    overlap_count: dict[tuple[int, int], int] = defaultdict(int)

    for product_ids in customer_products.values():
        sorted_ids = sorted(product_ids)  # deterministic iteration order
        for a in sorted_ids:
            source_count[a] += 1
            for b in sorted_ids:
                if a != b:
                    overlap_count[(a, b)] += 1

    # Full refresh — delete all existing rows for this workspace before reinserting
    db.query(ProductBehaviorRelationship).filter(
        ProductBehaviorRelationship.workspace_id == workspace_id
    ).delete()

    created = 0
    for (a, b), overlap in sorted(overlap_count.items()):  # sorted for deterministic insert order
        src_count = source_count[a]
        if src_count == 0:
            continue

        db.add(
            ProductBehaviorRelationship(
                workspace_id=workspace_id,
                source_product_db_id=a,
                target_product_db_id=b,
                strength=round(overlap / src_count, 6),
                customer_overlap_count=overlap,
                source_customer_count=src_count,
            )
        )
        created += 1

    db.commit()
    return created
