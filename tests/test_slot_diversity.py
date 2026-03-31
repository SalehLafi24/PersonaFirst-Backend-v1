"""
In-slot diversity tests.

Validates max-1-per-group_id behavior, ranking preservation,
pool exhaustion, and backward compatibility.
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


def slot_post(client, wid, customer_id, algorithm, top_n,
              diversity_enabled=False, filters=None, fallback_mode=None):
    slot = {
        "slot_id": "test_slot",
        "algorithm": algorithm,
        "top_n": top_n,
        "diversity_enabled": diversity_enabled,
    }
    if filters is not None:
        slot["filters"] = filters
    if fallback_mode is not None:
        slot["fallback_mode"] = fallback_mode
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={"customer_id": customer_id, "slot": slot},
    )


def multi_post(client, wid, customer_id, slots):
    return client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={"customer_id": customer_id, "slots": slots},
    )


# ---------------------------------------------------------------------------
# Core diversity behavior
# ---------------------------------------------------------------------------

def test_diversity_limits_to_one_per_group(client, db):
    """With diversity_enabled, only 1 product per group_id is returned."""
    ws = make_workspace(client, "DIV-1", "div-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # 3 products in same group, different scores via different attributes
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga Mat Pro",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga Mat Basic",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_c", "SKU-C", "Yoga Mat Lite",
                 group_id="grp_yoga", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     diversity_enabled=True)
    data = resp.json()["results"]
    # Only 1 from grp_yoga despite top_n=5
    group_ids = [r["group_id"] for r in data]
    assert group_ids.count("grp_yoga") == 1


def test_diversity_selects_from_multiple_groups(client, db):
    """Diversity picks one per group, spanning different groups."""
    ws = make_workspace(client, "DIV-2", "div-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.8)

    # 2 in grp_yoga
    seed_product(db, wid, "prod_y1", "SKU-Y1", "Yoga A",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_y2", "SKU-Y2", "Yoga B",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    # 2 in grp_running
    seed_product(db, wid, "prod_r1", "SKU-R1", "Running A",
                 group_id="grp_running", attributes=[("category", "running")])
    seed_product(db, wid, "prod_r2", "SKU-R2", "Running B",
                 group_id="grp_running", attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     diversity_enabled=True)
    data = resp.json()["results"]
    group_ids = [r["group_id"] for r in data]
    assert group_ids.count("grp_yoga") == 1
    assert group_ids.count("grp_running") == 1
    assert len(data) == 2


def test_diversity_preserves_rank_order(client, db):
    """The highest-scoring product from each group is selected, in rank order."""
    ws = make_workspace(client, "DIV-3", "div-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.5)

    seed_product(db, wid, "prod_y1", "SKU-Y1", "Best Yoga",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_r1", "SKU-R1", "Best Running",
                 group_id="grp_running", attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     diversity_enabled=True)
    data = resp.json()["results"]
    # Yoga (0.9) ranks above running (0.5)
    assert data[0]["product_id"] == "prod_y1"
    assert data[1]["product_id"] == "prod_r1"


# ---------------------------------------------------------------------------
# Without diversity — current dedup still works
# ---------------------------------------------------------------------------

def test_without_diversity_dedup_still_applies(client, db):
    """diversity_enabled=false uses existing group dedup (best per group)."""
    ws = make_workspace(client, "DIV-4", "div-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga A",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga B",
                 group_id="grp_yoga", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     diversity_enabled=False)
    data = resp.json()["results"]
    # Dedup keeps 1 per group even without diversity
    assert len(data) == 1


# ---------------------------------------------------------------------------
# Products without group_id are treated individually
# ---------------------------------------------------------------------------

def test_diversity_treats_no_group_as_unique(client, db):
    """Products with no group_id each get their own slot in diversity."""
    ws = make_workspace(client, "DIV-5", "div-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Product A",
                 group_id=None, attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                 group_id=None, attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_c", "SKU-C", "Product C",
                 group_id="grp_x", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_d", "SKU-D", "Product D",
                 group_id="grp_x", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 10,
                     diversity_enabled=True)
    data = resp.json()["results"]
    pids = [r["product_id"] for r in data]
    # Both ungrouped products appear (each is its own "group")
    assert "prod_a" in pids
    assert "prod_b" in pids
    # Only 1 from grp_x
    grp_x_items = [r for r in data if r["group_id"] == "grp_x"]
    assert len(grp_x_items) == 1


# ---------------------------------------------------------------------------
# Pool exhaustion — fewer than top_n returned
# ---------------------------------------------------------------------------

def test_diversity_returns_fewer_than_top_n_when_pool_exhausted(client, db):
    """If diversity limits selection and the pool runs out, fewer than top_n is OK."""
    ws = make_workspace(client, "DIV-6", "div-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # All 3 products in the same group
    for i in range(3):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Yoga {i}",
                     group_id="grp_yoga", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     diversity_enabled=True)
    data = resp.json()["results"]
    # Only 1 returned despite top_n=5 and 3 candidates
    assert len(data) == 1


# ---------------------------------------------------------------------------
# Diversity + filters
# ---------------------------------------------------------------------------

def test_diversity_works_with_filters(client, db):
    ws = make_workspace(client, "DIV-7", "div-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga A",
                 group_id="grp_yoga", attributes=[("category", "yoga"), ("tier", "premium")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga B",
                 group_id="grp_yoga", attributes=[("category", "yoga"), ("tier", "basic")])
    seed_product(db, wid, "prod_c", "SKU-C", "Running A",
                 group_id="grp_running", attributes=[("category", "yoga"), ("tier", "premium")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     diversity_enabled=True,
                     filters=[{"attribute_id": "tier", "operator": "eq", "value": "premium"}])
    data = resp.json()["results"]
    # Filter keeps prod_a (grp_yoga, premium) and prod_c (grp_running, premium)
    # Diversity: 1 per group → both groups represented
    assert len(data) == 2
    group_ids = {r["group_id"] for r in data}
    assert group_ids == {"grp_yoga", "grp_running"}


# ---------------------------------------------------------------------------
# Diversity in multi-slot
# ---------------------------------------------------------------------------

def test_diversity_works_in_multi_slot(client, db):
    ws = make_workspace(client, "DIV-8", "div-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga A",
                 group_id="grp_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga B",
                 group_id="grp_yoga", attributes=[("category", "yoga")])

    resp = multi_post(client, wid, "cust_1", [
        {"slot_id": "diverse_slot", "algorithm": "balanced", "top_n": 5,
         "diversity_enabled": True},
        {"slot_id": "normal_slot", "algorithm": "balanced", "top_n": 5,
         "diversity_enabled": False},
    ])
    body = resp.json()
    diverse = body["slots"][0]["results"]
    normal = body["slots"][1]["results"]

    # Diverse slot: 1 per group
    assert len(diverse) == 1
    # Normal slot: dedup also keeps 1 per group (existing behavior)
    assert len(normal) == 1


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_default_diversity_false_preserves_behavior(client, db):
    """Omitting diversity_enabled gives existing behavior."""
    ws = make_workspace(client, "DIV-9", "div-9")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5)
    data = resp.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"


def test_get_endpoint_unaffected(client, db):
    ws = make_workspace(client, "DIV-10", "div-10")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
