"""
Recommendation V7 tests: behavioral co-purchase scoring.

Behavioral score accumulates strength from product_behavior_relationships where
source_product_db_id is in the customer's purchase history.

  strength(A → B) = customers_who_bought_both / customers_who_bought_A
  behavioral_score(candidate) = Σ strength for each purchased product that points to candidate

Key properties verified:
- behavioral_weight=0 (default) never changes recommendation_score
- behavioral_score is always populated when relationships exist
- behavioral_weight > 0 shifts rankings
- suppression blocks behavioral candidates
- deterministic tie-break by product PK
- workspace isolation holds
- behavioral-only candidates (no affinity) surface correctly
- direct + behavioral scores combine correctly
"""
import pytest
from datetime import date, timedelta

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.product_behavior_relationship import ProductBehaviorRelationship


# ---------------------------------------------------------------------------
# Helpers (mirrors pattern from existing test files)
# ---------------------------------------------------------------------------

def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value, score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None,
                 repurchase_behavior=None, repurchase_window_days=None,
                 recommendation_role="same_use_case", attributes=None):
    p = Product(
        workspace_id=workspace_id, product_id=product_id, sku=sku, name=name,
        group_id=group_id, repurchase_behavior=repurchase_behavior,
        repurchase_window_days=repurchase_window_days,
        recommendation_role=recommendation_role,
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


def seed_behavior_rel(db, workspace_id, source_product, target_product, strength,
                      overlap=1, source_count=1):
    db.add(ProductBehaviorRelationship(
        workspace_id=workspace_id,
        source_product_db_id=source_product.id,
        target_product_db_id=target_product.id,
        strength=strength,
        customer_overlap_count=overlap,
        source_customer_count=source_count,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# V7-1: Behavioral-only recommendation (no affinity at all)
# ---------------------------------------------------------------------------

def test_behavioral_only_recommendation(client, db):
    """
    Customer has zero affinity data. They purchased product A.
    Behavioral relationship A→B (strength=0.8) exists.
    With behavioral_weight > 0, product B must surface.
    B has no direct/relationship/popularity signal.
    """
    ws = make_workspace(client, "V7-1", "v7-1")
    wid = ws["id"]

    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    p_b = seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                       attributes=[("type", "cardio")])

    seed_purchase(db, wid, "cust_1", "prod_a")
    seed_behavior_rel(db, wid, p_a, p_b, strength=0.8, overlap=1, source_count=1)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=1.0"
    ).json()

    product_ids = [r["product_id"] for r in data]
    assert "prod_b" in product_ids
    assert "prod_a" not in product_ids   # exact-purchase suppression

    rec = next(r for r in data if r["product_id"] == "prod_b")
    assert rec["behavioral_score"] == pytest.approx(0.8)
    assert rec["direct_score"] == pytest.approx(0.0)
    assert rec["relationship_score"] == pytest.approx(0.0)
    assert rec["recommendation_source"] == "behavioral"
    assert len(rec["behavioral_matches"]) == 1
    assert rec["behavioral_matches"][0]["source_product_id"] == "prod_a"
    assert rec["behavioral_matches"][0]["strength"] == pytest.approx(0.8)
    assert rec["behavioral_matches"][0]["contribution"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# V7-2: Direct + behavioral combination
# ---------------------------------------------------------------------------

def test_direct_plus_behavioral_combination(client, db):
    """
    Product C has both a direct affinity match and a behavioral relationship
    from the customer's purchased product A.
    Both scores contribute to recommendation_score.
    """
    ws = make_workspace(client, "V7-2", "v7-2")
    wid = ws["id"]

    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    p_c = seed_product(db, wid, "prod_c", "SKU-C", "Product C",
                       attributes=[("type", "yoga")])

    seed_affinity(db, wid, "cust_1", "type", "yoga", 0.6)
    seed_purchase(db, wid, "cust_1", "prod_a")
    seed_behavior_rel(db, wid, p_a, p_c, strength=0.5)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=1.0&relationship_weight=1.0&behavioral_weight=1.0"
    ).json()

    assert len(data) == 1
    rec = data[0]
    assert rec["product_id"] == "prod_c"
    # direct_score: type=yoga, affinity=0.6, weight=1.0 (core) → 0.6
    assert rec["direct_score"] == pytest.approx(0.6)
    # behavioral_score: A→C strength=0.5
    assert rec["behavioral_score"] == pytest.approx(0.5)
    # recommendation_score = 0.6*1.0 + 0.0*1.0 + 0.0*0.0 + 0.5*1.0 = 1.1
    assert rec["recommendation_score"] == pytest.approx(1.1)
    assert rec["recommendation_source"] == "direct+behavioral"


# ---------------------------------------------------------------------------
# V7-3: behavioral_weight affects ranking
# ---------------------------------------------------------------------------

def test_behavioral_weight_affects_ranking(client, db):
    """
    Product D has higher direct score; Product E has no affinity but high behavioral.
    behavioral_weight=0 → D ranks first.
    behavioral_weight=1.0 → E ranks first when its behavioral_score > D's direct_score.
    """
    ws = make_workspace(client, "V7-3", "v7-3")
    wid = ws["id"]

    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    p_d = seed_product(db, wid, "prod_d", "SKU-D", "Product D",
                       attributes=[("type", "strength")])
    p_e = seed_product(db, wid, "prod_e", "SKU-E", "Product E",
                       attributes=[("brand", "niche")])

    seed_affinity(db, wid, "cust_1", "type", "strength", 0.9)
    seed_purchase(db, wid, "cust_1", "prod_a")
    # A→E strength=2.0; no relationship A→D
    seed_behavior_rel(db, wid, p_a, p_e, strength=2.0)

    # Without behavioral_weight: D wins on direct_score (0.9 > 0)
    data_no_beh = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=0.0"
    ).json()
    assert data_no_beh[0]["product_id"] == "prod_d"

    # With behavioral_weight=1.0:
    # D: direct=0.9*1.0=0.9, behavioral=0 → final=0.9
    # E: direct=0 (brand not in affinity), behavioral=2.0 → final=2.0  → E wins
    data_with_beh = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=1.0"
    ).json()
    assert data_with_beh[0]["product_id"] == "prod_e"
    assert data_with_beh[0]["behavioral_score"] == pytest.approx(2.0)
    assert data_with_beh[0]["recommendation_score"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# V7-4: Suppression blocks behavioral candidates
# ---------------------------------------------------------------------------

def test_exact_suppression_blocks_behavioral_target(client, db):
    """
    Behavioral relationship A→B exists, but the customer already bought B (one_time).
    Even with a high behavioral_weight, B must not appear.
    """
    ws = make_workspace(client, "V7-4", "v7-4")
    wid = ws["id"]

    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    p_b = seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                       repurchase_behavior="one_time")

    seed_purchase(db, wid, "cust_1", "prod_a")
    seed_purchase(db, wid, "cust_1", "prod_b")
    seed_behavior_rel(db, wid, p_a, p_b, strength=1.0)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=10.0"
    ).json()

    product_ids = [r["product_id"] for r in data]
    assert "prod_b" not in product_ids


