"""Endpoint tests for ``/api/shared-memory``."""

import state


def enable_shared():
    state.INTERNAL_INTERACTIONS = True
    state.SHARED_MEMORY_ENABLED = True


def test_create_shared_variable(client, login_admin):
    login_admin(client)
    enable_shared()
    response = client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": ["p1"], "reader": [],
    })
    assert response.status_code == 201


def test_create_shared_variable_requires_plugin(client, login_admin):
    login_admin(client)
    enable_shared()
    response = client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": [], "reader": [],
    })
    assert response.status_code == 400


def test_shared_memory_disabled(client, login_admin):
    login_admin(client)
    response = client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": ["p1"], "reader": [],
    })
    assert response.status_code == 403


def test_create_shared_variable_duplicate(client, login_admin):
    login_admin(client)
    enable_shared()
    client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": ["p1"], "reader": [],
    })
    response = client.post("/api/shared-memory", json={
        "name": "v", "value": "x", "type": "string", "editor": ["p2"], "reader": [],
    })
    assert response.status_code == 409


def test_list_shared_memory(client, login_admin):
    login_admin(client)
    enable_shared()
    client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": ["p1"], "reader": [],
    })
    response = client.get("/api/shared-memory")
    assert response.status_code == 200
    assert any(v["name"] == "v" for v in response.get_json()["sharedMemory"])


def test_update_shared_variable(client, login_admin):
    login_admin(client)
    enable_shared()
    client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": ["p1"], "reader": [],
    })
    response = client.patch("/api/shared-memory/v", json={"value": "world"})
    assert response.status_code == 200
    assert response.get_json()["value"] == "world"


def test_delete_shared_variable(client, login_admin):
    login_admin(client)
    enable_shared()
    client.post("/api/shared-memory", json={
        "name": "v", "value": "hello", "type": "string", "editor": ["p1"], "reader": [],
    })
    assert client.delete("/api/shared-memory/v").status_code == 200
    assert client.delete("/api/shared-memory/v").status_code == 404
