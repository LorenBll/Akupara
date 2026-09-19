"""Service configuration — port, feature flags and local address helpers."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import threading
from pathlib import Path

import state
import env_store as _env_store
from logginglib import log_debug, log_info, log_warn


def _load_configuration() -> dict:
    if state._CONFIG_CACHE is not None:
        log_debug("Configuration loaded from cache")
        return state._CONFIG_CACHE

    config_path = Path(__file__).resolve().parent.parent / "resources" / "configuration.json"
    if not config_path.exists():
        log_warn("Configuration file not found", {"path": str(config_path)})
        raise FileNotFoundError("Configuration file not found.")

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        log_warn("Configuration file contains invalid JSON", {"error": str(exc)})
        raise ValueError("Configuration file contains invalid JSON") from exc

    state._CONFIG_CACHE = config
    log_debug("Configuration loaded", {"path": str(config_path)})
    return config


def _initialize_service_config() -> None:
    from dotenv import load_dotenv  # local import to avoid top-level cycle
    import audio
    from cryptography.fernet import Fernet

    # Local imports for auth helpers to avoid circular imports at module load
    from auth import _load_api_keys, _login_credentials_configured, _refresh_api_key_store

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    config = _load_configuration()

    configured_port = config.get("port", 49150)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        log_warn("Invalid port value in configuration; defaulting to 49150", {"port": configured_port})
        configured_port = 49150
    state.SERVICE_PORT = configured_port

    state.GUI_ENABLED = config.get("guiEnabled", True)
    dev_val = config.get("development", False)
    if isinstance(dev_val, bool):
        state.DEVELOPMENT = dev_val
    elif isinstance(dev_val, str):
        state.DEVELOPMENT = _env_store._parse_bool(dev_val, False)
    else:
        state.DEVELOPMENT = bool(dev_val)

    state.API_KEYS_ENABLED = _env_store._parse_bool(os.getenv("API_KEYS_ENABLED"), False)
    state.DISPLAY_PROMOTION = _env_store._parse_bool(os.getenv("DISPLAY_PROMOTION"), True)
    state.PLAY_AUDIOS = _env_store._parse_bool(os.getenv("PLAY_AUDIOS"), True)
    audio.set_audio_worker_enabled(state.PLAY_AUDIOS)

    state.PLAY_LOG_SOUNDS = _env_store._parse_bool(os.getenv("PLAY_LOG_SOUNDS"), False)
    try:
        import logginglib
        logginglib.set_log_sounds_config(state.PLAY_AUDIOS, state.PLAY_LOG_SOUNDS)
    except Exception:
        pass
    try:
        audio.set_play_log_sounds_enabled(state.PLAY_LOG_SOUNDS)
    except Exception:
        pass

    state.PLAY_STARTUP_SOUND = _env_store._parse_bool(os.getenv("PLAY_STARTUP_SOUND"), True)
    state.SHARED_MEMORY_ENABLED = _env_store._parse_bool(os.getenv("SHARED_MEMORY_ENABLED"), True)
    state.INTERNAL_INTERACTIONS = _env_store._parse_bool(os.getenv("INTERNAL_INTERACTIONS"), False)
    state.EXTERNAL_INTERACTIONS = _env_store._parse_bool(os.getenv("EXTERNAL_INTERACTIONS"), False)
    state.EXTERNAL_INTERACTIONS_ALLOW_NEW = _env_store._parse_bool(os.getenv("EXTERNAL_INTERACTIONS_ALLOW_NEW"), False)
    state.EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING = _env_store._parse_bool(os.getenv("EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING"), False)
    state.AUTOMATIC_UPDATE = _env_store._parse_bool(os.getenv("AUTOMATIC_UPDATE"), False)
    state.AUTOMATIC_PLUGIN_LIBRARY_UPDATE = _env_store._parse_bool(os.getenv("AUTOMATIC_PLUGIN_LIBRARY_UPDATE"), False)
    state.AUTOMATIC_PLUGIN_UPGRADE = _env_store._parse_bool(os.getenv("AUTOMATIC_PLUGIN_UPGRADE"), False)

    for sound_event, env_name in audio.SOUND_ENV_VARS.items():
        if _env_store.read_env_var(env_name, None) is None:
            _env_store.write_env_var(env_name, audio.DEFAULT_SOUND_FILES.get(sound_event, ""))

    if not _env_store.read_env_var("API_KEY_ENCRYPTION_KEY", None):
        if not _load_api_keys():
            try:
                _new_key = Fernet.generate_key().decode("utf-8")
                _env_store.write_env_var("API_KEY_ENCRYPTION_KEY", _new_key)
                try:
                    os.environ["API_KEY_ENCRYPTION_KEY"] = _new_key
                except Exception:
                    pass
                log_info("Generated API key encryption key")
            except Exception as exc:
                log_warn("Failed to generate API key encryption key", {"error": str(exc)})

    state._SESSION_STORE.clear()
    _refresh_api_key_store()

    if not _login_credentials_configured():
        log_warn("Login credentials not configured", {"hint": "set USERS in .env"})

    log_debug("Resolved config values", {"port": state.SERVICE_PORT, "guiEnabled": state.GUI_ENABLED, "internalInteractions": state.INTERNAL_INTERACTIONS, "apiKeysEnabled": state.API_KEYS_ENABLED, "externalInteractions": state.EXTERNAL_INTERACTIONS, "automaticUpdate": state.AUTOMATIC_UPDATE, "automaticPluginLibraryUpdate": state.AUTOMATIC_PLUGIN_LIBRARY_UPDATE, "automaticPluginUpgrade": state.AUTOMATIC_PLUGIN_UPGRADE})
    log_info("Service configuration initialized")


def _get_local_device_addresses() -> set[str]:
    now = time.time()
    with state._LOCAL_ADDR_LOCK:
        if state._LOCAL_ADDR_CACHE is not None and (now - state._LOCAL_ADDR_CACHE_TS) < state._LOCAL_ADDR_TTL:
            return set(state._LOCAL_ADDR_CACHE)
    local_addresses: set[str] = set()
    candidate_names = {socket.gethostname(), socket.getfqdn()}

    for candidate_name in candidate_names:
        if not candidate_name:
            continue
        try:
            local_addresses.update(
                address_info[4][0]
                for address_info in socket.getaddrinfo(candidate_name, None)
            )
        except OSError:
            log_debug("getaddrinfo failed", {"host": candidate_name})
        try:
            local_addresses.update(socket.gethostbyname_ex(candidate_name)[2])
        except OSError:
            log_debug("gethostbyname_ex failed", {"host": candidate_name})

    for probe_address in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as socket_handle:
                socket_handle.connect((probe_address, 80))
                local_addresses.add(socket_handle.getsockname()[0])
        except OSError:
            log_debug("UDP probe failed", {"address": probe_address})

    normalized_addresses: set[str] = set()
    for address_value in local_addresses:
        try:
            normalized_addresses.add(ipaddress.ip_address(address_value).compressed)
        except ValueError:
            log_debug("Invalid local address value ignored", {"address": address_value})
            continue

    normalized_addresses.update({"127.0.0.1", "::1"})
    log_debug("Local device address cache populated", {"count": len(normalized_addresses)})
    with state._LOCAL_ADDR_LOCK:
        state._LOCAL_ADDR_CACHE = set(normalized_addresses)
        state._LOCAL_ADDR_CACHE_TS = now
    return normalized_addresses
