from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product
from app.schemas.purchase import PurchaseCreate


def bulk_create_purchases(
    db: Session, workspace_id: int, items: list[PurchaseCreate]
) -> list[CustomerPurchase]:
    # Resolve all external product_ids to Product records
    ext_ids = list({item.product_id for item in items})
    products = (
        db.query(Product)
        .filter(Product.workspace_id == workspace_id, Product.product_id.in_(ext_ids))
        .all()
    )
    product_map: dict[str, Product] = {p.product_id: p for p in products}

    # All referenced product_ids must exist in this workspace
    missing = [pid for pid in ext_ids if pid not in product_map]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown product_id(s) for this workspace: {missing}",
        )

    records = [
        CustomerPurchase(
            workspace_id=workspace_id,
            customer_id=item.customer_id,
            product_db_id=product_map[item.product_id].id,
            product_id=item.product_id,  # denormalized external string
            group_id=product_map[item.product_id].group_id,
            order_date=item.order_date,
            quantity=item.quantity,
            revenue=item.revenue,
        )
        for item in items
    ]
    db.add_all(records)
    db.commit()
    for r in records:
        db.refresh(r)
    return records
