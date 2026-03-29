def test_add_member(client):
    ws = client.post("/workspaces", json={"name": "Acme", "slug": "acme"}).json()
    user = client.post("/users", json={"email": "alice@example.com", "name": "Alice"}).json()

    response = client.post(
        f"/workspaces/{ws['id']}/members",
        json={"user_id": user["id"], "role": "admin"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["workspace_id"] == ws["id"]
    assert data["user_id"] == user["id"]
    assert data["role"] == "admin"


def test_add_member_workspace_not_found(client):
    user = client.post("/users", json={"email": "alice@example.com", "name": "Alice"}).json()
    response = client.post("/workspaces/999/members", json={"user_id": user["id"], "role": "member"})
    assert response.status_code == 404


def test_add_member_default_role(client):
    ws = client.post("/workspaces", json={"name": "Acme", "slug": "acme"}).json()
    user = client.post("/users", json={"email": "alice@example.com", "name": "Alice"}).json()

    response = client.post(
        f"/workspaces/{ws['id']}/members",
        json={"user_id": user["id"]},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "member"
