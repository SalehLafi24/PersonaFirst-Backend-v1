"""
Recommendation V5 tests: popularity-based fallback.

When the affinity + relationship pipeline returns fewer results than top_n,
popular products (ranked by SUM(quantity) across all customers in the workspace)
are appended to fill the gap. All suppression rules apply to fallback candidates.
"""
import pytest
from datetime import date, timedelta

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


def seed_purchase(
    db, workspace_id, customer_id, product_id,
    group_id=None, order_date=None, quantity=1,
):
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
# Fallback fires when main pipeline returns nothing
# ---------------------------------------------------------------------------

def test_no_affinities_fallback_returns_popular_products(client, db):
    """
    Customer with no affinity data gets fallback recommendations based on
    workspace-level purchase popularity.
    """
    ws = make_workspace(client, "V5-1", "v5-1")
    wid = ws["id"]

    seed_product(db, wid, "prod_popular", "SKU-P", "Popular Item")
    seed_product(db, wid, "prod_unpopular", "SKU-U", "Unpopular Item")

    # Another customer bought prod_popular → it has workspace popularity
    seed_purchase(db, wid, "other_cust", "prod_popular", quantity=5)

    # cust_1 has no affinities at all → main pipeline produces nothing
    # Use popularity_weight=1.0 so recommendation_score = popularity_score
    data = client.get(
        f"/workspaces/{wid}/recommendations/cust_1?popularity_weight=1.0"
    ).json()

    assert len(data) == 1
    assert data[0]["product_id"] == "prod_popular"
    assert data[0]["recommendation_source"] == "popular"
    assert data[0]["popularity_score"] == pytest.approx(5.0)
    assert data[0]["recommendation_score"] == pytest.approx(5.0)
    assert data[0]["direct_score"] == pytest.approx(0.0)
    assert data[0]["relationship_score"] == pytest.approx(0.0)
    assert data[0]["matched_attributes"] == []
    assert "Popular in this workspace" in data[0]["explanation"]


