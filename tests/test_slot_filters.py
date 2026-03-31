"""
Slot filter tests.

Validates eq/in filters, AND logic, empty-result behavior,
validation errors, and backward compatibility.
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
                 recommendation_role="same_use_case", attributes=None):
    p = Product(
        workspace_id=workspace_id, product_id=product_id, sku=sku, name=name,
        group_id=group_id, recommendation_role=recommendation_role,
    )
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id, attribute_value=attr_val))
    db.commit()
    return p


def slot_post(client, wid, customer_id, algorithm, top_n, filters=None):
    """Helper to POST to the slot endpoint."""
    slot = {
        "slot_id": "test_carousel",
        "algorithm": algorithm,
        "top_n": top_n,
    }
    if filters is not None:
        slot["filters"] = filters
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={"customer_id": customer_id, "slot": slot},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_invalid_operator_returns_422(client, db):
    ws = make_workspace(client, "SF-V1", "sf-v1")
    resp = slot_post(client, ws["id"], "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "not_eq", "value": "yoga"},
    ])
    assert resp.status_code == 422
    assert "not_eq" in resp.text


def test_empty_filters_list_works(client, db):
    """An explicit empty filters list is the same as no filters."""
    ws = make_workspace(client, "SF-V2", "sf-v2")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[])
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


# ---------------------------------------------------------------------------
# eq operator
# ---------------------------------------------------------------------------

def test_eq_filter_includes_matching_product(client, db):
    ws = make_workspace(client, "SF-EQ1", "sf-eq1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.8)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "yoga"},
    ])
    data = resp.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_yoga"


def test_eq_filter_excludes_non_matching(client, db):
    ws = make_workspace(client, "SF-EQ2", "sf-eq2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "running", 0.9)
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "yoga"},
    ])
    assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# in operator
# ---------------------------------------------------------------------------

def test_in_filter_matches_any_listed_value(client, db):
    ws = make_workspace(client, "SF-IN1", "sf-in1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.8)
    seed_affinity(db, wid, "cust_1", "category", "swimming", 0.7)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "running")])
    seed_product(db, wid, "prod_swim", "SKU-S", "Swim Cap",
                 attributes=[("category", "swimming")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "in", "value": ["yoga", "running"]},
    ])
    data = resp.json()["results"]
    pids = [r["product_id"] for r in data]
    assert "prod_yoga" in pids
    assert "prod_run" in pids
    assert "prod_swim" not in pids


# ---------------------------------------------------------------------------
# AND logic — multiple filters
# ---------------------------------------------------------------------------

def test_multiple_filters_use_and_logic(client, db):
    ws = make_workspace(client, "SF-AND", "sf-and")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "blue", 0.5)
    # prod_a: category=yoga, color=blue → passes both
    seed_product(db, wid, "prod_a", "SKU-A", "Blue Yoga Mat",
                 attributes=[("category", "yoga"), ("color", "blue")])
    # prod_b: category=yoga, color=red → fails color filter
    seed_product(db, wid, "prod_b", "SKU-B", "Red Yoga Mat",
                 attributes=[("category", "yoga"), ("color", "red")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "yoga"},
        {"attribute_id": "color", "operator": "eq", "value": "blue"},
    ])
    data = resp.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_a"


# ---------------------------------------------------------------------------
# Missing attribute fails filter
# ---------------------------------------------------------------------------

def test_product_without_filtered_attribute_is_excluded(client, db):
    ws = make_workspace(client, "SF-MISS", "sf-miss")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # prod_1 has category=yoga but no color attribute at all
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "color", "operator": "eq", "value": "blue"},
    ])
    assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# All candidates filtered out → empty list
# ---------------------------------------------------------------------------

def test_all_filtered_out_returns_empty(client, db):
    ws = make_workspace(client, "SF-EMPTY", "sf-empty")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "electronics"},
    ])
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# Filters work with custom attributes
# ---------------------------------------------------------------------------

def test_filter_works_with_custom_attribute(client, db):
    ws = make_workspace(client, "SF-CUST", "sf-cust")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Premium Mat",
                 attributes=[("category", "yoga"), ("tier", "premium")])
    seed_product(db, wid, "prod_b", "SKU-B", "Basic Mat",
                 attributes=[("category", "yoga"), ("tier", "basic")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "tier", "operator": "eq", "value": "premium"},
    ])
    data = resp.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_a"


# ---------------------------------------------------------------------------
# Filters applied after scoring — scores are computed regardless of filters
# ---------------------------------------------------------------------------

def test_filtered_product_scores_are_accurate(client, db):
    """The surviving product should have a normal score, not one inflated
    or deflated by the filter step."""
    ws = make_workspace(client, "SF-SCORE", "sf-score")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "running")])

    # Without filter
    resp_all = slot_post(client, wid, "cust_1", "balanced", 5)
    # With filter — prod_yoga should have the same score
    resp_filtered = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "yoga"},
    ])

    all_data = resp_all.json()["results"]
    filtered_data = resp_filtered.json()["results"]

    yoga_from_all = next(r for r in all_data if r["product_id"] == "prod_yoga")
    yoga_from_filtered = filtered_data[0]

    assert yoga_from_filtered["recommendation_score"] == pytest.approx(
        yoga_from_all["recommendation_score"]
    )


# ---------------------------------------------------------------------------
# Backward compatibility — no filters = existing behavior
# ---------------------------------------------------------------------------

def test_slot_without_filters_unchanged(client, db):
    ws = make_workspace(client, "SF-BC", "sf-bc")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5)
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_get_endpoint_unaffected_by_filter_feature(client, db):
    """The original GET endpoint has no filter support and still works."""
    ws = make_workspace(client, "SF-GET", "sf-get")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# top_n applies after filtering
# ---------------------------------------------------------------------------

def test_top_n_applied_after_filter(client, db):
    """top_n limits results AFTER filters have reduced the candidate set."""
    ws = make_workspace(client, "SF-TN", "sf-tn")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Yoga Mat {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 2, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "yoga"},
    ])
    assert len(resp.json()["results"]) == 2
