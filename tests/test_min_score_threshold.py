"""
Minimum score threshold tests.

Validates that min_score_threshold filters weak recommendations
based on customer_signal_strength.
"""
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


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value,
                  score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def seed_purchase(db, workspace_id, customer_id, product_id, quantity=1):
    product = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=product_id,
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


def _build_high_signal_customer(db, wid, customer_id):
    """High signal (>0.7): many purchases + diverse affinities."""
    for i in range(20):
        seed_product(db, wid, f"hsig_p{i}", f"SKU-HS-{i}", f"HS Product {i}",
                     attributes=[("category", "yoga")])
        seed_purchase(db, wid, customer_id, f"hsig_p{i}")
    seed_affinity(db, wid, customer_id, "category", "yoga", 0.9)
    for attr_type in ["color", "brand", "activity", "size"]:
        for val in ["a", "b", "c", "d"]:
            seed_affinity(db, wid, customer_id, attr_type, val, 0.5)
    seed_purchase(db, wid, "minimal_cust", "hsig_p0")


def _assert_high_signal(client, wid, customer_id):
    ss_resp = client.get(f"/workspaces/{wid}/signal-strength/{customer_id}")
    strength = ss_resp.json()["customer_signal_strength"]
    assert strength > 0.7, f"Expected high signal, got {strength}"


# ---------------------------------------------------------------------------
# High signal -> weak candidates filtered (threshold=0.6)
# ---------------------------------------------------------------------------

def test_high_signal_filters_weak_candidates(client, db):
    """High signal customer (threshold=0.6): candidates scoring < 0.6 filtered."""
    ws = make_workspace(client, "MST-HIGH", "mst-high")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")
    seed_affinity(db, wid, "cust_power", "category", "running", 0.4)

    # Strong product: category=yoga -> score=0.9 (passes 0.6)
    seed_product(db, wid, "prod_strong", "SKU-S", "Strong Product",
                 attributes=[("category", "yoga")])
    # Weak product: category=running -> score=0.4 (filtered by 0.6)
    seed_product(db, wid, "prod_weak", "SKU-W", "Weak Product",
                 attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    product_ids = [r["product_id"] for r in data]
    assert "prod_strong" in product_ids
    assert "prod_weak" not in product_ids


# ---------------------------------------------------------------------------
# Low signal -> weak candidates allowed (threshold=0.2)
# ---------------------------------------------------------------------------

def test_low_signal_allows_weak_candidates(client, db):
    """Low signal customer (threshold=0.2): candidates scoring >= 0.2 kept."""
    ws = make_workspace(client, "MST-LOW", "mst-low")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_new", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_new", "category", "running", 0.3)

    seed_product(db, wid, "prod_strong", "SKU-S", "Strong Product",
                 attributes=[("category", "yoga")])
    # score=0.3 >= threshold 0.2
    seed_product(db, wid, "prod_weak", "SKU-W", "Weak Product",
                 attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_new", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    product_ids = [r["product_id"] for r in data]
    assert "prod_strong" in product_ids
    assert "prod_weak" in product_ids


# ---------------------------------------------------------------------------
# Threshold + diversity both enforced
# ---------------------------------------------------------------------------

def test_threshold_with_diversity(client, db):
    """Threshold filters weak candidates; diversity still enforced on survivors."""
    ws = make_workspace(client, "MST-DIV", "mst-div")
    wid = ws["id"]

    # Low signal customer -> threshold=0.2
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.1)

    # Group A: 2 strong products (score=0.9, above threshold)
    seed_product(db, wid, "grp_a_1", "SKU-GA1", "GroupA 1",
                 group_id="grp_A", attributes=[("category", "yoga")])
    seed_product(db, wid, "grp_a_2", "SKU-GA2", "GroupA 2",
                 group_id="grp_A", attributes=[("category", "yoga")])
    # Group B: 1 weak product (score=0.1 < threshold 0.2)
    seed_product(db, wid, "grp_b_1", "SKU-GB1", "GroupB 1",
                 group_id="grp_B", attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "diversity_mode": "strict",
    })
    data = resp.json()["results"]
    # Group B filtered by threshold (0.1 < 0.2)
    # Group A: 1 of 2 kept by diversity
    assert len(data) == 1
    assert data[0]["group_id"] == "grp_A"


# ---------------------------------------------------------------------------
# Underfill when all candidates below threshold
# ---------------------------------------------------------------------------

def test_threshold_underfill(client, db):
    """When all candidates score below threshold, results are empty."""
    ws = make_workspace(client, "MST-UNFILL", "mst-unfill")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")
    seed_affinity(db, wid, "cust_power", "category", "running", 0.4)

    # 5 products all score 0.4 < threshold 0.6
    for i in range(5):
        seed_product(db, wid, f"weak_{i}", f"SKU-W-{i}", f"Weak {i}",
                     attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    assert len(data) == 0


# ---------------------------------------------------------------------------
# Threshold + scan depth
# ---------------------------------------------------------------------------

def test_threshold_with_scan_depth(client, db):
    """Threshold filters within the scan window; both constraints active."""
    ws = make_workspace(client, "MST-SCAN", "mst-scan")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")
    seed_affinity(db, wid, "cust_power", "category", "running", 0.4)

    # 10 strong (score=0.9) + 10 weak (score=0.4)
    for i in range(10):
        seed_product(db, wid, f"strong_{i}", f"SKU-S-{i}", f"Strong {i}",
                     attributes=[("category", "yoga")])
    for i in range(10):
        seed_product(db, wid, f"weak_{i}", f"SKU-W-{i}", f"Weak {i}",
                     attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 20,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    # Only strong products survive threshold=0.6
    assert all(r["recommendation_score"] >= 0.6 for r in data)
    assert len(data) == 10


# ---------------------------------------------------------------------------
# Threshold + cross-slot exclusion
# ---------------------------------------------------------------------------

def test_threshold_with_cross_slot_exclusion(client, db):
    """Threshold and cross-slot exclusion work together correctly."""
    ws = make_workspace(client, "MST-EXC", "mst-exc")
    wid = ws["id"]

    # Low signal -> threshold=0.2
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.3)

    seed_product(db, wid, "prod_a", "SKU-A", "Product A",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                 attributes=[("category", "yoga")])
    # Weak product: score=0.3 >= threshold 0.2, survives
    seed_product(db, wid, "prod_weak", "SKU-W", "Weak Product",
                 attributes=[("category", "running")])

    resp = client.post(
        f"/workspaces/{wid}/recommendations/slots",
        json={
            "customer_id": "cust_1",
            "slots": [
                {"slot_id": "s1", "algorithm": "balanced", "top_n": 1,
                 "diversity_mode": "off"},
                {"slot_id": "s2", "algorithm": "balanced", "top_n": 5,
                 "diversity_mode": "off", "exclude_previous_slots": True},
            ],
        },
    )
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    s1_ids = {r["product_id"] for r in slots[0]["results"]}
    s2_ids = {r["product_id"] for r in slots[1]["results"]}
    # No overlap between slots
    assert s1_ids & s2_ids == set()
    # Weak product passes threshold and appears in slot 2
    assert "prod_weak" in s2_ids
