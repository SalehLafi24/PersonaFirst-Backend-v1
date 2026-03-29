def test_create_user(client):
    response = client.post("/users", json={"email": "alice@example.com", "name": "Alice"})
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "alice@example.com"
    assert data["name"] == "Alice"
    assert "id" in data


def test_create_user_invalid_email(client):
    response = client.post("/users", json={"email": "not-an-email", "name": "Alice"})
    assert response.status_code == 422


def test_create_user_duplicate_email(client):
    client.post("/users", json={"email": "alice@example.com", "name": "Alice"})
    response = client.post("/users", json={"email": "alice@example.com", "name": "Alice 2"})
    assert response.status_code == 409
