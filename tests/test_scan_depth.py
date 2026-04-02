"""
Dynamic scan depth tests.

Validates that max_scan_depth limits how many ranked candidates are
scanned during selection, based on customer_signal_strength.
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
    # Minimal second customer for normalization contrast
    seed_purchase(db, wid, "minimal_cust", "hsig_p0")


def _assert_high_signal(client, wid, customer_id):
    ss_resp = client.get(f"/workspaces/{wid}/signal-strength/{customer_id}")
    strength = ss_resp.json()["customer_signal_strength"]
    assert strength > 0.7, f"Expected high signal, got {strength}"
    return strength


# ---------------------------------------------------------------------------
# High signal -> fewer candidates scanned (depth=20)
# ---------------------------------------------------------------------------

def test_high_signal_scans_fewer_candidates(client, db):
    """Customer with signal > 0.7 gets max_scan_depth=20.
    With 25 eligible products, at most 20 are scanned."""
    ws = make_workspace(client, "SD-HIGH", "sd-high")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    # 25 recommendation candidates (not purchased, so not suppressed)
    for i in range(25):
        seed_product(db, wid, f"rec_p{i}", f"SKU-R-{i}", f"Rec Product {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 25,
        "diversity_mode": "off",
    })
    assert resp.status_code == 200
    data = resp.json()["results"]
    # depth=20 limits scan to top 20 ranked candidates
    assert len(data) <= 20


# ---------------------------------------------------------------------------
# Low signal -> more candidates scanned (depth=100)
# ---------------------------------------------------------------------------

def test_low_signal_scans_more_candidates(client, db):
    """Customer with signal < 0.4 gets max_scan_depth=100.
    With 25 eligible products and top_n=25, all 25 returned."""
    ws = make_workspace(client, "SD-LOW", "sd-low")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_new", "category", "yoga", 0.9)

    for i in range(25):
        seed_product(db, wid, f"rec_p{i}", f"SKU-R-{i}", f"Rec Product {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_new", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 25,
        "diversity_mode": "off",
    })
    assert resp.status_code == 200
    data = resp.json()["results"]
    assert len(data) == 25


# ---------------------------------------------------------------------------
# Results correctness - no regression in ranking
# ---------------------------------------------------------------------------

def test_scan_depth_preserves_ranking(client, db):
    """Within the scan window, highest-scored products are returned first."""
    ws = make_workspace(client, "SD-RANK", "sd-rank")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "running", 0.5)

    seed_product(db, wid, "yoga_prod", "SKU-Y", "Yoga Product",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "run_prod", "SKU-R", "Running Product",
                 attributes=[("category", "running")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    assert len(data) == 2
    assert data[0]["product_id"] == "yoga_prod"
    assert data[1]["product_id"] == "run_prod"


# ---------------------------------------------------------------------------
# Diversity still respected with scan depth
# ---------------------------------------------------------------------------

def test_diversity_respected_with_scan_depth(client, db):
    """diversity_mode='strict' still enforces max 1 per group within scan window."""
    ws = make_workspace(client, "SD-DIV", "sd-div")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    for i in range(3):
        seed_product(db, wid, f"grp_prod_{i}", f"SKU-G-{i}",
                     f"Group Product {i}", group_id="grp_yoga",
                     attributes=[("category", "yoga")])
    seed_product(db, wid, "solo_prod", "SKU-S", "Solo Product",
                 group_id="grp_solo", attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 5,
        "diversity_mode": "strict",
    })
    data = resp.json()["results"]
    groups = [r["group_id"] for r in data]
    assert groups.count("grp_yoga") == 1
    assert groups.count("grp_solo") == 1


# ---------------------------------------------------------------------------
# Cross-slot exclusion still respected with scan depth
# ---------------------------------------------------------------------------

def test_exclusion_respected_with_scan_depth(client, db):
    """Cross-slot product exclusion works correctly with scan depth applied."""
    ws = make_workspace(client, "SD-EXC", "sd-exc")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_a", "SKU-A", "Product A",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_b", "SKU-B", "Product B",
                 attributes=[("category", "yoga")])

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
    assert s1_ids & s2_ids == set()


# ---------------------------------------------------------------------------
# Scan cap fills past skipped candidates within the window
# ---------------------------------------------------------------------------

def test_scan_cap_fills_past_skipped_candidates(client, db):
    """Skipped candidates (exclusion, diversity) count toward the scan cap
    but do not prevent filling when valid candidates remain in the window."""
    ws = make_workspace(client, "SD-FILL", "sd-fill")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    # 3 products in the same group, then 3 ungrouped products — all same score.
    # With strict diversity, 2 group products are skipped but the ungrouped
    # products are still reached and selected.
    for i in range(3):
        seed_product(db, wid, f"grp_{i}", f"SKU-G-{i}", f"Grp {i}",
                     group_id="grp_A", attributes=[("category", "yoga")])
    for i in range(3):
        seed_product(db, wid, f"solo_{i}", f"SKU-S-{i}", f"Solo {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_1", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 4,
        "diversity_mode": "strict",
    })
    data = resp.json()["results"]
    # 1 from grp_A + 3 ungrouped = 4 results, despite 2 skipped candidates
    assert len(data) == 4
    assert sum(1 for r in data if r["group_id"] == "grp_A") == 1


# ---------------------------------------------------------------------------
# Scan cap counts inspections, not selections
# ---------------------------------------------------------------------------

def test_scan_cap_counts_inspections_not_selections(client, db):
    """max_scan_depth limits how many ranked candidates are inspected,
    regardless of how many are actually selected."""
    ws = make_workspace(client, "SD-INSP", "sd-insp")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    # 25 eligible products — all same score, ranked by product_id then PK.
    # depth=20 means only 20 are inspected; remaining 5 are never reached.
    for i in range(25):
        seed_product(db, wid, f"rec_p{i:02d}", f"SKU-R-{i}", f"Rec {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 25,
        "diversity_mode": "off",
    })
    data = resp.json()["results"]
    # Only 20 inspected → at most 20 selected (all valid, so exactly 20)
    assert len(data) == 20


# ---------------------------------------------------------------------------
# Shallow scan underfills when too many early candidates are skipped
# ---------------------------------------------------------------------------

def test_scan_cap_underfills_when_skips_exhaust_depth(client, db):
    """When diversity skips many candidates within the scan window,
    results can be fewer than top_n — the cap is on inspections, not output."""
    ws = make_workspace(client, "SD-UNDER", "sd-under")
    wid = ws["id"]

    _build_high_signal_customer(db, wid, "cust_power")
    _assert_high_signal(client, wid, "cust_power")

    # 15 products in grp_A (ranked first by product_id "grp_a_*" < "solo_*"),
    # then 10 ungrouped products.  depth=20 inspects all 15 grp_A + 5 solo.
    # strict diversity keeps only 1 from grp_A → 1 + 5 = 6 results.
    for i in range(15):
        seed_product(db, wid, f"grp_a_{i:02d}", f"SKU-GA-{i}",
                     f"GroupA {i}", group_id="grp_A",
                     attributes=[("category", "yoga")])
    for i in range(10):
        seed_product(db, wid, f"solo_{i:02d}", f"SKU-S-{i}", f"Solo {i}",
                     attributes=[("category", "yoga")])

    resp = slot_post(client, wid, "cust_power", {
        "slot_id": "s1", "algorithm": "balanced", "top_n": 25,
        "diversity_mode": "strict",
    })
    data = resp.json()["results"]
    # depth=20: 15 grp_A inspected (14 skipped) + 5 solo inspected → 6 selected
    assert len(data) == 6
    assert sum(1 for r in data if r["group_id"] == "grp_A") == 1
    assert sum(1 for r in data if r["group_id"] is None) == 5
