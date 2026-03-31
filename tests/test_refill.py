"""
Refill tests — selection continues past excluded/diversity-skipped candidates
to fill top_n from the remaining ranked pool.
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


def slot_post(client, wid, customer_id, slot):
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={"customer_id": customer_id, "slot": slot},
    )


# ---------------------------------------------------------------------------
# Cross-slot exclusion refill
# ---------------------------------------------------------------------------

def test_exclusion_refills_from_pool(client, db):
    """
    Slot 1 takes prod_a. Slot 2 (exclude, top_n=2) would have gotten
    [prod_a, prod_b] but prod_a is excluded. Refill picks prod_c to reach top_n=2.
    """
    ws = make_workspace(client, "RF-1", "rf-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    seed_affinity(db, wid, "cust_1", "category", "swimming", 0.5)

    seed_product(db, wid, "prod_a", "SKU-A", "A", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B", attributes=[("category", "running")])
    seed_product(db, wid, "prod_c", "SKU-C", "C", attributes=[("category", "swimming")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 2,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    s1 = body["slots"][0]["results"]
    s2 = body["slots"][1]["results"]

    assert len(s1) == 1
    assert s1[0]["product_id"] == "prod_a"

    # s2 excluded prod_a, refilled with prod_c to reach top_n=2
    assert len(s2) == 2
    s2_pids = {r["product_id"] for r in s2}
    assert "prod_a" not in s2_pids
    assert "prod_b" in s2_pids
    assert "prod_c" in s2_pids


def test_exclusion_refill_exhausts_pool(client, db):
    """When exclusion removes candidates and pool is exhausted, return what's available."""
    ws = make_workspace(client, "RF-2", "rf-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "A", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B", attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    s2 = body["slots"][1]["results"]

    # Only 2 products total, s1 took 1, s2 gets the remaining 1 (not 5)
    assert len(s2) == 1
    assert s2[0]["product_id"] != body["slots"][0]["results"][0]["product_id"]


def test_three_slot_chain_refills_correctly(client, db):
    """Three sequential excluding slots each get refilled from the remaining pool."""
    ws = make_workspace(client, "RF-3", "rf-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(6):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 2},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 2,
         "exclude_previous_slots": True},
        {"slot_id": "s3", "algorithm": "balanced", "top_n": 2,
         "exclude_previous_slots": True},
    ])
    body = resp.json()
    all_pids = []
    for s in body["slots"]:
        assert len(s["results"]) == 2
        all_pids.extend(r["product_id"] for r in s["results"])

    # All 6 products used, no duplicates across slots
    assert len(set(all_pids)) == 6


# ---------------------------------------------------------------------------
# Diversity + refill
# ---------------------------------------------------------------------------

def test_diversity_refills_from_new_groups(client, db):
    """
    With diversity_enabled, skipped same-group candidates are passed over
    and the selection continues to fill from other groups.
    """
    ws = make_workspace(client, "RF-4", "rf-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)
    seed_affinity(db, wid, "cust_1", "category", "swimming", 0.5)

    # 3 yoga products (same group), 1 running, 1 swimming
    seed_product(db, wid, "yoga_1", "SKU-Y1", "Yoga 1",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "yoga_2", "SKU-Y2", "Yoga 2",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "yoga_3", "SKU-Y3", "Yoga 3",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "run_1", "SKU-R1", "Running 1",
                 group_id="grp_running", attributes=[("category", "running")])
    seed_product(db, wid, "swim_1", "SKU-S1", "Swimming 1",
                 group_id="grp_swimming", attributes=[("category", "swimming")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "diverse", "algorithm": "balanced", "top_n": 3,
        "diversity_enabled": True,
    })
    data = resp.json()["results"]

    # Refill scans past yoga_2 and yoga_3 to pick running and swimming
    assert len(data) == 3
    groups = [r["group_id"] for r in data]
    assert groups.count("grp_yoga") == 1
    assert groups.count("grp_running") == 1
    assert groups.count("grp_swimming") == 1


# ---------------------------------------------------------------------------
# Diversity + exclusion + refill combined
# ---------------------------------------------------------------------------

def test_diversity_plus_exclusion_refills(client, db):
    """
    Multi-slot: s1 returns yoga_1. s2 has diversity + exclusion.
    s2 skips yoga_1 (excluded) and yoga_2 (diversity), picks from other groups.
    """
    ws = make_workspace(client, "RF-5", "rf-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.7)

    seed_product(db, wid, "yoga_1", "SKU-Y1", "Yoga 1",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "yoga_2", "SKU-Y2", "Yoga 2",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "run_1", "SKU-R1", "Running 1",
                 group_id="grp_running", attributes=[("category", "running")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 2,
         "exclude_previous_slots": True, "diversity_enabled": True},
    ])
    body = resp.json()
    s1 = body["slots"][0]["results"]
    s2 = body["slots"][1]["results"]

    assert s1[0]["product_id"] == "yoga_1"

    # s2: yoga_1 excluded, yoga_2 picked for grp_yoga, run_1 for grp_running
    assert len(s2) == 2
    s2_pids = {r["product_id"] for r in s2}
    assert "yoga_1" not in s2_pids
    assert "yoga_2" in s2_pids
    assert "run_1" in s2_pids


# ---------------------------------------------------------------------------
# No refill needed — behavior unchanged
# ---------------------------------------------------------------------------

def test_no_exclusion_no_diversity_unchanged(client, db):
    """Without exclusion or diversity, top_n slicing works as before."""
    ws = make_workspace(client, "RF-6", "rf-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 3,
    })
    assert len(resp.json()["results"]) == 3


def test_get_endpoint_unaffected(client, db):
    ws = make_workspace(client, "RF-7", "rf-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Refill preserves ranking order
# ---------------------------------------------------------------------------

def test_refill_preserves_rank_order(client, db):
    """Refilled candidates appear in their original rank order."""
    ws = make_workspace(client, "RF-8", "rf-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.5)
    seed_affinity(db, wid, "cust_1", "category", "swimming", 0.3)

    seed_product(db, wid, "prod_top", "SKU-T", "Top",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_mid", "SKU-M", "Mid",
                 attributes=[("category", "running")])
    seed_product(db, wid, "prod_low", "SKU-L", "Low",
                 attributes=[("category", "swimming")])

    # s1 takes prod_top; s2 should get prod_mid then prod_low (rank order)
    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "s1", "algorithm": "balanced", "top_n": 1},
        {"slot_id": "s2", "algorithm": "balanced", "top_n": 2,
         "exclude_previous_slots": True},
    ])
    s2 = resp.json()["slots"][1]["results"]
    assert s2[0]["product_id"] == "prod_mid"
    assert s2[1]["product_id"] == "prod_low"
    assert s2[0]["recommendation_score"] >= s2[1]["recommendation_score"]
