"""
Recommendation V4 tests: relationship expansion for complementary recommendations.

An approved attribute_value_relationship allows a product to be scored via
relationship expansion even when it has no direct affinity overlap. The contribution
formula is: affinity_score * relationship.strength.
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


def seed_purchase(db, workspace_id, customer_id, product_id, group_id=None, order_date=None):
    product = db.query(Product).filter_by(workspace_id=workspace_id, product_id=product_id).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id, product_id=product_id,
        group_id=group_id if group_id is not None else product.group_id,
        order_date=order_date or date.today(),
    ))
    db.commit()


def seed_relationship(
    db, workspace_id,
    src_attr_id, src_val,
    tgt_attr_id, tgt_val,
    strength=0.8,
    status="approved",
):
    db.add(AttributeValueRelationship(
        workspace_id=workspace_id,
        source_attribute_id=src_attr_id,
        source_value=src_val,
        target_attribute_id=tgt_attr_id,
        target_value=tgt_val,
        relationship_type="complementary",
        source="manual",
        confidence=strength,
        strength=strength,
        lift=1.0,
        pair_count=1,
        status=status,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Baseline: direct-only scoring still works with no relationships
# ---------------------------------------------------------------------------

def test_direct_only_recommendation_unaffected(client, db):
    """
    When no relationships exist, the engine behaves identically to V3.
    recommendation_score = direct_score; relationship_score = 0.
    """
    ws = make_workspace(client, "V4-1", "v4-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id=None,
                 attributes=[("category", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    rec = data[0]
    assert rec["product_id"] == "prod_1"
    assert rec["direct_score"] == pytest.approx(0.9)
    assert rec["relationship_score"] == pytest.approx(0.0)
    assert rec["recommendation_score"] == pytest.approx(0.9)
    assert rec["relationship_matches"] == []


# ---------------------------------------------------------------------------
# Relationship-only recommendation
# ---------------------------------------------------------------------------

def test_relationship_only_recommendation(client, db):
    """
    A product with no direct affinity overlap is recommended solely through
    an approved relationship: category=yoga → size=large (strength=0.8).
    contribution = affinity_score(0.9) * strength(0.8) = 0.72
    """
    ws = make_workspace(client, "V4-2", "v4-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=0.8)

    # Product has NO category=yoga — only size=large (relationship target)
    seed_product(db, wid, "prod_1", "SKU-001", "Large Yoga Accessory", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    rec = data[0]
    assert rec["product_id"] == "prod_1"
    assert rec["direct_score"] == pytest.approx(0.0)
    assert rec["relationship_score"] == pytest.approx(0.72)
    assert rec["recommendation_score"] == pytest.approx(0.72)
    assert len(rec["relationship_matches"]) == 1
    rm = rec["relationship_matches"][0]
    assert rm["source_attribute_id"] == "category"
    assert rm["source_attribute_value"] == "yoga"
    assert rm["target_attribute_id"] == "size"
    assert rm["target_attribute_value"] == "large"
    assert rm["source_score"] == pytest.approx(0.9)
    assert rm["relationship_strength"] == pytest.approx(0.8)
    assert rm["contribution"] == pytest.approx(0.72)


def test_relationship_only_passes_meaningfulness_gate(client, db):
    """
    relationship_score > 0 is sufficient to pass the meaningfulness gate even
    with no CORE direct match and recommendation_role=same_use_case.
    """
    ws = make_workspace(client, "V4-3", "v4-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "brand", "Nike", strength=0.6)

    # Product only has brand=Nike (DESCRIPTIVE) — normally blocked by gate;
    # but relationship_score > 0 lifts the gate.
    seed_product(db, wid, "prod_1", "SKU-001", "Nike Product", group_id=None,
                 attributes=[("brand", "Nike")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"
    assert data[0]["relationship_score"] == pytest.approx(0.54)


# ---------------------------------------------------------------------------
# Combined direct + relationship scoring
# ---------------------------------------------------------------------------

def test_direct_and_relationship_scores_combine(client, db):
    """
    A product that matches an affinity directly AND has a relationship-expanded
    attribute earns direct_score + relationship_score as recommendation_score.
    """
    ws = make_workspace(client, "V4-4", "v4-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.8)
    seed_relationship(db, wid, "category", "yoga", "activity", "outdoor", strength=0.5)

    # Product has category=yoga (direct, CORE, weight=1.0) AND activity=outdoor (rel target)
    seed_product(db, wid, "prod_1", "SKU-001", "Outdoor Yoga Mat", group_id=None,
                 attributes=[("category", "yoga"), ("activity", "outdoor")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    rec = data[0]
    # direct: category=yoga → 0.8 * 1.0 = 0.8
    # relationship: category=yoga → activity=outdoor → 0.8 * 0.5 = 0.4
    assert rec["direct_score"] == pytest.approx(0.8)
    assert rec["relationship_score"] == pytest.approx(0.4)
    assert rec["recommendation_score"] == pytest.approx(1.2)


def test_multiple_relationships_sum(client, db):
    """
    Multiple approved relationships from different affinities all contribute
    to relationship_score.
    """
    ws = make_workspace(client, "V4-5", "v4-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.8)
    seed_affinity(db, wid, "cust_1", "activity", "outdoor", 0.6)
    # Two relationships both point to size=large as target
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=0.5)
    seed_relationship(db, wid, "activity", "outdoor", "size", "large", strength=0.4)

    seed_product(db, wid, "prod_1", "SKU-001", "Large Mat", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    rec = data[0]
    # yoga→size: 0.8 * 0.5 = 0.4; outdoor→size: 0.6 * 0.4 = 0.24; total = 0.64
    assert rec["relationship_score"] == pytest.approx(0.64)
    assert len(rec["relationship_matches"]) == 2


# ---------------------------------------------------------------------------
# Relationship-expanded candidates still obey suppression rules
# ---------------------------------------------------------------------------

def test_suppression_blocks_relationship_expanded_candidate(client, db):
    """
    A product surfaced only via relationship expansion is still suppressed when
    it belongs to a one_time-purchased group.
    """
    ws = make_workspace(client, "V4-6", "v4-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=0.8)

    # Candidate is in a suppressed group (customer bought something from it — one_time)
    purchased = seed_product(db, wid, "prod_bought", "SKU-B", "Bought Product",
                             group_id="g_suppressed", repurchase_behavior="one_time",
                             attributes=[("category", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_bought", group_id="g_suppressed")

    # Another product in the same suppressed group, but relationship-expanded
    seed_product(db, wid, "prod_rel", "SKU-R", "Related Large Product",
                 group_id="g_suppressed",
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_bought: exact suppression; prod_rel: group suppression; both gone
    assert data == []


def test_functional_suppression_blocks_relationship_expanded_candidate(client, db):
    """
    Functional suppression applies to relationship-expanded candidates that share
    the same functional signature as a one_time purchase.
    """
    ws = make_workspace(client, "V4-7", "v4-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "color", "red", strength=0.8)

    # Purchased: one_time yoga leggings
    seed_product(db, wid, "prod_purchased", "SKU-P", "Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_purchased")

    # Candidate: same functional signature (leggings + yoga), color=red added via relationship
    seed_product(db, wid, "prod_rel", "SKU-R", "Red Yoga Leggings",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_rel shares functional sig → suppressed despite relationship score
    assert data == []


def test_exact_product_suppression_blocks_relationship_expanded(client, db):
    """
    Exact product suppression (previously purchased) blocks a product even if
    it would otherwise score via relationship expansion.
    """
    ws = make_workspace(client, "V4-8", "v4-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=0.8)

    seed_product(db, wid, "prod_1", "SKU-001", "Large Yoga Item",
                 repurchase_behavior="one_time",
                 attributes=[("size", "large")])
    seed_purchase(db, wid, "cust_1", "prod_1")

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


# ---------------------------------------------------------------------------
# Inactive relationships do not contribute
# ---------------------------------------------------------------------------

def test_suggested_relationship_does_not_contribute(client, db):
    """Relationships with status=suggested are ignored (not approved)."""
    ws = make_workspace(client, "V4-9", "v4-9")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large",
                      strength=0.8, status="suggested")

    seed_product(db, wid, "prod_1", "SKU-001", "Large Product", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # No direct match, relationship not approved → product not returned
    assert data == []


def test_rejected_relationship_does_not_contribute(client, db):
    """Relationships with status=rejected are ignored."""
    ws = make_workspace(client, "V4-10", "v4-10")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large",
                      strength=0.8, status="rejected")

    seed_product(db, wid, "prod_1", "SKU-001", "Large Product", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_approved_vs_unapproved_relationship_selectivity(client, db):
    """
    Two products: one reachable only via an approved relationship, one only via
    a suggested (inactive) relationship. Only the approved one appears.
    """
    ws = make_workspace(client, "V4-11", "v4-11")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large",
                      strength=0.8, status="approved")
    seed_relationship(db, wid, "category", "yoga", "color", "red",
                      strength=0.8, status="suggested")

    seed_product(db, wid, "prod_large", "SKU-L", "Large Product", group_id=None,
                 attributes=[("size", "large")])
    seed_product(db, wid, "prod_red", "SKU-R", "Red Product", group_id=None,
                 attributes=[("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_large"


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------

def test_relationship_scoped_to_workspace(client, db):
    """
    An approved relationship in workspace A must not expand recommendations
    for a customer in workspace B.
    """
    ws_a = make_workspace(client, "V4-WA", "v4-wa")
    ws_b = make_workspace(client, "V4-WB", "v4-wb")

    # Relationship only in workspace A
    seed_affinity(db, ws_a["id"], "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, ws_a["id"], "category", "yoga", "size", "large",
                      strength=0.8, status="approved")

    # Product only in workspace B (same attrs)
    seed_affinity(db, ws_b["id"], "cust_1", "category", "yoga", 0.9)
    seed_product(db, ws_b["id"], "prod_1", "SKU-001", "Large Product", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{ws_b['id']}/recommendations/cust_1").json()
    # Workspace B has no approved relationships → product not returned
    assert data == []


# ---------------------------------------------------------------------------
# Determinism: same score → PK ascending tie-break
# ---------------------------------------------------------------------------

def test_relationship_expanded_tie_break_deterministic(client, db):
    """
    Two products with identical relationship_scores and no direct match are
    returned in internal PK ascending order.
    """
    ws = make_workspace(client, "V4-12", "v4-12")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=0.8)

    # prod_z inserted first → lower PK
    seed_product(db, wid, "prod_z", "SKU-Z", "Z Large Product", group_id=None,
                 attributes=[("size", "large")])
    seed_product(db, wid, "prod_a", "SKU-A", "A Large Product", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 2
    assert data[0]["product_id"] == "prod_z"   # lower PK → first
    assert data[1]["product_id"] == "prod_a"


# ---------------------------------------------------------------------------
# Scoring precision
# ---------------------------------------------------------------------------

def test_relationship_score_rounds_to_6_decimal_places(client, db):
    """relationship_score rounds to 6 decimal places."""
    ws = make_workspace(client, "V4-13", "v4-13")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 1 / 3)
    seed_relationship(db, wid, "category", "yoga", "size", "large", strength=1 / 3)

    seed_product(db, wid, "prod_1", "SKU-001", "Product", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    # (1/3) * (1/3) = 1/9 ≈ 0.111111
    assert data[0]["relationship_score"] == pytest.approx(1 / 9, rel=1e-5)


# ---------------------------------------------------------------------------
# min_score filters affinities used for relationship expansion
# ---------------------------------------------------------------------------

def test_min_score_also_filters_relationship_source_affinities(client, db):
    """
    Affinities below min_score are excluded from the affinity map and therefore
    cannot trigger relationship expansions.
    """
    ws = make_workspace(client, "V4-14", "v4-14")
    wid = ws["id"]

    # Low-score affinity — filtered out when min_score=0.5
    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.3)
    seed_relationship(db, wid, "category", "yoga", "size", "large",
                      strength=0.9, status="approved")

    seed_product(db, wid, "prod_1", "SKU-001", "Large Product", group_id=None,
                 attributes=[("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?min_score=0.5").json()
    # Affinity filtered → no relationship expansion → product not returned
    assert data == []
