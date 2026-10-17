"""Endpoint tests for login, logout and session."""


def test_login_success_issues_session(client, make_user, login):
    make_user("alice1234")
    response = login(client, "alice1234")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.get_json() == {"username": "alice1234", "admin": False, "root": False}


def test_login_admin_session(client, login_admin):
    login_admin(client)
    session = client.get("/api/session").get_json()
    assert session["admin"] is True


def test_login_failure(client, make_user, login):
    make_user("alice1234")
    response = login(client, "alice1234", password_hash="$argon2id$wrong")
    assert response.status_code == 401
    assert response.get_json() == {"error": "Invalid credentials."}


def test_login_when_not_configured(client, login):
    response = login(client, "nobody123")
    assert response.status_code == 403
    assert response.get_json() == {"error": "Login is not configured."}


def test_logout_clears_session(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/session").status_code == 401