def test_no_matches_fallback_fills_with_popular(client, db):
    """
    Customer has affinities but no products in the workspace match them.
    Fallback fills the gap.
    """
    ws = make_workspace(client, "V5-2", "v5-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    # Product doesn't match any affinity
    seed_product(db, wid, "prod_popular", "SKU-P", "Trending Item",
                 attributes=[("category", "electronics")])

    # Another customer bought it → popular
    seed_purchase(db, wid, "other_cust", "prod_popular", quantity=10)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    assert data[0]["product_id"] == "prod_popular"
    assert data[0]["recommendation_source"] == "popular"


# ---------------------------------------------------------------------------
# Fallback only fills remaining slots
# ---------------------------------------------------------------------------

def test_partial_results_fallback_fills_gap(client, db):
    """
    Main pipeline returns 1 result, top_n=3. Fallback fills 2 more slots.
    Fallback items appear AFTER main results in the list.
    """
    ws = make_workspace(client, "V5-3", "v5-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    # One direct-match product
    seed_product(db, wid, "prod_direct", "SKU-D", "Yoga Mat",
                 attributes=[("category", "yoga")])

    # Two popular products (no direct affinity match)
    seed_product(db, wid, "prod_pop_a", "SKU-PA", "Popular A")
    seed_product(db, wid, "prod_pop_b", "SKU-PB", "Popular B")

    seed_purchase(db, wid, "other_cust", "prod_pop_a", quantity=8)
    seed_purchase(db, wid, "other_cust", "prod_pop_b", quantity=3)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?top_n=3").json()

    assert len(data) == 3
    # Direct recommendation comes first
    assert data[0]["product_id"] == "prod_direct"
    assert data[0]["recommendation_source"] == "direct"
    # Fallback items appended in popularity order
    assert data[1]["product_id"] == "prod_pop_a"
    assert data[1]["recommendation_source"] == "popular"
    assert data[2]["product_id"] == "prod_pop_b"
    assert data[2]["recommendation_source"] == "popular"


def test_full_main_results_no_fallback(client, db):
    """
    When main pipeline reaches top_n, fallback does not fire even if popular
    products exist.
    """
    ws = make_workspace(client, "V5-4", "v5-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    seed_product(db, wid, "prod_match", "SKU-M", "Yoga Mat",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_popular", "SKU-P", "Popular Item")

    seed_purchase(db, wid, "other_cust", "prod_popular", quantity=20)

    # top_n=1 → main pipeline fills the slot, no fallback needed
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?top_n=1").json()

    assert len(data) == 1
    assert data[0]["product_id"] == "prod_match"
    assert data[0]["recommendation_source"] == "direct"


# ---------------------------------------------------------------------------
# Suppression rules apply to fallback candidates
# ---------------------------------------------------------------------------

def test_fallback_excludes_exact_purchased_products(client, db):
    """
    A product purchased by this customer must not appear in fallback,
    even if it is the most popular product in the workspace.
    """
    ws = make_workspace(client, "V5-5", "v5-5")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-1", "Customer Bought This",
                 repurchase_behavior="one_time")

    # cust_1 bought prod_1, making it popular
    seed_purchase(db, wid, "cust_1", "prod_1", quantity=5)
    # Another customer also bought it — still popular, but suppressed for cust_1
    seed_purchase(db, wid, "other_cust", "prod_1", quantity=3)

    # cust_1 has no affinities → main pipeline empty → fallback fires
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_fallback_excludes_group_suppressed_products(client, db):
    """
    A product in a group that is group-suppressed for this customer must not
    appear in fallback.
    """
    ws = make_workspace(client, "V5-6", "v5-6")
    wid = ws["id"]

    # cust_1 bought prod_1 (one_time) from group "g1"
    seed_product(db, wid, "prod_1", "SKU-1", "Bought Product",
                 group_id="g1", repurchase_behavior="one_time")
    # prod_2 is a different product in the same suppressed group
    seed_product(db, wid, "prod_2", "SKU-2", "Same Group Popular",
                 group_id="g1")

    seed_purchase(db, wid, "cust_1", "prod_1", quantity=1)
    # Other customers bought prod_2 → it's popular, but group-suppressed for cust_1
    seed_purchase(db, wid, "other_cust", "prod_2", quantity=10)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_fallback_excludes_functionally_suppressed_products(client, db):
    """
    A product sharing the functional signature (type + activity) of a one_time
    purchase must not appear in fallback.
    """
    ws = make_workspace(client, "V5-7", "v5-7")
    wid = ws["id"]

    # cust_1 bought yoga leggings (one_time) → functional sig suppressed
    seed_product(db, wid, "prod_purchased", "SKU-P", "Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_purchased", quantity=1)

    # Different product, same functional signature — popular but functionally suppressed
    seed_product(db, wid, "prod_similar", "SKU-S", "Other Yoga Leggings",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "other_cust", "prod_similar", quantity=15)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_fallback_excludes_already_returned_products(client, db):
    """
    A product already returned by the main pipeline must not also appear
    in the fallback, even if it is popular.
    """
    ws = make_workspace(client, "V5-8", "v5-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    # Another customer bought prod_1 → it's also popular
    seed_purchase(db, wid, "other_cust", "prod_1", quantity=20)

    # top_n=5 → main returns prod_1, fallback should NOT add prod_1 again
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?top_n=5").json()

    product_ids = [r["product_id"] for r in data]
    assert product_ids.count("prod_1") == 1
    assert data[0]["recommendation_source"] == "direct"


def test_fallback_excludes_already_returned_group(client, db):
    """
    When a group is already represented by the main pipeline, fallback must not
    add another product from the same group.
    """
    ws = make_workspace(client, "V5-9", "v5-9")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    # prod_a and prod_b are in the same group; prod_a matches the affinity
    seed_product(db, wid, "prod_a", "SKU-A", "Yoga Mat Premium",
                 group_id="g_yoga", attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Yoga Mat Budget",
                 group_id="g_yoga")

    # prod_b is very popular but in the same group as prod_a
    seed_purchase(db, wid, "other_cust", "prod_b", quantity=50)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?top_n=5").json()

    product_ids = [r["product_id"] for r in data]
    # Only prod_a from the main pipeline; prod_b excluded because group already covered
    assert "prod_a" in product_ids
    assert "prod_b" not in product_ids


# ---------------------------------------------------------------------------
# Popularity ordering is deterministic
# ---------------------------------------------------------------------------

def test_fallback_ordered_by_popularity_desc(client, db):
    """
    Fallback items are returned in descending popularity order (SUM quantity).
    """
    ws = make_workspace(client, "V5-10", "v5-10")
    wid = ws["id"]

    seed_product(db, wid, "prod_a", "SKU-A", "High Popularity")
    seed_product(db, wid, "prod_b", "SKU-B", "Medium Popularity")
    seed_product(db, wid, "prod_c", "SKU-C", "Low Popularity")

    seed_purchase(db, wid, "cust_x", "prod_a", quantity=20)
    seed_purchase(db, wid, "cust_x", "prod_b", quantity=10)
    seed_purchase(db, wid, "cust_x", "prod_c", quantity=2)

    # No affinities for cust_1 → pure fallback
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 3
    assert data[0]["product_id"] == "prod_a"  # 20
    assert data[1]["product_id"] == "prod_b"  # 10
    assert data[2]["product_id"] == "prod_c"  # 2


def test_fallback_tie_break_by_product_pk_asc(client, db):
    """
    When two products have identical popularity scores, the one with the lower
    internal PK (inserted first) is returned first.
    """
    ws = make_workspace(client, "V5-11", "v5-11")
    wid = ws["id"]

    # prod_z inserted first → lower PK
    seed_product(db, wid, "prod_z", "SKU-Z", "Tied Z")
    seed_product(db, wid, "prod_a", "SKU-A", "Tied A")

    seed_purchase(db, wid, "cust_x", "prod_z", quantity=5)
    seed_purchase(db, wid, "cust_x", "prod_a", quantity=5)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 2
    assert data[0]["product_id"] == "prod_z"  # lower PK → first
    assert data[1]["product_id"] == "prod_a"


# ---------------------------------------------------------------------------
# SUM(quantity) aggregation
# ---------------------------------------------------------------------------

def test_fallback_popularity_is_sum_of_quantity(client, db):
    """
    popularity_score = SUM(quantity) across all purchases of that product
    in the workspace, across all customers.
    """
    ws = make_workspace(client, "V5-12", "v5-12")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-1", "Multi-bought Product")

    # Three different customers purchased prod_1 with varying quantities
    seed_purchase(db, wid, "cust_a", "prod_1", quantity=3)
    seed_purchase(db, wid, "cust_b", "prod_1", quantity=7)
    seed_purchase(db, wid, "cust_c", "prod_1", quantity=2)

    # cust_1 has no affinities → fallback
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    assert data[0]["popularity_score"] == pytest.approx(12.0)  # 3+7+2


def test_fallback_multiple_purchases_same_customer_sum(client, db):
    """
    Multiple purchases of the same product by the same customer are all summed.
    """
    ws = make_workspace(client, "V5-13", "v5-13")
    wid = ws["id"]

    seed_product(db, wid, "prod_1", "SKU-1", "Repeat Purchased")

    seed_purchase(db, wid, "cust_x", "prod_1", quantity=4)
    seed_purchase(db, wid, "cust_x", "prod_1", quantity=6)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    assert data[0]["popularity_score"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Direct items now expose actual popularity_score (V6 Refinement)
# ---------------------------------------------------------------------------

def test_direct_recommendation_has_zero_popularity_score(client, db):
    """
    Products returned via the main pipeline now expose their actual workspace
    popularity_score even when they also have a direct affinity match.
    With default popularity_weight=0.0 the recommendation_score is still
    determined purely by direct_score.
    """
    ws = make_workspace(client, "V5-14", "v5-14")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])

    # Also popular (purchased by others)
    seed_purchase(db, wid, "other_cust", "prod_1", quantity=100)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()

    assert len(data) == 1
    assert data[0]["recommendation_source"] == "direct"
    # popularity_score is now populated for all eligible products
    assert data[0]["popularity_score"] == pytest.approx(100.0)
    # recommendation_score unaffected because popularity_weight=0 by default
    assert data[0]["recommendation_score"] == pytest.approx(data[0]["direct_score"])


# ---------------------------------------------------------------------------
# recommendation_source field on non-fallback items
# ---------------------------------------------------------------------------

def test_recommendation_source_direct(client, db):
    ws = make_workspace(client, "V5-15", "v5-15")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "p1", "SKU-1", "Yoga Mat",
                 attributes=[("category", "yoga")])
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data[0]["recommendation_source"] == "direct"


