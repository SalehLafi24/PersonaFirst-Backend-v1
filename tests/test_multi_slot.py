"""
Multi-slot recommendation tests.

Validates multi-slot request/response, independent slot processing,
validation, and backward compatibility.
"""
import pytest
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.product_behavior_relationship import ProductBehaviorRelationship


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value, score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None,
                 attributes=None):
    p = Product(
        workspace_id=workspace_id, product_id=product_id, sku=sku, name=name,
        group_id=group_id,
    )
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id, attribute_value=attr_val))
    db.commit()
    return p


def seed_purchase(db, workspace_id, customer_id, product_id, quantity=1):
    product = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=product_id
    ).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id, product_id=product_id,
        group_id=product.group_id,
        order_date=date.today(), quantity=quantity,
    ))
    db.commit()


def seed_behavior_rel(db, workspace_id, source_product_id, target_product_id,
                      strength, overlap=1, source_count=1):
    src = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=source_product_id
    ).first()
    tgt = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=target_product_id
    ).first()
    db.add(ProductBehaviorRelationship(
        workspace_id=workspace_id,
        source_product_db_id=src.id,
        target_product_db_id=tgt.id,
        strength=strength,
        customer_overlap_count=overlap,
        source_customer_count=source_count,
    ))
    db.commit()


def multi_post(client, wid, customer_id, slots):
    return client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={"customer_id": customer_id, "slots": slots},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_empty_slots_returns_422(client, db):
    ws = make_workspace(client, "MS-V1", "ms-v1")
    resp = multi_post(client, ws["id"], "cust_1", [])
    assert resp.status_code == 422


def test_duplicate_slot_ids_returns_422(client, db):
    ws = make_workspace(client, "MS-V2", "ms-v2")
    resp = multi_post(client, ws["id"], "cust_1", [
        {"slot_id": "dup", "algorithm": "balanced", "top_n": 2},
        {"slot_id": "dup", "algorithm": "balanced", "top_n": 3},
    ])
    assert resp.status_code == 422
    assert "dup" in resp.text


def test_invalid_algorithm_in_one_slot_returns_400(client, db):
    ws = make_workspace(client, "MS-V3", "ms-v3")
    resp = multi_post(client, ws["id"], "cust_1", [
        {"slot_id": "good", "algorithm": "balanced", "top_n": 2},
        {"slot_id": "bad", "algorithm": "nonexistent", "top_n": 2},
    ])
    assert resp.status_code == 400
    assert "nonexistent" in resp.json()["detail"]


def test_invalid_top_n_in_slot_returns_422(client, db):
    ws = make_workspace(client, "MS-V4", "ms-v4")
    resp = multi_post(client, ws["id"], "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 0},
    ])
    assert resp.status_code == 422


def test_missing_workspace_returns_404(client, db):
    resp = multi_post(client, 99999, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 2},
    ])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Basic multi-slot
# ---------------------------------------------------------------------------

def test_two_slots_return_independent_results(client, db):
    ws = make_workspace(client, "MS-B1", "ms-b1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_purchased", "SKU-P", "Purchased")
    seed_product(db, wid, "prod_beh", "SKU-B", "Behavioral Target")

    seed_purchase(db, wid, "cust_1", "prod_purchased")
    seed_behavior_rel(db, wid, "prod_purchased", "prod_beh",
                      strength=0.8, overlap=4, source_count=5)

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "affinity_slot", "algorithm": "affinity_first", "top_n": 5},
        {"slot_id": "behavior_slot", "algorithm": "behavioral_only", "top_n": 5},
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer_id"] == "cust_1"
    assert len(body["slots"]) == 2

    affinity_slot = body["slots"][0]
    behavior_slot = body["slots"][1]

    assert affinity_slot["slot_id"] == "affinity_slot"
    assert affinity_slot["algorithm"] == "affinity_first"
    assert any(r["product_id"] == "prod_yoga" for r in affinity_slot["results"])

    assert behavior_slot["slot_id"] == "behavior_slot"
    assert behavior_slot["algorithm"] == "behavioral_only"
    assert any(r["product_id"] == "prod_beh" for r in behavior_slot["results"])


def test_response_includes_metadata_per_slot(client, db):
    ws = make_workspace(client, "MS-META", "ms-meta")
    wid = ws["id"]

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 2},
        {"slot_id": "s2", "algorithm": "behavior_first", "top_n": 3,
         "fallback_mode": "relax_filters"},
    ])
    body = resp.json()
    assert body["slots"][0]["slot_id"] == "s1"
    assert body["slots"][0]["algorithm"] == "balanced"
    assert body["slots"][0]["fallback_mode"] == "strict"
    assert body["slots"][0]["fallback_applied"] is False

    assert body["slots"][1]["slot_id"] == "s2"
    assert body["slots"][1]["algorithm"] == "behavior_first"
    assert body["slots"][1]["fallback_mode"] == "relax_filters"


# ---------------------------------------------------------------------------
# No cross-slot dedup — same product in multiple slots is OK
# ---------------------------------------------------------------------------

def test_same_product_can_appear_in_multiple_slots(client, db):
    ws = make_workspace(client, "MS-DUP", "ms-dup")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "slot_a", "algorithm": "balanced", "top_n": 5},
        {"slot_id": "slot_b", "algorithm": "affinity_first", "top_n": 5},
    ])
    body = resp.json()
    pids_a = [r["product_id"] for r in body["slots"][0]["results"]]
    pids_b = [r["product_id"] for r in body["slots"][1]["results"]]
    assert "prod_1" in pids_a
    assert "prod_1" in pids_b


# ---------------------------------------------------------------------------
# Each slot respects its own top_n
# ---------------------------------------------------------------------------

def test_each_slot_respects_its_own_top_n(client, db):
    ws = make_workspace(client, "MS-TN", "ms-tn")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "small", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "large", "algorithm": "balanced", "top_n": 4},
    ])
    body = resp.json()
    assert len(body["slots"][0]["results"]) == 1
    assert len(body["slots"][1]["results"]) == 4


# ---------------------------------------------------------------------------
# Filters and fallback work per slot
# ---------------------------------------------------------------------------

def test_filters_and_fallback_work_independently_per_slot(client, db):
    ws = make_workspace(client, "MS-FF", "ms-ff")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "yoga"), ("type", "running")])

    resp = multi_post(client, wid, "cust_1", [
        {
            "slot_id": "strict_slot",
            "algorithm": "balanced",
            "top_n": 5,
            "filters": [{"attribute_id": "type", "operator": "eq", "value": "swimming"}],
            "fallback_mode": "strict",
        },
        {
            "slot_id": "relaxed_slot",
            "algorithm": "balanced",
            "top_n": 5,
            "filters": [{"attribute_id": "type", "operator": "eq", "value": "swimming"}],
            "fallback_mode": "relax_filters",
        },
    ])
    body = resp.json()
    strict = body["slots"][0]
    relaxed = body["slots"][1]

    assert strict["results"] == []
    assert strict["fallback_applied"] is False

    assert len(relaxed["results"]) > 0
    assert relaxed["fallback_applied"] is True


# ---------------------------------------------------------------------------
# Single-slot and GET endpoints still work
# ---------------------------------------------------------------------------

def test_single_slot_endpoint_still_works(client, db):
    ws = make_workspace(client, "MS-BC1", "ms-bc1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={
            "customer_id": "cust_1",
            "slot": {"slot_id": "s1", "algorithm": "balanced", "top_n": 5},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["slot_id"] == "s1"


def test_get_endpoint_still_works(client, db):
    ws = make_workspace(client, "MS-BC2", "ms-bc2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
