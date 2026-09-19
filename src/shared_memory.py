"""Shared-memory / internal-interactions helpers."""

from __future__ import annotations

import json

import state
import env_store as _env_store
from validation import _is_valid_key_name, _validate_plaintext_value


_SHARED_VALUE_TYPES = ("string", "list", "dictionary", "integer", "float", "boolean")


def _normalize_shared_value(value, value_type: str):
    if value_type == "string":
        if not isinstance(value, str):
            raise ValueError("A string value must be provided as a string.")
        return value
    if value_type == "list":
        if not isinstance(value, list):
            raise ValueError("A list value must be provided as a list.")
        return value
    if value_type == "dictionary":
        if not isinstance(value, dict):
            raise ValueError("A dictionary value must be provided as a dictionary.")
        return value
    if value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("An integer value must be provided as an integer.")
        return value
    if value_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("A float value must be provided as a number.")
        return float(value)
    if value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("A boolean value must be provided as true or false.")
        return value
    raise ValueError("Unknown shared variable type.")


def _load_shared_memory() -> list[dict]:
    raw = _env_store.read_env_var("SHARED_MEMORY")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    variables: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if "name" not in entry or "type" not in entry or "value" not in entry:
            continue
        if entry["type"] not in _SHARED_VALUE_TYPES:
            continue
        variables.append({"name": entry["name"], "type": entry["type"], "value": entry["value"]})
    return variables


def _save_shared_memory(variables: list[dict]) -> None:
    _env_store.write_env_var("SHARED_MEMORY", json.dumps(variables))


def _list_shared_memory() -> list[dict]:
    # Check enabled via state
    if not (state.INTERNAL_INTERACTIONS and state.SHARED_MEMORY_ENABLED):
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The internal interactions functionality is disabled.")
    return _load_shared_memory()


def _create_shared_variable(name: str, value, value_type: str) -> dict:
    if not (state.INTERNAL_INTERACTIONS and state.SHARED_MEMORY_ENABLED):
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The internal interactions functionality is disabled.")
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid shared variable name.")
    value = _normalize_shared_value(value, value_type)
    value = _validate_plaintext_value(value, "shared variable value")
    variables = _load_shared_memory()
    if any(entry["name"].lower() == name.lower() for entry in variables):
        try:
            from auth import DuplicateNameError
        except ImportError:
            class DuplicateNameError(RuntimeError):
                pass
        raise DuplicateNameError("A shared variable with this name already exists.")
    entry = {"name": name, "type": value_type, "value": value}
    variables.append(entry)
    _save_shared_memory(variables)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return entry


def _update_shared_variable(name: str, value, value_type=None) -> dict | None:
    if not (state.INTERNAL_INTERACTIONS and state.SHARED_MEMORY_ENABLED):
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The internal interactions functionality is disabled.")
    variables = _load_shared_memory()
    target = next((entry for entry in variables if entry["name"] == name), None)
    if not target:
        return None
    new_type = value_type if value_type is not None else target["type"]
    target["value"] = _normalize_shared_value(value, new_type)
    target["value"] = _validate_plaintext_value(target["value"], "shared variable value")
    target["type"] = new_type
    _save_shared_memory(variables)
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass
    return target


def _delete_shared_variable(name: str) -> bool:
    if not (state.INTERNAL_INTERACTIONS and state.SHARED_MEMORY_ENABLED):
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The internal interactions functionality is disabled.")
    variables = _load_shared_memory()
    remaining = [entry for entry in variables if entry["name"] != name]
    if len(remaining) == len(variables):
        return False
    _save_shared_memory(remaining)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return True
