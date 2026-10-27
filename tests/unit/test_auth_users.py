"""Unit tests for user management in the auth layer."""

import json

import pytest

import auth
import env_store
import state

from conftest import SENTINEL_HASH, TEST_CLIENT_SALT


def test_register_user_appends():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    users = auth._load_users()
    assert any(
        u["username"] == "alice1234" and u["admin"] is False and u["root"] is False
        for u in users
    )


def test_register_user_min_length():
    with pytest.raises(ValueError):
        auth._register_user("short", SENTINEL_HASH, False, TEST_CLIENT_SALT)


def test_register_user_prohibited_chars():
    with pytest.raises(ValueError):
        auth._register_user("bad name", SENTINEL_HASH, False, TEST_CLIENT_SALT)


def test_register_user_duplicate():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    with pytest.raises(auth.UsernameTakenError):
        auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)


def test_register_user_invalid_hash():
    with pytest.raises(ValueError):
        auth._register_user("alice1234", "not-an-argon-hash", False, TEST_CLIENT_SALT)


def test_register_user_generates_secret_id():
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    user = next(u for u in auth._load_users() if u["username"] == "alice1234")
    assert user["id"]


def test_register_user_refuses_to_replace_a_malformed_user_list():
    # A malformed USERS value must survive a registration attempt: starting a fresh
    # list instead would silently destroy whatever the operator wrote.
    env_store.write_env_var("USERS", "not json")
    with pytest.raises(auth.PasswordStorageError):
        auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    assert env_store.read_env_var("USERS") == "not json"


def test_register_user_refuses_a_user_list_that_is_not_a_json_list():
    env_store.write_env_var("USERS", '{"username": "alice1234"}')
    with pytest.raises(auth.PasswordStorageError):
        auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    assert env_store.read_env_var("USERS") == '{"username": "alice1234"}'


def test_register_user_appends_to_a_list_that_holds_a_malformed_entry():
    # A malformed *entry* is not a malformed list: the entry is preserved and the
    # new account is appended beside it.
    env_store.write_env_var("USERS", json.dumps([{"username": "broken"}]))
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    stored = json.loads(env_store.read_env_var("USERS"))
    assert {"username": "broken"} in stored
    assert any(entry.get("username") == "alice1234" for entry in stored)


def test_register_user_accepts_an_absent_user_list():
    env_store.write_env_var("USERS", "")
    auth._register_user("alice1234", SENTINEL_HASH, False, TEST_CLIENT_SALT)
    assert [user["username"] for user in auth._load_users()] == ["alice1234"]


@pytest.mark.parametrize("change", [
    lambda: auth._rename_user("alice1234", "bob123456"),
    lambda: auth._set_user_admin("alice1234", True),
    lambda: auth._delete_user("alice1234"),
], ids=["rename", "admin", "delete"])
def test_user_changes_refuse_a_list_holding_an_unreadable_entry(change, make_user):
    # These changes rewrite USERS from the parsed accounts, so an entry parsing
    # would drop must block the change instead of vanishing from the file.
    make_user("alice1234", SENTINEL_HASH)
    data = json.loads(env_store.read_env_var("USERS"))
    env_store.write_env_var("USERS", json.dumps(data + [{"username": "broken"}]))
    before = env_store.read_env_var("USERS")
    with pytest.raises(auth.PasswordStorageError):
        change()
    assert env_store.read_env_var("USERS") == before


def test_user_changes_refuse_duplicate_entries(make_user):
    make_user("alice1234", SENTINEL_HASH)
    data = json.loads(env_store.read_env_var("USERS"))
    env_store.write_env_var("USERS", json.dumps(data + [dict(data[0])]))
    before = env_store.read_env_var("USERS")
    with pytest.raises(auth.PasswordStorageError):
        auth._delete_user("alice1234")
    assert env_store.read_env_var("USERS") == before


def test_user_changes_refuse_a_user_list_that_is_not_a_json_list(make_user):
    make_user("alice1234", SENTINEL_HASH)
    env_store.write_env_var("USERS", "not json")
    with pytest.raises(auth.PasswordStorageError):
        auth._rename_user("alice1234", "bob123456")
    assert env_store.read_env_var("USERS") == "not json"


def test_authenticate_user_case_insensitive(make_user):
    make_user("Alice1234", SENTINEL_HASH)
    assert auth._authenticate_user("ALICE1234", SENTINEL_HASH) is not None
    assert auth._authenticate_user("alice1234", "$argon2id$wrong") is None


def test_list_users_sanitised_and_sorted(make_user):
    make_user("zetauser", SENTINEL_HASH, admin=False)
    make_user("adminuser", SENTINEL_HASH, admin=True)
    make_user("rootuser", SENTINEL_HASH, admin=True, root=True)
    users = auth._list_users()
    assert [u["username"] for u in users] == ["rootuser", "adminuser", "zetauser"]
    for user in users:
        assert set(user.keys()) == {"username", "admin", "root"}


def test_set_user_admin_root_protection(make_user):
    make_user("rootuser", SENTINEL_HASH, admin=True, root=True)
    with pytest.raises(ValueError):
        auth._set_user_admin("rootuser", False)


def test_delete_user_root_protection(make_user):
    make_user("rootuser", SENTINEL_HASH, admin=True, root=True)
    with pytest.raises(ValueError):
        auth._delete_user("rootuser")


def test_delete_user(make_user):
    make_user("alice1234", SENTINEL_HASH)
    assert auth._delete_user("alice1234") is True
    assert auth._delete_user("alice1234") is False


def test_rename_user_duplicate(make_user):
    make_user("alice1234", SENTINEL_HASH)
    make_user("bob123456", SENTINEL_HASH)
    with pytest.raises(auth.UsernameTakenError):
        auth._rename_user("bob123456", "alice1234")


def test_rename_user_success(make_user):
    make_user("bob123456", SENTINEL_HASH)
    target = auth._rename_user("bob123456", "charlie123")
    assert target["username"] == "charlie123"


def test_is_root_username(make_user):
    make_user("rootuser", SENTINEL_HASH, root=True)
    assert auth._is_root_username("rootuser") is True
    assert auth._is_root_username("other") is False


def test_failed_login_counter():
    state._FAILED_LOGIN_ATTEMPTS = 0
    assert auth._register_failed_login_attempt() is True
    assert auth._register_failed_login_attempt() is True
    assert auth._register_failed_login_attempt() is True
    assert auth._register_failed_login_attempt() is False
    auth._reset_failed_login_attempts()
    assert auth._register_failed_login_attempt() is True
