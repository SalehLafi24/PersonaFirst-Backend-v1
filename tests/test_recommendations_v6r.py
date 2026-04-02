"""
Recommendation V6 Refinement tests: popularity as a first-class signal.

In V6r every eligible product receives its actual workspace-wide popularity_score
(SUM of quantities across all customers). The weighted formula applies to ALL
candidates in a single unified pool. Phase 1 / Phase 2 split is removed.

Key properties verified:
- Direct products expose their real popularity_score (not 0)
- popularity_weight boosts recommendation_score for already-matched products
- A direct+popular product outranks a direct-only product when popularity_weight > 0
- Suppression still blocks popular products regardless of weights
- Default weights leave recommendation_score == direct_score (popularity_weight=0)
- Deterministic tie-break by product PK ASC
- Group dedup picks the best weighted winner across the unified pool
"""
import pytest
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute


# ---------------------------------------------------------------------------
# Helpers (mirrors helpers in other test files)
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


# ---------------------------------------------------------------------------
# V6r-1: Direct product gets non-zero popularity_score
# ---------------------------------------------------------------------------

def test_direct_product_exposes_popularity_score(client, db):
    """
    A product that matches via direct affinity AND has workspace purchases
    must report the actual SUM(quantity) as popularity_score, not 0.
    """
    ws = make_workspace(client, "V6R-1", "v6r-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "running", 0.8)
    seed_product(db, wid, "prod_run", "SKU-R", "Running Shoes",
                 attributes=[("category", "running")])

    # Popular among other customers
    seed_purchase(db, wid, "other_a", "prod_run", quantity=30)
    seed_purchase(db, wid, "other_b", "prod_run", quantity=20)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    rec = data[0]
    assert rec["recommendation_source"] == "direct"
    assert rec["popularity_score"] == pytest.approx(50.0)
    # Default popularity_weight=0 → recommendation_score is driven by direct only
    assert rec["recommendation_score"] == pytest.approx(rec["direct_score"])


# ---------------------------------------------------------------------------
# V6r-2: popularity_weight boosts recommendation_score for matched product
# ---------------------------------------------------------------------------

def test_popularity_weight_boosts_recommendation_score(client, db):
    """
    When popularity_weight > 0, popularity_score contributes to
    recommendation_score even for products that already have a direct match.
    """
    ws = make_workspace(client, "V6R-2", "v6r-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.5)
    seed_product(db, wid, "prod_yoga", "SKU-Y", "Yoga Mat",
                 attributes=[("category", "yoga")])

    seed_purchase(db, wid, "other_cust", "prod_yoga", quantity=10)

    # direct_score = 0.5 * 1.0 (category weight) = 0.5
    # popularity_score = 10.0
    # With direct_weight=1, popularity_weight=0.1:
    # recommendation_score = 0.5*1 + 10.0*0.1 = 0.5 + 1.0 = 1.5
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=1.0&relationship_weight=1.0&popularity_weight=0.1"
    ).json()

    assert len(data) == 1
    rec = data[0]
    assert rec["popularity_score"] == pytest.approx(10.0)
    assert rec["recommendation_score"] == pytest.approx(0.5 + 10.0 * 0.1)


# ---------------------------------------------------------------------------
# V6r-3: direct+popular outranks direct-only when popularity_weight > 0
# ---------------------------------------------------------------------------

def test_popular_direct_outranks_direct_only_with_weight(client, db):
    """
    Two products both match directly. The popular one should rank higher
    when popularity_weight is enabled.
    """
    ws = make_workspace(client, "V6R-3", "v6r-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "cycling", 0.6)

    # Both products have the same direct affinity
    p_pop = seed_product(db, wid, "prod_pop", "SKU-P", "Popular Bike Helmet",
                         attributes=[("category", "cycling")])
    p_plain = seed_product(db, wid, "prod_plain", "SKU-Q", "Plain Bike Helmet",
                           attributes=[("category", "cycling")])

    # Only prod_pop is popular
    seed_purchase(db, wid, "other_cust", "prod_pop", quantity=200)

    # Without popularity_weight both have same direct_score — plain wins by PK (lower id)
    data_no_pop = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=0.0"
    ).json()
    assert data_no_pop[0]["product_id"] == "prod_pop"  # lower PK wins tie
    assert data_no_pop[1]["product_id"] == "prod_plain"

    # With popularity_weight, prod_pop should rank first unambiguously
    data_with_pop = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=1.0&relationship_weight=1.0&popularity_weight=1.0"
    ).json()
    assert data_with_pop[0]["product_id"] == "prod_pop"
    assert data_with_pop[0]["popularity_score"] == pytest.approx(200.0)
    assert data_with_pop[0]["recommendation_score"] > data_with_pop[1]["recommendation_score"]


# ---------------------------------------------------------------------------
# V6r-4: suppression still blocks popular products regardless of weight
# ---------------------------------------------------------------------------

