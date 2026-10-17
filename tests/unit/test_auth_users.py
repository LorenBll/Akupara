"""Unit tests for user management in the auth layer."""

import pytest

import auth
import state

from conftest import SENTINEL_HASH


def test_register_user_appends():
    auth._register_user("alice1234", SENTINEL_HASH, False)
    users = auth._load_users()
    assert any(
        u["username"] == "alice1234" and u["admin"] is False and u["root"] is False
        for u in users
    )


def test_register_user_min_length():
    with pytest.raises(ValueError):
        auth._register_user("short", SENTINEL_HASH, False)


def test_register_user_prohibited_chars():
    with pytest.raises(ValueError):
        auth._register_user("bad name", SENTINEL_HASH, False)


def test_register_user_duplicate():
    auth._register_user("alice1234", SENTINEL_HASH, False)
    with pytest.raises(auth.UsernameTakenError):
        auth._register_user("alice1234", SENTINEL_HASH, False)


def test_register_user_invalid_hash():
    with pytest.raises(ValueError):
        auth._register_user("alice1234", "not-an-argon-hash", False)


def test_register_user_generates_secret_id():
    auth._register_user("alice1234", SENTINEL_HASH, False)
    user = next(u for u in auth._load_users() if u["username"] == "alice1234")
    assert user["id"]


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
