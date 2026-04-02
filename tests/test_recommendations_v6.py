"""
Recommendation V6 tests: slot strategy (weighted source blending).

Weights control how direct_score, relationship_score, and popularity_score
contribute to the final recommendation_score used for ranking:

    final_score = direct_score * direct_weight
                + relationship_score * relationship_weight
                + popularity_score * popularity_weight

Defaults: direct=1.0, relationship=1.0, popularity=0.0
All-zero weights → defaults applied silently.
"""
import pytest
from datetime import date

from app.models.attribute_value_relationship import AttributeValueRelationship
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
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
                 repurchase_behavior=None, recommendation_role="same_use_case", attributes=None):
    p = Product(
        workspace_id=workspace_id, product_id=product_id, sku=sku, name=name,
        group_id=group_id, repurchase_behavior=repurchase_behavior,
        recommendation_role=recommendation_role,
    )
    db.add(p)
    db.flush()
    for attr_id, attr_val in (attributes or []):
        db.add(ProductAttribute(product_id=p.id, attribute_id=attr_id, attribute_value=attr_val))
    db.commit()
    return p


def seed_purchase(db, workspace_id, customer_id, product_id, quantity=1, order_date=None):
    product = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=product_id
    ).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id, product_id=product_id,
        group_id=product.group_id,
        order_date=order_date or date.today(),
        quantity=quantity,
    ))
    db.commit()


