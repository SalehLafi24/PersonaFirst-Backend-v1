"""
Cross-slot exclusion level tests.

Validates product-level vs group-level exclusion, null group_id behavior,
validation, and backward compatibility.
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


def multi_post(client, wid, customer_id, slots):
    return client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={"customer_id": customer_id, "slots": slots},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_invalid_exclusion_level_returns_422(client, db):
    ws = make_workspace(client, "EL-V1", "el-v1")
    resp = client.post(
        f"/workspaces/{ws['id']}/recommendations/slot",
        json={
            "customer_id": "cust_1",
            "slot": {"slot_id": "s1", "algorithm": "balanced", "top_n": 5,
                     "exclusion_level": "category"},
        },
    )
    assert resp.status_code == 422
    assert "category" in resp.text


# ---------------------------------------------------------------------------
# Product-level exclusion (default, existing behavior)
# ---------------------------------------------------------------------------

def test_product_level_excludes_exact_product_only(client, db):
    """
    Product-level exclusion: s1 returns prod_a (grp_x). s2 excludes prod_a
    but still returns prod_b from the same group.
    """
    ws = make_workspace(client, "EL-P1", "el-p1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga A",
                 group_id="grp_x", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga B",
                 group_id="grp_x", attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True, "exclusion_level": "product"},
    ])
    body = resp.json()
    s1_pids = {r["product_id"] for r in body["slots"][0]["results"]}
    s2_pids = {r["product_id"] for r in body["slots"][1]["results"]}

    # s1 got 1 product; s2 excludes that product but keeps the other from same group
    assert len(s1_pids) == 1
    assert s1_pids & s2_pids == set()
    assert len(s2_pids) == 1  # the other product in grp_x


def test_default_exclusion_level_is_product(client, db):
    """Omitting exclusion_level defaults to product-level."""
    ws = make_workspace(client, "EL-P2", "el-p2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "A",
                 group_id="grp_x", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B",
                 group_id="grp_x", attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    s2_pids = {r["product_id"] for r in body["slots"][1]["results"]}
    # Product-level: other product from same group still appears
    assert len(s2_pids) == 1


# ---------------------------------------------------------------------------
# Group-level exclusion
# ---------------------------------------------------------------------------

def test_group_level_excludes_entire_group(client, db):
    """
    Group-level exclusion: s1 returns prod_a (grp_x). s2 with group exclusion
    skips all products in grp_x.
    """
    ws = make_workspace(client, "EL-G1", "el-g1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga A",
                 group_id="grp_x", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga B",
                 group_id="grp_x", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_c", "SKU-C", "Running C",
                 group_id="grp_y", attributes=[("category", "running")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True, "exclusion_level": "group"},
    ])
    body = resp.json()
    s1 = body["slots"][0]["results"]
    s2 = body["slots"][1]["results"]

    # s1 got prod_a (highest score, grp_x)
    assert s1[0]["product_id"] == "prod_a"

    # s2 with group exclusion: entire grp_x excluded, only grp_y remains
    s2_pids = {r["product_id"] for r in s2}
    assert "prod_a" not in s2_pids
    assert "prod_b" not in s2_pids
    assert "prod_c" in s2_pids


def test_group_level_refills_past_excluded_groups(client, db):
    """Group exclusion + refill: skips excluded groups, fills from others."""
    ws = make_workspace(client, "EL-G2", "el-g2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    seed_affinity(db, wid, "cust_1", "category", "swimming", 0.5)

    seed_product(db, wid, "prod_a", "SKU-A", "A",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B",
                 group_id="grp_running", attributes=[("category", "running")])
    seed_product(db, wid, "prod_c", "SKU-C", "C",
                 group_id="grp_swimming", attributes=[("category", "swimming")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 2,
         "exclude_previous_slots": True, "exclusion_level": "group"},
    ])
    body = resp.json()
    s2 = body["slots"][1]["results"]
    # s1 took grp_yoga; s2 refills with grp_running + grp_swimming
    assert len(s2) == 2
    s2_groups = {r["group_id"] for r in s2}
    assert "grp_yoga" not in s2_groups


# ---------------------------------------------------------------------------
# Null group_id behavior
# ---------------------------------------------------------------------------

def test_null_group_not_excluded_by_group_level(client, db):
    """
    Products with null group_id are not excluded by group-level exclusion
    unless the exact product_id matches.
    """
    ws = make_workspace(client, "EL-N1", "el-n1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # prod_a: no group_id
    seed_product(db, wid, "prod_a", "SKU-A", "Ungrouped A",
                 group_id=None, attributes=[("category", "yoga")])
    # prod_b: no group_id
    seed_product(db, wid, "prod_b", "SKU-B", "Ungrouped B",
                 group_id=None, attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True, "exclusion_level": "group"},
    ])
    body = resp.json()
    s1_pids = {r["product_id"] for r in body["slots"][0]["results"]}
    s2_pids = {r["product_id"] for r in body["slots"][1]["results"]}

    # s1 got 1 ungrouped product. s2 excludes it by product_id (always applies),
    # but the other ungrouped product is NOT excluded (null group_id = unique).
    assert len(s1_pids) == 1
    assert s1_pids & s2_pids == set()
    assert len(s2_pids) == 1


def test_null_group_mixed_with_grouped(client, db):
    """
    Group exclusion excludes grouped products but not null-group products
    (unless exact product match).
    """
    ws = make_workspace(client, "EL-N2", "el-n2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # prod_a: grp_x (will be excluded at group level)
    seed_product(db, wid, "prod_a", "SKU-A", "Grouped A",
                 group_id="grp_x", attributes=[("category", "yoga")])
    # prod_b: grp_x (same group, excluded)
    seed_product(db, wid, "prod_b", "SKU-B", "Grouped B",
                 group_id="grp_x", attributes=[("category", "yoga")])
    # prod_c: null group (not excluded by group)
    seed_product(db, wid, "prod_c", "SKU-C", "Ungrouped C",
                 group_id=None, attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True, "exclusion_level": "group"},
    ])
    body = resp.json()
    s1 = body["slots"][0]["results"]
    s2 = body["slots"][1]["results"]

    # s1 got prod_a (highest PK tiebreak, both have same score)
    assert s1[0]["group_id"] == "grp_x"

    # s2: grp_x excluded → prod_b gone. prod_c (null group) survives.
    s2_pids = {r["product_id"] for r in s2}
    assert "prod_b" not in s2_pids
    assert "prod_c" in s2_pids


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_single_slot_accepts_exclusion_level(client, db):
    """Single-slot endpoint accepts the field without error."""
    ws = make_workspace(client, "EL-BC1", "el-bc1")
    resp = client.post(
        f"/workspaces/{ws['id']}/recommendations/slot",
        json={
            "customer_id": "cust_1",
            "slot": {"slot_id": "s1", "algorithm": "balanced", "top_n": 5,
                     "exclusion_level": "group"},
        },
    )
    assert resp.status_code == 200


def test_get_endpoint_unaffected(client, db):
    ws = make_workspace(client, "EL-BC2", "el-bc2")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga",
                 attributes=[("category", "yoga")])
    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
