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
#
# The pepper belongs to one installation, and an installation is one setup run.
# ``resources/installation.json`` records that the installation has run; the
# setup scripts delete it, so the next start counts as a new installation.
#   * no pepper  + new installation        -> create
#   * no pepper  + installation has run    -> error (the process must not start)
#   * pepper     + new installation        -> rotate
#   * pepper     + installation has run    -> keep

def _pepper_bytes() -> bytes:
    raw = auth._password_pepper_path().read_text(encoding="utf-8").strip()
    return base64.b64decode(raw, validate=True)


def _forget_installation() -> None:
    """Turn the per-test installation back into one that has never run."""
    auth._password_pepper_path().unlink(missing_ok=True)
    auth._installation_marker_path().unlink(missing_ok=True)
    auth._reset_password_pepper_cache()


def test_new_installation_creates_a_pepper():
    _forget_installation()
    assert auth._initialize_password_storage() == "created"
    assert auth._password_pepper_path().is_file()
    assert len(_pepper_bytes()) == 32
    assert auth._installation_marker_path().is_file()


def test_new_installation_rotates_a_pepper_left_by_a_previous_installation():
    _forget_installation()
    previous = base64.b64encode(b"p" * 32).decode("ascii")
    auth._password_pepper_path().write_text(previous + "\n", encoding="utf-8")
    assert auth._initialize_password_storage() == "rotated"
    assert _pepper_bytes() != base64.b64decode(previous)
    assert auth._installation_marker_path().is_file()


def test_existing_installation_keeps_its_pepper():
    first = auth._ensure_password_pepper()
    auth._reset_password_pepper_cache()
    assert auth._initialize_password_storage() == "kept"
    assert auth._ensure_password_pepper() == first


def test_existing_installation_without_a_pepper_fails():
    auth._password_pepper_path().unlink(missing_ok=True)
    auth._reset_password_pepper_cache()
    with pytest.raises(auth.PasswordStorageError):
        auth._initialize_password_storage()
    assert not auth._password_pepper_path().exists()


def test_existing_installation_with_a_malformed_pepper_fails():
    auth._password_pepper_path().write_text("not base64!", encoding="utf-8")
    auth._reset_password_pepper_cache()
    with pytest.raises(auth.PasswordStorageError):
        auth._initialize_password_storage()
    # The malformed pepper is not silently replaced: the installation has run,
    # so its stored password verifiers depend on whatever this file should hold.
    assert auth._password_pepper_path().read_text(encoding="utf-8") == "not base64!"


def test_initialisation_is_idempotent_within_one_installation():
    pepper = auth._ensure_password_pepper()
    for _ in range(3):
        assert auth._initialize_password_storage() == "kept"
        assert auth._ensure_password_pepper() == pepper


def test_ensure_password_pepper_never_creates_a_file():
    # Minting is the startup policy's job; a missing pepper here is an error.
    _forget_installation()
    with pytest.raises(auth.PasswordStorageError):
        auth._ensure_password_pepper()
    assert not auth._password_pepper_path().exists()


# --- bootstrap account ------------------------------------------------------

def test_default_root_user_is_created_when_no_user_exists():
    assert auth._ensure_default_root_user() is True
    entry = _entry("admin")
    assert entry["admin"] is True
    assert entry["root"] is True
    assert entry["password"].startswith("akupara$2$")
    assert len(_users()) == 1


def test_default_root_user_can_log_in_with_its_documented_password():
    auth._ensure_default_root_user()
    entry = _entry("admin")
    client_hash = auth._client_password_hash("admin123", entry["client_salt"])
    assert auth._authenticate_user("admin", client_hash) is not None


def test_default_root_password_matches_the_browser_encoding():
    # The exact string ui/js/argon2 produces for this salt. If the parameters or
    # the salt encoding drift, the bootstrapped account stops being usable, so
    # this value is pinned rather than recomputed.
    assert auth._client_password_hash("admin123", "abcdefghijklmnopqrstuv") == (
        "$argon2id$v=19$m=65536,t=2,p=1$YWJjZGVmZ2hpamtsbW5vcHFyc3R1dg"
        "$6oKTX8+P9Rf7dEezBuVpEpwTOSocqCR+Oj/Pjf4lnPE"
    )


def test_default_root_user_is_not_created_when_a_user_exists(make_user):
    make_user("alice1234", SENTINEL_HASH)
    assert auth._ensure_default_root_user() is False
    assert [user["username"] for user in _users()] == ["alice1234"]


def test_default_root_user_leaves_malformed_users_untouched():
    env_store.write_env_var("USERS", "{not json")
    assert auth._ensure_default_root_user() is False
    assert env_store.read_env_var("USERS") == "{not json"


def test_default_root_user_is_not_duplicated_on_a_second_call():
    assert auth._ensure_default_root_user() is True
    assert auth._ensure_default_root_user() is False
    assert len(_users()) == 1


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
    # The tag is compared as decoded bytes, and base64url encodes 16 bytes in 22
    # characters whose last one carries only two significant bits: flipping that
    # last character often leaves the bytes identical. The first character is
    # fully significant, so the tamper always changes the decoded tag.
    prefix, tag = stored.rsplit("$", 1)
    flipped = "A" if tag[0] != "A" else "B"
    tampered = f"{prefix}${flipped}{tag[1:]}"
    assert auth._b64url_decode(tampered.rsplit("$", 1)[1]) != auth._b64url_decode(tag)
    entry["password"] = tampered
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
