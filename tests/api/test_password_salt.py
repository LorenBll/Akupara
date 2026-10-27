"""Endpoint tests for ``/api/password-salt``."""

from conftest import SENTINEL_HASH


def test_password_salt_is_public(client):
    response = client.post("/api/password-salt", json={"username": "nobody123"})
    assert response.status_code == 200
    data = response.get_json()
    assert set(data) == {"salt", "newSalt"}
    assert len(data["salt"]) == 22
    assert len(data["newSalt"]) == 22


def test_password_salt_does_not_reveal_existing_accounts(client, make_user):
    make_user("alice1234", SENTINEL_HASH)
    known = client.post("/api/password-salt", json={"username": "alice1234"}).get_json()
    unknown = client.post("/api/password-salt", json={"username": "nobody123"}).get_json()
    assert set(known) == set(unknown)
    # Two calls give the same "salt" for both known and unknown usernames.
    again = client.post("/api/password-salt", json={"username": "nobody123"}).get_json()
    assert unknown["salt"] == again["salt"]


def test_password_salt_requires_a_username(client):
    assert client.post("/api/password-salt", json={}).status_code == 400
    assert client.post("/api/password-salt", json={"username": "   "}).status_code == 400
    assert client.post("/api/password-salt", json={"username": 5}).status_code == 400
