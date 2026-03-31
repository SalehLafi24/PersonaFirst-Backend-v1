"""
Cross-slot exclusion tests for multi-slot recommendations.

Validates exclude_previous_slots behavior, order dependence,
and backward compatibility.
"""
import pytest
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.product import Product, ProductAttribute


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value, score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def seed_product(db, workspace_id, product_id, sku, name, attributes=None):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku, name=name)
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id, attribute_value=attr_val))
    db.commit()
    return p


def multi_post(client, wid, customer_id, slots):
    return client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={"customer_id": customer_id, "slots": slots},
    )


# ---------------------------------------------------------------------------
# Basic exclusion
# ---------------------------------------------------------------------------

def test_exclude_previous_slots_removes_duplicates(client, db):
    """Slot 2 with exclude_previous_slots=true should not return products from slot 1."""
    ws = make_workspace(client, "XS-1", "xs-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Product A",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                 attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 5},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    s1_pids = {r["product_id"] for r in body["slots"][0]["results"]}
    s2_pids = {r["product_id"] for r in body["slots"][1]["results"]}

    # No overlap between s1 and s2
    assert s1_pids & s2_pids == set()
    # s1 got both products
    assert s1_pids == {"prod_a", "prod_b"}
    # s2 has nothing left
    assert s2_pids == set()


def test_exclude_false_allows_duplicates(client, db):
    """Default exclude_previous_slots=false allows the same product in multiple slots."""
    ws = make_workspace(client, "XS-2", "xs-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Product 1",
                 attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 5},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5},
    ])
    body = resp.json()
    s1_pids = [r["product_id"] for r in body["slots"][0]["results"]]
    s2_pids = [r["product_id"] for r in body["slots"][1]["results"]]
    assert "prod_1" in s1_pids
    assert "prod_1" in s2_pids


# ---------------------------------------------------------------------------
# Order matters — only earlier slots excluded
# ---------------------------------------------------------------------------

def test_exclusion_is_sequential(client, db):
    """
    Three slots: s1 (no exclude), s2 (exclude), s3 (exclude).
    s2 excludes s1's results. s3 excludes s1+s2's results.
    Exclusion happens after top_n, so request enough results per slot.
    """
    ws = make_workspace(client, "XS-3", "xs-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "A", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_c", "SKU-C", "C", attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
        {"slot_id": "s3", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    s1_pids = {r["product_id"] for r in body["slots"][0]["results"]}
    s2_pids = {r["product_id"] for r in body["slots"][1]["results"]}
    s3_pids = {r["product_id"] for r in body["slots"][2]["results"]}

    assert len(s1_pids) == 1
    # s2 gets the other 2 products (3 returned minus 1 excluded)
    assert len(s2_pids) == 2
    assert s1_pids & s2_pids == set()
    # s3 excludes all 3 already returned → empty
    assert (s1_pids | s2_pids) & s3_pids == set()


def test_first_slot_exclude_is_noop(client, db):
    """exclude_previous_slots on the first slot has nothing to exclude."""
    ws = make_workspace(client, "XS-4", "xs-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Product 1",
                 attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    assert len(body["slots"][0]["results"]) == 1
    assert body["slots"][0]["results"][0]["product_id"] == "prod_1"


# ---------------------------------------------------------------------------
# Exclusion can produce empty results
# ---------------------------------------------------------------------------

def test_exclusion_can_empty_a_slot(client, db):
    """If all candidates were in earlier slots, the excluding slot returns []."""
    ws = make_workspace(client, "XS-5", "xs-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Product 1",
                 attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 5},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    assert len(body["slots"][0]["results"]) == 1
    assert body["slots"][1]["results"] == []


# ---------------------------------------------------------------------------
# Excluded slot still contributes to the running set
# ---------------------------------------------------------------------------

def test_excluded_slot_results_feed_into_later_exclusions(client, db):
    """
    s1 returns prod_a. s2 (exclude) gets prod_b.
    s3 (exclude) should exclude both prod_a and prod_b.
    """
    ws = make_workspace(client, "XS-6", "xs-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "A", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_c", "SKU-C", "C", attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 1,
         "exclude_previous_slots": True},
        {"slot_id": "s3", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    all_earlier = set()
    for s in body["slots"][:2]:
        all_earlier.update(r["product_id"] for r in s["results"])

    s3_pids = {r["product_id"] for r in body["slots"][2]["results"]}
    assert all_earlier & s3_pids == set()


# ---------------------------------------------------------------------------
# Single-slot and GET are unaffected
# ---------------------------------------------------------------------------

def test_single_slot_ignores_exclude_field(client, db):
    """The single-slot endpoint accepts the field but has no exclusion behavior."""
    ws = make_workspace(client, "XS-7", "xs-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Product 1",
                 attributes=[("category", "yoga")])

    resp = client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={
            "customer_id": "cust_1",
            "slot": {"slot_id": "s1", "algorithm": "balanced", "top_n": 5,
                     "exclude_previous_slots": True},
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_get_endpoint_unaffected(client, db):
    ws = make_workspace(client, "XS-8", "xs-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
