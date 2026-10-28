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
#   * pepper     + new installation        -> replace
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


def test_new_installation_replaces_a_pepper_left_by_a_previous_installation():
    _forget_installation()
    previous = base64.b64encode(b"p" * 32).decode("ascii")
    auth._password_pepper_path().write_text(previous + "\n", encoding="utf-8")
    assert auth._initialize_password_storage() == "replaced"
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

def test_default_root_user_is_not_created_with_a_predictable_password():
    assert auth._ensure_default_root_user() is False
    assert _users() == []


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


def test_default_root_user_is_not_created_on_repeated_calls():
    assert auth._ensure_default_root_user() is False
    assert auth._ensure_default_root_user() is False
    assert _users() == []


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


# --- malformed stored verifiers ---------------------------------------------
#
# A verifier in the peppered format that is nevertheless corrupt (bad base64,
# missing parts, wrong salt length) must be reported as a wrong password, not as
# a storage error: only a value that is not in the peppered format at all is a
# configuration error. These tests pin the exact boundaries of that rule.

@pytest.mark.parametrize("stored", [
    "$argon2id$v=19$m=65536,t=2,p=1$YWJj$ZGVm",     # raw hash, not wrapped
    "akupara$2$" + "A" * 22,                       # no separator between salt and tag
    "akupara$2$$" + "A" * 22,                      # empty wrap salt
    "akupara$2$" + "A" * 22 + "$",                 # empty tag
    "akupara$2$not base64!$" + "A" * 22,           # undecodable wrap salt
    "akupara$2$" + "A" * 22 + "$not base64!",      # undecodable tag
    "akupara$2$" + auth._b64url_encode(b"short") + "$" + "A" * 22,  # salt is 5 bytes
])
def test_malformed_verifiers_never_verify(stored):
    assert auth._verify_wrapped_password(stored, SENTINEL_HASH) is False


def test_malformed_wrapped_verifier_is_a_wrong_password_not_a_storage_error():
    # A corrupt verifier that still claims the peppered format is indistinguishable
    # from a wrong password at the login form, so it must answer 401 and not 500.
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    entry = _entry("alice1234")
    entry["password"] = "akupara$2$" + "A" * 22 + "$not base64!"
    env_store.write_env_var("USERS", json.dumps([entry]))
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is None


def test_entry_without_a_password_is_a_storage_error():
    with pytest.raises(auth.PasswordStorageError):
        auth._verify_stored_password({"username": "alice1234"}, SENTINEL_HASH)


def test_a_verifier_is_bound_to_the_installation_pepper():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is not None
    # A new installation mints a fresh pepper, and the old verifier cannot be
    # checked against it, which is why a re-setup invalidates every account.
    _forget_installation()
    assert auth._initialize_password_storage() == "created"
    assert auth._authenticate_user("alice1234", SENTINEL_HASH) is None


def test_verification_requires_the_pepper(make_user):
    make_user("alice1234", SENTINEL_HASH)
    auth._password_pepper_path().unlink(missing_ok=True)
    auth._reset_password_pepper_cache()
    with pytest.raises(auth.PasswordStorageError):
        auth._authenticate_user("alice1234", SENTINEL_HASH)


def test_ensure_password_pepper_reads_the_file_when_the_cache_is_empty():
    pepper = auth._ensure_password_pepper()
    auth._reset_password_pepper_cache()
    assert auth._ensure_password_pepper() == pepper


# --- browser hashing ---------------------------------------------------------

def test_client_password_hash_uses_the_browser_parameters():
    # The parameter block the browser writes into its encoded hash; the bootstrap
    # account is only usable while the two stay identical.
    value = auth._client_password_hash("secret", "abcdefghijklmnopqrstuv")
    assert value.startswith("$argon2id$v=19$m=65536,t=2,p=1$YWJjZGVmZ2hpamtsbW5vcHFyc3R1dg$")


def test_client_password_hash_depends_on_both_the_password_and_the_salt():
    first = auth._client_password_hash("secret", "aaaaaaaaaaaaaaaaaaaaaa")
    assert first != auth._client_password_hash("secret", "bbbbbbbbbbbbbbbbbbbbbb")
    assert first != auth._client_password_hash("secreu", "aaaaaaaaaaaaaaaaaaaaaa")


# --- client salts -----------------------------------------------------------

def test_registration_rejects_malformed_client_salt():
    with pytest.raises(ValueError):
        auth._register_user("alice1234", SENTINEL_HASH, False, "too-short")


def test_accepts_a_generated_client_salt():
    assert auth._is_valid_client_salt(auth._new_client_salt()) is True
    assert auth._is_valid_client_salt(TEST_CLIENT_SALT) is True


@pytest.mark.parametrize("value", [
    None,
    "",
    123,
    "x" * 65,
    "AAAA",            # decodes to 3 bytes
    "not base64!",
])
def test_rejects_malformed_client_salts(value):
    assert auth._is_valid_client_salt(value) is False


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


# --- stored users -----------------------------------------------------------
#
# ``USERS`` is operator-editable, so loading must tolerate anything it finds
# without crashing and without inventing accounts.

@pytest.mark.parametrize("raw", [
    "{not json",
    '{"username": "alice1234"}',
])
def test_loading_tolerates_a_value_that_is_not_a_list(raw):
    env_store.write_env_var("USERS", raw)
    assert auth._load_users() == []


def test_loading_ignores_malformed_entries_and_duplicates():
    env_store.write_env_var("USERS", json.dumps([
        "not an entry",
        {"username": "", "password": "akupara$2$salt$tag"},
        {"username": "alice1234"},
        {"username": "alice1234", "password": "akupara$2$salt$tag", "client_salt": TEST_CLIENT_SALT},
        {"username": "ALICE1234", "password": "akupara$2$other$tag", "client_salt": TEST_CLIENT_SALT},
    ]))
    users = auth._load_users()
    assert [user["username"] for user in users] == ["alice1234"]
    assert users[0]["password"] == "akupara$2$salt$tag"


def test_authentication_scans_past_other_users_and_ignores_case(make_user):
    make_user("bob123456", SENTINEL_HASH)
    make_user("alice1234", SENTINEL_HASH)
    assert auth._authenticate_user("ALICE1234", SENTINEL_HASH)["username"] == "alice1234"


def test_authentication_returns_none_for_an_unknown_username(make_user):
    make_user("alice1234", SENTINEL_HASH)
    assert auth._authenticate_user("nobody123", SENTINEL_HASH) is None
