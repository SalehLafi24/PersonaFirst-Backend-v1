"""
Slot fallback_behavior tests.

Validates that fallback_behavior allows graceful degradation when the
selected algorithm's primary signal is absent, using an effective_score
for thresholding and ranking while preserving the original
recommendation_score in the response.
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
    """High signal (>0.7): many purchases + diverse affinities.
    No behavioral relationships or attribute relationships are created,
    so behavioral_score and relationship_score will be 0 for new products."""
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
# Validation
# ---------------------------------------------------------------------------

def test_invalid_fallback_behavior_returns_422(client, db):
    ws = make_workspace(client, "FB-V", "fb-v")
    resp = slot_post(client, ws["id"], "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "fallback_behavior": "magic",
    })
    assert resp.status_code == 422
    assert "magic" in resp.text


# ---------------------------------------------------------------------------
# behavior_first + fallback_behavior="none" -> empty (default, unchanged)
# ---------------------------------------------------------------------------

def test_behavior_first_no_fallback_empty(client, db):
    """behavior_first with no behavioral signal and threshold=0.6 filters all.
    fallback_behavior='none' preserves this existing behavior."""
    ws = make_workspace(client, "FB-NONE", "fb-none")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    # Product matches category=yoga -> direct_score=0.9, behavioral_score=0
    # behavior_first: rec_score = 0.9*0.3 = 0.27 < threshold 0.6
    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "behavior_first", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "none",
    })
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 0


# ---------------------------------------------------------------------------
# behavior_first + fallback_behavior="direct" -> uses direct_score
# ---------------------------------------------------------------------------

def test_behavior_first_direct_fallback(client, db):
    """With fallback_behavior='direct', behavioral_score=0 triggers fallback.
    effective_score = direct_score = 0.9 >= threshold 0.6 -> returned."""
    ws = make_workspace(client, "FB-DIR", "fb-dir")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "behavior_first", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "direct",
    })
    data = resp.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_test"
    # Response still shows the original algorithm-weighted recommendation_score
    assert data[0]["recommendation_score"] < 0.6


# ---------------------------------------------------------------------------
# behavior_first + fallback_behavior="balanced" -> uses blended fallback
# ---------------------------------------------------------------------------

def test_behavior_first_balanced_fallback(client, db):
    """With fallback_behavior='balanced', effective_score =
    direct_score + relationship_score + behavioral_score."""
    ws = make_workspace(client, "FB-BAL", "fb-bal")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "behavior_first", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "balanced",
    })
    data = resp.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_test"


# ---------------------------------------------------------------------------
# relationship_only + fallback_behavior="direct" -> falls back
# ---------------------------------------------------------------------------

def test_relationship_only_direct_fallback(client, db):
    """relationship_only: direct_weight=0.0, rel_weight=1.0.
    With no relationship data, recommendation_score=0 -> eliminated.
    fallback_behavior='direct' rescues via effective_score = direct_score."""
    ws = make_workspace(client, "FB-REL", "fb-rel")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    # Without fallback: recommendation_score = 0.9*0.0 + 0*1.0 = 0 -> eliminated
    resp_none = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "relationship_only", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "none",
    })
    assert len(resp_none.json()["results"]) == 0

    # With fallback: effective_score = direct_score = 0.9 -> returned
    resp_fb = slot_post(client, wid, "cust_power", {
        "slot_id": "s2", "algorithm": "relationship_only", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "direct",
    })
    data = resp_fb.json()["results"]
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_test"
    # recommendation_score reflects original weighted score (0.0)
    assert data[0]["recommendation_score"] == 0.0


# ---------------------------------------------------------------------------
# Threshold uses effective_score, not raw recommendation_score
# ---------------------------------------------------------------------------

def test_threshold_uses_effective_score(client, db):
    """Product with recommendation_score < threshold passes because
    effective_score >= threshold when fallback applies."""
    ws = make_workspace(client, "FB-THR", "fb-thr")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    # behavior_first: rec_score = 0.9*0.3 = 0.27 (below threshold 0.6)
    # effective_score with "direct" fallback = 0.9 (above threshold 0.6)
    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "behavior_first", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "direct",
    })
    data = resp.json()["results"]
    assert len(data) == 1
    # recommendation_score is the original weighted score, below threshold
    assert data[0]["recommendation_score"] < 0.6
    # But the product was returned because effective_score >= 0.6
    assert data[0]["direct_score"] >= 0.6


# ---------------------------------------------------------------------------
# Explanation reflects fallback
# ---------------------------------------------------------------------------

def test_fallback_explanation_present(client, db):
    """When fallback triggers, explanation includes a fallback marker."""
    ws = make_workspace(client, "FB-EXP", "fb-exp")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "behavior_first", "top_n": 5,
        "diversity_mode": "off", "fallback_behavior": "direct",
    })
    data = resp.json()["results"]
    assert len(data) == 1
    explanation = data[0]["explanation"]
    assert "fell back to direct score" in explanation
    assert "Behavioral signal unavailable" in explanation


# ---------------------------------------------------------------------------
# Default fallback_behavior preserves existing behavior
# ---------------------------------------------------------------------------

def test_default_fallback_behavior_unchanged(client, db):
    """Without fallback_behavior in the request (defaults to 'none'),
    behavior is identical to before the feature was added."""
    ws = make_workspace(client, "FB-DEF", "fb-def")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    seed_product(db, wid, "prod_test", "SKU-T", "Test Product",
                 attributes=[("category", "yoga")])

    # No fallback_behavior specified -> defaults to "none"
    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "behavior_first", "top_n": 5,
        "diversity_mode": "off",
    })
    # behavior_first with no behavioral signal, threshold=0.6 -> empty
    assert len(resp.json()["results"]) == 0
