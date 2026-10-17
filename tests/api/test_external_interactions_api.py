"""Endpoint tests for external-interactions (firewall) endpoints."""

import firewall
import state


def test_incoming_ips_disabled(client, login_admin):
    login_admin(client)
    assert client.get("/api/external-interactions-incoming-ips").status_code == 403


def test_incoming_ips_list(client, login_admin):
    login_admin(client)
    state.EXTERNAL_INTERACTIONS = True
    response = client.get("/api/external-interactions-incoming-ips")
    assert response.status_code == 200
    assert response.get_json()["incomingIps"] == []


def test_allow_new_toggle(client, login_admin):
    login_admin(client)
    state.EXTERNAL_INTERACTIONS = True
    assert client.get("/api/external-interactions-allow-new").get_json()["externalInteractionsAllowNew"] is False
    response = client.post("/api/external-interactions-allow-new", json={"externalInteractionsAllowNew": True})
    assert response.get_json()["externalInteractionsAllowNew"] is True


def test_incoming_ip_patch(client, login_admin):
    login_admin(client)
    state.EXTERNAL_INTERACTIONS = True
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": [], "action": "unknown", "Note": ""},
    ])
    response = client.patch("/api/external-interactions-incoming-ips/1.2.3.4", json={"action": "allow"})
    assert response.status_code == 200
    assert response.get_json()["action"] == "allow"


def test_incoming_ip_delete(client, login_admin):
    login_admin(client)
    state.EXTERNAL_INTERACTIONS = True
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": [], "action": "unknown", "Note": ""},
    ])
    assert client.delete("/api/external-interactions-incoming-ips/1.2.3.4").status_code == 200
    assert client.delete("/api/external-interactions-incoming-ips/1.2.3.4").status_code == 404
