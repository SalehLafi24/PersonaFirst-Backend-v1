import pytest

from app.models.attribute_value_relationship import AttributeValueRelationship
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.services.relationship_engine_service import run_relationship_engine


def seed(db, workspace_id, data):
    """data: list of (customer_id, attribute_id, attribute_value)"""
    for customer_id, attribute_id, attribute_value in data:
        db.add(
            CustomerAttributeAffinity(
                workspace_id=workspace_id,
                customer_id=customer_id,
                attribute_id=attribute_id,
                attribute_value=attribute_value,
                score=1.0,
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def test_generates_relationships(client, db):
    ws = client.post("/workspaces", json={"name": "W1", "slug": "w1"}).json()
    wid = ws["id"]
    seed(db, wid, [
        ("c1", "color", "red"),  ("c1", "size", "large"),
        ("c2", "color", "red"),  ("c2", "size", "large"),
        ("c3", "color", "blue"), ("c3", "size", "small"),
    ])
    # Only (color=red, size=large) pair meets min_pair_count=2 → 2 directed rules
    created = run_relationship_engine(db, wid, min_pair_count=2)
    assert created == 2


def test_no_relationships_when_no_data(client, db):
    ws = client.post("/workspaces", json={"name": "W2", "slug": "w2"}).json()
    created = run_relationship_engine(db, ws["id"])
    assert created == 0


def test_idempotent_does_not_duplicate(client, db):
    ws = client.post("/workspaces", json={"name": "W3", "slug": "w3"}).json()
    wid = ws["id"]
    seed(db, wid, [
        ("c1", "color", "red"), ("c1", "size", "large"),
        ("c2", "color", "red"), ("c2", "size", "large"),
    ])
    run_relationship_engine(db, wid, min_pair_count=1)
    run_relationship_engine(db, wid, min_pair_count=1)  # second run

    count = db.query(AttributeValueRelationship).filter(
        AttributeValueRelationship.workspace_id == wid
    ).count()
    assert count == 2  # not 4


# ---------------------------------------------------------------------------
# Confidence calculation
# ---------------------------------------------------------------------------

def test_confidence_calculation(client, db):
    ws = client.post("/workspaces", json={"name": "W4", "slug": "w4"}).json()
    wid = ws["id"]
    # 3 customers with color=red, but only 2 also have size=large
    seed(db, wid, [
        ("c1", "color", "red"), ("c1", "size", "large"),
        ("c2", "color", "red"), ("c2", "size", "large"),
        ("c3", "color", "red"), ("c3", "size", "small"),
    ])
    run_relationship_engine(db, wid, min_pair_count=1, min_lift=0.1)

    rel = (
        db.query(AttributeValueRelationship)
        .filter(
            AttributeValueRelationship.workspace_id == wid,
            AttributeValueRelationship.source_attribute_id == "color",
            AttributeValueRelationship.source_value == "red",
            AttributeValueRelationship.target_attribute_id == "size",
            AttributeValueRelationship.target_value == "large",
        )
        .first()
    )
    assert rel is not None
    # confidence = count(red AND large) / count(red) = 2/3
    assert abs(rel.confidence - 2 / 3) < 0.0001


# ---------------------------------------------------------------------------
# Lift calculation
# ---------------------------------------------------------------------------

def test_lift_calculation(client, db):
    ws = client.post("/workspaces", json={"name": "W5", "slug": "w5"}).json()
    wid = ws["id"]
    # 4 customers: 3 have color=red, 2 have size=large, 2 have both
    seed(db, wid, [
        ("c1", "color", "red"),  ("c1", "size", "large"),
        ("c2", "color", "red"),  ("c2", "size", "large"),
        ("c3", "color", "red"),
        ("c4", "color", "blue"),
    ])
    run_relationship_engine(db, wid, min_pair_count=1, min_lift=0.1)

    rel = (
        db.query(AttributeValueRelationship)
        .filter(
            AttributeValueRelationship.workspace_id == wid,
            AttributeValueRelationship.source_attribute_id == "color",
            AttributeValueRelationship.source_value == "red",
            AttributeValueRelationship.target_attribute_id == "size",
            AttributeValueRelationship.target_value == "large",
        )
        .first()
    )
    assert rel is not None
    # confidence = 2/3, support(size=large) = 2/4 = 0.5
    # lift = (2/3) / 0.5 = 4/3
    expected_lift = (2 / 3) / (2 / 4)
    assert abs(rel.lift - expected_lift) < 0.0001


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

def test_excludes_same_attribute(client, db):
    ws = client.post("/workspaces", json={"name": "W6", "slug": "w6"}).json()
    wid = ws["id"]
    # Same attribute_id — should produce no cross-attribute pairs
    seed(db, wid, [
        ("c1", "color", "red"),  ("c1", "color", "blue"),
        ("c2", "color", "red"),  ("c2", "color", "blue"),
    ])
    created = run_relationship_engine(db, wid, min_pair_count=1)
    assert created == 0


def test_excludes_group_id(client, db):
    ws = client.post("/workspaces", json={"name": "W7", "slug": "w7"}).json()
    wid = ws["id"]
    # group_id is in DEFAULT_EXCLUDED_ATTRIBUTES; stripped out before analysis
    seed(db, wid, [
        ("c1", "group_id", "grp1"), ("c1", "color", "red"),
        ("c2", "group_id", "grp1"), ("c2", "color", "red"),
    ])
    # After stripping group_id, each basket has only color=red — no pairs possible
    created = run_relationship_engine(db, wid, min_pair_count=1)
    assert created == 0


def test_excludes_low_pair_count(client, db):
    ws = client.post("/workspaces", json={"name": "W8", "slug": "w8"}).json()
    wid = ws["id"]
    seed(db, wid, [
        ("c1", "color", "red"),  ("c1", "size", "large"),
        ("c2", "color", "blue"), ("c2", "size", "small"),
    ])
    # Each pair appears only once — filtered by min_pair_count=3
    created = run_relationship_engine(db, wid, min_pair_count=3)
    assert created == 0


def test_excludes_low_confidence(client, db):
    ws = client.post("/workspaces", json={"name": "W9", "slug": "w9"}).json()
    wid = ws["id"]
    # c1–c3 have color=red but only c1 has size=large → confidence = 1/3 ≈ 0.33
    seed(db, wid, [
        ("c1", "color", "red"), ("c1", "size", "large"),
        ("c2", "color", "red"),
        ("c3", "color", "red"),
        ("c4", "color", "blue"), ("c4", "size", "small"),
    ])
    created = run_relationship_engine(db, wid, min_pair_count=1, min_confidence=0.9, min_lift=0.0)
    # color=red → size=large has confidence ≈ 0.33 < 0.9, so filtered out
    rel = (
        db.query(AttributeValueRelationship)
        .filter(
            AttributeValueRelationship.workspace_id == wid,
            AttributeValueRelationship.source_attribute_id == "color",
            AttributeValueRelationship.source_value == "red",
        )
        .first()
    )
    assert rel is None


# ---------------------------------------------------------------------------
# Generate endpoint
# ---------------------------------------------------------------------------

def test_generate_endpoint(client, db):
    ws = client.post("/workspaces", json={"name": "W10", "slug": "w10"}).json()
    wid = ws["id"]
    seed(db, wid, [
        ("c1", "color", "red"), ("c1", "size", "large"),
        ("c2", "color", "red"), ("c2", "size", "large"),
    ])
    response = client.post(
        f"/workspaces/{wid}/relationships/generate?min_pair_count=1"
    )
    assert response.status_code == 200
    assert response.json()["created"] == 2


def test_generate_endpoint_workspace_not_found(client):
    response = client.post("/workspaces/999/relationships/generate")
    assert response.status_code == 404
