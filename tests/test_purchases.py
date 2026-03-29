from datetime import date

from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku, name=name, group_id=group_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------------------------------------------------------------------------
# POST /workspaces/{workspace_id}/purchases
# ---------------------------------------------------------------------------

def test_bulk_create_purchases(client, db):
    ws = make_workspace(client, "P1", "p1")
    wid = ws["id"]
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="group_yoga")

    response = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "cust_1", "product_id": "prod_1", "order_date": "2026-01-15",
         "quantity": 2, "revenue": 39.98},
    ])
    assert response.status_code == 201
    data = response.json()
    assert len(data) == 1
    assert data[0]["customer_id"] == "cust_1"
    assert data[0]["product_id"] == "prod_1"
    assert data[0]["group_id"] == "group_yoga"
    assert data[0]["order_date"] == "2026-01-15"
    assert data[0]["quantity"] == 2
    assert data[0]["revenue"] == 39.98
    assert data[0]["workspace_id"] == wid


def test_stores_internal_fk_and_returns_external_product_id(client, db):
    """DB row stores product_db_id (int FK); API response returns external product_id string."""
    ws = make_workspace(client, "P-FK", "p-fk")
    wid = ws["id"]
    product = seed_product(db, wid, "ext-123", "SKU-X", "Widget")

    resp = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "c1", "product_id": "ext-123", "order_date": "2026-03-01"},
    ])
    assert resp.status_code == 201
    assert resp.json()[0]["product_id"] == "ext-123"

    # Verify DB row stores int FK
    row = db.query(CustomerPurchase).filter_by(workspace_id=wid).first()
    assert row.product_db_id == product.id
    assert row.product_id == "ext-123"


def test_unknown_product_id_returns_422(client, db):
    """Purchasing an unknown product_id (not in workspace) must return 422."""
    ws = make_workspace(client, "P-422", "p-422")
    wid = ws["id"]

    resp = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "c1", "product_id": "ghost_prod", "order_date": "2026-02-01"},
    ])
    assert resp.status_code == 422


def test_group_id_denormalized_from_product(client, db):
    ws = make_workspace(client, "P2", "p2")
    wid = ws["id"]
    seed_product(db, wid, "prod_1", "SKU-001", "Product A", group_id="my_group")

    resp = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "c1", "product_id": "prod_1", "order_date": "2026-02-01"},
    ])
    assert resp.json()[0]["group_id"] == "my_group"


def test_bulk_create_multiple_purchases(client, db):
    ws = make_workspace(client, "P4", "p4")
    wid = ws["id"]
    seed_product(db, wid, "prod_1", "SKU-001", "Product A", group_id="g1")
    seed_product(db, wid, "prod_2", "SKU-002", "Product B", group_id="g2")

    resp = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "c1", "product_id": "prod_1", "order_date": "2026-01-01"},
        {"customer_id": "c1", "product_id": "prod_2", "order_date": "2026-01-02"},
        {"customer_id": "c2", "product_id": "prod_1", "order_date": "2026-01-03"},
    ])
    assert resp.status_code == 201
    assert len(resp.json()) == 3


def test_workspace_not_found(client):
    resp = client.post("/workspaces/999/purchases", json=[
        {"customer_id": "c1", "product_id": "p1", "order_date": "2026-01-01"},
    ])
    assert resp.status_code == 404


def test_quantity_defaults_to_one(client, db):
    ws = make_workspace(client, "P5", "p5")
    wid = ws["id"]
    seed_product(db, wid, "prod_1", "SKU-001", "Product A")

    resp = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "c1", "product_id": "prod_1", "order_date": "2026-01-01"},
    ])
    assert resp.json()[0]["quantity"] == 1


def test_revenue_optional(client, db):
    ws = make_workspace(client, "P6", "p6")
    wid = ws["id"]
    seed_product(db, wid, "prod_1", "SKU-001", "Product A")

    resp = client.post(f"/workspaces/{wid}/purchases", json=[
        {"customer_id": "c1", "product_id": "prod_1", "order_date": "2026-01-01"},
    ])
    assert resp.json()[0]["revenue"] is None
