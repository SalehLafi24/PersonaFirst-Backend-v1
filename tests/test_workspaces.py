def test_create_workspace(client):
    response = client.post("/workspaces", json={"name": "Acme", "slug": "acme"})
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Acme"
    assert data["slug"] == "acme"
    assert "id" in data


def test_list_workspaces_empty(client):
    response = client.get("/workspaces")
    assert response.status_code == 200
    assert response.json() == []


def test_list_workspaces(client):
    client.post("/workspaces", json={"name": "Acme", "slug": "acme"})
    client.post("/workspaces", json={"name": "Beta", "slug": "beta"})
    response = client.get("/workspaces")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_create_workspace_duplicate_slug(client):
    client.post("/workspaces", json={"name": "Acme", "slug": "acme"})
    response = client.post("/workspaces", json={"name": "Acme 2", "slug": "acme"})
    assert response.status_code == 409