def seed_relationship(db, workspace_id, src_attr, src_val, tgt_attr, tgt_val,
                      strength=0.8, status="approved"):
    db.add(AttributeValueRelationship(
        workspace_id=workspace_id,
        source_attribute_id=src_attr, source_value=src_val,
        target_attribute_id=tgt_attr, target_value=tgt_val,
        relationship_type="complementary", source="manual",
        confidence=strength, strength=strength, lift=1.0, pair_count=1,
        status=status,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# 1. Default weights preserve pre-V6 behavior
# ---------------------------------------------------------------------------

def test_default_weights_preserve_behavior(client, db):
    """
    With no weight parameters, the engine behaves identically to V5:
    recommendation_score = direct_score + relationship_score, popular items
    only appear if there are fewer direct/relationship results than top_n.
    """
    ws = make_workspace(client, "V6-1", "v6-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "outdoor", 0.6)

    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga"), ("activity", "outdoor")])

    # Direct result: 0.9*1.0 + 0.6*1.0 = 1.5 (both CORE weight=1.0)
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    assert data[0]["product_id"] == "prod_direct"
    assert data[0]["recommendation_score"] == pytest.approx(1.5)
    assert data[0]["recommendation_source"] == "direct"
    assert data[0]["popularity_score"] == pytest.approx(0.0)


def test_default_weights_direct_score_unchanged(client, db):
    """direct_score component is unchanged — only weighting changes final rank."""
    ws = make_workspace(client, "V6-2", "v6-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.8)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    rec = data[0]
    assert rec["direct_score"] == pytest.approx(0.8)
    assert rec["relationship_score"] == pytest.approx(0.0)
    assert rec["popularity_score"] == pytest.approx(0.0)
    # recommendation_score = 0.8 * 1.0 + 0.0 * 1.0 + 0.0 * 0.0 = 0.8
    assert rec["recommendation_score"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 2. Popularity weight moves popular items up
# ---------------------------------------------------------------------------

def test_popularity_weight_affects_ranking(client, db):
    """
    With popularity_weight=0 (default), a weak direct match outranks a popular item.
    With popularity_weight=1.0, the popular item overtakes the weak match.
    """
    ws = make_workspace(client, "V6-3", "v6-3")
    wid = ws["id"]

    # Weak direct match: direct_score = 0.1
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.1)
    seed_product(db, wid, "prod_direct", "SKU-D", "Weak Yoga Match",
                 attributes=[("category", "yoga")])

    # Popular item: pop_score = 50, no direct affinity
    seed_product(db, wid, "prod_popular", "SKU-P", "Very Popular Item")
    seed_purchase(db, wid, "other_cust", "prod_popular", quantity=50)

    # Default weights → direct match wins (0.1 > 0.0)
    data_default = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data_default[0]["product_id"] == "prod_direct"

    # popularity_weight=1.0 → popular item wins (50.0 > 0.1)
    data_weighted = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=1.0"
    ).json()
    assert data_weighted[0]["product_id"] == "prod_popular"
    assert data_weighted[0]["recommendation_score"] == pytest.approx(50.0)


def test_popularity_weight_zero_keeps_popular_items_last(client, db):
    """With popularity_weight=0.0, popular-only items score 0 and are excluded."""
    ws = make_workspace(client, "V6-4", "v6-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.01)
    seed_product(db, wid, "prod_direct", "SKU-D", "Direct Match",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_pop", "SKU-P", "Popular")
    seed_purchase(db, wid, "other_cust", "prod_pop", quantity=1000)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=0.0"
    ).json()
    # Direct item scores 0.01; popular item scores 0 → filtered out
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_direct"


# ---------------------------------------------------------------------------
# 3. Direct weight dominates when set high
# ---------------------------------------------------------------------------

def test_direct_weight_dominates_when_high(client, db):
    """
    High direct_weight keeps direct matches at the top even when popularity
    is non-zero.
    """
    ws = make_workspace(client, "V6-5", "v6-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga")])

    seed_product(db, wid, "prod_pop", "SKU-P", "Popular Item")
    seed_purchase(db, wid, "other_cust", "prod_pop", quantity=100)

    # direct_weight=10.0 → direct item: 0.9 * 10 = 9.0
    # popularity_weight=1.0 → popular item: 100 * 1.0 = 100.0 ... still wins here!
    # Let's use direct_weight=200.0 to be safe:
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?direct_weight=200.0&popularity_weight=1.0"
    ).json()
    # prod_direct: 0.9 * 200 = 180.0; prod_pop: 100 * 1.0 = 100.0 → direct wins
    assert data[0]["product_id"] == "prod_direct"
    assert data[0]["recommendation_score"] == pytest.approx(180.0)
    assert data[1]["product_id"] == "prod_pop"


def test_direct_weight_zero_removes_direct_only_items(client, db):
    """
    direct_weight=0 means direct-only items score 0 and are excluded from results.
    A popular item with popularity_weight>0 outranks and is the only result.
    """
    ws = make_workspace(client, "V6-6", "v6-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga")])

    seed_product(db, wid, "prod_pop", "SKU-P", "Popular Item")
    seed_purchase(db, wid, "other_cust", "prod_pop", quantity=5)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?direct_weight=0.0&popularity_weight=1.0"
    ).json()
    # prod_direct: 0.9 * 0 = 0 → filtered out; prod_pop: 5 * 1.0 = 5.0 → only result
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_pop"
    assert data[0]["recommendation_score"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 4. All-zero weights fall back to defaults
# ---------------------------------------------------------------------------

def test_zero_weights_fallback_to_defaults(client, db):
    """
    Passing all weights as 0 is treated as 'use defaults' (not as 'score everything 0').
    """
    ws = make_workspace(client, "V6-7", "v6-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    # All-zero → defaults applied → direct_score * 1.0 = 0.9
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=0.0&relationship_weight=0.0&popularity_weight=0.0"
    ).json()

    assert len(data) == 1
    assert data[0]["recommendation_score"] == pytest.approx(0.9)


def test_zero_weights_same_as_no_weights(client, db):
    """All-zero weights and no weights produce identical results."""
    ws = make_workspace(client, "V6-8", "v6-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.75)
    seed_affinity(db, wid, "cust_1", "activity", "outdoor", 0.5)
    seed_product(db, wid, "prod_1", "SKU-1", "Outdoor Yoga Mat",
                 attributes=[("category", "yoga"), ("activity", "outdoor")])

    no_weights = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
    ).json()
    zero_weights = client.get(
        f"/workspaces/{wid}/recommendations/cust_1"
        "?direct_weight=0.0&relationship_weight=0.0&popularity_weight=0.0"
    ).json()

    assert no_weights[0]["recommendation_score"] == pytest.approx(
        zero_weights[0]["recommendation_score"]
    )
    assert no_weights[0]["product_id"] == zero_weights[0]["product_id"]


# ---------------------------------------------------------------------------
# 5. Weighted ranking is deterministic
# ---------------------------------------------------------------------------

def test_weighted_ranking_deterministic(client, db):
    """Same weights + same data → same result every call."""
    ws = make_workspace(client, "V6-9", "v6-9")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "A", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "B", attributes=[("category", "yoga")])
    seed_purchase(db, wid, "other", "prod_b", quantity=10)

    url = f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=0.5"
    r1 = client.get(url).json()
    r2 = client.get(url).json()
    r3 = client.get(url).json()

    assert [r["product_id"] for r in r1] == [r["product_id"] for r in r2]
    assert [r["product_id"] for r in r1] == [r["product_id"] for r in r3]


# ---------------------------------------------------------------------------
# 6. Fallback items can compete in unified ranking
# ---------------------------------------------------------------------------

def test_fallback_items_can_compete_in_ranking(client, db):
    """
    With a high enough popularity_weight, a popular fallback item outranks
    a weak direct match in the unified ranking.
    """
    ws = make_workspace(client, "V6-10", "v6-10")
    wid = ws["id"]

    # Very weak direct match
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.05)
    seed_product(db, wid, "prod_weak", "SKU-W", "Weak Match",
                 attributes=[("category", "yoga")])

    # Very popular item
    seed_product(db, wid, "prod_popular", "SKU-P", "Popular Item")
    seed_purchase(db, wid, "other_cust", "prod_popular", quantity=100)

    # Default (popularity_weight=0): prod_weak wins (0.05 > 0)
    data_default = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data_default[0]["product_id"] == "prod_weak"

    # popularity_weight=1.0: prod_popular wins (100 > 0.05)
    data_boosted = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=1.0"
    ).json()
    assert data_boosted[0]["product_id"] == "prod_popular"
    assert data_boosted[0]["recommendation_source"] == "popular"
    assert data_boosted[1]["product_id"] == "prod_weak"
    assert data_boosted[1]["recommendation_source"] == "direct"