def test_recommendation_source_relationship(client, db):
    from app.models.attribute_value_relationship import AttributeValueRelationship
    ws = make_workspace(client, "V5-16", "v5-16")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    db.add(AttributeValueRelationship(
        workspace_id=wid,
        source_attribute_id="category", source_value="yoga",
        target_attribute_id="size", target_value="large",
        relationship_type="complementary", source="manual",
        confidence=0.8, strength=0.8, lift=1.0, pair_count=1,
        status="approved",
    ))
    db.commit()
    # Product has only size=large (relationship target, not CORE direct)
    seed_product(db, wid, "p1", "SKU-1", "Large Item",
                 attributes=[("size", "large")])
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data[0]["recommendation_source"] == "relationship"


def test_recommendation_source_direct_plus_relationship(client, db):
    from app.models.attribute_value_relationship import AttributeValueRelationship
    ws = make_workspace(client, "V5-17", "v5-17")
    wid = ws["id"]
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    db.add(AttributeValueRelationship(
        workspace_id=wid,
        source_attribute_id="category", source_value="yoga",
        target_attribute_id="size", target_value="large",
        relationship_type="complementary", source="manual",
        confidence=0.8, strength=0.8, lift=1.0, pair_count=1,
        status="approved",
    ))
    db.commit()
    # Product has both category=yoga (direct) and size=large (relationship target)
    seed_product(db, wid, "p1", "SKU-1", "Large Yoga Mat",
                 attributes=[("category", "yoga"), ("size", "large")])
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data[0]["recommendation_source"] == "direct+relationship"


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------

