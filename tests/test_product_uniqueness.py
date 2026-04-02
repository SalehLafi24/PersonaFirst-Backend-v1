"""
In-slot product uniqueness invariant tests.

Ensures no slot ever contains duplicate product_ids, regardless of
diversity mode, exclusion settings, or candidate pool contents.
"""
import pytest
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None,
                 attributes=None):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku,
                name=name, group_id=group_id)
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id,
                                attribute_value=attr_val))
    db.commit()
    return p


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value, score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def seed_purchase(db, workspace_id, customer_id, product_id, quantity=1):
    product = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=product_id
    ).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id, product_id=product_id,
        order_date=date.today(), quantity=quantity,
    ))
    db.commit()


def slot_post(client, wid, customer_id, slot):
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={"customer_id": customer_id, "slot": slot},
    )


def multi_post(client, wid, customer_id, slots):
    return client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={"customer_id": customer_id, "slots": slots},
    )


def _assert_unique_product_ids(results: list[dict]):
    """Helper: assert no duplicate product_ids in a result list."""
    pids = [r["product_id"] for r in results]
    assert len(pids) == len(set(pids)), f"Duplicate product_ids found: {pids}"


# ---------------------------------------------------------------------------
# Uniqueness with diversity_mode = "off"
# ---------------------------------------------------------------------------

def test_no_duplicates_with_diversity_off(client, db):
    """Even without diversity, each product_id appears at most once."""
    ws = make_workspace(client, "PU-1", "pu-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # Multiple products, some in same group
    for i in range(4):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     group_id="grp_a", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 10,
        "diversity_mode": "off",
    })
    _assert_unique_product_ids(resp.json()["results"])


# ---------------------------------------------------------------------------
# Uniqueness with diversity_mode = "strict"
# ---------------------------------------------------------------------------

def test_no_duplicates_with_diversity_strict(client, db):
    ws = make_workspace(client, "PU-2", "pu-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    for i in range(3):
        seed_product(db, wid, f"yoga_{i}", f"SKU-Y{i}", f"Yoga {i}",
                     group_id="grp_yoga", attributes=[("category", "yoga")])
    for i in range(3):
        seed_product(db, wid, f"run_{i}", f"SKU-R{i}", f"Running {i}",
                     group_id="grp_run", attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 10,
        "diversity_mode": "strict",
    })
    _assert_unique_product_ids(resp.json()["results"])


# ---------------------------------------------------------------------------
# Uniqueness with diversity_mode = "adaptive"
# ---------------------------------------------------------------------------

def test_no_duplicates_with_diversity_adaptive(client, db):
    ws = make_workspace(client, "PU-3", "pu-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     group_id="grp_a", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 10,
        "diversity_mode": "adaptive",
    })
    _assert_unique_product_ids(resp.json()["results"])


# ---------------------------------------------------------------------------
# Uniqueness with cross-slot exclusion
# ---------------------------------------------------------------------------

def test_no_duplicates_with_cross_slot_exclusion(client, db):
    ws = make_workspace(client, "PU-4", "pu-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(6):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 3},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    _assert_unique_product_ids(body["slots"][0]["results"])
    _assert_unique_product_ids(body["slots"][1]["results"])


def test_no_duplicates_with_group_level_exclusion(client, db):
    ws = make_workspace(client, "PU-5", "pu-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    for i in range(3):
        seed_product(db, wid, f"yoga_{i}", f"SKU-Y{i}", f"Yoga {i}",
                     group_id="grp_yoga", attributes=[("category", "yoga")])
    for i in range(3):
        seed_product(db, wid, f"run_{i}", f"SKU-R{i}", f"Running {i}",
                     group_id="grp_run", attributes=[("category", "running")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 2},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True, "exclusion_level": "group"},
    ])
    body = resp.json()
    _assert_unique_product_ids(body["slots"][0]["results"])
    _assert_unique_product_ids(body["slots"][1]["results"])


# ---------------------------------------------------------------------------
# Uniqueness with null group_id
# ---------------------------------------------------------------------------

def test_no_duplicates_with_null_groups(client, db):
    ws = make_workspace(client, "PU-6", "pu-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(4):
        seed_product(db, wid, f"ungrouped_{i}", f"SKU-U{i}", f"U {i}",
                     group_id=None, attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 10,
        "diversity_mode": "strict",
    })
    _assert_unique_product_ids(resp.json()["results"])


# ---------------------------------------------------------------------------
# GET endpoint also unique
# ---------------------------------------------------------------------------

def test_get_endpoint_no_duplicates(client, db):
    ws = make_workspace(client, "PU-7", "pu-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     group_id="grp_a", attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    _assert_unique_product_ids(resp.json())


# ---------------------------------------------------------------------------
# Refill continues past skipped duplicates
# ---------------------------------------------------------------------------

def test_refill_continues_past_duplicates(client, db):
    """
    Even if the ranked pool somehow had duplicate entries,
    the guard skips them and refill picks up the next valid candidate.
    Result count should not be reduced unless pool is truly exhausted.
    """
    ws = make_workspace(client, "PU-8", "pu-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    # 3 distinct products
    seed_product(db, wid, "prod_a", "SKU-A", "A",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B",
                 attributes=[("category", "running")])
    seed_product(db, wid, "prod_c", "SKU-C", "C",
                 attributes=[("category", "yoga"), ("category", "running")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 3,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    _assert_unique_product_ids(data)
    assert len(data) == 3
