from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.schemas.affinity_generate import AffinityGenerateResult
from app.schemas.customer_attribute_affinity import AffinityCreate


def bulk_create_affinities(
    db: Session, workspace_id: int, items: list[AffinityCreate]
) -> list[CustomerAttributeAffinity]:
    records = [
        CustomerAttributeAffinity(
            workspace_id=workspace_id,
            customer_id=item.customer_id,
            attribute_id=item.attribute_name,
            attribute_value=item.value_label,
            score=item.score,
        )
        for item in items
    ]
    db.add_all(records)
    db.commit()
    for record in records:
        db.refresh(record)
    return records


def list_affinities(
    db: Session,
    workspace_id: int,
    min_score: float | None = None,
    sort_by_score_desc: bool = False,
) -> list[CustomerAttributeAffinity]:
    q = db.query(CustomerAttributeAffinity).filter(
        CustomerAttributeAffinity.workspace_id == workspace_id
    )
    if min_score is not None:
        q = q.filter(CustomerAttributeAffinity.score >= min_score)
    if sort_by_score_desc:
        q = q.order_by(CustomerAttributeAffinity.score.desc())
    return q.all()


def generate_affinities_from_purchases(
    db: Session,
    workspace_id: int,
    customer_id: str | None = None,
) -> AffinityGenerateResult:
    """
    For each customer, count (attribute_id, attribute_value) occurrences across
    their purchased products, normalize by max count, and upsert into
    customer_attribute_affinities.
    """
    q = db.query(CustomerPurchase).filter(CustomerPurchase.workspace_id == workspace_id)
    if customer_id:
        q = q.filter(CustomerPurchase.customer_id == customer_id)
    purchases = q.all()

    # Group purchases by customer
    by_customer: dict[str, list[CustomerPurchase]] = defaultdict(list)
    for p in purchases:
        by_customer[p.customer_id].append(p)

    if not by_customer:
        return AffinityGenerateResult(customers_processed=0, affinities_upserted=0)

    # Load all products for this workspace, keyed by internal PK for FK-based joins
    all_products = db.query(Product).filter(Product.workspace_id == workspace_id).all()
    products_by_db_id: dict[int, Product] = {p.id: p for p in all_products}

    # Load all product attributes for workspace products
    product_db_ids = [p.id for p in all_products]
    all_attrs = (
        db.query(ProductAttribute)
        .filter(ProductAttribute.product_id.in_(product_db_ids))
        .all()
        if product_db_ids else []
    )
    attrs_by_product_db_id: dict[int, list[ProductAttribute]] = defaultdict(list)
    for attr in all_attrs:
        attrs_by_product_db_id[attr.product_id].append(attr)

    total_upserted = 0

    for cust_id, cust_purchases in by_customer.items():
        # Count (attribute_id, attribute_value) occurrences across all purchases
        pair_counts: dict[tuple[str, str], int] = defaultdict(int)
        for purchase in cust_purchases:
            product = products_by_db_id.get(purchase.product_db_id)
            if product is None:
                continue
            for attr in attrs_by_product_db_id[product.id]:
                pair_counts[(attr.attribute_id, attr.attribute_value)] += purchase.quantity

        if not pair_counts:
            continue

        max_count = max(pair_counts.values())

        for (attr_id, attr_val), count in pair_counts.items():
            score = round(count / max_count, 6)
            existing = (
                db.query(CustomerAttributeAffinity)
                .filter_by(
                    workspace_id=workspace_id,
                    customer_id=cust_id,
                    attribute_id=attr_id,
                    attribute_value=attr_val,
                )
                .first()
            )
            if existing:
                existing.score = score
            else:
                db.add(
                    CustomerAttributeAffinity(
                        workspace_id=workspace_id,
                        customer_id=cust_id,
                        attribute_id=attr_id,
                        attribute_value=attr_val,
                        score=score,
                    )
                )
            total_upserted += 1

    db.commit()
    return AffinityGenerateResult(
        customers_processed=len(by_customer),
        affinities_upserted=total_upserted,
    )
