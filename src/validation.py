"""Input validation helpers — plaintext, key names and event names."""

from __future__ import annotations

import re


_FORBIDDEN_KEY_NAME_CHARS: set[str] = set(" ,;:\\/%\"'")


def _validate_plaintext_string(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"The {field_name} must be a string.")
    if any(ch in value for ch in _FORBIDDEN_KEY_NAME_CHARS):
        raise ValueError(f"The {field_name} contains prohibited characters.")
    return value


def _validate_plaintext_value(value, field_name: str):
    if isinstance(value, str):
        return _validate_plaintext_string(value, field_name)
    if isinstance(value, list):
        return [_validate_plaintext_value(item, field_name) for item in value]
    if isinstance(value, dict):
        return {
            _validate_plaintext_string(key, f"{field_name} key"): _validate_plaintext_value(item, field_name)
            for key, item in value.items()
        }
    return value


def _is_valid_key_name(name: str) -> bool:
    return bool(name) and not any(ch in name for ch in _FORBIDDEN_KEY_NAME_CHARS)


def _is_valid_event_name(event: str) -> bool:
    return isinstance(event, str) and bool(event.strip()) and bool(re.match(r"[A-Za-z0-9_.-]+", event.strip()))
