"""Endpoint tests for ``/api/plugin-events``."""

import state


def test_plugin_events_disabled(client, login_admin):
    login_admin(client)
    assert client.get("/api/plugin-events").status_code == 403


def test_plugin_event_create(client, login_admin):
    login_admin(client)
    state.INTERNAL_INTERACTIONS = True
    response = client.post("/api/plugin-events", json={"event": "event1", "plugin": "plugin1"})
    assert response.status_code == 201
    assert response.get_json()["event"] == "event1"


def test_plugin_event_duplicate(client, login_admin):
    login_admin(client)
    state.INTERNAL_INTERACTIONS = True
    client.post("/api/plugin-events", json={"event": "event1", "plugin": "plugin1"})
    response = client.post("/api/plugin-events", json={"event": "event1", "plugin": "plugin2"})
    assert response.status_code == 409


def test_plugin_event_add_plugin(client, login_admin):
    login_admin(client)
    state.INTERNAL_INTERACTIONS = True
    client.post("/api/plugin-events", json={"event": "event1"})
    response = client.post("/api/plugin-events/event1/plugins", json={"plugin": "plugin1"})
    assert response.status_code == 201


def test_plugin_event_delete(client, login_admin):
    login_admin(client)
    state.INTERNAL_INTERACTIONS = True
    client.post("/api/plugin-events", json={"event": "event1"})
    assert client.delete("/api/plugin-events/event1").status_code == 200
