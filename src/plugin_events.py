"""Plugin event subscriptions — internal-interactions event bus."""

from __future__ import annotations

import json
import re

import state
import env_store as _env_store
from validation import _is_valid_key_name


def _load_plugin_event_subscriptions() -> dict[str, list[str]]:
    raw = _env_store.read_env_var("PLUGIN_EVENT_SUBSCRIPTIONS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[str]] = {}
    for event, plugins in data.items():
        if not isinstance(event, str) or not event.strip():
            continue
        if not isinstance(plugins, list):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", event.strip()):
            continue
        cleaned: list[str] = []
        for p in plugins:
            if not isinstance(p, str) or not p.strip():
                continue
            if not _is_valid_key_name(p):
                continue
            cleaned.append(p.strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for p in cleaned:
            key = p.casefold()
            if key not in seen:
                seen.add(key)
                deduped.append(p)
        result[event.strip()] = deduped
    return result


def _save_plugin_event_subscriptions(data: dict[str, list[str]]) -> None:
    normalized: dict[str, list[str]] = {}
    for event in sorted(data.keys()):
        plugins = data[event]
        if not isinstance(plugins, list):
            plugins = []
        normalized[event] = sorted(plugins, key=lambda x: x.casefold())
    _env_store.write_env_var("PLUGIN_EVENT_SUBSCRIPTIONS", json.dumps(normalized, ensure_ascii=False))


def _is_valid_event_name(event: str) -> bool:
    return isinstance(event, str) and bool(event.strip()) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", event.strip()))


def _add_plugin_event(event: str, plugin: str | None = None) -> bool:
    if not _is_valid_event_name(event):
        raise ValueError("Invalid event name.")
    event = event.strip()
    if plugin is not None and (not isinstance(plugin, str) or not _is_valid_key_name(plugin)):
        raise ValueError("Invalid plugin name.")
    plugin = plugin.strip() if plugin is not None else None
    data = _load_plugin_event_subscriptions()
    if event in data:
        try:
            from auth import DuplicateNameError
        except ImportError:
            class DuplicateNameError(RuntimeError):
                pass
        raise DuplicateNameError("An event with this name already exists.")
    data[event] = [plugin] if plugin else []
    _save_plugin_event_subscriptions(data)
    return True


def _remove_plugin_event(event: str) -> bool:
    if not _is_valid_event_name(event):
        raise ValueError("Invalid event name.")
    event = event.strip()
    data = _load_plugin_event_subscriptions()
    if event not in data:
        return False
    del data[event]
    _save_plugin_event_subscriptions(data)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return True


def _add_plugin_to_event(event: str, plugin: str) -> bool:
    if not _is_valid_event_name(event):
        raise ValueError("Invalid event name.")
    if not isinstance(plugin, str) or not _is_valid_key_name(plugin):
        raise ValueError("Invalid plugin name.")
    event = event.strip()
    plugin = plugin.strip()
    data = _load_plugin_event_subscriptions()
    if event not in data:
        raise ValueError("Event not found.")
    if any(p.casefold() == plugin.casefold() for p in data[event]):
        try:
            from auth import DuplicateNameError
        except ImportError:
            class DuplicateNameError(RuntimeError):
                pass
        raise DuplicateNameError("Plugin already subscribed to this event.")
    data[event].append(plugin)
    data[event] = sorted(data[event], key=lambda x: x.casefold())
    _save_plugin_event_subscriptions(data)
    return True


def _remove_plugin_from_event(event: str, plugin: str) -> bool:
    if not _is_valid_event_name(event):
        raise ValueError("Invalid event name.")
    if not isinstance(plugin, str) or not plugin.strip():
        raise ValueError("Invalid plugin name.")
    event = event.strip()
    plugin = plugin.strip()
    data = _load_plugin_event_subscriptions()
    if event not in data:
        return False
    original = data[event]
    remaining = [p for p in original if p.casefold() != plugin.casefold()]
    if len(remaining) == len(original):
        return False
    data[event] = remaining
    _save_plugin_event_subscriptions(data)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return True