def test_fallback_scoped_to_workspace(client, db):
    """
    Popularity from workspace A must not affect recommendations in workspace B.
    """
    ws_a = make_workspace(client, "V5-WA", "v5-wa")
    ws_b = make_workspace(client, "V5-WB", "v5-wb")

    # prod_1 in ws_a is very popular
    seed_product(db, ws_a["id"], "prod_1", "SKU-1", "WA Popular")
    seed_purchase(db, ws_a["id"], "cust_x", "prod_1", quantity=100)

    # prod_2 in ws_b has no purchases
    seed_product(db, ws_b["id"], "prod_2", "SKU-2", "WB Item")

    # cust_1 in ws_b has no affinities, no purchases → fallback fires for ws_b
    data = client.get(f"/workspaces/{ws_b['id']}/recommendations/cust_1").json()

    # ws_b has no popular products → fallback empty
    assert data == []


# ---------------------------------------------------------------------------
# Fallback respects top_n
# ---------------------------------------------------------------------------

def test_fallback_capped_by_top_n(client, db):
    """
    Fallback only fills slots up to top_n total; excess popular products are dropped.
    """
    ws = make_workspace(client, "V5-18", "v5-18")
    wid = ws["id"]

    for i in range(5):
        pid = f"prod_{i}"
        seed_product(db, wid, pid, f"SKU-{i}", f"Product {i}")
        seed_purchase(db, wid, "cust_x", pid, quantity=i + 1)

    # cust_1 has no affinities → pure fallback, top_n=3
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?top_n=3").json()

    assert len(data) == 3
    assert all(r["recommendation_source"] == "popular" for r in data)
