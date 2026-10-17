"""Authorisation matrix for the three cookie/API-key decorators."""


def test_session_endpoint_requires_cookie(client):
    response = client.get("/api/session")
    assert response.status_code == 401
    assert response.get_json() == {"error": "API key required."}


def test_admin_endpoint_missing_session(client):
    assert client.get("/api/audio").status_code == 401


def test_admin_endpoint_non_admin(client, make_user, login):
    make_user("alice1234", admin=False)
    login(client, "alice1234")
    response = client.get("/api/audio")
    assert response.status_code == 403
    assert response.get_json() == {"error": "Admin privileges required."}


def test_admin_endpoint_admin(client, login_admin):
    login_admin(client)
    assert client.get("/api/audio").status_code == 200


def test_api_key_or_admin_accepts_api_key(client, make_api_key):
    key = make_api_key("authz-key")
    response = client.get("/api/api-keys", headers={"X-Api-Key": key})
    assert response.status_code == 200


def test_api_key_or_admin_accepts_admin_session(client, login_admin, enable_api_keys):
    login_admin(client)
    enable_api_keys()
    assert client.get("/api/api-keys").status_code == 200


def test_api_key_or_admin_rejects_non_admin(client, make_user, login):
    make_user("alice1234", admin=False)
    login(client, "alice1234")
    assert client.get("/api/api-keys").status_code == 401


def test_api_key_or_admin_rejects_no_auth(client):
    assert client.get("/api/api-keys").status_code == 401


def test_page_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
