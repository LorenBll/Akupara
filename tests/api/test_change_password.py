"""Endpoint tests for ``/api/change-password``."""

from conftest import SENTINEL_HASH, TEST_CLIENT_SALT


def test_change_password_success(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    response = client.post("/api/change-password", json={
        "current_password_hash": SENTINEL_HASH,
        "new_password_hash": "$argon2id$new-hash",
        "new_client_salt": TEST_CLIENT_SALT,
    })
    assert response.status_code == 200


def test_change_password_wrong_current(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    response = client.post("/api/change-password", json={
        "current_password_hash": "$argon2id$wrong",
        "new_password_hash": "$argon2id$new-hash",
        "new_client_salt": TEST_CLIENT_SALT,
    })
    assert response.status_code == 403
    assert response.get_json() == {"error": "Current password is incorrect."}


def test_change_password_invalid_new(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    response = client.post("/api/change-password", json={
        "current_password_hash": SENTINEL_HASH,
        "new_password_hash": "not-a-hash",
        "new_client_salt": TEST_CLIENT_SALT,
    })
    assert response.status_code == 400


def test_change_password_missing_salt(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    response = client.post("/api/change-password", json={
        "current_password_hash": SENTINEL_HASH,
        "new_password_hash": "$argon2id$new-hash",
    })
    assert response.status_code == 400


def test_change_password_requires_session(client):
    response = client.post("/api/change-password", json={
        "current_password_hash": "x",
        "new_password_hash": "$argon2id$new-hash",
        "new_client_salt": TEST_CLIENT_SALT,
    })
    assert response.status_code == 401
