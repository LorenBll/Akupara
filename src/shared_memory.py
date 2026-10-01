"""Shared-memory / internal-interactions helpers."""

from __future__ import annotations

import json

import state
import env_store as _env_store
from validation import _is_valid_key_name, _validate_plaintext_value


_SHARED_VALUE_TYPES = ("string", "list", "dictionary", "integer", "float", "boolean")

_MISSING = object()


def _normalize_plugins_list(plugins) -> list[str]:
    if plugins is None:
        return []
    if not isinstance(plugins, list):
        raise ValueError("Invalid plugins list.")
    result: list[str] = []
    seen: set[str] = set()
    for p in plugins:
        if not isinstance(p, str):
            raise ValueError("Invalid plugin name.")
        p = p.strip()
        if not p:
            continue
        if not _is_valid_key_name(p):
            raise ValueError("Invalid plugin name.")
        key = p.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return sorted(result, key=lambda x: x.casefold())


def _resolve_plugin_exclusivity(editor: list[str], reader: list[str]) -> tuple[list[str], list[str]]:
    """Keep a plugin present in both lists only in the editor list."""
    editor_keys = {p.casefold() for p in editor}
    reader = [p for p in reader if p.casefold() not in editor_keys]
    return editor, reader


class SharedVariableAccessError(PermissionError):
    """Raised when a plugin is not allowed to read or edit a shared variable."""


def _shared_variable_role_for_plugin(plugin_name: str, variable: dict) -> str | None:
    """Return ``'editor'``, ``'reader'`` or ``None`` for *plugin_name* on *variable*."""
    p = str(plugin_name).casefold()
    for e in variable.get("editor", []):
        if str(e).casefold() == p:
            return "editor"
    for r in variable.get("reader", []):
        if str(r).casefold() == p:
            return "reader"
    return None


def _require_shared_variable_read(plugin_name: str, variable: dict, privileged: bool = False) -> None:
    """Raise unless *plugin_name* may read *variable* (editor, reader or privileged)."""
    if privileged:
        return
    if _shared_variable_role_for_plugin(plugin_name, variable) is None:
        raise SharedVariableAccessError("The plugin has no access to this shared variable.")


def _require_shared_variable_edit(plugin_name: str, variable: dict, privileged: bool = False) -> None:
    """Raise unless *plugin_name* may edit *variable* (editor or privileged)."""
    if privileged:
        return
    if _shared_variable_role_for_plugin(plugin_name, variable) != "editor":
        raise SharedVariableAccessError("The plugin is not an editor of this shared variable.")


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
        try:
            editor = _normalize_plugins_list(entry.get("editor"))
        except ValueError:
            editor = []
        try:
            reader = _normalize_plugins_list(entry.get("reader"))
        except ValueError:
            reader = []
        variables.append({
            "name": entry["name"],
            "type": entry["type"],
            "value": entry["value"],
            "editor": editor,
            "reader": reader,
        })
    return variables


def _save_shared_memory(variables: list[dict]) -> None:
    _env_store.write_env_var("SHARED_MEMORY", json.dumps(variables))


def _list_shared_memory(plugin_name: str | None = None, privileged: bool = False) -> list[dict]:
    # Check enabled via state
    if not (state.INTERNAL_INTERACTIONS and state.SHARED_MEMORY_ENABLED):
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The internal interactions functionality is disabled.")
    variables = _load_shared_memory()
    if privileged:
        return variables
    return [v for v in variables if _shared_variable_role_for_plugin(plugin_name, v) is not None]


def _get_shared_variable(name: str, plugin_name: str | None = None, privileged: bool = False) -> dict | None:
    if not (state.INTERNAL_INTERACTIONS and state.SHARED_MEMORY_ENABLED):
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The internal interactions functionality is disabled.")
    target = next((v for v in _load_shared_memory() if v["name"] == name), None)
    if target is None:
        return None
    _require_shared_variable_read(plugin_name, target, privileged)
    return target


def _create_shared_variable(name: str, value, value_type: str, editor=None, reader=None, plugin_name: str | None = None, privileged: bool = False) -> dict:
    if not privileged:
        raise SharedVariableAccessError("Creating a shared variable requires an API key or admin authorisation.")
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
    editor = _normalize_plugins_list(editor)
    reader = _normalize_plugins_list(reader)
    editor, reader = _resolve_plugin_exclusivity(editor, reader)
    if not editor and not reader:
        raise ValueError("At least one plugin is required.")
    variables = _load_shared_memory()
    if any(entry["name"].lower() == name.lower() for entry in variables):
        try:
            from auth import DuplicateNameError
        except ImportError:
            class DuplicateNameError(RuntimeError):
                pass
        raise DuplicateNameError("A shared variable with this name already exists.")
    entry = {"name": name, "type": value_type, "value": value, "editor": editor, "reader": reader}
    variables.append(entry)
    _save_shared_memory(variables)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return entry


def _update_shared_variable(name: str, value=_MISSING, value_type=_MISSING, editor=_MISSING, reader=_MISSING, plugin_name: str | None = None, privileged: bool = False) -> dict | None:
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
    _require_shared_variable_edit(plugin_name, target, privileged)
    if value is not _MISSING or value_type is not _MISSING:
        new_type = value_type if value_type is not _MISSING else target["type"]
        new_value = value if value is not _MISSING else target["value"]
        target["value"] = _normalize_shared_value(new_value, new_type)
        target["value"] = _validate_plaintext_value(target["value"], "shared variable value")
        target["type"] = new_type
    if editor is not _MISSING:
        target["editor"] = _normalize_plugins_list(editor)
    if reader is not _MISSING:
        target["reader"] = _normalize_plugins_list(reader)
    target["editor"], target["reader"] = _resolve_plugin_exclusivity(target["editor"], target["reader"])
    _save_shared_memory(variables)
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass
    return target


def _delete_shared_variable(name: str, plugin_name: str | None = None, privileged: bool = False) -> bool:
    if not privileged:
        raise SharedVariableAccessError("Deleting a shared variable requires an API key or admin authorisation.")
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