def test_group_suppression_blocks_behavioral_target(client, db):
    """
    Product B is in the same group as purchased product A (one_time).
    Behavioral relationship points at B — it must still be suppressed.
    """
    ws = make_workspace(client, "V7-4b", "v7-4b")
    wid = ws["id"]

    p_src = seed_product(db, wid, "prod_src", "SKU-SRC", "Source")
    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A",
                       group_id="grp_x", repurchase_behavior="one_time")
    p_b = seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                       group_id="grp_x")

    seed_purchase(db, wid, "cust_1", "prod_src")
    seed_purchase(db, wid, "cust_1", "prod_a")
    seed_behavior_rel(db, wid, p_src, p_b, strength=1.0)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=10.0"
    ).json()

    product_ids = [r["product_id"] for r in data]
    assert "prod_b" not in product_ids


# ---------------------------------------------------------------------------
# V7-5: Deterministic ordering with equal behavioral scores
# ---------------------------------------------------------------------------

def test_deterministic_ordering_with_equal_behavioral(client, db):
    """
    Two products with identical behavioral_score must be ordered by product PK ASC.
    """
    ws = make_workspace(client, "V7-5", "v7-5")
    wid = ws["id"]

    p_src = seed_product(db, wid, "prod_src", "SKU-SRC", "Source")
    p_f = seed_product(db, wid, "prod_f", "SKU-F", "Product F",
                       attributes=[("type", "running")])
    p_g = seed_product(db, wid, "prod_g", "SKU-G", "Product G",
                       attributes=[("type", "running")])

    seed_purchase(db, wid, "cust_1", "prod_src")
    seed_behavior_rel(db, wid, p_src, p_f, strength=0.7)
    seed_behavior_rel(db, wid, p_src, p_g, strength=0.7)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=1.0"
    ).json()

    assert len(data) == 2
    assert data[0]["behavioral_score"] == pytest.approx(data[1]["behavioral_score"])
    # Lower PK (inserted first) wins tie
    assert data[0]["product_id"] == "prod_f"
    assert data[1]["product_id"] == "prod_g"


