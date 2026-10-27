"""Unit tests for password change in the auth layer."""

import pytest

import auth

from conftest import SENTINEL_HASH, TEST_CLIENT_SALT


def test_change_password_success(make_user):
    make_user("alice1234", SENTINEL_HASH)
    auth._change_password("alice1234", SENTINEL_HASH, "$argon2id$new-hash", TEST_CLIENT_SALT)
    assert auth._authenticate_user("alice1234", "$argon2id$new-hash") is not None


def test_change_password_wrong_current(make_user):
    make_user("alice1234", SENTINEL_HASH)
    with pytest.raises(auth.CurrentPasswordError):
        auth._change_password("alice1234", "$argon2id$wrong", "$argon2id$new-hash", TEST_CLIENT_SALT)


def test_change_password_missing_account():
    with pytest.raises(auth.AccountNotFoundError):
        auth._change_password("nobody123", SENTINEL_HASH, "$argon2id$new-hash", TEST_CLIENT_SALT)
