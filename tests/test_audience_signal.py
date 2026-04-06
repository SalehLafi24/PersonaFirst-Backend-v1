"""
Audience-level signal summary tests.

Validates aggregation of customer_signal_strength across an audience:
average, min, max, distribution buckets, dedup, edge cases.
"""
import pytest
from datetime import date

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_product(db, workspace_id, product_id, sku, name, attributes=None):
    p = Product(workspace_id=workspace_id, product_id=product_id, sku=sku,
                name=name)
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


def audience_post(client, wid, customer_ids):
    return client.post(
        f"/workspaces/{wid}/signal-strength/audience",
        json={"customer_ids": customer_ids},
    )


def get_signal(client, wid, customer_id):
    resp = client.get(f"/workspaces/{wid}/signal-strength/{customer_id}")
    return resp.json()["customer_signal_strength"]


def _build_high_signal_customer(db, wid, customer_id):
    """High signal (>0.7): many purchases + diverse affinities."""
    for i in range(20):
        seed_product(db, wid, f"hsig_{customer_id}_{i}",
                     f"SKU-HS-{customer_id}-{i}", f"HS {i}",
                     attributes=[("category", "yoga")])
        seed_purchase(db, wid, customer_id, f"hsig_{customer_id}_{i}")
    seed_affinity(db, wid, customer_id, "category", "yoga", 0.9)
    for attr_type in ["color", "brand", "activity", "size"]:
        for val in ["a", "b", "c", "d"]:
            seed_affinity(db, wid, customer_id, attr_type, val, 0.5)


# ---------------------------------------------------------------------------
# Mixed audience with low / high / unknown customers
# ---------------------------------------------------------------------------

def test_mixed_audience(client, db):
    """Audience with a high-signal, a low-signal, and an unknown customer."""
    ws = make_workspace(client, "AUD-MIX", "aud-mix")
    wid = ws["id"]

    # High-signal customer
    _build_high_signal_customer(db, wid, "cust_high")
    # Minimal low-signal customer for normalization contrast
    seed_purchase(db, wid, "cust_low", f"hsig_cust_high_0")
    seed_affinity(db, wid, "cust_low", "category", "yoga", 0.3)

    # Get individual signals for assertions
    ss_high = get_signal(client, wid, "cust_high")
    ss_low = get_signal(client, wid, "cust_low")
    assert ss_high > 0.7, f"Expected high, got {ss_high}"
    assert ss_low < 0.4, f"Expected low, got {ss_low}"

    # Audience includes unknown customer -> signal 0.0
    resp = audience_post(client, wid, ["cust_high", "cust_low", "cust_unknown"])
    assert resp.status_code == 200
    data = resp.json()

    assert data["audience_size"] == 3
    assert data["min_signal_strength"] == 0.0
    assert data["max_signal_strength"] == pytest.approx(ss_high, abs=1e-5)

    expected_avg = round((ss_high + ss_low + 0.0) / 3, 6)
    assert data["average_signal_strength"] == pytest.approx(expected_avg, abs=1e-5)

    # Bucket verification
    strengths = [ss_high, ss_low, 0.0]
    dist = data["signal_distribution"]
    assert dist["low"] == sum(1 for s in strengths if s < 0.4)
    assert dist["medium"] == sum(1 for s in strengths if 0.4 <= s <= 0.7)
    assert dist["high"] == sum(1 for s in strengths if s > 0.7)


# ---------------------------------------------------------------------------
# Unknown customer_id included as 0.0
# ---------------------------------------------------------------------------

def test_unknown_customer_included_as_zero(client, db):
    """All-unknown audience returns all zeros."""
    ws = make_workspace(client, "AUD-UNK", "aud-unk")
    wid = ws["id"]

    resp = audience_post(client, wid, ["ghost_1", "ghost_2"])
    assert resp.status_code == 200
    data = resp.json()

    assert data["audience_size"] == 2
    assert data["average_signal_strength"] == 0.0
    assert data["min_signal_strength"] == 0.0
    assert data["max_signal_strength"] == 0.0
    assert data["signal_distribution"] == {"low": 2, "medium": 0, "high": 0}


# ---------------------------------------------------------------------------
# Average / min / max computed correctly
# ---------------------------------------------------------------------------

def test_average_min_max(client, db):
    """Single known customer — avg, min, max should all equal that signal."""
    ws = make_workspace(client, "AUD-AMM", "aud-amm")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    ss = get_signal(client, wid, "cust_1")

    resp = audience_post(client, wid, ["cust_1"])
    data = resp.json()

    assert data["audience_size"] == 1
    assert data["average_signal_strength"] == pytest.approx(ss, abs=1e-5)
    assert data["min_signal_strength"] == pytest.approx(ss, abs=1e-5)
    assert data["max_signal_strength"] == pytest.approx(ss, abs=1e-5)


# ---------------------------------------------------------------------------
# Bucket counts computed correctly
# ---------------------------------------------------------------------------

def test_bucket_counts(client, db):
    """Verify distribution buckets: low < 0.4, 0.4 <= medium <= 0.7, high > 0.7."""
    ws = make_workspace(client, "AUD-BKT", "aud-bkt")
    wid = ws["id"]

    # Two unknown customers (signal=0.0, bucket=low)
    resp = audience_post(client, wid, ["u1", "u2"])
    dist = resp.json()["signal_distribution"]
    assert dist == {"low": 2, "medium": 0, "high": 0}


# ---------------------------------------------------------------------------
# Empty customer_ids list rejected
# ---------------------------------------------------------------------------

def test_empty_customer_ids_rejected(client, db):
    ws = make_workspace(client, "AUD-EMPTY", "aud-empty")
    resp = audience_post(client, ws["id"], [])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Duplicate customer_ids deduplicated
# ---------------------------------------------------------------------------

def test_duplicate_customer_ids_deduplicated(client, db):
    """Duplicates are removed — audience_size reflects unique count."""
    ws = make_workspace(client, "AUD-DUP", "aud-dup")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    resp = audience_post(client, wid, ["cust_1", "cust_1", "cust_1"])
    data = resp.json()

    assert data["audience_size"] == 1


# ---------------------------------------------------------------------------
# Unknown workspace returns 404
# ---------------------------------------------------------------------------

def test_unknown_workspace_returns_404(client, db):
    resp = audience_post(client, 99999, ["cust_1"])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Endpoint wiring works (basic round-trip)
# ---------------------------------------------------------------------------

def test_endpoint_round_trip(client, db):
    """Basic check that the endpoint returns the expected shape."""
    ws = make_workspace(client, "AUD-RT", "aud-rt")
    wid = ws["id"]

    resp = audience_post(client, wid, ["c1"])
    assert resp.status_code == 200

    data = resp.json()
    assert "audience_size" in data
    assert "average_signal_strength" in data
    assert "min_signal_strength" in data
    assert "max_signal_strength" in data
    assert "signal_distribution" in data
    assert set(data["signal_distribution"].keys()) == {"low", "medium", "high"}
