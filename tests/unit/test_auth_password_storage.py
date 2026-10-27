"""Unit tests for password storage: peppered verifiers and per-account salts."""

import base64
import json

import pytest

import auth
import env_store

from conftest import SENTINEL_HASH, TEST_CLIENT_SALT


def _users() -> list[dict]:
    raw = env_store.read_env_var("USERS") or "[]"
    return json.loads(raw)


def _entry(username: str) -> dict:
    return next(user for user in _users() if user["username"] == username)


# --- pepper -----------------------------------------------------------------

def test_pepper_created_with_32_bytes():
    pepper = auth._ensure_password_pepper()
    assert len(pepper) == 32
    path = auth._password_pepper_path()
    assert path.is_file()
    assert len(base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)) == 32


def test_pepper_is_stable_across_calls():
    first = auth._ensure_password_pepper()
    auth._reset_password_pepper_cache()
    assert auth._ensure_password_pepper() == first


def test_invalid_pepper_fails_closed():
    auth._password_pepper_path().write_text("not base64!", encoding="utf-8")
    auth._reset_password_pepper_cache()
    with pytest.raises(auth.PasswordStorageError):
        auth._ensure_password_pepper()


# --- stored verifiers -------------------------------------------------------

def test_registration_stores_a_wrapped_verifier():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    entry = _entry("alice1234")
    assert entry["password"].startswith("akupara$2$")
    assert SENTINEL_HASH not in entry["password"]
    assert entry["client_salt"] == TEST_CLIENT_SALT


def test_stored_verifier_is_not_a_replayable_credential():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    stored = _entry("alice1234")["password"]
    # Pass-the-hash: presenting the stored value must not authenticate.
    assert auth._authenticate_user("alice1234", stored) is None
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is not None


def test_same_client_hash_yields_different_stored_verifiers(make_user):
    make_user("alice1234", SENTINEL_HASH)
    make_user("bob123456", SENTINEL_HASH)
    assert _entry("alice1234")["password"] != _entry("bob123456")["password"]


def test_raw_hash_entry_is_rejected_as_a_storage_error():
    # The old fixed-salt format is no longer supported: it must be reported, not
    # mistaken for a wrong password.
    env_store.write_env_var("USERS", json.dumps([{
        "username": "alice1234",
        "password": SENTINEL_HASH,
        "client_salt": TEST_CLIENT_SALT,
        "admin": False,
        "root": False,
    }]))
    with pytest.raises(auth.PasswordStorageError):
        auth._authenticate_user("alice1234", SENTINEL_HASH)


def test_login_does_not_rewrite_the_stored_verifier(make_user):
    make_user("alice1234", SENTINEL_HASH)
    before = _entry("alice1234")
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is not None
    assert _entry("alice1234") == before


def test_tampered_verifier_is_rejected():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    entry = _entry("alice1234")
    stored = entry["password"]
    flipped = "A" if stored[-1] != "A" else "B"
    entry["password"] = stored[:-1] + flipped
    env_store.write_env_var("USERS", json.dumps([entry]))
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is None


def test_wrong_client_hash_is_rejected():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    assert auth._authenticate_user("alice1234", "$argon2id$other") is None


# --- client salts -----------------------------------------------------------

def test_registration_rejects_malformed_client_salt():
    with pytest.raises(ValueError):
        auth._register_user("alice1234", SENTINEL_HASH, False, "too-short")


def test_change_password_rejects_malformed_client_salt(make_user):
    make_user("alice1234", SENTINEL_HASH)
    with pytest.raises(ValueError):
        auth._change_password("alice1234", SENTINEL_HASH, "$argon2id$new", "too-short")


def test_salt_endpoint_is_stable_and_opaque_for_unknown_users():
    first = auth._password_salts_for_username("nobody123")
    second = auth._password_salts_for_username("nobody123")
    # ``salt`` is the enumeration-sensitive value and must never change for a
    # given username; ``newSalt`` is deliberately fresh on every call.
    assert first["salt"] == second["salt"]
    assert first["newSalt"] != second["newSalt"]
    assert len(first["salt"]) == 22
    assert len(first["newSalt"]) == 22


def test_salt_endpoint_returns_the_stored_client_salt():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    assert auth._password_salts_for_username("alice1234")["salt"] == TEST_CLIENT_SALT


def test_salt_endpoint_rejects_an_account_without_a_client_salt(make_user):
    make_user("alice1234", SENTINEL_HASH)
    users = _users()
    users[0].pop("client_salt")
    env_store.write_env_var("USERS", json.dumps(users))
    with pytest.raises(auth.PasswordStorageError):
        auth._password_salts_for_username("alice1234")


def test_renaming_an_account_keeps_its_password_usable():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    auth._rename_user("alice1234", "alice5678")
    assert auth._password_salts_for_username("alice5678")["salt"] == TEST_CLIENT_SALT
    assert auth._authenticate_user("alice5678", SENTINEL_HASH) is not None


def test_password_change_replaces_both_the_verifier_and_the_salt(make_user):
    make_user("alice1234", SENTINEL_HASH, client_salt="aaaaaaaaaaaaaaaaaaaaaa")
    auth._change_password("alice1234", SENTINEL_HASH, "$argon2id$new", TEST_CLIENT_SALT)
    entry = _entry("alice1234")
    assert entry["password"].startswith("akupara$2$")
    assert entry["client_salt"] == TEST_CLIENT_SALT
    assert auth._authenticate_user("alice1234", "$argon2id$new") is not None
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is None
