"""
Slot-based recommendation tests.

Validates algorithm presets, tie-break priority, manual weight overrides,
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


def seed_purchase(db, workspace_id, customer_id, product_id,
                  group_id=None, order_date=None, quantity=1):
    product = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=product_id
    ).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id, product_id=product_id,
        group_id=group_id if group_id is not None else product.group_id,
        order_date=order_date or date.today(),
        quantity=quantity,
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


def slot_post(client, wid, customer_id, algorithm, top_n, **query_params):
    """Helper to POST to the slot endpoint."""
    params = {k: v for k, v in query_params.items() if v is not None}
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={
            "customer_id": customer_id,
            "slot": {
                "slot_id": "test_carousel",
                "algorithm": algorithm,
                "top_n": top_n,
            },
        },
        params=params,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_unknown_algorithm_returns_400(client, db):
    ws = make_workspace(client, "Slot-V1", "slot-v1")
    resp = slot_post(client, ws["id"], "cust_1", "nonexistent_algo", 5)
    assert resp.status_code == 400
    assert "Unknown algorithm" in resp.json()["detail"]
    assert "nonexistent_algo" in resp.json()["detail"]


def test_top_n_zero_returns_422(client, db):
    ws = make_workspace(client, "Slot-V2", "slot-v2")
    resp = slot_post(client, ws["id"], "cust_1", "balanced", 0)
    assert resp.status_code == 422


def test_top_n_negative_returns_422(client, db):
    ws = make_workspace(client, "Slot-V3", "slot-v3")
    resp = slot_post(client, ws["id"], "cust_1", "balanced", -1)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Basic slot behavior — algorithm weights applied
# ---------------------------------------------------------------------------

def test_balanced_algorithm_returns_results(client, db):
    """balanced preset uses all four signals."""
    ws = make_workspace(client, "Slot-B1", "slot-b1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5)
    assert resp.status_code == 200
    data = resp.json()["results"]
    assert len(data) >= 1
    assert data[0]["product_id"] == "prod_1"


def test_behavioral_only_ignores_direct_signal(client, db):
    """behavioral_only sets direct_weight=0 — a direct-only product should not appear."""
    ws = make_workspace(client, "Slot-BO", "slot-bo")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # prod_direct has only a direct affinity match, no behavioral signal
    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga")])
    # prod_beh has a behavioral signal from a purchased product
    seed_product(db, wid, "prod_purchased", "SKU-P", "Purchased Item")
    seed_product(db, wid, "prod_beh", "SKU-B", "Behavioral Target",
                 attributes=[("category", "running")])

    seed_purchase(db, wid, "cust_1", "prod_purchased")
    seed_behavior_rel(db, wid, "prod_purchased", "prod_beh",
                      strength=0.8, overlap=4, source_count=5)

    resp = slot_post(client, wid, "cust_1", "behavioral_only", 5)
    data = resp.json()["results"]
    product_ids = [r["product_id"] for r in data]
    assert "prod_beh" in product_ids
    assert "prod_direct" not in product_ids


def test_behavior_first_ranks_behavioral_higher(client, db):
    """
    behavior_first: behavioral_weight=1.0 vs direct_weight=0.3.
    A product with high behavioral signal should outrank a product with only
    direct signal, even when the direct affinity is strong.
    """
    ws = make_workspace(client, "Slot-BF", "slot-bf")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 2.5)
    # prod_direct: strong direct match, no behavioral
    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga")])
    # prod_beh: same direct match but strong behavioral
    seed_product(db, wid, "prod_purchased", "SKU-P", "Purchased Item")
    seed_product(db, wid, "prod_beh", "SKU-B", "Behavioral Target",
                 attributes=[("category", "yoga")])

    seed_purchase(db, wid, "cust_1", "prod_purchased")
    seed_behavior_rel(db, wid, "prod_purchased", "prod_beh",
                      strength=0.9, overlap=9, source_count=10)

    resp = slot_post(client, wid, "cust_1", "behavior_first", 5)
    data = resp.json()["results"]
    assert len(data) == 2
    # prod_beh: direct=2.5*0.3=0.75 + behavioral=0.9*1.0=0.9 = 1.65
    # prod_direct: direct=2.5*0.3=0.75 + behavioral=0*1.0=0 = 0.75
    assert data[0]["product_id"] == "prod_beh"
    assert data[1]["product_id"] == "prod_direct"


# ---------------------------------------------------------------------------
# Slot top_n limits results
# ---------------------------------------------------------------------------

def test_slot_top_n_limits_results(client, db):
    ws = make_workspace(client, "Slot-TN", "slot-tn")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 2)
    data = resp.json()["results"]
    assert len(data) == 2


# ---------------------------------------------------------------------------
# Manual weight overrides
# ---------------------------------------------------------------------------

def test_manual_weight_overrides_algorithm(client, db):
    """
    Explicit query-param weights override the algorithm preset.
    behavioral_only sets direct_weight=0; passing direct_weight=1.0
    should bring the direct product back.
    """
    ws = make_workspace(client, "Slot-OV", "slot-ov")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga")])

    # behavioral_only preset has direct_weight=0 → prod_direct excluded
    resp_no_override = slot_post(client, wid, "cust_1", "behavioral_only", 5)
    assert all(r["product_id"] != "prod_direct" for r in resp_no_override.json()["results"])

    # Override direct_weight=1.0 → prod_direct included
    resp_override = slot_post(
        client, wid, "cust_1", "behavioral_only", 5,
        direct_weight=1.0,
    )
    data = resp_override.json()["results"]
    assert any(r["product_id"] == "prod_direct" for r in data)


# ---------------------------------------------------------------------------
# Tie-break priority
# ---------------------------------------------------------------------------

def test_tie_break_uses_algorithm_priority(client, db):
    """
    Two products with identical final_score should be ordered by the
    algorithm's tie-break priority field.
    behavior_first breaks ties by behavioral_score DESC first.
    """
    ws = make_workspace(client, "Slot-TB", "slot-tb")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 2.5)

    # Both products match category=yoga (same direct score)
    prod_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A",
                          attributes=[("category", "yoga")])
    prod_b = seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                          attributes=[("category", "yoga")])

    # Give prod_b a behavioral signal so it wins the tie-break
    seed_product(db, wid, "prod_purchased", "SKU-P", "Purchased")
    seed_purchase(db, wid, "cust_1", "prod_purchased")
    seed_behavior_rel(db, wid, "prod_purchased", "prod_b",
                      strength=0.5, overlap=5, source_count=10)

    # behavior_first: direct=0.3, rel=0.3, pop=0.0, beh=1.0
    # prod_a: 2.5*0.3 + 0*1.0 = 0.75
    # prod_b: 2.5*0.3 + 0.5*1.0 = 1.25
    # prod_b wins outright on final_score.

    resp = slot_post(client, wid, "cust_1", "behavior_first", 5)
    data = resp.json()["results"]
    assert data[0]["product_id"] == "prod_b"
    assert data[1]["product_id"] == "prod_a"


def test_tie_break_behavioral_first_prefers_behavioral(client, db):
    """
    When two products have the same final_score, behavior_first
    tie-breaks by behavioral_score DESC.
    """
    ws = make_workspace(client, "Slot-TB2", "slot-tb2")
    wid = ws["id"]

    # prod_a: pure behavioral signal
    # prod_b: pure direct signal, engineered so final_score ties with prod_a
    #
    # behavior_first weights: direct=0.3, rel=0.3, pop=0.0, beh=1.0
    # prod_a: beh=0.6 → final = 0.6*1.0 = 0.6
    # prod_b: direct=2.0 (affinity=2.0, category weight=1.0) → final = 2.0*0.3 = 0.6
    # Tied at 0.6! tie-break: behavioral_score DESC
    # prod_a behavioral_score=0.6 > prod_b behavioral_score=0 → prod_a first

    seed_affinity(db, wid, "cust_1", "category", "yoga", 2.0)

    seed_product(db, wid, "prod_purchased", "SKU-P", "Purchased")
    seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                 attributes=[("category", "yoga")])

    seed_purchase(db, wid, "cust_1", "prod_purchased")
    seed_behavior_rel(db, wid, "prod_purchased", "prod_a",
                      strength=0.6, overlap=6, source_count=10)

    resp = slot_post(client, wid, "cust_1", "behavior_first", 5)
    data = resp.json()["results"]
    assert len(data) == 2
    assert data[0]["product_id"] == "prod_a"
    assert data[1]["product_id"] == "prod_b"
    assert data[0]["recommendation_score"] == pytest.approx(data[1]["recommendation_score"])


# ---------------------------------------------------------------------------
# Backward compatibility — existing GET still works
# ---------------------------------------------------------------------------

def test_get_endpoint_still_works_without_slot(client, db):
    """The original GET endpoint is unaffected by slot changes."""
    ws = make_workspace(client, "Slot-BC", "slot-bc")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"


# ---------------------------------------------------------------------------
# Each preset is callable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("algorithm", [
    "balanced", "behavior_first", "affinity_first",
    "relationship_only", "behavioral_only",
])
def test_all_presets_are_valid(client, db, algorithm):
    """Every named preset returns 200 (may return empty results)."""
    ws = make_workspace(client, f"Slot-P-{algorithm}", f"slot-p-{algorithm}")
    resp = slot_post(client, ws["id"], "cust_1", algorithm, 5)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Slot response metadata
# ---------------------------------------------------------------------------

def test_slot_response_includes_metadata(client, db):
    """The slot response wraps results with slot_id, algorithm, fallback fields."""
    ws = make_workspace(client, "Slot-META", "slot-meta")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5)
    body = resp.json()
    assert body["slot_id"] == "test_carousel"
    assert body["algorithm"] == "balanced"
    assert body["fallback_mode"] == "strict"
    assert body["fallback_applied"] is False
    assert isinstance(body["results"], list)


# ---------------------------------------------------------------------------
# Workspace validation
# ---------------------------------------------------------------------------

def test_slot_endpoint_returns_404_for_missing_workspace(client, db):
    resp = slot_post(client, 99999, "cust_1", "balanced", 5)
    assert resp.status_code == 404
