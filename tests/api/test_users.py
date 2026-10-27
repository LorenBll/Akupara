"""Endpoint tests for ``/api/users``."""

import env_store

from conftest import SENTINEL_HASH, TEST_CLIENT_SALT


def test_register_user(client, login_admin):
    login_admin(client)
    response = client.post("/api/users", json={
        "username": "newuser123", "password_hash": SENTINEL_HASH, "client_salt": TEST_CLIENT_SALT, "admin": False,
    })
    assert response.status_code == 201


def test_register_user_duplicate(client, login_admin, make_user):
    login_admin(client)
    make_user("newuser123")
    response = client.post("/api/users", json={
        "username": "newuser123", "password_hash": SENTINEL_HASH, "client_salt": TEST_CLIENT_SALT, "admin": False,
    })
    assert response.status_code == 409


def test_register_user_short(client, login_admin):
    login_admin(client)
    response = client.post("/api/users", json={
        "username": "short", "password_hash": SENTINEL_HASH, "client_salt": TEST_CLIENT_SALT, "admin": False,
    })
    assert response.status_code == 400


def test_register_user_prohibited(client, login_admin):
    login_admin(client)
    response = client.post("/api/users", json={
        "username": "bad name", "password_hash": SENTINEL_HASH, "client_salt": TEST_CLIENT_SALT, "admin": False,
    })
    assert response.status_code == 400


def test_register_user_refuses_a_malformed_user_list(client, login_admin):
    # The endpoint reports unusable password storage instead of overwriting the
    # operator's malformed value.
    login_admin(client)
    env_store.write_env_var("USERS", "not json")
    response = client.post("/api/users", json={
        "username": "newuser123", "password_hash": SENTINEL_HASH, "client_salt": TEST_CLIENT_SALT, "admin": False,
    })
    assert response.status_code == 500
    assert response.get_json()["error"] == "Password storage is unavailable."
    assert env_store.read_env_var("USERS") == "not json"


def test_list_users_sanitised(client, login_admin):
    login_admin(client)
    response = client.get("/api/users")
    assert response.status_code == 200
    for user in response.get_json()["users"]:
        assert "password" not in user
        assert "id" not in user


def test_rename_user(client, login_admin, make_user):
    login_admin(client)
    make_user("someuser123")
    response = client.patch("/api/users/someuser123", json={"new_username": "renamed123"})
    assert response.status_code == 200
    assert response.get_json()["username"] == "renamed123"


def test_delete_self(client, login_admin):
    login_admin(client)
    assert client.delete("/api/users/adminuser").status_code == 403


def test_change_self_admin(client, login_admin):
    login_admin(client)
    assert client.patch("/api/users/adminuser", json={"admin": False}).status_code == 403


def test_delete_user(client, login_admin, make_user):
    login_admin(client)
    make_user("victim123")
    assert client.delete("/api/users/victim123").status_code == 200
