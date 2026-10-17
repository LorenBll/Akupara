"""Endpoint tests for settings and feature toggles."""


def test_settings_get(client, login_admin):
    login_admin(client)
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert set(response.get_json().keys()) == {"internalInteractions", "displayPromotion", "externalInteractions"}


def test_settings_post_requires_admin(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    assert client.post("/api/settings", json={"displayPromotion": False}).status_code == 403


def test_settings_post_admin(client, login_admin):
    login_admin(client)
    response = client.post("/api/settings", json={"displayPromotion": False})
    assert response.status_code == 200
    assert response.get_json()["displayPromotion"] is False


def test_settings_post_invalid_bool(client, login_admin):
    login_admin(client)
    assert client.post("/api/settings", json={"displayPromotion": "not-a-bool"}).status_code == 400


def test_settings_post_unknown(client, login_admin):
    login_admin(client)
    assert client.post("/api/settings", json={}).status_code == 400


def test_audio_toggle(client, login_admin):
    login_admin(client)
    assert client.get("/api/audio").status_code == 200
    response = client.post("/api/audio", json={"playAudios": False})
    assert response.status_code == 200
    assert response.get_json()["playAudios"] is False


def test_api_keys_enabled_toggle(client, login_admin):
    login_admin(client)
    assert client.get("/api/api-keys-enabled").get_json()["apiKeysEnabled"] is False
    assert client.post("/api/api-keys-enabled", json={"apiKeysEnabled": True}).get_json()["apiKeysEnabled"] is True


def test_shared_memory_enabled_requires_internal(client, login_admin):
    login_admin(client)
    response = client.post("/api/shared-memory-enabled", json={"sharedMemoryEnabled": True})
    assert response.status_code == 403


def test_external_interactions_enabled_get(client, login_admin):
    login_admin(client)
    assert client.get("/api/external-interactions-enabled").get_json()["externalInteractions"] is False


def test_automatic_update_enabled_get(client, login_admin):
    login_admin(client)
    response = client.get("/api/automatic-update-enabled")
    assert response.status_code == 200
    assert response.get_json()["automaticUpdate"] is False
