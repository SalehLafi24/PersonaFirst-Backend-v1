import pytest

from app.models.attribute_value_relationship import AttributeValueRelationship


def make_relationship(db, workspace_id, status="suggested", source_value="red"):
    rel = AttributeValueRelationship(
        workspace_id=workspace_id,
        source_attribute_id="color",
        source_value=source_value,
        target_attribute_id="size",
        target_value="large",
        relationship_type="complementary",
        source="cooccurrence",
        confidence=0.8,
        lift=1.5,
        pair_count=5,
        status=status,
    )
    db.add(rel)
    db.commit()
    db.refresh(rel)
    return rel


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def test_list_relationships_empty(client):
    ws = client.post("/workspaces", json={"name": "R1", "slug": "r1"}).json()
    response = client.get(f"/workspaces/{ws['id']}/relationships")
    assert response.status_code == 200
    assert response.json() == []


def test_list_relationships(client, db):
    ws = client.post("/workspaces", json={"name": "R2", "slug": "r2"}).json()
    wid = ws["id"]
    make_relationship(db, wid, status="suggested")
    make_relationship(db, wid, status="approved", source_value="blue")

    response = client.get(f"/workspaces/{wid}/relationships")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_list_relationships_filter_by_status(client, db):
    ws = client.post("/workspaces", json={"name": "R3", "slug": "r3"}).json()
    wid = ws["id"]
    make_relationship(db, wid, status="suggested", source_value="red")
    make_relationship(db, wid, status="approved", source_value="blue")
    make_relationship(db, wid, status="rejected", source_value="green")

    response = client.get(f"/workspaces/{wid}/relationships?status=suggested")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["status"] == "suggested"


def test_list_relationships_workspace_not_found(client):
    response = client.get("/workspaces/999/relationships")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

def test_approve_relationship(client, db):
    ws = client.post("/workspaces", json={"name": "R4", "slug": "r4"}).json()
    wid = ws["id"]
    rel = make_relationship(db, wid, status="suggested")

    response = client.post(f"/workspaces/{wid}/relationships/{rel.id}/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_approve_not_found(client):
    ws = client.post("/workspaces", json={"name": "R5", "slug": "r5"}).json()
    response = client.post(f"/workspaces/{ws['id']}/relationships/999/approve")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------

def test_reject_relationship(client, db):
    ws = client.post("/workspaces", json={"name": "R6", "slug": "r6"}).json()
    wid = ws["id"]
    rel = make_relationship(db, wid, status="suggested")

    response = client.post(f"/workspaces/{wid}/relationships/{rel.id}/reject")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_reject_not_found(client):
    ws = client.post("/workspaces", json={"name": "R7", "slug": "r7"}).json()
    response = client.post(f"/workspaces/{ws['id']}/relationships/999/reject")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def test_archive_relationship(client, db):
    ws = client.post("/workspaces", json={"name": "R8", "slug": "r8"}).json()
    wid = ws["id"]
    rel = make_relationship(db, wid, status="approved")

    response = client.post(f"/workspaces/{wid}/relationships/{rel.id}/archive")
    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_archive_not_found(client):
    ws = client.post("/workspaces", json={"name": "R9", "slug": "r9"}).json()
    response = client.post(f"/workspaces/{ws['id']}/relationships/999/archive")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Workspace isolation (multi-tenancy)
# ---------------------------------------------------------------------------

def test_relationships_scoped_to_workspace(client, db):
    ws1 = client.post("/workspaces", json={"name": "R10", "slug": "r10"}).json()
    ws2 = client.post("/workspaces", json={"name": "R11", "slug": "r11"}).json()
    make_relationship(db, ws1["id"])

    response = client.get(f"/workspaces/{ws2['id']}/relationships")
    assert response.status_code == 200
    assert response.json() == []
