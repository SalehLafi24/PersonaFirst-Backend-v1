"""
Recommendation V3 tests: attribute weighting, meaningfulness gate, recommendation_role,
and complementary product behaviour.
"""
import pytest

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
    from datetime import date
    product = db.query(Product).filter_by(workspace_id=workspace_id, product_id=product_id).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id,
        product_id=product_id,
        group_id=group_id if group_id is not None else product.group_id,
        order_date=order_date or date.today(),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Meaningfulness gate: descriptive-only match is excluded
# ---------------------------------------------------------------------------

def test_descriptive_only_match_filtered_out(client, db):
    """
    A product matching only a DESCRIPTIVE attribute (color) with no CORE match
    must not appear in recommendations.
    """
    ws = make_workspace(client, "V3-1", "v3-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "color", "red", 0.9)
    # No CORE affinity at all — any product with only color=red would fail gate
    seed_product(db, wid, "prod_1", "SKU-001", "Red Shirt", group_id=None,
                 attributes=[("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_core_attribute_match_passes_gate(client, db):
    """
    A product with at least one CORE attribute match is surfaced.
    """
    ws = make_workspace(client, "V3-2", "v3-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.7)
    seed_product(db, wid, "prod_1", "SKU-001", "Red Yoga Mat", group_id=None,
                 attributes=[("category", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"


# ---------------------------------------------------------------------------
# Scoring: attribute weights applied in recommendation_score
# ---------------------------------------------------------------------------

def test_scoring_reflects_attribute_weighting(client, db):
    """
    recommendation_score = sum(affinity_score * weight) per matched attribute.
    CORE weight=1.0, DESCRIPTIVE weight=0.2, others weight=0.5.
    """
    ws = make_workspace(client, "V3-3", "v3-3")
    wid = ws["id"]

    # CORE: category (weight=1.0), DESCRIPTIVE: color (weight=0.2), medium: size (weight=0.5)
    seed_affinity(db, wid, "cust_1", "category", "yoga", 1.0)
    seed_affinity(db, wid, "cust_1", "color", "red", 1.0)
    seed_affinity(db, wid, "cust_1", "size", "large", 1.0)
    seed_product(db, wid, "prod_1", "SKU-001", "Red Large Yoga Mat", group_id=None,
                 attributes=[("category", "yoga"), ("color", "red"), ("size", "large")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    # Expected: 1.0*1.0 (category) + 1.0*0.2 (color) + 1.0*0.5 (size) = 1.7
    assert data[0]["recommendation_score"] == pytest.approx(1.7)


def test_matched_attributes_carry_weight_field(client, db):
    """
    Each entry in matched_attributes exposes the weight that was applied.
    """
    ws = make_workspace(client, "V3-4", "v3-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.8)
    seed_affinity(db, wid, "cust_1", "brand", "Nike", 0.6)
    seed_product(db, wid, "prod_1", "SKU-001", "Nike Leggings", group_id=None,
                 attributes=[("type", "leggings"), ("brand", "Nike")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    attrs = {a["attribute_id"]: a for a in data[0]["matched_attributes"]}
    assert attrs["type"]["weight"] == pytest.approx(1.0)   # CORE
    assert attrs["brand"]["weight"] == pytest.approx(0.2)  # DESCRIPTIVE


def test_descriptive_score_lower_than_core_same_affinity(client, db):
    """
    When two products have identical affinity scores but one matches only CORE
    and the other only DESCRIPTIVE, the CORE product scores higher.
    """
    ws = make_workspace(client, "V3-5", "v3-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.9)

    seed_product(db, wid, "prod_core", "SKU-C", "Yoga Mat", group_id=None,
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_desc", "SKU-D", "Red Widget", group_id=None,
                 attributes=[("category", "yoga"), ("color", "red")])  # also needs CORE to pass gate

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_desc: 0.9*1.0 + 0.9*0.2 = 1.08; prod_core: 0.9*1.0 = 0.9 → prod_desc ranked first
    assert data[0]["product_id"] == "prod_desc"
    assert data[0]["recommendation_score"] == pytest.approx(1.08)
    assert data[1]["product_id"] == "prod_core"
    assert data[1]["recommendation_score"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Complementary role: bypasses meaningfulness gate
# ---------------------------------------------------------------------------

def test_complementary_bypasses_meaningfulness_gate(client, db):
    """
    A complementary product with no CORE attribute match is still recommended.
    """
    ws = make_workspace(client, "V3-6", "v3-6")
    wid = ws["id"]

    # Only a DESCRIPTIVE affinity — a normal product would be filtered by the gate
    seed_affinity(db, wid, "cust_1", "color", "red", 0.8)
    seed_product(db, wid, "prod_1", "SKU-001", "Red Accessory", group_id=None,
                 recommendation_role="complementary",
                 attributes=[("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"


def test_non_complementary_same_setup_filtered(client, db):
    """
    Same setup as above but recommendation_role=same_use_case → filtered by gate.
    """
    ws = make_workspace(client, "V3-7", "v3-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "color", "red", 0.8)
    seed_product(db, wid, "prod_1", "SKU-001", "Red Accessory", group_id=None,
                 recommendation_role="same_use_case",
                 attributes=[("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


# ---------------------------------------------------------------------------
# Complementary role: bypasses functional suppression
# ---------------------------------------------------------------------------

def test_complementary_bypasses_functional_suppression(client, db):
    """
    A complementary product with the same functional signature as a one_time
    purchase must still appear in recommendations.
    """
    ws = make_workspace(client, "V3-8", "v3-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.7)

    # Purchased: one_time yoga leggings → functional sig {type=leggings, activity=yoga} suppressed
    seed_product(db, wid, "prod_purchased", "SKU-P", "Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_purchased")

    # Complementary: same functional signature but complementary role → NOT suppressed
    seed_product(db, wid, "prod_comp", "SKU-C", "Yoga Leggings Accessory",
                 recommendation_role="complementary",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_purchased: exact suppression; prod_comp: complementary → bypasses FS
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_comp"


def test_same_use_case_does_not_bypass_functional_suppression(client, db):
    """
    A same_use_case product with the same functional signature IS suppressed.
    """
    ws = make_workspace(client, "V3-9", "v3-9")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)

    seed_product(db, wid, "prod_purchased", "SKU-P", "Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_purchased")

    seed_product(db, wid, "prod_same", "SKU-S", "Another Yoga Leggings",
                 recommendation_role="same_use_case",
                 attributes=[("type", "leggings"), ("activity", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []
