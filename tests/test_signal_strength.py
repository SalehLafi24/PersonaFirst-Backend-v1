"""
Customer signal strength tests.

Validates computation of purchase_depth, attribute_richness,
behavioral_graph components, normalization, and edge cases.
"""
import pytest
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.product_behavior_relationship import ProductBehaviorRelationship


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku, name=name)
    db.add(p)
    db.flush()
    db.commit()
    return p


def seed_purchase(db, workspace_id, customer_id, product_id, quantity=1):
    product = db.query(Product).filter_by(
        workspace_id=workspace_id, product_id=product_id
    ).first()
    db.add(CustomerPurchase(
        workspace_id=workspace_id, customer_id=customer_id,
        product_db_id=product.id, product_id=product_id,
        order_date=date.today(), quantity=quantity,
    ))
    db.commit()


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value, score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
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


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_no_data_returns_zero(client, db):
    ws = make_workspace(client, "SS-1", "ss-1")
    resp = client.get(f"/workspaces/{ws['id']}/signal-strength/cust_unknown")
    assert resp.status_code == 200
    data = resp.json()
    assert data["customer_id"] == "cust_unknown"
    assert data["customer_signal_strength"] == 0.0
    assert data["components"]["purchase_depth"] == 0.0
    assert data["components"]["attribute_richness"] == 0.0
    assert data["components"]["behavioral_graph"] == 0.0


def test_missing_workspace_returns_404(client, db):
    resp = client.get("/workspaces/99999/signal-strength/cust_1")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Purchase depth only
# ---------------------------------------------------------------------------

def test_single_customer_purchase_depth(client, db):
    """Single customer with nonzero data — min==max and value>0 → normalized = 1.0."""
    ws = make_workspace(client, "SS-2", "ss-2")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "Product 1")
    seed_purchase(db, wid, "cust_1", "p1")

    resp = client.get(f"/workspaces/{wid}/signal-strength/cust_1")
    data = resp.json()
    # Only 1 customer, nonzero data → min == max, value > 0 → 1.0
    assert data["components"]["purchase_depth"] == pytest.approx(1.0)


def test_two_customers_purchase_depth_normalization(client, db):
    """Two customers with different purchase counts — proper normalization."""
    ws = make_workspace(client, "SS-3", "ss-3")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "Product 1")
    seed_product(db, wid, "p2", "SKU-2", "Product 2")
    seed_product(db, wid, "p3", "SKU-3", "Product 3")

    # cust_light: 1 purchase, 1 unique product
    seed_purchase(db, wid, "cust_light", "p1")
    # cust_heavy: 3 purchases, 2 unique products
    seed_purchase(db, wid, "cust_heavy", "p1")
    seed_purchase(db, wid, "cust_heavy", "p2")
    seed_purchase(db, wid, "cust_heavy", "p2")

    resp_heavy = client.get(f"/workspaces/{wid}/signal-strength/cust_heavy")
    resp_light = client.get(f"/workspaces/{wid}/signal-strength/cust_light")

    heavy = resp_heavy.json()
    light = resp_light.json()

    # cust_heavy is the max → normalized = 1.0 for both sub-metrics
    # purchase_depth = 0.7 * 1.0 + 0.3 * 1.0 = 1.0
    assert heavy["components"]["purchase_depth"] == pytest.approx(1.0)

    # cust_light is the min → normalized = 0.0 for both
    assert light["components"]["purchase_depth"] == pytest.approx(0.0)

    # Final score: cust_heavy > cust_light
    assert heavy["customer_signal_strength"] > light["customer_signal_strength"]


# ---------------------------------------------------------------------------
# Attribute richness only
# ---------------------------------------------------------------------------