def test_same_group_both_returned_ranked_by_weight(client, db):
    """
    Two products in the same group with different signal types.
    Without diversity, both are returned ranked by weighted score.
    """
    ws = make_workspace(client, "V6-11", "v6-11")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.05)

    # prod_a: weak direct match in group "g1"
    seed_product(db, wid, "prod_a", "SKU-A", "Weak Yoga A",
                 group_id="g1", attributes=[("category", "yoga")])

    # prod_b: very popular, same group "g1", no direct affinity
    seed_product(db, wid, "prod_b", "SKU-B", "Popular B", group_id="g1")
    seed_purchase(db, wid, "other_cust", "prod_b", quantity=200)

    # Default (popularity_weight=0): only prod_a scores > 0 (0.05), prod_b has pop but weight 0
    data_default = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data_default) == 1
    assert data_default[0]["product_id"] == "prod_a"

    # popularity_weight=1.0: both score > 0, prod_b wins on score (200 vs 0.05)
    data_boosted = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=1.0"
    ).json()
    assert len(data_boosted) == 2
    assert data_boosted[0]["product_id"] == "prod_b"
    assert data_boosted[0]["recommendation_source"] == "popular"
    assert data_boosted[1]["product_id"] == "prod_a"


# ---------------------------------------------------------------------------
# Score field verification for each source type under explicit weights
# ---------------------------------------------------------------------------

def test_weighted_recommendation_score_formula_direct(client, db):
    """recommendation_score = direct_score * direct_weight when no other components."""
    ws = make_workspace(client, "V6-12", "v6-12")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.8)
    seed_product(db, wid, "p1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?direct_weight=2.5"
    ).json()
    assert data[0]["direct_score"] == pytest.approx(0.8)
    assert data[0]["recommendation_score"] == pytest.approx(0.8 * 2.5)


def test_weighted_recommendation_score_formula_popular(client, db):
    """recommendation_score = popularity_score * popularity_weight for fallback items."""
    ws = make_workspace(client, "V6-13", "v6-13")
    wid = ws["id"]
    seed_product(db, wid, "p1", "SKU-1", "Popular Item")
    seed_purchase(db, wid, "cust_x", "p1", quantity=7)
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=3.0"
    ).json()
    assert data[0]["popularity_score"] == pytest.approx(7.0)
    assert data[0]["recommendation_score"] == pytest.approx(7.0 * 3.0)


def test_weighted_recommendation_score_formula_combined(client, db):
    """All three components combine correctly in recommendation_score."""
    ws = make_workspace(client, "V6-14", "v6-14")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.8)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=0.5)

    # Product has direct match (category=yoga) + relationship target (size=large)
    seed_product(db, wid, "p1", "SKU-1", "Large Yoga Mat",
                 attributes=[("category", "yoga"), ("size", "large")])

    # direct_score = 0.8 * 1.0 (CORE) = 0.8
    # relationship_score = 0.8 * 0.5 (strength) = 0.4
    # popularity_score = 0.0 (no purchases)
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?direct_weight=2.0&relationship_weight=3.0"
    ).json()
    rec = data[0]
    assert rec["direct_score"] == pytest.approx(0.8)
    assert rec["relationship_score"] == pytest.approx(0.4)
    assert rec["popularity_score"] == pytest.approx(0.0)
    # final = 0.8 * 2.0 + 0.4 * 3.0 + 0.0 = 1.6 + 1.2 = 2.8
    assert rec["recommendation_score"] == pytest.approx(2.8)


# ---------------------------------------------------------------------------
# Suppression unaffected by weights
# ---------------------------------------------------------------------------

def test_suppression_respected_regardless_of_weights(client, db):
    """
    Even with very high popularity_weight, suppressed products never appear.
    """
    ws = make_workspace(client, "V6-15", "v6-15")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-1", "One-time Product",
                 repurchase_behavior="one_time")
    # cust_1 bought prod_1 (exact suppression + group suppression + popular)
    seed_purchase(db, wid, "cust_1", "prod_1", quantity=100)
    # Other customer also bought it → very popular
    seed_purchase(db, wid, "other_cust", "prod_1", quantity=100)

    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=999.0"
    ).json()
    assert data == []


# ---------------------------------------------------------------------------
# relationship_weight
# ---------------------------------------------------------------------------

def test_relationship_weight_scales_contribution(client, db):
    """Halving relationship_weight halves relationship_score's impact on final score."""
    ws = make_workspace(client, "V6-16", "v6-16")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 1.0)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=1.0)

    # Product: only relationship target (no direct match)
    seed_product(db, wid, "p1", "SKU-1", "Large Item",
                 attributes=[("size", "large")])

    # relationship_weight=1.0 → recommendation_score = 1.0 * 1.0 * 1.0 = 1.0
    data_full = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?relationship_weight=1.0"
    ).json()
    assert data_full[0]["recommendation_score"] == pytest.approx(1.0)

    # relationship_weight=0.5 → recommendation_score = 1.0 * 1.0 * 0.5 = 0.5
    data_half = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?relationship_weight=0.5"
    ).json()
    assert data_half[0]["recommendation_score"] == pytest.approx(0.5)