def test_suppressed_product_excluded_even_if_popular(client, db):
    """
    A product already purchased by cust_1 (one_time behavior) must never
    appear in recommendations, no matter how high popularity_weight is.
    """
    ws = make_workspace(client, "V6R-4", "v6r-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "swimming", 0.9)
    seed_product(db, wid, "prod_swim", "SKU-S", "Swim Goggles",
                 repurchase_behavior="one_time",
                 attributes=[("category", "swimming")])

    # cust_1 already owns it
    seed_purchase(db, wid, "cust_1", "prod_swim", quantity=1)
    # Also very popular with others
    seed_purchase(db, wid, "other_a", "prod_swim", quantity=500)
    seed_purchase(db, wid, "other_b", "prod_swim", quantity=300)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=1.0&relationship_weight=1.0&popularity_weight=10.0"
    ).json()

    product_ids = [r["product_id"] for r in data]
    assert "prod_swim" not in product_ids


# ---------------------------------------------------------------------------
# V6r-5: default weights — popularity_score populated but doesn't affect rank
# ---------------------------------------------------------------------------

def test_default_weights_popularity_score_populated_not_ranked(client, db):
    """
    With default weights (popularity_weight=0), popularity_score is set on all
    products, but recommendation_score equals direct_score exactly.
    """
    ws = make_workspace(client, "V6R-5", "v6r-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "cardio", 0.7)
    seed_product(db, wid, "prod_c", "SKU-C", "Cardio Band",
                 attributes=[("type", "cardio")])

    seed_purchase(db, wid, "other_cust", "prod_c", quantity=999)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    rec = data[0]
    # popularity_score is populated
    assert rec["popularity_score"] == pytest.approx(999.0)
    # but recommendation_score is unaffected by it
    assert rec["recommendation_score"] == pytest.approx(rec["direct_score"])
    assert rec["recommendation_source"] == "direct"


# ---------------------------------------------------------------------------
# V6r-6: deterministic tie-break when popularity boosts two products equally
# ---------------------------------------------------------------------------

def test_deterministic_tiebreak_with_equal_popularity_boost(client, db):
    """
    Two products with identical direct_score and identical popularity_score
    must be ordered deterministically by product PK ASC.
    """
    ws = make_workspace(client, "V6R-6", "v6r-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "strength", 0.5)

    p1 = seed_product(db, wid, "prod_str_a", "SKU-A", "Strength Band A",
                      attributes=[("category", "strength")])
    p2 = seed_product(db, wid, "prod_str_b", "SKU-B", "Strength Band B",
                      attributes=[("category", "strength")])

    # Equal popularity
    seed_purchase(db, wid, "other_cust", "prod_str_a", quantity=50)
    seed_purchase(db, wid, "other_cust", "prod_str_b", quantity=50)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=1.0&relationship_weight=1.0&popularity_weight=1.0"
    ).json()

    assert len(data) == 2
    assert data[0]["recommendation_score"] == pytest.approx(data[1]["recommendation_score"])
    # Lower PK (p1, inserted first) must come first
    assert data[0]["product_id"] == "prod_str_a"
    assert data[1]["product_id"] == "prod_str_b"


# ---------------------------------------------------------------------------
# V6r-7: group dedup picks popular winner when popularity_weight is high
# ---------------------------------------------------------------------------

def test_same_group_both_returned_ranked_by_weight(client, db):
    """
    Two products in the same group: one has a direct match, the other is
    more popular. Without diversity, both are returned ranked by weighted score.
    """
    ws = make_workspace(client, "V6R-7", "v6r-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "pilates", 0.4)

    # Direct-match member of the group
    seed_product(db, wid, "prod_g_direct", "SKU-GD", "Pilates Ring",
                 group_id="grp_pilates",
                 attributes=[("category", "pilates")])

    # Popular-only member — replace attribute with brand=generic (no affinity match)
    seed_product(db, wid, "prod_g_pop", "SKU-GP", "Pilates Ball",
                 group_id="grp_pilates",
                 attributes=[("category", "pilates")])
    db.query(ProductAttribute).filter_by(
        product_id=db.query(Product).filter_by(
            workspace_id=wid, product_id="prod_g_pop"
        ).first().id
    ).delete()
    db.commit()
    prod_g_pop_obj = db.query(Product).filter_by(
        workspace_id=wid, product_id="prod_g_pop"
    ).first()
    db.add(ProductAttribute(
        product_id=prod_g_pop_obj.id,
        attribute_id="brand",
        attribute_value="generic",
    ))
    db.commit()

    seed_purchase(db, wid, "other_a", "prod_g_pop", quantity=500)
    seed_purchase(db, wid, "other_b", "prod_g_direct", quantity=1)

    # popularity_weight=0: only prod_g_direct scores > 0 (direct=0.4)
    data_direct_only = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=0.0"
    ).json()
    group_results = [r for r in data_direct_only if r["group_id"] == "grp_pilates"]
    assert len(group_results) == 1
    assert group_results[0]["product_id"] == "prod_g_direct"

    # popularity_weight=0.01: both score > 0; pop product ranks first
    # prod_g_direct: 0.4*1 + 1*0.01 = 0.41
    # prod_g_pop:    0 + 500*0.01 = 5.0
    data_both = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=1.0&relationship_weight=1.0&popularity_weight=0.01"
    ).json()
    group_results_both = [r for r in data_both if r["group_id"] == "grp_pilates"]
    assert len(group_results_both) == 2
    assert group_results_both[0]["product_id"] == "prod_g_pop"
    assert group_results_both[0]["recommendation_source"] == "popular"
    assert group_results_both[0]["popularity_score"] == pytest.approx(500.0)
    assert group_results_both[1]["product_id"] == "prod_g_direct"