def test_attribute_richness_computation(client, db):
    ws = make_workspace(client, "SS-4", "ss-4")
    wid = ws["id"]

    # cust_rich: 4 affinities across 3 attribute types
    seed_affinity(db, wid, "cust_rich", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_rich", "category", "running", 0.7)
    seed_affinity(db, wid, "cust_rich", "color", "blue", 0.5)
    seed_affinity(db, wid, "cust_rich", "brand", "nike", 0.3)

    # cust_sparse: 1 affinity, 1 type
    seed_affinity(db, wid, "cust_sparse", "category", "yoga", 0.9)

    resp_rich = client.get(f"/workspaces/{wid}/signal-strength/cust_rich")
    resp_sparse = client.get(f"/workspaces/{wid}/signal-strength/cust_sparse")

    rich = resp_rich.json()
    sparse = resp_sparse.json()

    # cust_rich = max → 0.6*1 + 0.4*1 = 1.0
    assert rich["components"]["attribute_richness"] == pytest.approx(1.0)
    # cust_sparse = min → 0.0
    assert sparse["components"]["attribute_richness"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Behavioral graph
# ---------------------------------------------------------------------------

def test_behavioral_graph_computation(client, db):
    ws = make_workspace(client, "SS-5", "ss-5")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_product(db, wid, "p2", "SKU-2", "P2")
    seed_product(db, wid, "p3", "SKU-3", "P3")
    seed_product(db, wid, "p4", "SKU-4", "P4")

    # cust_connected: purchased p1, which has 2 behavioral edges
    seed_purchase(db, wid, "cust_connected", "p1")
    seed_behavior_rel(db, wid, "p1", "p2", strength=0.8, overlap=4, source_count=5)
    seed_behavior_rel(db, wid, "p1", "p3", strength=0.6, overlap=3, source_count=5)

    # cust_weak: purchased p3, which has 1 edge with low strength
    seed_purchase(db, wid, "cust_weak", "p3")
    seed_behavior_rel(db, wid, "p3", "p4", strength=0.2, overlap=1, source_count=5)

    resp_conn = client.get(f"/workspaces/{wid}/signal-strength/cust_connected")
    resp_weak = client.get(f"/workspaces/{wid}/signal-strength/cust_weak")

    conn = resp_conn.json()
    weak = resp_weak.json()

    # cust_connected has more edges and higher avg strength
    assert conn["components"]["behavioral_graph"] > weak["components"]["behavioral_graph"]
    # cust_connected = max → 1.0
    assert conn["components"]["behavioral_graph"] == pytest.approx(1.0)
    # cust_weak = min → 0.0
    assert weak["components"]["behavioral_graph"] == pytest.approx(0.0)


def test_no_behavioral_edges_returns_zero(client, db):
    ws = make_workspace(client, "SS-6", "ss-6")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_purchase(db, wid, "cust_1", "p1")

    resp = client.get(f"/workspaces/{wid}/signal-strength/cust_1")
    assert resp.json()["components"]["behavioral_graph"] == 0.0


# ---------------------------------------------------------------------------
# Combined score
# ---------------------------------------------------------------------------

def test_full_combined_score(client, db):
    """Customer with all three signals has a positive combined score."""
    ws = make_workspace(client, "SS-7", "ss-7")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_product(db, wid, "p2", "SKU-2", "P2")
    seed_product(db, wid, "p3", "SKU-3", "P3")

    # Purchases
    seed_purchase(db, wid, "cust_full", "p1")
    seed_purchase(db, wid, "cust_full", "p2")
    # Need a second customer for normalization to produce non-zero
    seed_purchase(db, wid, "cust_min", "p1")

    # Affinities
    seed_affinity(db, wid, "cust_full", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_full", "color", "blue", 0.5)
    seed_affinity(db, wid, "cust_min", "category", "yoga", 0.9)

    # Behavioral
    seed_behavior_rel(db, wid, "p1", "p3", strength=0.7, overlap=3, source_count=5)

    resp = client.get(f"/workspaces/{wid}/signal-strength/cust_full")
    data = resp.json()

    assert data["customer_signal_strength"] > 0.0
    assert data["customer_signal_strength"] <= 1.0
    assert data["components"]["purchase_depth"] > 0.0
    assert data["components"]["attribute_richness"] > 0.0
    assert data["components"]["behavioral_graph"] >= 0.0


def test_score_clamped_to_unit_range(client, db):
    """Score is always in [0, 1]."""
    ws = make_workspace(client, "SS-8", "ss-8")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_purchase(db, wid, "cust_1", "p1")

    resp = client.get(f"/workspaces/{wid}/signal-strength/cust_1")
    score = resp.json()["customer_signal_strength"]
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Batch endpoint
# ---------------------------------------------------------------------------

def test_batch_returns_all_customers(client, db):
    ws = make_workspace(client, "SS-9", "ss-9")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_purchase(db, wid, "cust_a", "p1")
    seed_purchase(db, wid, "cust_b", "p1")
    seed_affinity(db, wid, "cust_c", "category", "yoga", 0.9)

    resp = client.get(f"/workspaces/{wid}/signal-strength")
    data = resp.json()
    assert data["workspace_id"] == wid
    cids = {r["customer_id"] for r in data["results"]}
    assert cids == {"cust_a", "cust_b", "cust_c"}


def test_batch_empty_workspace(client, db):
    ws = make_workspace(client, "SS-10", "ss-10")
    resp = client.get(f"/workspaces/{ws['id']}/signal-strength")
    data = resp.json()
    assert data["results"] == []


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------

def test_workspace_isolation(client, db):
    ws_a = make_workspace(client, "SS-WA", "ss-wa")
    ws_b = make_workspace(client, "SS-WB", "ss-wb")

    seed_product(db, ws_a["id"], "p1", "SKU-1", "P1")
    seed_purchase(db, ws_a["id"], "cust_1", "p1", quantity=10)

    # cust_1 in ws_b has no data
    resp = client.get(f"/workspaces/{ws_b['id']}/signal-strength/cust_1")
    assert resp.json()["customer_signal_strength"] == 0.0


# ---------------------------------------------------------------------------
# Fix 1: identical nonzero values → 1.0, identical zero values → 0.0
# ---------------------------------------------------------------------------

def test_identical_nonzero_purchases_normalize_to_one(client, db):
    """All customers have the same nonzero purchase count → min==max, value>0 → 1.0."""
    ws = make_workspace(client, "SS-EQ1", "ss-eq1")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_product(db, wid, "p2", "SKU-2", "P2")
    # Both customers: 1 purchase, 1 unique product
    seed_purchase(db, wid, "cust_a", "p1")
    seed_purchase(db, wid, "cust_b", "p2")

    resp_a = client.get(f"/workspaces/{wid}/signal-strength/cust_a")
    resp_b = client.get(f"/workspaces/{wid}/signal-strength/cust_b")
    assert resp_a.json()["components"]["purchase_depth"] == pytest.approx(1.0)
    assert resp_b.json()["components"]["purchase_depth"] == pytest.approx(1.0)


def test_identical_nonzero_affinities_normalize_to_one(client, db):
    ws = make_workspace(client, "SS-EQ2", "ss-eq2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_a", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_b", "category", "running", 0.7)

    resp_a = client.get(f"/workspaces/{wid}/signal-strength/cust_a")
    resp_b = client.get(f"/workspaces/{wid}/signal-strength/cust_b")
    # Both have 1 affinity, 1 type → identical → 1.0
    assert resp_a.json()["components"]["attribute_richness"] == pytest.approx(1.0)
    assert resp_b.json()["components"]["attribute_richness"] == pytest.approx(1.0)


def test_identical_nonzero_behavioral_normalize_to_one(client, db):
    """All customers have identical behavioral stats → 1.0."""
    ws = make_workspace(client, "SS-EQ3", "ss-eq3")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_product(db, wid, "p2", "SKU-2", "P2")
    seed_product(db, wid, "p3", "SKU-3", "P3")
    seed_behavior_rel(db, wid, "p1", "p2", strength=0.5, overlap=2, source_count=4)
    seed_behavior_rel(db, wid, "p3", "p2", strength=0.5, overlap=2, source_count=4)

    # Both customers purchase a product with 1 edge of strength 0.5
    seed_purchase(db, wid, "cust_a", "p1")
    seed_purchase(db, wid, "cust_b", "p3")

    resp_a = client.get(f"/workspaces/{wid}/signal-strength/cust_a")
    resp_b = client.get(f"/workspaces/{wid}/signal-strength/cust_b")
    assert resp_a.json()["components"]["behavioral_graph"] == pytest.approx(1.0)
    assert resp_b.json()["components"]["behavioral_graph"] == pytest.approx(1.0)


def test_no_data_still_returns_zero(client, db):
    """Unknown customer with no data at all → 0.0 (value == 0, min == max == 0)."""
    ws = make_workspace(client, "SS-EQ4", "ss-eq4")
    resp = client.get(f"/workspaces/{ws['id']}/signal-strength/cust_unknown")
    data = resp.json()
    assert data["customer_signal_strength"] == 0.0
    assert data["components"]["purchase_depth"] == 0.0


# ---------------------------------------------------------------------------
# Fix 2: log1p reduces outlier distortion for counts
# ---------------------------------------------------------------------------

def test_log1p_preserves_separation_with_outlier(client, db):
    """
    With a heavy outlier, mid-range customers should still have meaningful
    normalized scores (not crushed near 0 by raw min-max).
    """
    ws = make_workspace(client, "SS-LOG1", "ss-log1")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_product(db, wid, "p2", "SKU-2", "P2")
    seed_product(db, wid, "p3", "SKU-3", "P3")

    # cust_light: 1 purchase
    seed_purchase(db, wid, "cust_light", "p1")
    # cust_mid: 5 purchases across 2 products
    for _ in range(3):
        seed_purchase(db, wid, "cust_mid", "p1")
    for _ in range(2):
        seed_purchase(db, wid, "cust_mid", "p2")
    # cust_outlier: 500 purchases
    for _ in range(500):
        seed_purchase(db, wid, "cust_outlier", "p3")

    resp_mid = client.get(f"/workspaces/{wid}/signal-strength/cust_mid")
    mid_depth = resp_mid.json()["components"]["purchase_depth"]

    # With raw min-max, cust_mid would score ~(4/499)≈0.008 for purchase_count.
    # With log1p, cust_mid scores log1p(5)/log1p(500) ≈ 1.79/6.22 ≈ 0.29.
    # Verify mid is meaningfully above 0 (not crushed by the outlier).
    assert mid_depth > 0.15


def test_avg_edge_strength_uses_raw_normalization(client, db):
    """avg_edge_strength should NOT use log1p — raw min-max only."""
    ws = make_workspace(client, "SS-LOG2", "ss-log2")
    wid = ws["id"]

    seed_product(db, wid, "p1", "SKU-1", "P1")
    seed_product(db, wid, "p2", "SKU-2", "P2")
    seed_product(db, wid, "p3", "SKU-3", "P3")
    seed_product(db, wid, "p4", "SKU-4", "P4")

    # cust_strong: 1 edge, strength 0.9
    seed_purchase(db, wid, "cust_strong", "p1")
    seed_behavior_rel(db, wid, "p1", "p2", strength=0.9, overlap=5, source_count=6)

    # cust_weak: 1 edge, strength 0.1
    seed_purchase(db, wid, "cust_weak", "p3")
    seed_behavior_rel(db, wid, "p3", "p4", strength=0.1, overlap=1, source_count=6)

    resp_strong = client.get(f"/workspaces/{wid}/signal-strength/cust_strong")
    resp_weak = client.get(f"/workspaces/{wid}/signal-strength/cust_weak")

    strong_beh = resp_strong.json()["components"]["behavioral_graph"]
    weak_beh = resp_weak.json()["components"]["behavioral_graph"]

    # edge_count identical (1 each) → normalized edge_count is 1.0 for both (min==max, >0)
    # avg_strength: raw min-max → strong=1.0, weak=0.0
    # behavioral_graph = 0.5 * 1.0 + 0.5 * {1.0 or 0.0}
    assert strong_beh == pytest.approx(1.0)   # 0.5*1.0 + 0.5*1.0
    assert weak_beh == pytest.approx(0.5)     # 0.5*1.0 + 0.5*0.0