# ---------------------------------------------------------------------------
# V7-6: Default behavioral_weight=0.0 preserves previous behavior exactly
# ---------------------------------------------------------------------------

def test_default_behavioral_weight_zero_preserves_behavior(client, db):
    """
    behavioral_weight defaults to 0.0.
    Even with a huge behavioral_score, recommendation_score must equal direct_score.
    """
    ws = make_workspace(client, "V7-6", "v7-6")
    wid = ws["id"]

    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    p_b = seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                       attributes=[("type", "cycling")])

    seed_affinity(db, wid, "cust_1", "type", "cycling", 0.5)
    seed_purchase(db, wid, "cust_1", "prod_a")
    seed_behavior_rel(db, wid, p_a, p_b, strength=100.0)  # enormous signal, zero weight

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    rec = data[0]
    assert rec["behavioral_score"] == pytest.approx(100.0)
    # recommendation_score unaffected by behavioral (weight=0.0)
    assert rec["recommendation_score"] == pytest.approx(rec["direct_score"])
    assert rec["recommendation_source"] == "direct"


# ---------------------------------------------------------------------------
# V7-7: Workspace isolation — behavioral rels are scoped to workspace
# ---------------------------------------------------------------------------

def test_workspace_isolation_behavioral(client, db):
    """
    Behavioral relationships from workspace 1 must not bleed into workspace 2.
    """
    ws1 = make_workspace(client, "V7-WS1", "v7-ws1")
    ws2 = make_workspace(client, "V7-WS2", "v7-ws2")

    # ws1: prod_a → prod_b with high behavioral strength
    pa1 = seed_product(db, ws1["id"], "prod_a", "SKU-A", "Product A")
    pb1 = seed_product(db, ws1["id"], "prod_b", "SKU-B", "Product B",
                       attributes=[("type", "running")])
    seed_purchase(db, ws1["id"], "cust_1", "prod_a")
    seed_behavior_rel(db, ws1["id"], pa1, pb1, strength=0.9)

    # ws2: same product_ids, no behavioral relationships; cust_1 has affinity only
    seed_product(db, ws2["id"], "prod_a", "SKU-A", "Product A")
    pb2 = seed_product(db, ws2["id"], "prod_b", "SKU-B", "Product B",
                       attributes=[("type", "running")])
    seed_affinity(db, ws2["id"], "cust_1", "type", "running", 0.3)

    data_ws2 = client.get(
        f"/workspaces/{ws2['id']}/recommendations/cust_1?behavioral_weight=1.0"
    ).json()

    rec = next((r for r in data_ws2 if r["product_id"] == "prod_b"), None)
    assert rec is not None
    # ws2 prod_b scored only via direct affinity; behavioral_score must be 0
    assert rec["behavioral_score"] == pytest.approx(0.0)
    assert rec["behavioral_matches"] == []


# ---------------------------------------------------------------------------
# V7-8: Multiple purchased products each contribute to behavioral_score
# ---------------------------------------------------------------------------

def test_multiple_sources_accumulate_behavioral_score(client, db):
    """
    Customer purchased both A and B.
    Both A→C and B→C behavioral relationships exist.
    behavioral_score for C = strength(A→C) + strength(B→C).
    """
    ws = make_workspace(client, "V7-8", "v7-8")
    wid = ws["id"]

    p_a = seed_product(db, wid, "prod_a", "SKU-A", "Product A")
    p_b = seed_product(db, wid, "prod_b", "SKU-B", "Product B")
    p_c = seed_product(db, wid, "prod_c", "SKU-C", "Product C",
                       attributes=[("type", "cardio")])

    seed_purchase(db, wid, "cust_1", "prod_a")
    seed_purchase(db, wid, "cust_1", "prod_b")
    seed_behavior_rel(db, wid, p_a, p_c, strength=0.4)
    seed_behavior_rel(db, wid, p_b, p_c, strength=0.6)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?behavioral_weight=1.0"
    ).json()

    rec = next((r for r in data if r["product_id"] == "prod_c"), None)
    assert rec is not None
    assert rec["behavioral_score"] == pytest.approx(1.0)  # 0.4 + 0.6
    assert len(rec["behavioral_matches"]) == 2
    assert rec["recommendation_score"] == pytest.approx(1.0)
