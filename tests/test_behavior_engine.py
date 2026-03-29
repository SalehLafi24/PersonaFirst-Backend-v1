"""
Tests for behavior_engine_service: product co-purchase relationship generation.

strength(A → B) = customers_who_bought_both_A_and_B / customers_who_bought_A
"""
import pytest
from datetime import date

from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product
from app.models.product_behavior_relationship import ProductBehaviorRelationship
from app.services import behavior_engine_service


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku, name=name)
    db.add(p)
    db.flush()
    db.commit()
    return p


def seed_purchase(db, workspace_id, customer_id, product):
    db.add(CustomerPurchase(
        workspace_id=workspace_id,
        customer_id=customer_id,
        product_db_id=product.id,
        product_id=product.product_id,
        order_date=date.today(),
        quantity=1,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# BE-1: All buyers of A also bought B → strength = 1.0
# ---------------------------------------------------------------------------

def test_all_buyers_overlap_strength_is_one(client, db):
    ws = make_workspace(client, "BE-1", "be-1")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")

    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pb)
    seed_purchase(db, wid, "cust_2", pa)
    seed_purchase(db, wid, "cust_2", pb)

    count = behavior_engine_service.run_behavior_engine(db, wid)
    assert count == 2  # A→B and B→A

    rel_ab = db.query(ProductBehaviorRelationship).filter_by(
        workspace_id=wid,
        source_product_db_id=pa.id,
        target_product_db_id=pb.id,
    ).first()
    assert rel_ab is not None
    assert rel_ab.strength == pytest.approx(1.0)
    assert rel_ab.customer_overlap_count == 2
    assert rel_ab.source_customer_count == 2


# ---------------------------------------------------------------------------
# BE-2: Half of A buyers also bought B → strength = 0.5
# ---------------------------------------------------------------------------

def test_half_overlap_strength_is_half(client, db):
    ws = make_workspace(client, "BE-2", "be-2")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")

    # cust_1 bought both; cust_2 bought only A
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pb)
    seed_purchase(db, wid, "cust_2", pa)

    behavior_engine_service.run_behavior_engine(db, wid)

    rel_ab = db.query(ProductBehaviorRelationship).filter_by(
        workspace_id=wid,
        source_product_db_id=pa.id,
        target_product_db_id=pb.id,
    ).first()
    assert rel_ab.strength == pytest.approx(0.5)
    assert rel_ab.customer_overlap_count == 1
    assert rel_ab.source_customer_count == 2

    # B→A: only cust_1 bought B, and cust_1 also bought A → strength = 1.0
    rel_ba = db.query(ProductBehaviorRelationship).filter_by(
        workspace_id=wid,
        source_product_db_id=pb.id,
        target_product_db_id=pa.id,
    ).first()
    assert rel_ba.strength == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# BE-3: Workspace isolation
# ---------------------------------------------------------------------------

def test_workspace_isolation(client, db):
    ws1 = make_workspace(client, "BE-WS1", "be-ws1")
    ws2 = make_workspace(client, "BE-WS2", "be-ws2")

    pa1 = seed_product(db, ws1["id"], "prod_a", "SKU-A", "Product A")
    pb1 = seed_product(db, ws1["id"], "prod_b", "SKU-B", "Product B")
    seed_purchase(db, ws1["id"], "cust_1", pa1)
    seed_purchase(db, ws1["id"], "cust_1", pb1)

    behavior_engine_service.run_behavior_engine(db, ws1["id"])
    behavior_engine_service.run_behavior_engine(db, ws2["id"])

    ws2_rels = db.query(ProductBehaviorRelationship).filter_by(workspace_id=ws2["id"]).all()
    assert len(ws2_rels) == 0

    ws1_rels = db.query(ProductBehaviorRelationship).filter_by(workspace_id=ws1["id"]).all()
    assert len(ws1_rels) == 2  # A→B and B→A


# ---------------------------------------------------------------------------
# BE-4: Full refresh on second run — no duplicates
# ---------------------------------------------------------------------------

def test_refresh_replaces_existing(client, db):
    ws = make_workspace(client, "BE-4", "be-4")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pb)

    behavior_engine_service.run_behavior_engine(db, wid)
    count2 = behavior_engine_service.run_behavior_engine(db, wid)

    all_rels = db.query(ProductBehaviorRelationship).filter_by(workspace_id=wid).all()
    assert len(all_rels) == count2 == 2


# ---------------------------------------------------------------------------
# BE-5: No purchases → zero relationships
# ---------------------------------------------------------------------------

def test_no_purchases_returns_zero(client, db):
    ws = make_workspace(client, "BE-5", "be-5")
    result = behavior_engine_service.run_behavior_engine(db, ws["id"])
    assert result == 0


# ---------------------------------------------------------------------------
# BE-6: Each customer bought a different single product → no co-purchase pairs
# ---------------------------------------------------------------------------

def test_single_product_per_customer_no_pairs(client, db):
    ws = make_workspace(client, "BE-6", "be-6")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_2", pb)

    result = behavior_engine_service.run_behavior_engine(db, wid)
    assert result == 0


# ---------------------------------------------------------------------------
# BE-7: Multiple purchases of same product by same customer counts once (set dedup)
# ---------------------------------------------------------------------------

def test_multiple_purchases_same_product_counted_once(client, db):
    ws = make_workspace(client, "BE-7", "be-7")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")

    # cust_1 bought A twice and B once — should be treated as one customer with {A, B}
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pb)

    behavior_engine_service.run_behavior_engine(db, wid)

    rel_ab = db.query(ProductBehaviorRelationship).filter_by(
        workspace_id=wid,
        source_product_db_id=pa.id,
        target_product_db_id=pb.id,
    ).first()
    assert rel_ab.source_customer_count == 1
    assert rel_ab.customer_overlap_count == 1
    assert rel_ab.strength == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# BE-8: Generate endpoint returns relationships_created
# ---------------------------------------------------------------------------

def test_generate_endpoint(client, db):
    ws = make_workspace(client, "BE-8", "be-8")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pb)

    resp = client.post(f"/workspaces/{wid}/behavioral-relationships/generate")
    assert resp.status_code == 200
    assert resp.json()["relationships_created"] == 2


# ---------------------------------------------------------------------------
# BE-9: List endpoint returns generated relationships
# ---------------------------------------------------------------------------

def test_list_endpoint(client, db):
    ws = make_workspace(client, "BE-9", "be-9")
    wid = ws["id"]

    pa = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    pb = seed_product(db, wid, "prod_b", "SKU-B", "Product B")
    seed_purchase(db, wid, "cust_1", pa)
    seed_purchase(db, wid, "cust_1", pb)

    client.post(f"/workspaces/{wid}/behavioral-relationships/generate")

    resp = client.get(f"/workspaces/{wid}/behavioral-relationships/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(r["workspace_id"] == wid for r in data)
