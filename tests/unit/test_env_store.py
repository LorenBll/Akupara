"""Unit tests for the ``.env`` persistence helpers."""

import pytest

import env_store


def test_write_and_read_roundtrip():
    env_store.write_env_var("FOO", "bar")
    assert env_store.read_env_var("FOO") == "bar"


def test_write_updates_existing():
    env_store.write_env_var("FOO", "one")
    env_store.write_env_var("FOO", "two")
    assert env_store.read_env_var("FOO") == "two"


def test_read_missing_returns_default():
    assert env_store.read_env_var("DOES_NOT_EXIST", "dflt") == "dflt"


def test_write_rejects_newline():
    with pytest.raises(ValueError):
        env_store.write_env_var("FOO", "a\nb")
    with pytest.raises(ValueError):
        env_store.write_env_var("FO\nO", "bar")


def test_write_bool_roundtrip():
    env_store.write_env_bool("BOOL", True)
    assert env_store.read_env_bool("BOOL") is True
    env_store.write_env_bool("BOOL", False)
    assert env_store.read_env_bool("BOOL") is False


def test_parse_bool():
    assert env_store._parse_bool("true") is True
    assert env_store._parse_bool("1") is True
    assert env_store._parse_bool("yes") is True
    assert env_store._parse_bool("on") is True
    assert env_store._parse_bool("TRUE") is True
    assert env_store._parse_bool("false") is False
    assert env_store._parse_bool("0") is False
    assert env_store._parse_bool("no") is False
    assert env_store._parse_bool(None) is False
    assert env_store._parse_bool(None, True) is True
