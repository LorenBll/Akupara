"""Unit tests for input-validation helpers."""

import pytest

from validation import (
    _FORBIDDEN_KEY_NAME_CHARS,
    _is_valid_event_name,
    _is_valid_key_name,
    _validate_plaintext_string,
    _validate_plaintext_value,
)


def test_is_valid_key_name_accepts_clean():
    assert _is_valid_key_name("my-key") is True
    assert _is_valid_key_name("a") is True
    assert _is_valid_key_name("Plugin_Name.1") is True


def test_is_valid_key_name_rejects_empty():
    assert _is_valid_key_name("") is False
    assert _is_valid_key_name("   ") is False


def test_is_valid_key_name_rejects_forbidden_chars():
    for char in _FORBIDDEN_KEY_NAME_CHARS:
        assert _is_valid_key_name(f"key{char}name") is False


def test_is_valid_event_name():
    assert _is_valid_event_name("event_name-1.2") is True
    assert _is_valid_event_name("") is False
    assert _is_valid_event_name("   ") is False
    # ``re.match`` anchors at the start only, so a leading invalid character
    # fails while a trailing one does not.
    assert _is_valid_event_name("!bad") is False
    assert _is_valid_event_name("bad event") is True
    assert _is_valid_event_name(123) is False


def test_validate_plaintext_string():
    assert _validate_plaintext_string("ok", "field") == "ok"
    with pytest.raises(ValueError):
        _validate_plaintext_string("has space", "field")
    with pytest.raises(ValueError):
        _validate_plaintext_string(123, "field")


def test_validate_plaintext_value_recurses():
    assert _validate_plaintext_value({"a": ["b", "c"]}, "field") == {"a": ["b", "c"]}
    with pytest.raises(ValueError):
        _validate_plaintext_value({"a": ["b c"]}, "field")
    # Non-string/container values pass through untouched.
    assert _validate_plaintext_value(5, "field") == 5
