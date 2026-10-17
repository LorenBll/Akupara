"""Unit tests for API-key management in the auth layer."""

import pytest

import auth
import state


def test_create_api_key_requires_enabled():
    state.API_KEYS_ENABLED = False
    with pytest.raises(auth.FeatureDisabledError):
        auth._create_api_key("my-key-name")


def test_create_api_key(enable_api_keys):
    enable_api_keys()
    entry = auth._create_api_key("my-key-name")
    assert entry["name"] == "my-key-name"
    assert entry["key"]


def test_create_api_key_duplicate(enable_api_keys):
    enable_api_keys()
    auth._create_api_key("dup-key")
    with pytest.raises(auth.DuplicateNameError):
        auth._create_api_key("dup-key")


def test_create_api_key_invalid_name(enable_api_keys):
    enable_api_keys()
    with pytest.raises(ValueError):
        auth._create_api_key("bad name")


def test_list_api_keys_strips_id(enable_api_keys):
    enable_api_keys()
    auth._create_api_key("list-key")
    keys = auth._list_api_keys()
    for key in keys:
        assert set(key.keys()) == {"name", "key"}


def test_delete_api_key(enable_api_keys):
    enable_api_keys()
    entry = auth._create_api_key("del-key")
    assert auth._delete_api_key(entry["key"]) is True
    assert auth._delete_api_key(entry["key"]) is False


def test_rename_api_key(enable_api_keys):
    enable_api_keys()
    entry = auth._create_api_key("old-key")
    renamed = auth._rename_api_key(entry["key"], "new-key")
    assert renamed["name"] == "new-key"


def test_rename_api_key_duplicate(enable_api_keys):
    enable_api_keys()
    first = auth._create_api_key("key-one")
    auth._create_api_key("key-two")
    with pytest.raises(auth.DuplicateNameError):
        auth._rename_api_key(first["key"], "key-two")


def test_api_key_encrypt_decrypt_roundtrip(enable_api_keys):
    enable_api_keys()
    encrypted = auth._api_key_encrypt("plain-secret")
    assert auth._api_key_decrypt(encrypted) == "plain-secret"
