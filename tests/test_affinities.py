from app.models.customer_attribute_affinity import CustomerAttributeAffinity


PAYLOAD = [
    {"customer_id": "c1", "attribute_name": "color", "value_label": "red",   "score": 0.9},
    {"customer_id": "c1", "attribute_name": "size",  "value_label": "large", "score": 0.8},
    {"customer_id": "c2", "attribute_name": "color", "value_label": "blue",  "score": 0.7},
]


# ---------------------------------------------------------------------------
# POST /workspaces/{workspace_id}/affinities
# ---------------------------------------------------------------------------

def test_bulk_create_affinities(client):
    ws = client.post("/workspaces", json={"name": "A1", "slug": "a1"}).json()
    wid = ws["id"]

    response = client.post(f"/workspaces/{wid}/affinities", json=PAYLOAD)
    assert response.status_code == 201
    data = response.json()
    assert len(data) == 3
    assert data[0]["workspace_id"] == wid
    assert data[0]["customer_id"] == "c1"
    assert data[0]["attribute_id"] == "color"
    assert data[0]["attribute_value"] == "red"
    assert data[0]["score"] == 0.9


def test_bulk_create_affinities_workspace_not_found(client):
    response = client.post(
        "/workspaces/999/affinities",
        json=[{"customer_id": "c1", "attribute_name": "color", "value_label": "red", "score": 0.9}],
    )
    assert response.status_code == 404


def test_bulk_create_empty_list(client):
    ws = client.post("/workspaces", json={"name": "A2", "slug": "a2"}).json()
    response = client.post(f"/workspaces/{ws['id']}/affinities", json=[])
    assert response.status_code == 201
    assert response.json() == []


# ---------------------------------------------------------------------------
# GET /workspaces/{workspace_id}/affinities
# ---------------------------------------------------------------------------

def test_list_affinities_empty(client):
    ws = client.post("/workspaces", json={"name": "A3", "slug": "a3"}).json()
    response = client.get(f"/workspaces/{ws['id']}/affinities")
    assert response.status_code == 200
    assert response.json() == []


def test_list_affinities(client):
    ws = client.post("/workspaces", json={"name": "A4", "slug": "a4"}).json()
    wid = ws["id"]

    client.post(f"/workspaces/{wid}/affinities", json=PAYLOAD)

    response = client.get(f"/workspaces/{wid}/affinities")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    assert all("score" in row for row in data)


def test_list_affinities_workspace_not_found(client):
    response = client.get("/workspaces/999/affinities")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------

def test_affinities_scoped_to_workspace(client):
    ws1 = client.post("/workspaces", json={"name": "A5", "slug": "a5"}).json()
    ws2 = client.post("/workspaces", json={"name": "A6", "slug": "a6"}).json()

    client.post(
        f"/workspaces/{ws1['id']}/affinities",
        json=[{"customer_id": "c1", "attribute_name": "color", "value_label": "red", "score": 0.9}],
    )

    response = client.get(f"/workspaces/{ws2['id']}/affinities")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Field mapping: attribute_name → attribute_id, value_label → attribute_value
# ---------------------------------------------------------------------------

def test_field_mapping_stored_correctly(client, db):
    ws = client.post("/workspaces", json={"name": "A7", "slug": "a7"}).json()
    wid = ws["id"]

    client.post(
        f"/workspaces/{wid}/affinities",
        json=[{"customer_id": "c1", "attribute_name": "shoe_size", "value_label": "42", "score": 0.85}],
    )

    record = db.query(CustomerAttributeAffinity).filter(
        CustomerAttributeAffinity.workspace_id == wid
    ).first()
    assert record.attribute_id == "shoe_size"
    assert record.attribute_value == "42"
    assert record.score == 0.85


# ---------------------------------------------------------------------------
# Score persistence (round-trip through API)
# ---------------------------------------------------------------------------

def test_score_persisted_for_each_record(client):
    ws = client.post("/workspaces", json={"name": "A8", "slug": "a8"}).json()
    wid = ws["id"]

    payload = [
        {"customer_id": "c1", "attribute_name": "color", "value_label": "red",   "score": 0.95},
        {"customer_id": "c1", "attribute_name": "size",  "value_label": "large", "score": 0.60},
        {"customer_id": "c2", "attribute_name": "color", "value_label": "blue",  "score": 0.10},
    ]
    client.post(f"/workspaces/{wid}/affinities", json=payload)

    data = client.get(f"/workspaces/{wid}/affinities").json()
    scores = {(r["attribute_id"], r["attribute_value"]): r["score"] for r in data}

    assert scores[("color", "red")]   == 0.95
    assert scores[("size",  "large")] == 0.60
    assert scores[("color", "blue")]  == 0.10


# ---------------------------------------------------------------------------
# Filter by score threshold  [NOT YET IMPLEMENTED — expected to fail]
# ---------------------------------------------------------------------------

def test_filter_by_min_score(client):
    ws = client.post("/workspaces", json={"name": "A9", "slug": "a9"}).json()
    wid = ws["id"]

    payload = [
        {"customer_id": "c1", "attribute_name": "color", "value_label": "red",   "score": 0.95},
        {"customer_id": "c1", "attribute_name": "size",  "value_label": "large", "score": 0.60},
        {"customer_id": "c2", "attribute_name": "color", "value_label": "blue",  "score": 0.10},
    ]
    client.post(f"/workspaces/{wid}/affinities", json=payload)

    response = client.get(f"/workspaces/{wid}/affinities?min_score=0.85")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["score"] == 0.95


def test_filter_by_min_score_returns_all_when_threshold_is_zero(client):
    ws = client.post("/workspaces", json={"name": "A10", "slug": "a10"}).json()
    wid = ws["id"]

    payload = [
        {"customer_id": "c1", "attribute_name": "color", "value_label": "red",  "score": 0.9},
        {"customer_id": "c1", "attribute_name": "size",  "value_label": "large","score": 0.5},
    ]
    client.post(f"/workspaces/{wid}/affinities", json=payload)

    response = client.get(f"/workspaces/{wid}/affinities?min_score=0.0")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_filter_by_min_score_returns_empty_when_none_qualify(client):
    ws = client.post("/workspaces", json={"name": "A11", "slug": "a11"}).json()
    wid = ws["id"]

    payload = [
        {"customer_id": "c1", "attribute_name": "color", "value_label": "red", "score": 0.3},
    ]
    client.post(f"/workspaces/{wid}/affinities", json=payload)

    response = client.get(f"/workspaces/{wid}/affinities?min_score=0.9")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Sort by score DESC  [NOT YET IMPLEMENTED — expected to fail]
# ---------------------------------------------------------------------------

def test_list_sorted_by_score_desc(client):
    ws = client.post("/workspaces", json={"name": "A12", "slug": "a12"}).json()
    wid = ws["id"]

    # Insert in ascending score order to confirm the response isn't just insertion order
    payload = [
        {"customer_id": "c1", "attribute_name": "tier",     "value_label": "bronze", "score": 0.10},
        {"customer_id": "c1", "attribute_name": "lifestyle", "value_label": "yoga",   "score": 0.80},
        {"customer_id": "c1", "attribute_name": "color",    "value_label": "red",    "score": 0.50},
    ]
    client.post(f"/workspaces/{wid}/affinities", json=payload)

    response = client.get(f"/workspaces/{wid}/affinities?sort=score_desc")
    assert response.status_code == 200
    data = response.json()
    scores = [r["score"] for r in data]
    assert scores == sorted(scores, reverse=True)
