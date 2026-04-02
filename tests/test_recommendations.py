import pytest

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.product import Product, ProductAttribute


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_affinities(db, workspace_id, customer_id, affinities):
    """affinities: list of (attribute_id, attribute_value, score)"""
    for attribute_id, attribute_value, score in affinities:
        db.add(CustomerAttributeAffinity(
            workspace_id=workspace_id,
            customer_id=customer_id,
            attribute_id=attribute_id,
            attribute_value=attribute_value,
            score=score,
        ))
    db.commit()


def seed_product(db, workspace_id, product_id, sku, name, group_id, attributes):
    """attributes: list of (attribute_id, attribute_value)"""
    product = Product(
        workspace_id=workspace_id,
        product_id=product_id,
        sku=sku,
        name=name,
        group_id=group_id,
    )
    db.add(product)
    db.flush()
    for attribute_id, attribute_value in attributes:
        db.add(ProductAttribute(
            product_id=product.id,
            attribute_id=attribute_id,
            attribute_value=attribute_value,
        ))
    db.commit()
    return product


# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------

def test_returns_matching_product(client, db):
    ws = make_workspace(client, "R1", "r1")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "yoga", 0.9),
        ("activity", "pregnant", 0.8),
    ])
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", "group_yoga", [
        ("category", "yoga"),
    ])

    response = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"
    assert data[0]["sku"] == "SKU-001"
    assert data[0]["name"] == "Yoga Mat"
    assert data[0]["group_id"] == "group_yoga"
    assert data[0]["recommendation_score"] == pytest.approx(0.9)
    assert len(data[0]["matched_attributes"]) == 1
    assert data[0]["matched_attributes"][0]["attribute_id"] == "category"
    assert data[0]["matched_attributes"][0]["score"] == pytest.approx(0.9)


def test_matched_attributes_included_in_response(client, db):
    ws = make_workspace(client, "R2", "r2")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "yoga", 0.9),
        ("activity", "pregnant", 0.8),
    ])
    seed_product(db, wid, "prod_1", "SKU-001", "Prenatal Yoga Kit", "group_1", [
        ("category", "yoga"),
        ("activity", "pregnant"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data[0]["matched_attributes"]) == 2
    attr_ids = {a["attribute_id"] for a in data[0]["matched_attributes"]}
    assert attr_ids == {"category", "activity"}


def test_explanation_is_present(client, db):
    ws = make_workspace(client, "R3", "r3")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [("category", "yoga", 0.9)])
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", "group_1", [
        ("category", "yoga"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert "category" in data[0]["explanation"]
    assert "yoga" in data[0]["explanation"]


# ---------------------------------------------------------------------------
# No matches
# ---------------------------------------------------------------------------

def test_no_affinities_returns_empty(client, db):
    ws = make_workspace(client, "R4", "r4")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", "group_1", [
        ("category", "yoga"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_unknown").json()
    assert data == []


def test_no_products_returns_empty(client, db):
    ws = make_workspace(client, "R5", "r5")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [("category", "yoga", 0.9)])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_product_with_no_matching_attribute_excluded(client, db):
    ws = make_workspace(client, "R6", "r6")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [("category", "yoga", 0.9)])
    seed_product(db, wid, "prod_1", "SKU-001", "Football Kit", "group_1", [
        ("type", "football"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_recommendation_score_is_sum_of_matched_affinity_scores(client, db):
    ws = make_workspace(client, "R7", "r7")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "yoga", 0.9),
        ("activity", "pregnant", 0.8),
    ])
    seed_product(db, wid, "prod_1", "SKU-001", "Prenatal Kit", "group_1", [
        ("category", "yoga"),
        ("activity", "pregnant"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data[0]["recommendation_score"] == pytest.approx(1.7)


def test_results_sorted_by_recommendation_score_desc(client, db):
    ws = make_workspace(client, "R8", "r8")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "yoga", 0.9),
        ("activity", "pregnant", 0.8),
        ("type", "football", 0.3),
    ])
    # prod_low matches 1 attribute (score 0.3)
    seed_product(db, wid, "prod_low", "SKU-LOW", "Football Kit", "group_low", [
        ("type", "football"),
    ])
    # prod_high matches 2 attributes (score 1.7)
    seed_product(db, wid, "prod_high", "SKU-HIGH", "Prenatal Yoga Kit", "group_high", [
        ("category", "yoga"),
        ("activity", "pregnant"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data[0]["product_id"] == "prod_high"
    assert data[1]["product_id"] == "prod_low"


# ---------------------------------------------------------------------------
# Same group — no automatic dedup (diversity controls this)
# ---------------------------------------------------------------------------

def test_same_group_returns_all_ranked(client, db):
    """Without diversity, multiple products from the same group are returned
    in score order."""
    ws = make_workspace(client, "R9", "r9")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "yoga", 0.9),
        ("activity", "pregnant", 0.8),
    ])
    # Two products in the same group — both returned, highest score first
    seed_product(db, wid, "prod_basic", "SKU-A", "Basic Yoga Mat", "group_yoga", [
        ("category", "yoga"),
    ])
    seed_product(db, wid, "prod_better", "SKU-B", "Prenatal Yoga Mat", "group_yoga", [
        ("category", "yoga"),
        ("activity", "pregnant"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 2
    assert data[0]["product_id"] == "prod_better"
    assert data[1]["product_id"] == "prod_basic"


# ---------------------------------------------------------------------------
# Query params
# ---------------------------------------------------------------------------

def test_min_score_filters_affinities(client, db):
    ws = make_workspace(client, "R10", "r10")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "yoga", 0.9),
        ("type", "football", 0.3),  # below threshold
    ])
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat", "group_yoga", [
        ("category", "yoga"),
    ])
    seed_product(db, wid, "prod_ball", "SKU-F", "Football", "group_sport", [
        ("type", "football"),
    ])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?min_score=0.5").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_yoga"


def test_top_n_limits_results(client, db):
    ws = make_workspace(client, "R11", "r11")
    wid = ws["id"]

    seed_affinities(db, wid, "cust_1", [
        ("category", "val_a", 0.9),
        ("type", "val_b", 0.8),
        ("activity", "val_c", 0.7),
    ])
    for i, (attr, val) in enumerate([("category", "val_a"), ("type", "val_b"), ("activity", "val_c")]):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}", f"group_{i}", [(attr, val)])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?top_n=2").json()
    assert len(data) == 2


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------

def test_recommendations_scoped_to_workspace(client, db):
    ws1 = make_workspace(client, "R12", "r12")
    ws2 = make_workspace(client, "R13", "r13")

    # Affinities and products only in ws1
    seed_affinities(db, ws1["id"], "cust_1", [("category", "yoga", 0.9)])
    seed_product(db, ws1["id"], "prod_1", "SKU-001", "Yoga Mat", "group_1", [
        ("category", "yoga"),
    ])

    # ws2 should return nothing for the same customer
    data = client.get(f"/workspaces/{ws2['id']}/recommendations/cust_1").json()
    assert data == []


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------

def test_workspace_not_found(client):
    response = client.get("/workspaces/999/recommendations/cust_1")
    assert response.status_code == 404
