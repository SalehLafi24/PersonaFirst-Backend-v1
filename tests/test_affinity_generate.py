from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None, attributes=None):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku, name=name, group_id=group_id)
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id, attribute_value=attr_val))
    db.commit()
    return p


def seed_purchase(db, workspace_id, customer_id, product_id, group_id=None, order_date=None):
    product = db.query(Product).filter_by(workspace_id=workspace_id, product_id=product_id).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id,
        customer_id=customer_id,
        product_db_id=product.id,
        product_id=product_id,
        group_id=group_id if group_id is not None else (product.group_id if product else None),
        order_date=order_date or date(2026, 1, 1),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# POST /workspaces/{workspace_id}/affinities/generate
# ---------------------------------------------------------------------------

def test_generate_creates_affinities_from_purchases(client, db):
    ws = make_workspace(client, "AG1", "ag1")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g1", attributes=[
        ("lifestyle", "yoga"),
        ("pregnancy_stage", "pregnant"),
    ])
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g1")

    resp = client.post(f"/workspaces/{wid}/affinities/generate")
    assert resp.status_code == 200
    result = resp.json()
    assert result["customers_processed"] == 1
    assert result["affinities_upserted"] == 2

    affinities = db.query(CustomerAttributeAffinity).filter_by(
        workspace_id=wid, customer_id="cust_1"
    ).all()
    assert len(affinities) == 2
    scores = {(a.attribute_id, a.attribute_value): a.score for a in affinities}
    # Both attributes appear once → both score = 1.0
    assert scores[("lifestyle", "yoga")] == 1.0
    assert scores[("pregnancy_stage", "pregnant")] == 1.0


def test_generate_normalizes_score_by_max_count(client, db):
    """
    cust_1 buys prod_1 (lifestyle=yoga) and prod_2 (lifestyle=yoga, size=large).
    lifestyle=yoga appears 2x, size=large appears 1x → max=2.
    Expected: lifestyle=yoga score=1.0, size=large score=0.5
    """
    ws = make_workspace(client, "AG2", "ag2")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g1", attributes=[
        ("lifestyle", "yoga"),
    ])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Kit", group_id="g2", attributes=[
        ("lifestyle", "yoga"),
        ("size", "large"),
    ])
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g1")
    seed_purchase(db, wid, "cust_1", "prod_2", group_id="g2")

    client.post(f"/workspaces/{wid}/affinities/generate")

    affinities = db.query(CustomerAttributeAffinity).filter_by(
        workspace_id=wid, customer_id="cust_1"
    ).all()
    scores = {(a.attribute_id, a.attribute_value): a.score for a in affinities}
    assert scores[("lifestyle", "yoga")] == 1.0
    assert scores[("size", "large")] == 0.5


def test_generate_strongest_affinity_is_always_1(client, db):
    ws = make_workspace(client, "AG3", "ag3")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "A", attributes=[("a", "x"), ("b", "y"), ("c", "z")])
    seed_purchase(db, wid, "cust_1", "prod_1")
    seed_purchase(db, wid, "cust_1", "prod_1")  # buy again → a=x,b=y,c=z all count=2

    client.post(f"/workspaces/{wid}/affinities/generate")

    affinities = db.query(CustomerAttributeAffinity).filter_by(
        workspace_id=wid, customer_id="cust_1"
    ).all()
    max_score = max(a.score for a in affinities)
    assert max_score == 1.0


def test_generate_upserts_on_rerun(client, db):
    """Running generate twice updates scores rather than creating duplicates."""
    ws = make_workspace(client, "AG4", "ag4")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "A", attributes=[("lifestyle", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_1")

    client.post(f"/workspaces/{wid}/affinities/generate")
    client.post(f"/workspaces/{wid}/affinities/generate")  # rerun

    count = db.query(CustomerAttributeAffinity).filter_by(
        workspace_id=wid, customer_id="cust_1"
    ).count()
    assert count == 1  # no duplicates


def test_generate_filtered_to_customer_id(client, db):
    ws = make_workspace(client, "AG5", "ag5")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "A", attributes=[("lifestyle", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_1")
    seed_purchase(db, wid, "cust_2", "prod_1")

    resp = client.post(f"/workspaces/{wid}/affinities/generate?customer_id=cust_1")
    assert resp.json()["customers_processed"] == 1

    cust_2_affinities = db.query(CustomerAttributeAffinity).filter_by(
        workspace_id=wid, customer_id="cust_2"
    ).all()
    assert cust_2_affinities == []  # cust_2 not processed


def test_generate_no_purchases_returns_zero(client, db):
    ws = make_workspace(client, "AG6", "ag6")
    resp = client.post(f"/workspaces/{ws['id']}/affinities/generate")
    assert resp.status_code == 200
    assert resp.json() == {"customers_processed": 0, "affinities_upserted": 0}



def test_generate_workspace_not_found(client):
    resp = client.post("/workspaces/999/affinities/generate")
    assert resp.status_code == 404


def test_generate_weights_by_quantity(client, db):
    """
    cust_1 buys prod_1 (lifestyle=yoga) with quantity=3 and
    prod_2 (lifestyle=yoga, size=large) with quantity=1.
    lifestyle=yoga weighted count = 3+1 = 4, size=large weighted count = 1.
    max = 4 → lifestyle=yoga score=1.0, size=large score=0.25
    """
    ws = make_workspace(client, "AG8", "ag8")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", attributes=[
        ("lifestyle", "yoga"),
    ])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Kit", attributes=[
        ("lifestyle", "yoga"),
        ("size", "large"),
    ])
    seed_purchase(db, wid, "cust_1", "prod_1", order_date=date(2026, 1, 1))
    # Override quantity via direct DB insert since seed_purchase uses default quantity=1
    from app.models.customer_purchase import CustomerPurchase
    db.query(CustomerPurchase).filter_by(
        workspace_id=wid, customer_id="cust_1", product_id="prod_1"
    ).first().quantity = 3
    db.commit()

    seed_purchase(db, wid, "cust_1", "prod_2", order_date=date(2026, 1, 2))

    client.post(f"/workspaces/{wid}/affinities/generate")

    from app.models.customer_attribute_affinity import CustomerAttributeAffinity
    affinities = db.query(CustomerAttributeAffinity).filter_by(
        workspace_id=wid, customer_id="cust_1"
    ).all()
    scores = {(a.attribute_id, a.attribute_value): a.score for a in affinities}
    assert scores[("lifestyle", "yoga")] == 1.0
    assert scores[("size", "large")] == 0.25
