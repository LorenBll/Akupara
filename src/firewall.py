"""External-interactions / firewall helpers."""

from __future__ import annotations

import ipaddress
import json

import state
import env_store as _env_store
from logginglib import log_warn
from validation import _is_valid_key_name, _validate_plaintext_string


def _sort_entry_plugins(plugins: list) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for plugin in plugins:
        if not isinstance(plugin, str) or not plugin.strip():
            continue
        key = plugin.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(plugin.strip())
    akupara = [p for p in ordered if p.casefold() == "akupara"]
    rest = sorted([p for p in ordered if p.casefold() != "akupara"], key=lambda p: p.casefold())
    return akupara + rest


def _entries_var(direction: str) -> str:
    if direction == "incoming":
        return state._INCOMING_IPS_VAR
    if direction == "outgoing":
        return state._OUTGOING_IPS_VAR
    raise ValueError("Direction must be 'incoming' or 'outgoing'.")


def _load_external_interactions_entries(direction: str) -> list[dict]:
    raw = _env_store.read_env_var(_entries_var(direction))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            canonical = _canonical_network_ip(ipaddress.ip_address(str(entry.get("ip", "")).strip()))
        except ValueError:
            continue
        action = entry.get("action", "unknown")
        if action not in state._EXTERNAL_INTERACTIONS_ACTIONS:
            action = "unknown"
        plugins = entry.get("plugins", [])
        if not isinstance(plugins, list):
            plugins = []
        plugins = [p for p in _sort_entry_plugins(plugins) if _is_valid_key_name(p)]
        note = entry.get("Note")
        if not isinstance(note, str):
            note = ""
        entries.append({"ip": canonical, "plugins": plugins, "action": action, "Note": note})
    return entries


def _load_external_interactions_incoming_ips() -> list[dict]:
    return _load_external_interactions_entries("incoming")


def _load_external_interactions_outgoing_ips() -> list[dict]:
    return _load_external_interactions_entries("outgoing")


def _save_external_interactions_entries(direction: str, entries: list[dict]) -> None:
    normalized = [
        {"ip": entry["ip"], "plugins": _sort_entry_plugins(entry.get("plugins", [])), "action": entry["action"], "Note": entry.get("Note", "")}
        for entry in entries
    ]
    _env_store.write_env_var(_entries_var(direction), json.dumps(normalized))


def _list_external_interactions_entries(direction: str) -> list[dict]:
    _require_external_interactions_enabled()
    return _load_external_interactions_entries(direction)


def _find_external_interactions_entry(entries: list[dict], canonical: str) -> dict | None:
    return next((entry for entry in entries if entry.get("ip") == canonical), None)


def _update_external_interactions_entry(direction: str, ip: str, action: str | None = None, note: str | None = None, new_ip: str | None = None, remove_plugin: str | None = None, add_plugin: str | None = None) -> dict | None:
    import audio
    _require_external_interactions_enabled()
    entries = _load_external_interactions_entries(direction)
    canonical = _maximize_network_ip(ip)
    match = _find_external_interactions_entry(entries, canonical)
    if match is None:
        return None
    if action is not None and action not in state._EXTERNAL_INTERACTIONS_ACTIONS:
        raise ValueError("The value must be 'allow', 'unknown' or 'block'.")
    if note is not None:
        _validate_plaintext_string(note, "note")
    if remove_plugin is not None and (not isinstance(remove_plugin, str) or not remove_plugin.strip()):
        raise ValueError("Invalid plugin name.")
    if add_plugin is not None and (not isinstance(add_plugin, str) or not add_plugin.strip()):
        raise ValueError("Invalid plugin name.")
    new_canonical = _maximize_network_ip(new_ip) if new_ip is not None else canonical
    if new_canonical != canonical and _find_external_interactions_entry(entries, new_canonical) is not None:
        from auth import DuplicateNameError
        raise DuplicateNameError("That IP is already in the list.")
    plugins = _sort_entry_plugins(match.get("plugins", []))
    if remove_plugin is not None:
        plugins = [p for p in plugins if p.casefold() != remove_plugin.strip().casefold()]
    if add_plugin is not None:
        candidate = add_plugin.strip()
        if not _is_valid_key_name(candidate):
            raise ValueError("Invalid plugin name.")
        if candidate.casefold() not in [p.casefold() for p in plugins]:
            plugins = _sort_entry_plugins(plugins + [candidate])
    updated = {
        "ip": new_canonical,
        "plugins": plugins,
        "action": action if action is not None else match.get("action", "unknown"),
        "Note": note if note is not None else match.get("Note", ""),
    }
    _save_external_interactions_entries(direction, [updated if entry is match else entry for entry in entries])
    # play acknowledge sound
    try:
        audio.play_audio("acknowledge")()
    except Exception:
        pass
    return updated


def _delete_external_interactions_entry(direction: str, ip: str) -> bool:
    _require_external_interactions_enabled()
    canonical = _maximize_network_ip(ip)
    entries = _load_external_interactions_entries(direction)
    remaining = [entry for entry in entries if entry.get("ip") != canonical]
    if len(remaining) == len(entries):
        return False
    _save_external_interactions_entries(direction, remaining)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return True


def _set_external_interactions_allow_new(value: bool) -> None:
    _require_external_interactions_enabled()
    state.EXTERNAL_INTERACTIONS_ALLOW_NEW = value
    _env_store.write_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW", str(value).lower())
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass


def _set_external_interactions_allow_new_outgoing(value: bool) -> None:
    _require_external_interactions_enabled()
    state.EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING = value
    _env_store.write_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING", str(value).lower())
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass


def _record_external_interactions_ip_automatic(canonical: str, action: str) -> None:
    entries = _load_external_interactions_entries("incoming")
    if _find_external_interactions_entry(entries, canonical) is not None:
        return
    if len(entries) >= 1000:
        log_warn("Firewall automatic recording capped: too many entries", {"cap": 1000, "canonical": canonical})
        return
    entries.append({"ip": canonical, "plugins": ["akupara"], "action": action, "Note": ""})
    _save_external_interactions_entries("incoming", entries)
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass


def _external_interactions_worker_ip_policy(remote_addr: str) -> bool:
    try:
        canonical = _canonical_network_ip(ipaddress.ip_address(remote_addr))
    except ValueError:
        return False
    match = _find_external_interactions_entry(_load_external_interactions_entries("incoming"), canonical)
    if match is not None:
        if match.get("action") == "allow":
            return True
        if match.get("action") == "block":
            return False
    _record_external_interactions_ip_automatic(canonical, "unknown")
    return _env_store.read_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW", state.EXTERNAL_INTERACTIONS_ALLOW_NEW) if isinstance(_env_store.read_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW", None), str) else _env_store.read_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW", state.EXTERNAL_INTERACTIONS_ALLOW_NEW) or _env_store._parse_bool(_env_store.read_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW", ""), state.EXTERNAL_INTERACTIONS_ALLOW_NEW) if False else state.EXTERNAL_INTERACTIONS_ALLOW_NEW
    # Fallback simple:
    # return _env_store._parse_bool(_env_store.read_env_var("EXTERNAL_INTERACTIONS_ALLOW_NEW", ""), state.EXTERNAL_INTERACTIONS_ALLOW_NEW)


# Re-implement correctly to avoid the messy line above
def _external_interactions_worker_ip_policy_clean(remote_addr: str) -> bool:
    try:
        canonical = _canonical_network_ip(ipaddress.ip_address(remote_addr))
    except ValueError:
        return False
    match = _find_external_interactions_entry(_load_external_interactions_entries("incoming"), canonical)
    if match is not None:
        if match.get("action") == "allow":
            return True
        if match.get("action") == "block":
            return False
    _record_external_interactions_ip_automatic(canonical, "unknown")
    # Use env_store helper that handles bool parsing
    try:
        import env_store as _es
        return _es.read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", state.EXTERNAL_INTERACTIONS_ALLOW_NEW)
    except Exception:
        return state.EXTERNAL_INTERACTIONS_ALLOW_NEW


# Alias
_external_interactions_worker_ip_policy = _external_interactions_worker_ip_policy_clean


def _parse_network_ip(ip: str):
    if not isinstance(ip, str) or not ip.strip():
        raise ValueError("Invalid IP address.")
    try:
        return ipaddress.ip_address(ip.strip())
    except ValueError:
        raise ValueError("Invalid IP address.") from None


def _canonical_network_ip(address) -> str:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return address.exploded
    return str(address)


def _maximize_network_ip(ip: str) -> str:
    return _canonical_network_ip(_parse_network_ip(ip))


def _external_interactions_enabled() -> bool:
    return bool(state.EXTERNAL_INTERACTIONS)


def _require_external_interactions_enabled() -> None:
    if not state.EXTERNAL_INTERACTIONS:
        # Need FeatureDisabledError
        try:
            from auth import FeatureDisabledError
        except ImportError:
            class FeatureDisabledError(RuntimeError):
                pass
        raise FeatureDisabledError("The external interactions functionality is disabled.")


def _resolve_external_host() -> str | None:
    from config import _get_local_device_addresses
    import ipaddress as _ip
    candidates = sorted(_get_local_device_addresses(), key=lambda address: (":" in address, address))
    for address in candidates:
        try:
            ip = _ip.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.version != 4:
            continue
        return address
    return None


def _external_interactions_worker_bind_address() -> dict:
    if state._external_interactions_worker is None:
        return {"address": None, "port": state.SERVICE_PORT}
    host = _resolve_external_host()
    return {"address": host, "port": state.SERVICE_PORT}


def _start_external_interactions_worker() -> None:
    if state._external_interactions_worker is not None:
        return
    host = _resolve_external_host()
    if host is None:
        log_warn("External interactions worker not started: no non-loopback device address available")
        return
    import network
    worker = network.ExternalInteractionsWorker(state.app, host, state.SERVICE_PORT, ip_policy=_external_interactions_worker_ip_policy)
    try:
        worker.start()
    except OSError as exc:
        from logginglib import log_error
        log_error("External interactions worker failed to start", {"host": host, "port": state.SERVICE_PORT, "error": str(exc)})
        return
    state._external_interactions_worker = worker


def _stop_external_interactions_worker() -> None:
    worker = state._external_interactions_worker
    state._external_interactions_worker = None
    if worker is not None:
        worker.stop()


def _set_external_interactions(value: bool) -> None:
    state.EXTERNAL_INTERACTIONS = value
    _env_store.write_env_var("EXTERNAL_INTERACTIONS", str(value).lower())
    if value:
        _start_external_interactions_worker()
    else:
        _stop_external_interactions_worker()
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass
