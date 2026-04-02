"""
Recommendation V2 tests: eligibility rules, suppression, repurchase logic.
"""
import pytest
from datetime import date, timedelta

from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.services import recommendation_service


def make_workspace(client, name, slug):
    return client.post("/workspaces", json={"name": name, "slug": slug}).json()


def seed_affinity(db, workspace_id, customer_id, attribute_id, attribute_value, score):
    db.add(CustomerAttributeAffinity(
        workspace_id=workspace_id, customer_id=customer_id,
        attribute_id=attribute_id, attribute_value=attribute_value, score=score,
    ))
    db.commit()


def seed_product(db, workspace_id, product_id, sku, name, group_id=None,
                 repurchase_behavior=None, repurchase_window_days=None, attributes=None):
    p = Product(
        workspace_id=workspace_id, product_id=product_id, sku=sku, name=name,
        group_id=group_id, repurchase_behavior=repurchase_behavior,
        repurchase_window_days=repurchase_window_days,
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
        product_db_id=product.id,
        product_id=product_id,
        group_id=group_id if group_id is not None else product.group_id,
        order_date=order_date or date.today(),
    ))
    db.commit()


TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
NINETY_DAYS_AGO = TODAY - timedelta(days=90)


# ---------------------------------------------------------------------------
# Rule 1: suppress exact purchased product_id
# ---------------------------------------------------------------------------

def test_exact_purchased_product_suppressed(client, db):
    ws = make_workspace(client, "V2-1", "v2-1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g1",
                 attributes=[("category", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g1")

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


# ---------------------------------------------------------------------------
# Rule 2 + 3: suppress same group when repurchase_behavior=one_time
# ---------------------------------------------------------------------------

def test_group_suppressed_when_one_time(client, db):
    ws = make_workspace(client, "V2-2", "v2-2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g_yoga",
                 repurchase_behavior="one_time",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Block", group_id="g_yoga",
                 repurchase_behavior="one_time",
                 attributes=[("category", "yoga")])

    # cust_1 bought prod_1 → entire g_yoga group should be suppressed
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga")

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_different_group_not_suppressed(client, db):
    ws = make_workspace(client, "V2-3", "v2-3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g_yoga",
                 repurchase_behavior="one_time",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Block", group_id="g_accessories",
                 attributes=[("category", "yoga")])

    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga")

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_2"


# ---------------------------------------------------------------------------
# Fix 2: no repurchase metadata → do NOT suppress the group
# ---------------------------------------------------------------------------

def test_no_repurchase_metadata_does_not_suppress_group(client, db):
    """
    A product with no repurchase_behavior (None) is treated as non-repurchasable,
    so its group is suppressed — prod_2 in the same group is not returned.
    """
    ws = make_workspace(client, "V2-Fix2", "v2-fix2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # prod_1: no repurchase metadata
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat v1", group_id="g_yoga",
                 attributes=[("category", "yoga")])
    # prod_2: same group, not purchased
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Mat v2", group_id="g_yoga",
                 attributes=[("category", "yoga")])

    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga")

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_1 suppressed (exact purchase); group suppressed (no repurchase_behavior → True)
    # → prod_2 also suppressed
    assert data == []


# ---------------------------------------------------------------------------
# Rule 4: repurchase_window_days — suppress within window, allow after
# ---------------------------------------------------------------------------

def test_within_repurchase_window_suppresses_group(client, db):
    ws = make_workspace(client, "V2-4", "v2-4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g_yoga",
                 repurchase_behavior="repurchasable", repurchase_window_days=30,
                 attributes=[("category", "yoga")])

    # Purchased yesterday — within 30-day window
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga", order_date=YESTERDAY)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_outside_repurchase_window_allows_group(client, db):
    ws = make_workspace(client, "V2-5", "v2-5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g_yoga",
                 repurchase_behavior="repurchasable", repurchase_window_days=30,
                 attributes=[("category", "yoga")])

    # Purchased 90 days ago — window expired (30 days)
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga", order_date=NINETY_DAYS_AGO)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_1 is suppressed (exact product); group is eligible again
    # Since it's the only product in the group, list is empty
    assert data == []


def test_outside_window_allows_other_product_in_group(client, db):
    ws = make_workspace(client, "V2-6", "v2-6")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat v1", group_id="g_yoga",
                 repurchase_behavior="repurchasable", repurchase_window_days=30,
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Mat v2", group_id="g_yoga",
                 repurchase_behavior="repurchasable", repurchase_window_days=30,
                 attributes=[("category", "yoga")])

    # Purchased prod_1 90 days ago — window expired
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga", order_date=NINETY_DAYS_AGO)

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_1 suppressed (exact purchase), but group eligible → prod_2 should appear
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_2"


# ---------------------------------------------------------------------------
# Rule: repurchasable with no window → never suppress group
# ---------------------------------------------------------------------------

def test_repurchasable_no_window_keeps_group_eligible(client, db):
    ws = make_workspace(client, "V2-7", "v2-7")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat v1", group_id="g_yoga",
                 repurchase_behavior="repurchasable",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Mat v2", group_id="g_yoga",
                 repurchase_behavior="repurchasable",
                 attributes=[("category", "yoga")])

    seed_purchase(db, wid, "cust_1", "prod_1", group_id="g_yoga")

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_1 suppressed exactly; prod_2 in same group but group is eligible
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_2"


# ---------------------------------------------------------------------------
# Rule 5: same group returns all ranked (diversity controls dedup)
# ---------------------------------------------------------------------------

def test_same_group_returns_all_ranked_by_score(client, db):
    """Without diversity, both products in the same group are returned,
    highest score first."""
    ws = make_workspace(client, "V2-8", "v2-8")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "pregnant", 0.8)

    seed_product(db, wid, "prod_1", "SKU-001", "Basic Mat", group_id="g_yoga",
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_2", "SKU-002", "Premium Mat", group_id="g_yoga",
                 attributes=[("category", "yoga"), ("activity", "pregnant")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 2
    assert data[0]["product_id"] == "prod_2"
    assert data[0]["recommendation_score"] == pytest.approx(1.7)
    assert data[1]["product_id"] == "prod_1"


def test_null_group_id_treated_as_own_group(client, db):
    ws = make_workspace(client, "V2-9", "v2-9")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # Two products with no group_id — each treated as its own group (both returned)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga A", group_id=None,
                 attributes=[("category", "yoga")])
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga B", group_id=None,
                 attributes=[("category", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 2


# ---------------------------------------------------------------------------
# Fix 6: deterministic tie-break by internal product PK ascending
# ---------------------------------------------------------------------------

def test_tie_break_is_deterministic_by_product_db_id(client, db):
    ws = make_workspace(client, "V2-10", "v2-10")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    # prod_z seeded first → lower product DB id
    seed_product(db, wid, "prod_z", "SKU-Z", "Z Product", group_id=None,
                 attributes=[("category", "yoga")])
    # prod_a seeded second → higher product DB id
    seed_product(db, wid, "prod_a", "SKU-A", "A Product", group_id=None,
                 attributes=[("category", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # Same score → lower product DB id (prod_z, inserted first) comes first
    assert data[0]["product_id"] == "prod_z"
    assert data[1]["product_id"] == "prod_a"


# ---------------------------------------------------------------------------
# No purchases → no suppression applied
# ---------------------------------------------------------------------------

def test_no_purchases_no_suppression(client, db):
    ws = make_workspace(client, "V2-11", "v2-11")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id="g1",
                 repurchase_behavior="one_time",
                 attributes=[("category", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"


# ---------------------------------------------------------------------------
# Fix 5: matched_attributes is structured output
# ---------------------------------------------------------------------------

def test_matched_attributes_structured(client, db):
    ws = make_workspace(client, "V2-MA", "v2-ma")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "mat", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.5)
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id=None,
                 attributes=[("category", "mat"), ("activity", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    rec = data[0]
    assert isinstance(rec["matched_attributes"], list)
    assert len(rec["matched_attributes"]) == 2
    # Highest scoring attribute first (both CORE weight=1.0, sorted by score desc)
    assert rec["matched_attributes"][0]["attribute_id"] == "category"
    assert rec["matched_attributes"][0]["attribute_value"] == "mat"
    assert rec["matched_attributes"][0]["score"] == pytest.approx(0.9)
    assert rec["matched_attributes"][0]["weight"] == pytest.approx(1.0)
    assert rec["matched_attributes"][1]["attribute_id"] == "activity"
    assert rec["matched_attributes"][1]["score"] == pytest.approx(0.5)
    assert rec["matched_attributes"][1]["weight"] == pytest.approx(1.0)
    assert rec["recommendation_score"] == pytest.approx(1.4)


# ---------------------------------------------------------------------------
# Fix 7: min_score filters affinities before scoring
# ---------------------------------------------------------------------------

def test_min_score_filters_low_affinity_products(client, db):
    ws = make_workspace(client, "V2-MS", "v2-ms")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "category", "budget", 0.2)

    # prod_1 matches only on high-score attribute
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat", group_id=None,
                 attributes=[("type", "yoga")])
    # prod_2 matches only on low-score attribute
    seed_product(db, wid, "prod_2", "SKU-002", "Budget Item", group_id=None,
                 attributes=[("category", "budget")])

    # min_score=0.5 → only lifestyle=yoga(0.9) affinity qualifies
    data = client.get(f"/workspaces/{wid}/recommendations/cust_1?min_score=0.5").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_1"


# ---------------------------------------------------------------------------
# Functional suppression: same (type, activity) = same functional use-case
# Functional suppression follows the SAME repurchase rules as group suppression:
#   - one_time                    → suppress forever
#   - repurchasable + window      → suppress only while inside the window
#   - repurchasable + no window   → never suppress
#   - no repurchase metadata      → never suppress
# ---------------------------------------------------------------------------

def test_functional_suppression_same_signature_different_color(client, db):
    """
    one_time purchase of black yoga leggings → red yoga leggings suppressed.
    Both share type=leggings + activity=yoga.  Color difference is irrelevant.
    """
    ws = make_workspace(client, "V2-FS1", "v2-fs1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.8)

    # Purchased product: one_time black yoga leggings
    seed_product(db, wid, "prod_black", "SKU-BLK", "Black Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "black")])
    seed_purchase(db, wid, "cust_1", "prod_black")

    # Candidate: red yoga leggings — same functional signature, different color
    seed_product(db, wid, "prod_red", "SKU-RED", "Red Yoga Leggings",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # prod_black: suppressed (exact purchase)
    # prod_red:   suppressed (same functional signature — one_time → active forever)
    assert data == []


def test_functional_suppression_different_group_same_signature_suppressed(client, db):
    """
    Purchased one_time product and candidate are in different groups but share
    the same functional signature — candidate must still be suppressed.
    """
    ws = make_workspace(client, "V2-FS2", "v2-fs2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)

    # Purchased: group A, one_time
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Leggings A", group_id="group-a",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_1", group_id="group-a")

    # Candidate: group B — different group, same functional signature
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Leggings B", group_id="group-b",
                 attributes=[("type", "leggings"), ("activity", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert data == []


def test_functional_suppression_different_activity_not_suppressed(client, db):
    """
    one_time yoga leggings purchased → running leggings NOT suppressed.
    Same type but different activity = different functional signature.
    """
    ws = make_workspace(client, "V2-FS3", "v2-fs3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.8)
    seed_affinity(db, wid, "cust_1", "activity", "running", 0.7)

    seed_product(db, wid, "prod_yoga", "SKU-YG", "Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_yoga")

    seed_product(db, wid, "prod_run", "SKU-RN", "Running Leggings",
                 attributes=[("type", "leggings"), ("activity", "running")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # Different activity → different functional signature → not suppressed
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_run"


def test_functional_suppression_no_signature_attrs_not_suppressed(client, db):
    """
    Even when a one_time purchased product has a functional signature, a candidate
    with no type/activity attributes (empty signature) must not be suppressed.
    """
    ws = make_workspace(client, "V2-FS4", "v2-fs4")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "category", "yoga", 0.9)

    # Purchased: one_time, has functional signature
    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Mat",
                 repurchase_behavior="one_time",
                 attributes=[("type", "mat"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_1")

    # Candidate has no type/activity → empty candidate signature → not suppressed by FS
    # Uses CORE attr "category" so it passes the meaningfulness gate
    seed_product(db, wid, "prod_2", "SKU-002", "Yoga Block",
                 attributes=[("category", "yoga")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_2"


def test_functional_suppression_partial_signature_not_suppressed(client, db):
    """
    Purchased one_time product has type=leggings + activity=yoga.
    Candidate has only type=leggings (no activity).
    Signatures differ → candidate is NOT suppressed.
    """
    ws = make_workspace(client, "V2-FS5", "v2-fs5")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.8)

    seed_product(db, wid, "prod_1", "SKU-001", "Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga")])
    seed_purchase(db, wid, "cust_1", "prod_1")

    # Has type but no activity — different signature
    seed_product(db, wid, "prod_2", "SKU-002", "Generic Leggings",
                 attributes=[("type", "leggings")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_2"


# ---------------------------------------------------------------------------
# Functional suppression respects repurchase window
# ---------------------------------------------------------------------------

def test_functional_suppression_within_window_applies(client, db):
    """
    Purchase inside the repurchase window → functional suppression is active.
    """
    ws = make_workspace(client, "V2-FSW1", "v2-fsw1")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.8)

    # Purchased yesterday — inside 30-day window
    seed_product(db, wid, "prod_black", "SKU-BLK", "Black Yoga Leggings",
                 repurchase_behavior="repurchasable", repurchase_window_days=30,
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "black")])
    seed_purchase(db, wid, "cust_1", "prod_black", order_date=YESTERDAY)

    # Candidate: same functional signature, different color, no group overlap
    seed_product(db, wid, "prod_red", "SKU-RED", "Red Yoga Leggings",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # Window active → group suppressed → functional sig collected → prod_red suppressed
    assert data == []


def test_functional_suppression_outside_window_does_not_apply(client, db):
    """
    Purchase outside the repurchase window → functional suppression is lifted.
    Candidate with same functional signature should appear.
    """
    ws = make_workspace(client, "V2-FSW2", "v2-fsw2")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.8)

    # Purchased 90 days ago — outside 30-day window
    seed_product(db, wid, "prod_black", "SKU-BLK", "Black Yoga Leggings",
                 repurchase_behavior="repurchasable", repurchase_window_days=30,
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "black")])
    seed_purchase(db, wid, "cust_1", "prod_black", order_date=NINETY_DAYS_AGO)

    # Candidate: same functional signature, different color, different group
    seed_product(db, wid, "prod_red", "SKU-RED", "Red Yoga Leggings",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # Window expired → group not suppressed → functional sig NOT collected → prod_red eligible
    # prod_black still suppressed by exact-product rule
    assert len(data) == 1
    assert data[0]["product_id"] == "prod_red"


def test_functional_suppression_one_time_always_applies(client, db):
    """
    one_time purchase always suppresses group and functional signature,
    regardless of how long ago it happened.
    """
    ws = make_workspace(client, "V2-FSW3", "v2-fsw3")
    wid = ws["id"]

    seed_affinity(db, wid, "cust_1", "type", "leggings", 0.9)
    seed_affinity(db, wid, "cust_1", "activity", "yoga", 0.9)
    seed_affinity(db, wid, "cust_1", "color", "red", 0.8)

    # Purchased 90 days ago but one_time → still suppressed
    seed_product(db, wid, "prod_black", "SKU-BLK", "Black Yoga Leggings",
                 repurchase_behavior="one_time",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "black")])
    seed_purchase(db, wid, "cust_1", "prod_black", order_date=NINETY_DAYS_AGO)

    # Candidate: same functional signature, different color, different group
    seed_product(db, wid, "prod_red", "SKU-RED", "Red Yoga Leggings",
                 attributes=[("type", "leggings"), ("activity", "yoga"), ("color", "red")])

    data = client.get(f"/workspaces/{wid}/recommendations/cust_1").json()
    # one_time → group suppressed forever → functional sig always collected → prod_red suppressed
    assert data == []
