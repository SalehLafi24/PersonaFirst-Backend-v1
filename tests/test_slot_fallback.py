"""
Slot fallback mode tests.

Validates strict vs relax_filters behavior, fallback_applied metadata,
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


def seed_product(db, workspace_id, product_id, sku, name, attributes=None):
    p = Product(
        workspace_id=workspace_id, product_id=product_id, sku=sku, name=name,
    )
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id, attribute_value=attr_val))
    db.commit()
    return p


def slot_post(client, wid, customer_id, algorithm, top_n,
              filters=None, fallback_mode=None):
    slot = {
        "slot_id": "test_carousel",
        "algorithm": algorithm,
        "top_n": top_n,
    }
    if filters is not None:
        slot["filters"] = filters
    if fallback_mode is not None:
        slot["fallback_mode"] = fallback_mode
    return client.post(
        f"/workspaces/{wid}/recommendations/slot",
        json={"customer_id": customer_id, "slot": slot},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_invalid_fallback_mode_returns_422(client, db):
    ws = make_workspace(client, "FB-V1", "fb-v1")
    resp = slot_post(client, ws["id"], "cust_1", "balanced", 5,
                     fallback_mode="magic")
    assert resp.status_code == 422
    assert "magic" in resp.text


def test_default_fallback_mode_is_strict(client, db):
    """Omitting fallback_mode behaves like strict — empty filters → empty list."""
    ws = make_workspace(client, "FB-V2", "fb-v2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5, filters=[
        {"attribute_id": "category", "operator": "eq", "value": "electronics"},
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["fallback_applied"] is False


# ---------------------------------------------------------------------------
# strict mode
# ---------------------------------------------------------------------------

def test_strict_returns_empty_when_filters_eliminate_all(client, db):
    ws = make_workspace(client, "FB-S1", "fb-s1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     filters=[
                         {"attribute_id": "category", "operator": "eq", "value": "electronics"},
                     ],
                     fallback_mode="strict")
    body = resp.json()
    assert body["results"] == []
    assert body["fallback_applied"] is False


def test_strict_returns_matches_when_filters_pass(client, db):
    ws = make_workspace(client, "FB-S2", "fb-s2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     filters=[
                         {"attribute_id": "category", "operator": "eq", "value": "yoga"},
                     ],
                     fallback_mode="strict")
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["product_id"] == "prod_1"
    assert body["fallback_applied"] is False


# ---------------------------------------------------------------------------
# relax_filters mode
# ---------------------------------------------------------------------------

def test_relax_filters_returns_unfiltered_when_filters_eliminate_all(client, db):
    """When filters match nothing, relax_filters falls back to full scored set."""
    ws = make_workspace(client, "FB-R1", "fb-r1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "yoga"), ("type", "running")])

    # Filter for electronics — matches nothing
    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     filters=[
                         {"attribute_id": "category", "operator": "eq", "value": "electronics"},
                     ],
                     fallback_mode="relax_filters")
    body = resp.json()
    pids = [r["product_id"] for r in body["results"]]
    assert "prod_yoga" in pids
    assert "prod_run" in pids
    assert body["fallback_applied"] is True


def test_relax_filters_uses_filtered_set_when_filters_pass(client, db):
    """When filters match at least one candidate, relax_filters uses the filtered set."""
    ws = make_workspace(client, "FB-R2", "fb-r2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoe",
                 attributes=[("category", "yoga"), ("type", "running")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     filters=[
                         {"attribute_id": "type", "operator": "eq", "value": "running"},
                     ],
                     fallback_mode="relax_filters")
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["product_id"] == "prod_run"
    assert body["fallback_applied"] is False


def test_relax_filters_respects_top_n(client, db):
    ws = make_workspace(client, "FB-R3", "fb-r3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    for i in range(5):
        seed_product(db, wid, f"prod_{i}", f"SKU-{i}", f"Product {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 2,
                     filters=[
                         {"attribute_id": "category", "operator": "eq", "value": "electronics"},
                     ],
                     fallback_mode="relax_filters")
    body = resp.json()
    assert len(body["results"]) == 2
    assert body["fallback_applied"] is True


def test_relax_filters_preserves_scores(client, db):
    """Fallback results should have the same scores as unfiltered results."""
    ws = make_workspace(client, "FB-R4", "fb-r4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    # No filter
    resp_no_filter = slot_post(client, wid, "cust_1", "balanced", 5)
    # Filter misses → fallback
    resp_fallback = slot_post(client, wid, "cust_1", "balanced", 5,
                              filters=[
                                  {"attribute_id": "type", "operator": "eq", "value": "nope"},
                              ],
                              fallback_mode="relax_filters")

    score_normal = resp_no_filter.json()["results"][0]["recommendation_score"]
    score_fallback = resp_fallback.json()["results"][0]["recommendation_score"]
    assert score_fallback == pytest.approx(score_normal)
    assert resp_fallback.json()["fallback_applied"] is True


# ---------------------------------------------------------------------------
# No filters — fallback_mode is irrelevant
# ---------------------------------------------------------------------------

def test_relax_filters_with_no_filters_returns_normal_results(client, db):
    """When there are no filters, fallback_mode has no effect."""
    ws = make_workspace(client, "FB-NF", "fb-nf")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", "balanced", 5,
                     fallback_mode="relax_filters")
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["product_id"] == "prod_1"
    assert body["fallback_applied"] is False


# ---------------------------------------------------------------------------
# GET endpoint unchanged
# ---------------------------------------------------------------------------

def test_get_endpoint_unaffected(client, db):
    ws = make_workspace(client, "FB-GET", "fb-get")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    resp = client.get(f"/workspaces/{wid}/recommendations/cust_1")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
