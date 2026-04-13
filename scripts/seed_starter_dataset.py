"""Seed the PersonaFirst starter dataset.

Loads data from seed_data/ and persists it via the service / ORM layer
(no raw SQL):
    1. Validates attribute definitions against the AttributeDefinition schema
    2. Creates a workspace via workspace_service.create_workspace
    3. Creates Product and ProductAttribute rows via the ORM
    4. Loads customer list from customers.json (customers are implicit —
       they are registered via their first purchase)
    5. Ingests purchases via purchase_service.bulk_create_purchases

Idempotent for the fixed slug "personafirst-starter": if that workspace
already exists, its products / product_attributes / purchases / affinities
are cleared before re-seeding.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from app.core.database import SessionLocal
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.workspace import Workspace
from app.schemas.attribute_enrichment import AttributeDefinition
from app.schemas.purchase import PurchaseCreate
from app.schemas.workspace import WorkspaceCreate
from app.services.purchase_service import bulk_create_purchases
from app.services.workspace_service import create_workspace

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed_data"
WORKSPACE_SLUG = "personafirst-starter"
WORKSPACE_NAME = "PersonaFirst Starter"


def _load_json(filename: str):
    with (SEED_DIR / filename).open(encoding="utf-8") as f:
        return json.load(f)


def _get_or_reset_workspace(db) -> Workspace:
    existing = db.query(Workspace).filter(Workspace.slug == WORKSPACE_SLUG).first()
    if existing is None:
        return create_workspace(
            db, WorkspaceCreate(name=WORKSPACE_NAME, slug=WORKSPACE_SLUG)
        )
    # Reset: drop previous seed data in this workspace only.
    product_ids = [
        pid for (pid,) in db.query(Product.id)
        .filter(Product.workspace_id == existing.id).all()
    ]
    if product_ids:
        db.query(ProductAttribute).filter(
            ProductAttribute.product_id.in_(product_ids)
        ).delete(synchronize_session=False)
    db.query(CustomerPurchase).filter(
        CustomerPurchase.workspace_id == existing.id
    ).delete(synchronize_session=False)
    db.query(CustomerAttributeAffinity).filter(
        CustomerAttributeAffinity.workspace_id == existing.id
    ).delete(synchronize_session=False)
    db.query(Product).filter(
        Product.workspace_id == existing.id
    ).delete(synchronize_session=False)
    db.commit()
    return existing


def _validate_attribute_definitions(defs_raw: list[dict]) -> list[AttributeDefinition]:
    return [AttributeDefinition(**d) for d in defs_raw]


def _seed_products(db, workspace_id: int, products_raw: list[dict]) -> int:
    created = 0
    for p in products_raw:
        product = Product(
            workspace_id=workspace_id,
            product_id=p["product_id"],
            sku=p["sku"],
            name=p["name"],
            group_id=p.get("category"),  # use category as group_id
        )
        db.add(product)
        db.flush()  # populate product.id without full commit
        # Category as an explicit attribute too.
        attrs: list[ProductAttribute] = [
            ProductAttribute(
                product_id=product.id,
                attribute_id="category",
                attribute_value=p["category"],
            )
        ]
        for k, v in (p.get("catalog_attributes") or {}).items():
            attrs.append(
                ProductAttribute(
                    product_id=product.id,
                    attribute_id=k,
                    attribute_value=str(v),
                )
            )
        db.add_all(attrs)
        created += 1
    db.commit()
    return created


def _seed_purchases(
    db, workspace_id: int, purchases_raw: list[dict]
) -> list[CustomerPurchase]:
    items = [
        PurchaseCreate(
            customer_id=row["customer_id"],
            product_id=row["product_id"],
            order_date=date.fromisoformat(row["order_date"]),
            quantity=row.get("quantity", 1),
            revenue=row.get("revenue"),
        )
        for row in purchases_raw
    ]
    return bulk_create_purchases(db, workspace_id, items)


def _sample_joined_records(db, workspace_id: int, limit: int = 5) -> list[dict]:
    rows = (
        db.query(CustomerPurchase, Product)
        .join(Product, CustomerPurchase.product_db_id == Product.id)
        .filter(CustomerPurchase.workspace_id == workspace_id)
        .order_by(CustomerPurchase.order_date.asc(), CustomerPurchase.id.asc())
        .limit(limit)
        .all()
    )
    result = []
    for purchase, product in rows:
        attrs = (
            db.query(ProductAttribute)
            .filter(ProductAttribute.product_id == product.id)
            .all()
        )
        result.append(
            {
                "customer_id": purchase.customer_id,
                "product_id": product.product_id,
                "sku": product.sku,
                "name": product.name,
                "category": product.group_id,
                "order_date": purchase.order_date.isoformat(),
                "quantity": purchase.quantity,
                "revenue": purchase.revenue,
                "attributes": {a.attribute_id: a.attribute_value for a in attrs},
            }
        )
    return result


def main() -> dict:
    db = SessionLocal()
    try:
        # 1. Attribute definitions — validate via the pydantic schema (no DB table).
        defs_raw = _load_json("attribute_definitions.json")
        defs = _validate_attribute_definitions(defs_raw)

        # 2. Workspace via service layer.
        ws = _get_or_reset_workspace(db)

        # 3. Products via ORM (no raw SQL).
        products_raw = _load_json("products.json")
        n_products = _seed_products(db, ws.id, products_raw)

        # 4. Customers: registered implicitly via purchases.
        customers_raw = _load_json("customers.json")
        declared_customer_ids = {c["customer_id"] for c in customers_raw}

        # 5. Purchases via purchase_service.
        purchases_raw = _load_json("purchases.json")
        created_purchases = _seed_purchases(db, ws.id, purchases_raw)

        # Count what's actually in the DB.
        total_products = (
            db.query(Product).filter(Product.workspace_id == ws.id).count()
        )
        total_purchases = (
            db.query(CustomerPurchase)
            .filter(CustomerPurchase.workspace_id == ws.id)
            .count()
        )
        distinct_customer_ids = {
            row.customer_id
            for row in db.query(CustomerPurchase.customer_id)
            .filter(CustomerPurchase.workspace_id == ws.id)
            .distinct()
            .all()
        }

        sample_rows = _sample_joined_records(db, ws.id, limit=5)

        summary = {
            "workspace": {"id": ws.id, "slug": ws.slug, "name": ws.name},
            "attribute_definitions": [d.name for d in defs],
            "totals": {
                "attribute_definitions": len(defs),
                "products": total_products,
                "customers_declared": len(declared_customer_ids),
                "customers_registered": len(distinct_customer_ids),
                "purchases": total_purchases,
            },
            "sample_joined_records": sample_rows,
        }

        print(json.dumps(summary, indent=2, default=str))
        return summary
    finally:
        db.close()


if __name__ == "__main__":
    main()
