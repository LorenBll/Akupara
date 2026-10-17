"""Endpoint tests for ``/api/api-keys``."""


def test_create_api_key(client, login_admin, enable_api_keys):
    login_admin(client)
    enable_api_keys()
    response = client.post("/api/api-keys", json={"name": "mykey"})
    assert response.status_code == 201
    data = response.get_json()
    assert data["name"] == "mykey"
    assert data["key"]


def test_create_api_key_disabled(client, login_admin):
    login_admin(client)
    assert client.post("/api/api-keys", json={"name": "mykey"}).status_code == 403


def test_create_api_key_duplicate(client, login_admin, enable_api_keys):
    login_admin(client)
    enable_api_keys()
    client.post("/api/api-keys", json={"name": "mykey"})
    assert client.post("/api/api-keys", json={"name": "mykey"}).status_code == 409


def test_list_api_keys_strips_id(client, login_admin, enable_api_keys):
    login_admin(client)
    enable_api_keys()
    client.post("/api/api-keys", json={"name": "mykey"})
    response = client.get("/api/api-keys")
    assert response.status_code == 200
    keys = response.get_json()["apiKeys"]
    assert any(k["name"] == "mykey" for k in keys)
    for key in keys:
        assert set(key.keys()) == {"name", "key"}


def test_rename_api_key(client, login_admin, enable_api_keys):
    login_admin(client)
    enable_api_keys()
    created = client.post("/api/api-keys", json={"name": "oldkey"}).get_json()
    response = client.patch(f"/api/api-keys/{created['key']}", json={"name": "newkey"})
    assert response.status_code == 200
    assert response.get_json()["name"] == "newkey"


def test_delete_api_key(client, login_admin, enable_api_keys):
    login_admin(client)
    enable_api_keys()
    created = client.post("/api/api-keys", json={"name": "delkey"}).get_json()
    assert client.delete(f"/api/api-keys/{created['key']}").status_code == 200
