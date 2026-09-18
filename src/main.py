"""Akupara local web service template."""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

# ============================================================================
# STARTUP DEPENDENCY CHECK
# ============================================================================
_missing_libraries: list[str] = []
for _module, _package in {
    "flask": "Flask",
    "dotenv": "python-dotenv",
    "cryptography": "cryptography",
}.items():
    try:
        __import__(_module)
    except ImportError:
        _missing_libraries.append(_package)

if _missing_libraries:
    import sys
    sys.stderr.write(
        "ERROR: Missing required libraries: "
        + ", ".join(_missing_libraries)
        + ". Install them with: pip install -r requirements.txt\n"
    )
    sys.exit(1)

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, render_template_string

from cryptography.fernet import Fernet

import audio

import network

import plugin_bridge

import github_api

from logginglib import init_logging, log_debug, log_error, log_info, log_warn

import env_store as _env_store

# Re-export env helpers for backward compat (main._write_env_var etc. remain importable)
# New code should import from env_store directly.
_write_env_var = _env_store.write_env_var
_write_env_bool = _env_store.write_env_bool
_read_env_var = _env_store.read_env_var
_read_env_bool = _env_store.read_env_bool
_parse_bool = _env_store._parse_bool

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = None

GUI_ENABLED: bool = True
DEVELOPMENT: bool = False

INTERNAL_INTERACTIONS: bool = False

API_KEYS_ENABLED: bool = False

DISPLAY_PROMOTION: bool = True

PLAY_AUDIOS: bool = True

PLAY_LOG_SOUNDS: bool = False

PLAY_STARTUP_SOUND: bool = True

SHARED_MEMORY_ENABLED: bool = False

EXTERNAL_INTERACTIONS: bool = False
EXTERNAL_INTERACTIONS_ALLOW_NEW: bool = False
EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING: bool = False

AUTOMATIC_UPDATE: bool = False
AUTOMATIC_PLUGIN_LIBRARY_UPDATE: bool = False
AUTOMATIC_PLUGIN_UPGRADE: bool = False

_UPDATE_AVAILABLE: bool = False
_UPDATE_AVAILABLE_AT_STARTUP: bool = False
_PLUGIN_UPDATE_AVAILABLE: bool = False
_PLUGIN_UPDATE_AVAILABLE_AT_STARTUP: bool = False
_INSTALLED_PLUGINS_PENDING_UPGRADES: list[dict] = []
_PROJECT_INTEGRITY_OK: bool = False
_PLUGIN_INTEGRITY_OK: bool = False

_external_interactions_worker: network.ExternalInteractionsWorker | None = None

_CONFIG_CACHE: dict | None = None

SESSION_COOKIE_NAME = "akupara-refresh"
_SESSION_STORE: dict[str, dict] = {}
_SESSION_MAX_AGE: int = 900
_SESSION_LOCK = threading.Lock()

_FAILED_LOGIN_ATTEMPTS: int = 0
_FAILED_LOGIN_LOCK = threading.Lock()
_MAX_FAILED_LOGIN_WARNS = 3


def _format_exc() -> str:
    return traceback.format_exc()


def _load_configuration() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        log_debug("Configuration loaded from cache")
        return _CONFIG_CACHE

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

    _CONFIG_CACHE = config
    log_debug("Configuration loaded", {"path": str(config_path)})
    return config


def _initialize_service_config() -> None:
    global SERVICE_PORT, GUI_ENABLED, DEVELOPMENT, INTERNAL_INTERACTIONS, API_KEYS_ENABLED, DISPLAY_PROMOTION, PLAY_AUDIOS, PLAY_LOG_SOUNDS, PLAY_STARTUP_SOUND, SHARED_MEMORY_ENABLED, EXTERNAL_INTERACTIONS, EXTERNAL_INTERACTIONS_ALLOW_NEW, EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING, AUTOMATIC_UPDATE, AUTOMATIC_PLUGIN_LIBRARY_UPDATE, AUTOMATIC_PLUGIN_UPGRADE
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    config = _load_configuration()

    configured_port = config.get("port", 49150)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        log_warn("Invalid port value in configuration; defaulting to 49150", {"port": configured_port})
        configured_port = 49150
    SERVICE_PORT = configured_port

    GUI_ENABLED = config.get("guiEnabled", True)
    global DEVELOPMENT
    dev_val = config.get("development", False)
    if isinstance(dev_val, bool):
        DEVELOPMENT = dev_val
    elif isinstance(dev_val, str):
        DEVELOPMENT = _parse_bool(dev_val, False)
    else:
        DEVELOPMENT = bool(dev_val)

    API_KEYS_ENABLED = _parse_bool(os.getenv("API_KEYS_ENABLED"), False)

    DISPLAY_PROMOTION = _parse_bool(os.getenv("DISPLAY_PROMOTION"), True)

    PLAY_AUDIOS = _parse_bool(os.getenv("PLAY_AUDIOS"), True)
    audio.set_audio_worker_enabled(PLAY_AUDIOS)

    PLAY_LOG_SOUNDS = _parse_bool(os.getenv("PLAY_LOG_SOUNDS"), False)
    # Configure logging sounds (depends on PLAY_AUDIOS)
    try:
        import logginglib
        logginglib.set_log_sounds_config(PLAY_AUDIOS, PLAY_LOG_SOUNDS)
    except Exception:
        pass
    try:
        audio.set_play_log_sounds_enabled(PLAY_LOG_SOUNDS)
    except Exception:
        pass

    PLAY_STARTUP_SOUND = _parse_bool(os.getenv("PLAY_STARTUP_SOUND"), True)

    SHARED_MEMORY_ENABLED = _parse_bool(os.getenv("SHARED_MEMORY_ENABLED"), True)

    INTERNAL_INTERACTIONS = _parse_bool(os.getenv("INTERNAL_INTERACTIONS"), False)

    EXTERNAL_INTERACTIONS = _parse_bool(os.getenv("EXTERNAL_INTERACTIONS"), False)

    EXTERNAL_INTERACTIONS_ALLOW_NEW = _parse_bool(os.getenv("EXTERNAL_INTERACTIONS_ALLOW_NEW"), False)

    EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING = _parse_bool(os.getenv("EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING"), False)

    AUTOMATIC_UPDATE = _parse_bool(os.getenv("AUTOMATIC_UPDATE"), False)
    AUTOMATIC_PLUGIN_LIBRARY_UPDATE = _parse_bool(os.getenv("AUTOMATIC_PLUGIN_LIBRARY_UPDATE"), False)
    AUTOMATIC_PLUGIN_UPGRADE = _parse_bool(os.getenv("AUTOMATIC_PLUGIN_UPGRADE"), False)

    for sound_event, env_name in audio.SOUND_ENV_VARS.items():
        if _read_env_var(env_name, None) is None:
            _write_env_var(env_name, audio.DEFAULT_SOUND_FILES.get(sound_event, ""))

    # Generate API key encryption key on first run if not set and no API keys stored.
    if not _read_env_var("API_KEY_ENCRYPTION_KEY", None):
        if not _load_api_keys():
            try:
                _new_key = Fernet.generate_key().decode("utf-8")
                _write_env_var("API_KEY_ENCRYPTION_KEY", _new_key)
                # Ensure current process sees the new key (file-backed reads will also see it)
                try:
                    os.environ["API_KEY_ENCRYPTION_KEY"] = _new_key
                except Exception:
                    pass
                log_info("Generated API key encryption key")
            except Exception as exc:
                log_warn("Failed to generate API key encryption key", {"error": str(exc)})

    _SESSION_STORE.clear()

    _refresh_api_key_store()

    if not _login_credentials_configured():
        log_warn("Login credentials not configured", {"hint": "set USERS in .env"})

    log_debug("Resolved config values", {"port": SERVICE_PORT, "guiEnabled": GUI_ENABLED, "internalInteractions": INTERNAL_INTERACTIONS, "apiKeysEnabled": API_KEYS_ENABLED, "externalInteractions": EXTERNAL_INTERACTIONS, "automaticUpdate": AUTOMATIC_UPDATE, "automaticPluginLibraryUpdate": AUTOMATIC_PLUGIN_LIBRARY_UPDATE, "automaticPluginUpgrade": AUTOMATIC_PLUGIN_UPGRADE})
    log_info("Service configuration initialized")


_LOCAL_ADDR_CACHE: set[str] | None = None
_LOCAL_ADDR_CACHE_TS: float = 0.0
_LOCAL_ADDR_TTL: float = 30.0
_LOCAL_ADDR_LOCK = threading.Lock()

def _get_local_device_addresses() -> set[str]:
    global _LOCAL_ADDR_CACHE, _LOCAL_ADDR_CACHE_TS
    now = time.time()
    with _LOCAL_ADDR_LOCK:
        if _LOCAL_ADDR_CACHE is not None and (now - _LOCAL_ADDR_CACHE_TS) < _LOCAL_ADDR_TTL:
            return set(_LOCAL_ADDR_CACHE)
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
    with _LOCAL_ADDR_LOCK:
        _LOCAL_ADDR_CACHE = set(normalized_addresses)
        _LOCAL_ADDR_CACHE_TS = now
    return normalized_addresses


def _generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def _prune_expired_sessions() -> None:
    cutoff = time.time() - _SESSION_MAX_AGE
    with _SESSION_LOCK:
        expired = [token for token, session in _SESSION_STORE.items() if session["last_refresh"] < cutoff]
        for token in expired:
            del _SESSION_STORE[token]
    if expired:
        log_debug("Pruned expired sessions", {"count": len(expired)})


def _active_session() -> dict | None:
    provided_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not provided_token:
        return None
    with _SESSION_LOCK:
        session = _SESSION_STORE.get(provided_token)
        if session is None:
            return None
        if time.time() - session["last_refresh"] > _SESSION_MAX_AGE:
            _SESSION_STORE.pop(provided_token, None)
            return None
        return {"username": session["username"], "admin": session["admin"], "root": bool(session.get("root", False))}


def _is_valid_session_cookie() -> bool:
    return _active_session() is not None


def _issue_session_cookie(response, username: str, admin: bool, root: bool = False) -> None:
    _prune_expired_sessions()
    token = _generate_session_token()
    with _SESSION_LOCK:
        _SESSION_STORE[token] = {"username": username, "admin": admin, "root": bool(root), "last_refresh": time.time()}
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Lax",
        max_age=_SESSION_MAX_AGE,
        path="/",
    )


def _renew_session_cookie(response) -> None:
    provided_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not provided_token:
        return
    with _SESSION_LOCK:
        session = _SESSION_STORE.get(provided_token)
        if session is None:
            return
        session["last_refresh"] = time.time()
        new_token = _generate_session_token()
        # Keep the provided token valid so in-flight requests carrying it still
        # authenticate; all tokens of a session share the same session dict and
        # expire together. The value written to the cookie is always server-generated.
        _SESSION_STORE[new_token] = session
    response.set_cookie(
        SESSION_COOKIE_NAME,
        new_token,
        httponly=True,
        samesite="Lax",
        max_age=_SESSION_MAX_AGE,
        path="/",
    )


def _unauthorized_response():
    if request.path.startswith("/api/"):
        log_warn("Rejected API request: missing or invalid refresh cookie", {"client": request.remote_addr})
        return jsonify({"error": "API key required."}), 401
    log_warn("Redirecting unauthenticated request to /login", {"client": request.remote_addr})
    return redirect("/login")


def _refresh_cookie_when_valid(func, *args, **kwargs):
    response = app.make_response(func(*args, **kwargs))
    _renew_session_cookie(response)
    return response


def session_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_valid_session_cookie():
            return _refresh_cookie_when_valid(func, *args, **kwargs)
        return _unauthorized_response()
    return wrapper


def _is_valid_api_key() -> bool:
    provided_key = request.headers.get("X-Api-Key")
    if not provided_key:
        return False
    return any(hmac.compare_digest(provided_key, entry["key"]) for entry in _api_key_store)


def api_key_or_admin_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_valid_api_key():
            return func(*args, **kwargs)
        session = _active_session()
        if session is not None and session["admin"]:
            return _refresh_cookie_when_valid(func, *args, **kwargs)
        log_warn("Rejected admin request: logged-in user is not an admin", {"client": request.remote_addr})
        return _unauthorized_response()
    return wrapper


def _require_admin_session():
    """Return a 401/403 response when the active session is not an admin, else None."""
    session = _active_session()
    if session is None:
        return _unauthorized_response()
    if not session["admin"]:
        log_warn("Rejected admin request: logged-in user is not an admin", {"client": request.remote_addr})
        return jsonify({"error": "Admin privileges required."}), 403
    return None


def admin_session_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        denied = _require_admin_session()
        if denied is not None:
            return denied
        return _refresh_cookie_when_valid(func, *args, **kwargs)
    return wrapper


def _resolve_actor() -> tuple[str, str] | None:
    """Return (kind, id) of the authenticated caller (a user session or an API key), or None.

    The id is used only for change logging; it is never exposed through any response.
    """
    provided_key = request.headers.get("X-Api-Key")
    if provided_key:
        for entry in _api_key_store:
            if hmac.compare_digest(provided_key, entry["key"]):
                return ("api-key", str(entry.get("id") or ""))
        return None
    session = _active_session()
    if session:
        for user in _load_users():
            if user["username"].casefold() == session["username"].casefold():
                return ("user", str(user.get("id") or ""))
        return None
    return None


def log_change(func):
    """Mark an endpoint as a change: after a successful authenticated call, log the
    caller's id (user or API key) alongside the request, for the change logs."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        status = result[1] if isinstance(result, tuple) and len(result) > 1 else 200
        if request.method in ("POST", "PATCH", "DELETE") and isinstance(status, int) and 200 <= status < 300:
            actor = _resolve_actor()
            if actor and actor[1]:
                kind, actor_id = actor
                log_info("Change recorded", {"kind": kind, "id": actor_id, "method": request.method, "path": request.path, "status": status})
        return result

    return wrapper


def _login_credentials_configured() -> bool:
    return bool(_load_users())


def _load_users() -> list[dict]:
    raw = _read_env_var("USERS")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    users: list[dict] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        password = entry.get("password")
        if not isinstance(username, str) or not username.strip():
            continue
        if not isinstance(password, str) or not password.strip():
            continue
        key = username.casefold()
        if key in seen:
            continue
        seen.add(key)
        users.append({
            "username": username,
            "password": password,
            "admin": bool(entry.get("admin", False)),
            "root": bool(entry.get("root", False)),
            "id": str(entry.get("id") or ""),
        })
    return users


def _authenticate_user(username: str, password_hash: str) -> dict | None:
    username_key = username.casefold()
    for user in _load_users():
        if username_key == user["username"].casefold() and hmac.compare_digest(password_hash, user["password"]):
            return {"username": user["username"], "admin": user["admin"], "root": user["root"]}
    return None


def _validate_login(username: str, password_hash: str) -> bool:
    return _authenticate_user(username, password_hash) is not None


def _register_failed_login_attempt() -> bool:
    """Record a failed login attempt; return True while the warn sound should still play."""
    global _FAILED_LOGIN_ATTEMPTS
    with _FAILED_LOGIN_LOCK:
        _FAILED_LOGIN_ATTEMPTS += 1
        return _FAILED_LOGIN_ATTEMPTS <= _MAX_FAILED_LOGIN_WARNS


def _reset_failed_login_attempts() -> None:
    """Reset the consecutive failed-login counter (called on successful login)."""
    global _FAILED_LOGIN_ATTEMPTS
    with _FAILED_LOGIN_LOCK:
        _FAILED_LOGIN_ATTEMPTS = 0


class AccountNotFoundError(RuntimeError):
    """Raised when the account is not found in the configured credentials."""


class CurrentPasswordError(RuntimeError):
    """Raised when the current password provided does not match the stored one."""


def _matching_stored_passwords(username: str) -> list[tuple[str, str]]:
    """Return the (source, password hash) records configured for the username."""
    username_key = username.casefold()
    records: list[tuple[str, str]] = []
    for user in _load_users():
        if username_key == user["username"].casefold():
            records.append(("users", user["password"]))
    return records


def _set_user_password_env(username: str, new_password_hash: str) -> bool:
    raw = _read_env_var("USERS")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return False
    if not isinstance(data, list):
        return False
    username_key = username.casefold()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_username = entry.get("username")
        if isinstance(entry_username, str) and entry_username.casefold() == username_key:
            entry["password"] = new_password_hash
            _write_env_var("USERS", json.dumps(data, ensure_ascii=False))
            return True
    return False


def _change_password(username: str, current_password_hash: str, new_password_hash: str, keep_token: str | None = None) -> None:
    records = _matching_stored_passwords(username)
    if not records:
        raise AccountNotFoundError("Account not found.")
    if not any(hmac.compare_digest(current_password_hash, stored) for _, stored in records):
        raise CurrentPasswordError("Current password is incorrect.")
    for source, _ in records:
        if not _set_user_password_env(username, new_password_hash):
            raise AccountNotFoundError("Account not found.")
    _revoke_other_sessions(username, keep_token)
    log_info("Password changed for user", {"username": username})


class UsernameTakenError(RuntimeError):
    """Raised when registering a username that already exists."""


def _register_user(username: str, password_hash: str, admin: bool) -> None:
    username = username.strip()
    if not username:
        raise ValueError("Username must not be empty.")
    if any(ch in username for ch in _FORBIDDEN_KEY_NAME_CHARS):
        raise ValueError("Username contains prohibited characters.")
    if len(username) < 8:
        raise ValueError("Username must be at least 8 characters long.")
    if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$"):
        raise ValueError("Invalid password hash.")
    username_key = username.casefold()
    for user in _load_users():
        if username_key == user["username"].casefold():
            raise UsernameTakenError("A user with that username already exists.")
    raw = _read_env_var("USERS")
    try:
        data = json.loads(raw) if raw else []
    except (json.JSONDecodeError, ValueError, TypeError):
        data = []
    if not isinstance(data, list):
        data = []
    data.append({"username": username, "password": password_hash, "admin": bool(admin), "root": False, "id": secrets.token_hex(16)})
    _write_env_var("USERS", json.dumps(data, ensure_ascii=False))
    audio.play_audio("success")()
    log_info("User registered", {"username": username})


def _list_users() -> list[dict]:
    users = [{"username": user["username"], "admin": user["admin"], "root": user["root"]} for user in _load_users()]
    users.sort(key=lambda user: (not user["root"], not user["admin"], user["username"].casefold()))
    return users


def _is_root_username(username: str) -> bool:
    return any(user["root"] for user in _load_users() if user["username"].casefold() == username.casefold())


def _save_users(users: list[dict]) -> None:
    _write_env_var("USERS", json.dumps(users, ensure_ascii=False))


def _rename_session_username(old_username: str, new_username: str) -> None:
    old_key = old_username.casefold()
    with _SESSION_LOCK:
        for session in _SESSION_STORE.values():
            if session["username"].casefold() == old_key:
                session["username"] = new_username


def _set_session_admin(username: str, admin: bool) -> None:
    key = username.casefold()
    with _SESSION_LOCK:
        for session in _SESSION_STORE.values():
            if session["username"].casefold() == key:
                session["admin"] = admin


def _delete_session_username(username: str) -> None:
    key = username.casefold()
    with _SESSION_LOCK:
        for token in [t for t, s in _SESSION_STORE.items() if s["username"].casefold() == key]:
            del _SESSION_STORE[token]


def _revoke_other_sessions(username: str, keep_token: str | None) -> None:
    key = username.casefold()
    with _SESSION_LOCK:
        for token in [t for t, s in _SESSION_STORE.items() if s["username"].casefold() == key and t != keep_token]:
            del _SESSION_STORE[token]


@audio.play_audio("acknowledge")
def _rename_user(username: str, new_username: str) -> dict | None:
    if not isinstance(new_username, str) or not new_username.strip():
        raise ValueError("Username must not be empty.")
    if any(ch in new_username for ch in _FORBIDDEN_KEY_NAME_CHARS):
        raise ValueError("Username contains prohibited characters.")
    if len(new_username) < 8:
        raise ValueError("Username must be at least 8 characters long.")
    users = _load_users()
    target = next((user for user in users if user["username"].casefold() == username.casefold()), None)
    if not target:
        return None
    new_key = new_username.casefold()
    if any(user is not target and user["username"].casefold() == new_key for user in users):
        raise UsernameTakenError("A user with that username already exists.")
    old_username = target["username"]
    target["username"] = new_username
    _save_users(users)
    _rename_session_username(old_username, new_username)
    log_info("User renamed", {"old_username": old_username, "new_username": new_username})
    return {"username": new_username, "admin": target["admin"], "root": target["root"]}


@audio.play_audio("acknowledge")
def _set_user_admin(username: str, admin: bool) -> dict | None:
    if not isinstance(admin, bool):
        raise ValueError("Invalid admin value.")
    users = _load_users()
    target = next((user for user in users if user["username"].casefold() == username.casefold()), None)
    if not target:
        return None
    if target["root"] and not admin:
        raise ValueError("The root user cannot lose admin status.")
    target["admin"] = admin
    _save_users(users)
    _set_session_admin(target["username"], admin)
    log_info("Admin status set for user", {"username": target["username"], "admin": admin})
    return {"username": target["username"], "admin": target["admin"], "root": target["root"]}


def _delete_user(username: str) -> bool:
    users = _load_users()
    target = next((user for user in users if user["username"].casefold() == username.casefold()), None)
    if target is None:
        return False
    if target["root"]:
        raise ValueError("The root user cannot be deleted.")
    remaining = [user for user in users if user["username"].casefold() != username.casefold()]
    _save_users(remaining)
    _delete_session_username(username)
    audio.play_audio("success")()
    log_info("User deleted", {"username": username})
    return True


class FeatureDisabledError(RuntimeError):
    """Raised when a feature is disabled and its functionality is unavailable."""


class DuplicateNameError(RuntimeError):
    """Raised when creating/renaming an entity whose name is already taken."""


def _require_api_keys_enabled() -> None:
    if not API_KEYS_ENABLED:
        raise FeatureDisabledError("The API keys functionality is disabled.")


def _require_internal_interactions_enabled() -> None:
    if not INTERNAL_INTERACTIONS:
        raise FeatureDisabledError("The internal interactions functionality is disabled.")


def _effective_shared_memory_enabled() -> bool:
    return INTERNAL_INTERACTIONS and SHARED_MEMORY_ENABLED


def _effective_internal_interactions_enabled() -> bool:
    return INTERNAL_INTERACTIONS and SHARED_MEMORY_ENABLED


def _require_shared_memory_enabled() -> None:
    if not _effective_shared_memory_enabled():
        raise FeatureDisabledError("The internal interactions functionality is disabled.")


# Plugins catalog (hash-range library) research lives in plugin_bridge.
# main.py keeps only the endpoint (see search_plugins below); no handling
# (download / load / start / stop) of installed plugins lives here.


app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def _options_response(allowed_methods: list[str]) -> tuple:
    response = jsonify({})
    response.headers["Allow"] = ", ".join(allowed_methods)
    response.headers["Access-Control-Allow-Methods"] = ", ".join(allowed_methods)
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response, 200


def _head_response() -> tuple:
    response = jsonify({})
    return response, 200


@app.after_request
def set_connection_header(response):
    content_type = response.headers.get("Content-Type", "")
    if content_type.startswith("text/html"):
        response.headers["Connection"] = "keep-alive"
        log_debug("Connection set to keep-alive", {"path": request.path})
    else:
        response.headers["Connection"] = "close"
        log_debug("Connection set to close", {"path": request.path})
    return response


def standard_endpoint(*methods: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                log_debug("OPTIONS request handled", {"path": request.path})
                return _options_response(list(methods))
            if request.method == "HEAD":
                log_debug("HEAD request handled", {"path": request.path})
                return _head_response()
            return func(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/api/health", methods=["GET", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def health() -> tuple:
    log_info("Health check", {"client": request.remote_addr})

    bind_address = SERVICE_HOST
    # Requests arriving through the external interactions worker (non-local
    # client) see the worker's bind address instead of loopback.
    if _external_interactions_worker is not None and request.remote_addr not in _get_local_device_addresses():
        worker_bind = _external_interactions_worker_bind_address().get("address")
        if worker_bind:
            bind_address = worker_bind

    return jsonify({
        "status": "ok",
        "service": "Akupara",
        "bind_address": bind_address,
        "port": SERVICE_PORT,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }), 200


def _terminate() -> None:
    log_info("Akupara terminating")
    os.kill(os.getpid(), signal.SIGTERM)


def _restart() -> None:
    log_info("Akupara restarting")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    os._exit(0)


@app.route("/api/terminate", methods=["POST", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("POST", "OPTIONS")
def terminate() -> tuple:
    log_info("Terminate requested", {"client": request.remote_addr})
    threading.Timer(0.5, _terminate).start()
    return jsonify({"status": "ok", "message": "Akupara is terminating."}), 200


@app.route("/api/restart", methods=["POST", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("POST", "OPTIONS")
def restart() -> tuple:
    log_info("Restart requested", {"client": request.remote_addr})
    threading.Timer(0.5, _restart).start()
    return jsonify({"status": "ok", "message": "Akupara is restarting."}), 200


# Env helpers moved to src/env_store.py — wrappers imported at top for compat.
# _write_env_var, _write_env_bool, _read_env_var, _read_env_bool, _parse_bool
# are now provided by env_store and re-exported above.


@audio.play_audio("acknowledge")
def _set_internal_interactions(value: bool) -> None:
    global INTERNAL_INTERACTIONS
    INTERNAL_INTERACTIONS = value
    _write_env_bool("INTERNAL_INTERACTIONS", value)


@audio.play_audio("acknowledge")
def _set_automatic_update(value: bool) -> None:
    if _is_project_functionality_disabled():
        raise FeatureDisabledError("Project functionalities disabled due to integrity check failure in development mode.")
    global AUTOMATIC_UPDATE
    AUTOMATIC_UPDATE = value
    _write_env_bool("AUTOMATIC_UPDATE", value)


@audio.play_audio("acknowledge")
def _set_automatic_plugin_library_update(value: bool) -> None:
    global AUTOMATIC_PLUGIN_LIBRARY_UPDATE
    AUTOMATIC_PLUGIN_LIBRARY_UPDATE = value
    _write_env_bool("AUTOMATIC_PLUGIN_LIBRARY_UPDATE", value)


@audio.play_audio("acknowledge")
def _set_automatic_plugin_upgrade(value: bool) -> None:
    global AUTOMATIC_PLUGIN_UPGRADE
    AUTOMATIC_PLUGIN_UPGRADE = value
    _write_env_bool("AUTOMATIC_PLUGIN_UPGRADE", value)


@audio.play_audio("acknowledge")
def _set_api_keys_enabled(value: bool) -> None:
    global API_KEYS_ENABLED
    API_KEYS_ENABLED = value
    _write_env_bool("API_KEYS_ENABLED", value)


def _external_interactions_enabled() -> bool:
    return bool(EXTERNAL_INTERACTIONS)


def _resolve_external_host() -> str | None:
    candidates = sorted(_get_local_device_addresses(), key=lambda address: (":" in address, address))
    for address in candidates:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.version != 4:
            continue
        return address
    return None


def _external_interactions_worker_bind_address() -> dict:
    if _external_interactions_worker is None:
        return {"address": None, "port": SERVICE_PORT}
    host = _resolve_external_host()
    return {"address": host, "port": SERVICE_PORT}


def _start_external_interactions_worker() -> None:
    global _external_interactions_worker
    if _external_interactions_worker is not None:
        return
    host = _resolve_external_host()
    if host is None:
        log_warn("External interactions worker not started: no non-loopback device address available")
        return
    worker = network.ExternalInteractionsWorker(app, host, SERVICE_PORT, ip_policy=_external_interactions_worker_ip_policy)
    try:
        worker.start()
    except OSError as exc:
        log_error("External interactions worker failed to start", {"host": host, "port": SERVICE_PORT, "error": str(exc)})
        return
    _external_interactions_worker = worker


def _stop_external_interactions_worker() -> None:
    global _external_interactions_worker
    worker = _external_interactions_worker
    _external_interactions_worker = None
    if worker is not None:
        worker.stop()


def _require_external_interactions_enabled() -> None:
    if not EXTERNAL_INTERACTIONS:
        raise FeatureDisabledError("The external interactions functionality is disabled.")


@audio.play_audio("acknowledge")
def _set_external_interactions(value: bool) -> None:
    global EXTERNAL_INTERACTIONS
    EXTERNAL_INTERACTIONS = value
    _write_env_bool("EXTERNAL_INTERACTIONS", value)
    if value:
        _start_external_interactions_worker()
    else:
        _stop_external_interactions_worker()


def _parse_network_ip(ip: str):
    if not isinstance(ip, str) or not ip.strip():
        raise ValueError("Invalid IP address.")
    try:
        return ipaddress.ip_address(ip.strip())
    except ValueError:
        raise ValueError("Invalid IP address.") from None


def _canonical_network_ip(address) -> str:
    if isinstance(address, ipaddress.IPv6Address):
        # Normalize IPv4-mapped IPv6 (e.g. ::ffff:192.0.2.1) to plain IPv4 to avoid bypass
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return address.exploded
    return str(address)


def _maximize_network_ip(ip: str) -> str:
    return _canonical_network_ip(_parse_network_ip(ip))



_EXTERNAL_INTERACTIONS_ACTIONS = ("allow", "unknown", "block")
_EXTERNAL_INTERACTIONS_DIRECTIONS = ("incoming", "outgoing")

_INCOMING_IPS_VAR = "EXTERNAL_INTERACTIONS_INCOMING_IPS"
_OUTGOING_IPS_VAR = "EXTERNAL_INTERACTIONS_OUTGOING_IPS"


def _sort_entry_plugins(plugins: list) -> list[str]:
    """Sort a firewall entry's plugin names: akupara first, then alphabetical.

    Entries are deduplicated case-insensitively (first spelling kept) so tag
    clouds are always stored and displayed in the same order.
    """
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
        return _INCOMING_IPS_VAR
    if direction == "outgoing":
        return _OUTGOING_IPS_VAR
    raise ValueError("Direction must be 'incoming' or 'outgoing'.")


def _load_external_interactions_entries(direction: str) -> list[dict]:
    """Load firewall entries ({"ip", "plugins", "action", "Note"}) for a direction."""
    raw = _read_env_var(_entries_var(direction))
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
        if action not in _EXTERNAL_INTERACTIONS_ACTIONS:
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
    _write_env_var(_entries_var(direction), json.dumps(normalized))


def _list_external_interactions_entries(direction: str) -> list[dict]:
    _require_external_interactions_enabled()
    return _load_external_interactions_entries(direction)


def _find_external_interactions_entry(entries: list[dict], canonical: str) -> dict | None:
    return next((entry for entry in entries if entry.get("ip") == canonical), None)


@audio.play_audio("acknowledge")
def _update_external_interactions_entry(direction: str, ip: str, action: str | None = None, note: str | None = None, new_ip: str | None = None, remove_plugin: str | None = None, add_plugin: str | None = None) -> dict | None:
    """Update a firewall entry: action/note/IP, or remove/add a plugin."""
    _require_external_interactions_enabled()
    entries = _load_external_interactions_entries(direction)
    canonical = _maximize_network_ip(ip)
    match = _find_external_interactions_entry(entries, canonical)
    if match is None:
        return None
    if action is not None and action not in _EXTERNAL_INTERACTIONS_ACTIONS:
        raise ValueError("The value must be 'allow', 'unknown' or 'block'.")
    if note is not None:
        _validate_plaintext_string(note, "note")
    if remove_plugin is not None and (not isinstance(remove_plugin, str) or not remove_plugin.strip()):
        raise ValueError("Invalid plugin name.")
    if add_plugin is not None and (not isinstance(add_plugin, str) or not add_plugin.strip()):
        raise ValueError("Invalid plugin name.")
    new_canonical = _maximize_network_ip(new_ip) if new_ip is not None else canonical
    if new_canonical != canonical and _find_external_interactions_entry(entries, new_canonical) is not None:
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
    return updated


def _delete_external_interactions_entry(direction: str, ip: str) -> bool:
    _require_external_interactions_enabled()
    canonical = _maximize_network_ip(ip)
    entries = _load_external_interactions_entries(direction)
    remaining = [entry for entry in entries if entry.get("ip") != canonical]
    if len(remaining) == len(entries):
        return False
    _save_external_interactions_entries(direction, remaining)
    audio.play_audio("success")()
    return True


@audio.play_audio("acknowledge")
def _set_external_interactions_allow_new(value: bool) -> None:
    _require_external_interactions_enabled()
    global EXTERNAL_INTERACTIONS_ALLOW_NEW
    EXTERNAL_INTERACTIONS_ALLOW_NEW = value
    _write_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", value)


@audio.play_audio("acknowledge")
def _set_external_interactions_allow_new_outgoing(value: bool) -> None:
    _require_external_interactions_enabled()
    global EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING
    EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING = value
    _write_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING", value)


def _record_external_interactions_ip_automatic(canonical: str, action: str) -> None:
    entries = _load_external_interactions_entries("incoming")
    if _find_external_interactions_entry(entries, canonical) is not None:
        return
    # Prevent unbounded growth / disk exhaustion from attacker-controlled IP spoofing
    if len(entries) >= 1000:
        log_warn("Firewall automatic recording capped: too many entries", {"cap": 1000, "canonical": canonical})
        return
    entries.append({"ip": canonical, "plugins": ["akupara"], "action": action, "Note": ""})
    _save_external_interactions_entries("incoming", entries)
    audio.play_audio("acknowledge")()


def _external_interactions_worker_ip_policy(remote_addr: str) -> bool:
    """Per-request access decision for the external interactions worker.

    Each IP in the incoming list carries one of three actions: ``"allow"``
    (requests pass through), ``"block"`` (requests are refused) and
    ``"unknown"``. New IPs are always recorded in the incoming list with
    ``"unknown"`` (scoped to ``akupara``). Requests from IPs whose action is
    ``"unknown"``, and requests from IPs not yet in the list, are decided by
    ``EXTERNAL_INTERACTIONS_ALLOW_NEW``. Recordings made here are automatic
    and play the acknowledge sound.
    """
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
    return _read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", EXTERNAL_INTERACTIONS_ALLOW_NEW)


def _set_display_promotion(value: bool) -> None:
    global DISPLAY_PROMOTION
    DISPLAY_PROMOTION = value
    _write_env_bool("DISPLAY_PROMOTION", value)


def _set_play_audios(value: bool) -> None:
    global PLAY_AUDIOS
    PLAY_AUDIOS = value
    _write_env_bool("PLAY_AUDIOS", value)
    audio.set_audio_worker_enabled(value)
    try:
        import logginglib
        logginglib.set_log_sounds_config(PLAY_AUDIOS, PLAY_LOG_SOUNDS)
    except Exception:
        pass


def _set_play_log_sounds(value: bool) -> None:
    _require_play_audios_enabled()
    global PLAY_LOG_SOUNDS
    PLAY_LOG_SOUNDS = value
    _write_env_bool("PLAY_LOG_SOUNDS", value)
    try:
        import logginglib
        logginglib.set_log_sounds_config(PLAY_AUDIOS, PLAY_LOG_SOUNDS)
    except Exception:
        pass
    try:
        audio.set_play_log_sounds_enabled(value)
    except Exception:
        pass
    if not value:
        try:
            audio.play_sound("acknowledge")
        except Exception:
            pass


STARTUP_SOUND_FILE = "logo-reveal.wav"


@audio.play_audio("acknowledge")
def _set_play_startup_sound(value: bool) -> None:
    _require_play_audios_enabled()
    global PLAY_STARTUP_SOUND
    PLAY_STARTUP_SOUND = value
    _write_env_bool("PLAY_STARTUP_SOUND", value)


def _play_startup_sound() -> None:
    """Play the fixed startup sound after all loading operations, when enabled.

    The sound is not customisable (always ``logo-reveal.wav``) and plays only
    when both ``PLAY_AUDIOS`` and ``PLAY_STARTUP_SOUND`` are on.
    """
    if not PLAY_AUDIOS:
        return
    if not PLAY_STARTUP_SOUND:
        return
    try:
        path = audio.AUDIOS_DIR / STARTUP_SOUND_FILE
        if not path.is_file():
            return
        audio.get_audio_orchestrator().play(path)
    except Exception:
        pass


def _require_play_audios_enabled() -> None:
    if not PLAY_AUDIOS:
        raise FeatureDisabledError("The audio functionality is disabled.")


def _is_valid_sound_file_name(file_name: str) -> bool:
    if "/" in file_name or "\\" in file_name or file_name in {".", ".."}:
        return False
    # Whitelist: only alphanum, dot, underscore, hyphen, ending .wav (preserves existing files unchanged)
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.wav", file_name):
        return False
    # Resolve containment to prevent symlink escape outside audios dir
    try:
        target = (audio.AUDIOS_DIR / file_name).resolve()
        if audio.AUDIOS_DIR.resolve() not in target.parents and target != audio.AUDIOS_DIR.resolve() / file_name:
            return False
    except Exception:
        return False
    return (audio.AUDIOS_DIR / file_name).is_file()


# The @audio.play_audio("acknowledge") decorator is intentionally not applied here:
# assigning an audio to an event already plays the selected event sound immediately
# afterwards (the frontend triggers it), so acknowledging the assignment as well would
# make two audios play at the same time. Keep this comment — do not delete it — to leave
# trace of the reason the acknowledge audio is not played when selecting an audio.
# @audio.play_audio("acknowledge")
def _set_sound_file(event_name: str, file_name: str) -> None:
    _require_play_audios_enabled()
    if event_name not in audio.SOUND_ENV_VARS:
        raise ValueError("Unknown sound event.")
    file_name = (file_name or "").strip()
    if file_name and not _is_valid_sound_file_name(file_name):
        raise ValueError("Invalid sound file.")
    _write_env_var(audio.SOUND_ENV_VARS[event_name], file_name)


def _play_sound_event(event_name: str) -> None:
    _require_play_audios_enabled()
    if event_name not in audio.SOUND_ENV_VARS:
        raise ValueError("Unknown sound event.")
    audio.play_sound(event_name)


@audio.play_audio("acknowledge")
def _set_shared_memory_enabled(value: bool) -> None:
    _require_internal_interactions_enabled()
    global SHARED_MEMORY_ENABLED
    SHARED_MEMORY_ENABLED = value
    _write_env_bool("SHARED_MEMORY_ENABLED", value)


@app.route("/api/settings", methods=["GET", "POST", "HEAD", "OPTIONS"])
@log_change
@session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def settings() -> tuple:
    if request.method == "GET":
        log_info("Settings read", {"client": request.remote_addr})
        return jsonify({
            "internalInteractions": _read_env_bool("INTERNAL_INTERACTIONS", INTERNAL_INTERACTIONS),
            "displayPromotion": _read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
            "externalInteractions": _read_env_bool("EXTERNAL_INTERACTIONS", EXTERNAL_INTERACTIONS),
        }), 200
    denied = _require_admin_session()
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    keys = [key for key in ("internalInteractions", "displayPromotion", "externalInteractions") if key in data]
    if not keys:
        return jsonify({"error": "No known setting provided."}), 400
    for key in keys:
        value = data[key]
        if not isinstance(value, bool):
            return jsonify({"error": f"{key} must be a boolean."}), 400
        if key == "internalInteractions":
            _set_internal_interactions(value)
        elif key == "externalInteractions":
            try:
                _set_external_interactions(value)
            except FeatureDisabledError:
                return jsonify({"error": "Functionality disabled."}), 403
        else:
            _set_display_promotion(value)
    log_info("Settings updated", {"client": request.remote_addr, "internalInteractions": INTERNAL_INTERACTIONS, "displayPromotion": DISPLAY_PROMOTION, "externalInteractions": EXTERNAL_INTERACTIONS})
    return jsonify({
        "internalInteractions": INTERNAL_INTERACTIONS,
        "displayPromotion": DISPLAY_PROMOTION,
        "externalInteractions": EXTERNAL_INTERACTIONS,
    }), 200


@app.route("/api/audio", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def audio_playback() -> tuple:
    if request.method == "GET":
        log_info("Audio settings read", {"client": request.remote_addr})
        return jsonify({
            "playAudios": _read_env_bool("PLAY_AUDIOS", PLAY_AUDIOS),
            "sounds": {event: _read_env_var(audio.SOUND_ENV_VARS[event], audio.DEFAULT_SOUND_FILES.get(event, "")) for event in audio.SOUND_EVENTS},
            "available": audio.list_audio_files(),
        }), 200

    data = request.get_json(silent=True) or {}
    if "playAudios" not in data and "event" not in data:
        return jsonify({"error": "Invalid request."}), 400
    if "playAudios" in data:
        if not isinstance(data["playAudios"], bool):
            return jsonify({"error": "Invalid request."}), 400
        _set_play_audios(data["playAudios"])
    if "event" in data:
        event_name = data["event"]
        sound_file = data.get("sound", "")
        if not isinstance(event_name, str) or not isinstance(sound_file, str):
            return jsonify({"error": "Invalid request."}), 400
        try:
            _set_sound_file(event_name, sound_file)
        except FeatureDisabledError:
            return jsonify({"error": "Functionality disabled."}), 403
        except ValueError:
            return jsonify({"error": "Invalid request."}), 400
    log_info("Audio settings updated", {"client": request.remote_addr, "playAudios": PLAY_AUDIOS})
    return jsonify({
        "playAudios": PLAY_AUDIOS,
        "sounds": {event: _read_env_var(audio.SOUND_ENV_VARS[event], audio.DEFAULT_SOUND_FILES.get(event, "")) for event in audio.SOUND_EVENTS},
        "available": audio.list_audio_files(),
    }), 200


@app.route("/api/log-sounds-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def log_sounds_enabled() -> tuple:
    if request.method == "GET":
        log_info("Log sounds enabled setting read", {"client": request.remote_addr})
        return jsonify({"playLogSounds": PLAY_LOG_SOUNDS}), 200

    data = request.get_json(silent=True) or {}
    if "playLogSounds" not in data or not isinstance(data["playLogSounds"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["playLogSounds"]
    try:
        _set_play_log_sounds(value)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("Log sounds enabled set", {"client": request.remote_addr, "playLogSounds": PLAY_LOG_SOUNDS})
    return jsonify({"playLogSounds": PLAY_LOG_SOUNDS}), 200


@app.route("/api/startup-sound-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def startup_sound_enabled() -> tuple:
    if request.method == "GET":
        log_info("Startup sound enabled setting read", {"client": request.remote_addr})
        return jsonify({"playStartupSound": PLAY_STARTUP_SOUND}), 200

    data = request.get_json(silent=True) or {}
    if "playStartupSound" not in data or not isinstance(data["playStartupSound"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["playStartupSound"]
    try:
        _set_play_startup_sound(value)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("Startup sound enabled set", {"client": request.remote_addr, "playStartupSound": PLAY_STARTUP_SOUND})
    return jsonify({"playStartupSound": PLAY_STARTUP_SOUND}), 200


@app.route("/api/audio/play", methods=["POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def play_audio_event() -> tuple:
    data = request.get_json(silent=True) or {}
    event_name = data.get("event", "")
    if not isinstance(event_name, str):
        return jsonify({"error": "Invalid request."}), 400
    try:
        _play_sound_event(event_name)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Audio play triggered", {"client": request.remote_addr, "event": event_name})
    return jsonify({"status": "ok"}), 200


@app.route("/api/shared-memory-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def shared_memory_enabled() -> tuple:
    if request.method == "GET":
        log_info("Shared memory enabled setting read", {"client": request.remote_addr})
        return jsonify({"sharedMemoryEnabled": SHARED_MEMORY_ENABLED}), 200

    data = request.get_json(silent=True) or {}
    if "sharedMemoryEnabled" not in data or not isinstance(data["sharedMemoryEnabled"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["sharedMemoryEnabled"]
    try:
        _set_shared_memory_enabled(value)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("Shared memory enabled set", {"client": request.remote_addr, "sharedMemoryEnabled": SHARED_MEMORY_ENABLED})
    return jsonify({"sharedMemoryEnabled": SHARED_MEMORY_ENABLED}), 200


@app.route("/api/api-keys-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def api_keys_enabled() -> tuple:
    if request.method == "GET":
        log_info("API keys enabled setting read", {"client": request.remote_addr})
        return jsonify({"apiKeysEnabled": API_KEYS_ENABLED}), 200

    data = request.get_json(silent=True) or {}
    if "apiKeysEnabled" not in data or not isinstance(data["apiKeysEnabled"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["apiKeysEnabled"]
    _set_api_keys_enabled(value)
    log_info("API keys enabled set", {"client": request.remote_addr, "apiKeysEnabled": API_KEYS_ENABLED})
    return jsonify({"apiKeysEnabled": API_KEYS_ENABLED}), 200


@app.route("/api/external-interactions-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def external_interactions_enabled() -> tuple:
    if request.method == "GET":
        log_info("External interactions enabled setting read", {"client": request.remote_addr})
        return jsonify({"externalInteractions": _external_interactions_enabled()}), 200

    data = request.get_json(silent=True) or {}
    if "externalInteractions" not in data or not isinstance(data["externalInteractions"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["externalInteractions"]
    _set_external_interactions(value)
    log_info("External interactions enabled set", {"client": request.remote_addr, "externalInteractions": value})
    return jsonify({"externalInteractions": _external_interactions_enabled()}), 200


@app.route("/api/automatic-update-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def automatic_update_enabled() -> tuple:
    if _is_project_functionality_disabled():
        log_warn("Automatic update toggle disabled due to integrity check failure in development mode")
        return jsonify({"automaticUpdate": AUTOMATIC_UPDATE, "disabled": True, "error": "Project functionalities disabled due to integrity check failure."}), 403
    if request.method == "GET":
        log_info("Automatic update enabled setting read", {"client": request.remote_addr})
        return jsonify({"automaticUpdate": AUTOMATIC_UPDATE}), 200

    # The toggle never changes in development mode (it is disabled there)
    if DEVELOPMENT:
        log_warn("Automatic update toggle disabled in development mode", {"client": request.remote_addr})
        return jsonify({"automaticUpdate": AUTOMATIC_UPDATE, "disabled": True, "error": "Automatic updates are disabled in development mode."}), 403

    data = request.get_json(silent=True) or {}
    if "automaticUpdate" not in data or not isinstance(data["automaticUpdate"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["automaticUpdate"]
    _set_automatic_update(value)
    log_info("Automatic update enabled set", {"client": request.remote_addr, "automaticUpdate": AUTOMATIC_UPDATE})
    return jsonify({"automaticUpdate": AUTOMATIC_UPDATE}), 200


@app.route("/api/automatic-plugin-library-update-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def automatic_plugin_library_update_enabled() -> tuple:
    if request.method == "GET":
        log_info("Automatic plugin library update enabled setting read", {"client": request.remote_addr})
        return jsonify({"automaticPluginLibraryUpdate": AUTOMATIC_PLUGIN_LIBRARY_UPDATE}), 200

    data = request.get_json(silent=True) or {}
    if "automaticPluginLibraryUpdate" not in data or not isinstance(data["automaticPluginLibraryUpdate"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["automaticPluginLibraryUpdate"]
    _set_automatic_plugin_library_update(value)
    log_info("Automatic plugin library update enabled set", {"client": request.remote_addr, "automaticPluginLibraryUpdate": AUTOMATIC_PLUGIN_LIBRARY_UPDATE})
    return jsonify({"automaticPluginLibraryUpdate": AUTOMATIC_PLUGIN_LIBRARY_UPDATE}), 200


@app.route("/api/automatic-plugin-upgrade-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def automatic_plugin_upgrade_enabled() -> tuple:
    if request.method == "GET":
        log_info("Automatic plugin upgrade enabled setting read", {"client": request.remote_addr})
        return jsonify({"automaticPluginUpgrade": AUTOMATIC_PLUGIN_UPGRADE}), 200

    data = request.get_json(silent=True) or {}
    if "automaticPluginUpgrade" not in data or not isinstance(data["automaticPluginUpgrade"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["automaticPluginUpgrade"]
    _set_automatic_plugin_upgrade(value)
    log_info("Automatic plugin upgrade enabled set", {"client": request.remote_addr, "automaticPluginUpgrade": AUTOMATIC_PLUGIN_UPGRADE})
    return jsonify({"automaticPluginUpgrade": AUTOMATIC_PLUGIN_UPGRADE}), 200


def _get_current_project_version() -> str:
    """Return the hash of the current stated version tag, or 'unknown'."""
    root = Path(__file__).resolve().parent.parent
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode("utf-8", errors="replace").strip()
    except Exception:
        return "unknown"
    if not tag:
        return "unknown"
    try:
        v = subprocess.check_output(
            ["git", "show", f"{tag}:hash"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode("utf-8", errors="replace").strip().split()[0]
    except Exception:
        return "unknown"
    if len(v) < 7:
        return "unknown"
    return v[:12]


def _get_effective_project_version() -> str:
    """Return the latest computed project hash (effective version), or 'unknown'."""
    try:
        h = _compute_local_project_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_indicated_project_version() -> str:
    """Return the hash indicated by the stored hash file, or 'unknown'."""
    try:
        h = _get_local_project_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_current_plugins_lib_version() -> str:
    """Return the hash of the current plugins-lib version, or 'unknown'."""
    try:
        h = plugin_bridge._read_stored_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
        h = plugin_bridge._compute_plugins_lib_hash()
        if h and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_effective_plugins_lib_version() -> str:
    """Return the latest computed plugins-lib hash (effective version), or 'unknown'."""
    try:
        h = plugin_bridge._compute_plugins_lib_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_indicated_plugins_lib_version() -> str:
    """Return the hash indicated by the stored plugins-lib hash file, or 'unknown'."""
    try:
        h = plugin_bridge._read_stored_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_local_project_hash() -> str | None:
    try:
        p = Path(__file__).resolve().parent.parent / "hash"
        return p.read_text(encoding="utf-8").strip().split()[0]
    except Exception:
        return None


# Mirrors the exclusion set of the CI workflow (.github/workflows/project-hash.yml).
_PROJECT_HASH_EXCLUDE_DIRS = {".git", ".venv", "venv", "ENV", "env", ".vscode", ".idea", "logs", "__pycache__", ".pytest_cache", "htmlcov", "dist", "build", ".github/__pycache__"}
_PROJECT_HASH_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _should_exclude_from_project_hash(rel: str) -> bool:
    """Decide whether a tracked file (relative posix path) is excluded from the project hash."""
    if rel == "hash":
        return True
    if rel == "resources/configuration.json":
        return True
    if rel.startswith("resources/plugins-lib/"):
        return True
    if rel.startswith("resources/audios/"):
        return True
    parts = rel.split("/")
    for part in parts:
        if part in _PROJECT_HASH_EXCLUDE_DIRS or part == "__pycache__":
            return True
        if part.endswith(".egg-info"):
            return True
    if Path(rel).suffix in _PROJECT_HASH_EXCLUDE_SUFFIXES:
        return True
    if Path(rel).suffix == ".so":
        return True
    return False


def _compute_local_project_hash() -> str | None:
    """Recompute the project hash from the tracked files on disk (local integrity check).

    Mirrors the CI algorithm: each tracked file (from ``git ls-files``) is hashed
    individually (SHA-256 hex), sorted by filename, concatenated and hashed again.
    Text files are normalised to LF before hashing so a Windows checkout
    (``core.autocrlf``) hashes identically to the Linux CI checkout. Returns None
    when the tracked file list cannot be determined (e.g. not a git checkout).
    """
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return None
        rels = [line for line in out.stdout.splitlines() if line]
    except Exception:
        return None
    entries: list[tuple[str, str]] = []
    for rel in rels:
        if _should_exclude_from_project_hash(rel):
            continue
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" not in data:
            data = data.replace(b"\r\n", b"\n")
        h = hashlib.sha256(data).hexdigest()
        entries.append((rel, h))
    entries.sort(key=lambda x: x[0])
    merged = "".join(h for _, h in entries)
    return hashlib.sha256(merged.encode("utf-8")).hexdigest()


def _verify_local_project_integrity() -> bool:
    """Recompute the local project hash and compare it against the stored ``hash`` file.

    A mismatch means the local project files differ from the recorded project hash
    (illicit interaction or a partial install) — logs an error and returns False.
    Also returns False when the check cannot run (e.g. not a git checkout).
    """
    stored = _get_local_project_hash()
    if stored is None or not stored:
        log_error("Project integrity check failed: local hash file missing", {"path": "hash"})
        return False
    computed = _compute_local_project_hash()
    if computed is None:
        log_error("Project integrity check failed: cannot enumerate tracked files (not a git checkout?)")
        return False
    if computed.strip().lower() != stored.strip().lower():
        log_error("Project integrity check failed: local files differ from recorded project hash (illicit interaction?)", {"computed": computed, "stored": stored})
        return False
    log_info("Project integrity check passed", {"hash": stored})
    return True


def _is_project_functionality_disabled() -> bool:
    """Return True when project functionalities must be disabled (integrity failed in development mode)."""
    return (not _PROJECT_INTEGRITY_OK and DEVELOPMENT)


def _verify_disabled_styles() -> bool:
    """Verify that every disabled interactive element has cursor: not-allowed.

    Reads ui/css/index.css and ensures each :disabled rule for interactive
    selectors contains cursor: not-allowed. Logs an error if any rule is missing
    and returns False; returns True when all checked rules are present.
    """
    css_path = Path(__file__).resolve().parent.parent / "ui" / "css" / "index.css"
    try:
        css = css_path.read_text(encoding="utf-8")
    except Exception as exc:
        log_warn("Disabled styles check skipped: cannot read CSS", {"error": str(exc)})
        return True
    import re
    # Find all :disabled blocks and verify cursor: not-allowed
    pattern = re.compile(r"([^{]+:disabled[^{]*)\{([^}]+)\}", re.MULTILINE)
    missing = []
    for selector, block in pattern.findall(css):
        if "cursor" not in block or "not-allowed" not in block:
            # Only enforce for interactive elements (button, input, select, textarea, toggle, etc.)
            if any(k in selector for k in ("button", "input", "select", "textarea", "toggle", "generate-api-key-btn", "api-key-name-input", "random-name-btn", "page-action-btn", "icon-btn", "external-interactions-segment", "sound-select", "pill-action-btn")):
                missing.append(selector.strip())
    if missing:
        log_error("Disabled styles check failed: missing cursor: not-allowed", {"selectors": missing})
        return False
    log_info("Disabled styles check passed")
    return True


def _get_version_tags() -> list[str]:
    """Return semantic-version tags on origin sorted ascending (e.g. ['v1.0.0', ...])."""
    try:
        root = Path(__file__).resolve().parent.parent
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", "origin"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).decode("utf-8", errors="replace")
    except Exception:
        return []
    versions: list[tuple[tuple[int, ...], str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):].removesuffix("^{}")
        m = re.match(r"^v(\d+(?:\.\d+)*)$", name)
        if not m:
            continue
        key = tuple(int(part) for part in m.group(1).split("."))
        versions.append((key, name))
    if not versions:
        return []
    versions.sort(key=lambda item: item[0])
    tags: list[str] = []
    for _, name in versions:
        if name not in tags:
            tags.append(name)
    return tags


def _get_latest_version_tag() -> str | None:
    """Return the highest semantic-version tag on origin (e.g. 'v3.0.0'), or None."""
    tags = _get_version_tags()
    return tags[-1] if tags else None


def _fetch_remote_project_hash(timeout: int = 8) -> str | None:
    import ssl
    import urllib.request
    # Compare against the hash asset of the latest release, not the latest commit.
    url = "https://github.com/LorenBll/Akupara/releases/latest/download/hash"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with github_api.github_urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()[0]
            if data:
                return data
    except github_api.GithubRateLimitedError:
        # Version check: rate limit on both token and anonymous → not an error, keep running.
        log_warn("GitHub API rate limit exceeded while fetching remote project hash — skipping version check", {"url": url})
        raise
    except Exception:
        pass
    # Fallback: the raw hash file at the highest version tag (releases without an asset yet)
    tag = _get_latest_version_tag()
    if not tag:
        return None
    url = f"https://raw.githubusercontent.com/LorenBll/Akupara/{tag}/hash"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with github_api.github_urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()[0]
            if data:
                return data
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit exceeded while fetching remote project hash (fallback) — skipping version check", {"url": url})
        raise
    except Exception:
        return None
    return None


def _fetch_release_hash_for_tag(tag: str, timeout: int = 8) -> str | None:
    """Fetch the stored ``hash`` file at a version tag (that release's hash)."""
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    url = f"https://raw.githubusercontent.com/LorenBll/Akupara/{tag}/hash"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with github_api.github_urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()
            if data:
                return data[0]
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit exceeded while fetching release hash for tag — skipping version check", {"tag": tag, "url": url})
        raise
    except Exception:
        return None
    return None


def _is_known_release_hash(local_hash: str | None) -> bool:
    """Return True when ``local_hash`` matches the latest or any previous release's hash.

    If GitHub is rate limited (both token and anonymous), the check is skipped
    and ``True`` is returned so the startup does **not** treat a version check
    rate limit as an illicit interaction and does **not** crash.
    """
    local = (local_hash or "").strip().lower()
    if not local:
        return False
    try:
        latest = _fetch_remote_project_hash()
    except github_api.GithubRateLimitedError:
        log_warn("GitHub rate limit during known-release check — assuming known to keep running", {"local": local})
        return True
    if latest and latest.strip().lower() == local:
        return True
    for tag in reversed(_get_version_tags()):
        try:
            known = _fetch_release_hash_for_tag(tag)
        except github_api.GithubRateLimitedError:
            log_warn("GitHub rate limit during known-release tag fetch — assuming known to keep running", {"tag": tag})
            return True
        if known and known.strip().lower() == local:
            return True
    return False


def _is_update_available() -> bool:
    # Integrity: effective vs indicated before update check
    effective = _compute_local_project_hash()
    indicated = _get_local_project_hash()
    if effective and indicated and effective.strip().lower() != indicated.strip().lower():
        global _PROJECT_INTEGRITY_OK
        _PROJECT_INTEGRITY_OK = False
        log_error("Project integrity check failed before update check", {"effective": effective, "indicated": indicated})
        try:
            audio.get_audio_orchestrator().start()
        except Exception:
            pass
        try:
            audio.play_sound("error")
        except Exception:
            pass
        if not DEVELOPMENT:
            return False
        # Development true: skip update check when integrity fails
        return False
    else:
        _PROJECT_INTEGRITY_OK = True
    local = effective or indicated
    if not local:
        local = _get_local_project_hash()
    try:
        remote = _fetch_remote_project_hash()
    except github_api.GithubRateLimitedError:
        # Version check rate limit → not an error, keep running as if no update.
        log_warn("GitHub API rate limit during update check — assuming no update", {"local": local})
        return False
    if not local or not remote:
        return False
    return local.strip().lower() != remote.strip().lower()


def _is_plugin_update_available() -> bool:
    # Integrity: effective vs indicated before update check (always mandatory, even in development)
    effective = plugin_bridge._compute_plugins_lib_hash()
    indicated = plugin_bridge._read_stored_hash()
    if effective and indicated and effective.strip().lower() != indicated.strip().lower():
        global _PLUGIN_INTEGRITY_OK
        _PLUGIN_INTEGRITY_OK = False
        log_error("Plugin library integrity check failed before update check", {"effective": effective, "indicated": indicated})
        try:
            audio.get_audio_orchestrator().start()
        except Exception:
            pass
        try:
            audio.play_sound("error")
        except Exception:
            pass
        try:
            plugin_bridge.get_plugin_bridge().stop()
        except Exception:
            pass
        # Still check remote for update availability to allow recovery, but keep integrity flag false
    else:
        _PLUGIN_INTEGRITY_OK = True
    local = effective
    remote = plugin_bridge._fetch_remote_hash()
    if not local or not remote:
        return False
    return local.strip().lower() != remote.strip().lower()


def _get_local_plugins_lib_hash() -> str | None:
    try:
        return plugin_bridge._read_stored_hash()
    except Exception:
        return None


def _perform_plugins_lib_update() -> bool:
    """Update the plugin library to the latest commit on the Akupara repository, then restart."""
    process_worker = None
    try:
        try:
            fname = audio._read_sound_file("process")
            if fname:
                p = audio.AUDIOS_DIR / fname
                if p.is_file():
                    try:
                        audio.get_audio_orchestrator().start()
                    except Exception:
                        pass
                    process_worker = audio.get_audio_orchestrator().play(p, loop=True)
        except Exception:
            pass
        root = Path(__file__).resolve().parent.parent
        subprocess.run(["git", "fetch", "origin"], cwd=root, capture_output=True, timeout=30)
        proc = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, timeout=30)
        if proc.returncode != 0:
            subprocess.run(["git", "checkout", "main"], cwd=root, capture_output=True, timeout=10)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=root, capture_output=True, timeout=30)
        log_info("Plugin library updated to latest version", {"remote_hash": plugin_bridge._fetch_remote_hash()})
        return True
    except Exception as exc:
        log_error("Plugin library update failed", {"error": str(exc)})
        return False
    finally:
        if process_worker is not None:
            try:
                process_worker.stop()
            except Exception:
                pass
            try:
                audio.get_audio_orchestrator().reap_finished()
            except Exception:
                pass


def _perform_project_update() -> bool:
    """Update local repo to latest released version without deleting stored data, then restart."""
    process_worker = None
    try:
        try:
            fname = audio._read_sound_file("process")
            if fname:
                p = audio.AUDIOS_DIR / fname
                if p.is_file():
                    try:
                        audio.get_audio_orchestrator().start()
                    except Exception:
                        pass
                    process_worker = audio.get_audio_orchestrator().play(p, loop=True)
        except Exception:
            pass
        root = Path(__file__).resolve().parent.parent
        # Use git to update — preserves untracked files (.env, logs, etc.) and ignored files
        # Fetch latest
        subprocess.run(["git", "fetch", "origin"], cwd=root, capture_output=True, timeout=30)
        # Try pull --ff-only; fallback to reset if needed but preserve untracked
        proc = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, timeout=30)
        if proc.returncode != 0:
            # Fallback: checkout main and reset hard to origin/main but keep untracked (not deleting)
            subprocess.run(["git", "checkout", "main"], cwd=root, capture_output=True, timeout=10)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=root, capture_output=True, timeout=30)
        try:
            remote_hash = _fetch_remote_project_hash()
        except github_api.GithubRateLimitedError:
            remote_hash = None
            log_warn("GitHub rate limit while fetching remote hash after project update — continuing")
        log_info("Project updated to latest version", {"remote_hash": remote_hash})
        return True
    except Exception as exc:
        log_error("Project update failed", {"error": str(exc)})
        return False
    finally:
        if process_worker is not None:
            try:
                process_worker.stop()
            except Exception:
                pass
            try:
                audio.get_audio_orchestrator().reap_finished()
            except Exception:
                pass


@app.route("/api/check-for-updates", methods=["POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def check_for_updates() -> tuple:
    global _UPDATE_AVAILABLE, _UPDATE_AVAILABLE_AT_STARTUP
    # No version check in development mode — never fetch the remote hash
    if DEVELOPMENT:
        log_info("Check for updates skipped in development mode", {"client": request.remote_addr})
        return jsonify({"updateAvailable": False, "currentVersion": _get_current_project_version(), "integrityOk": _PROJECT_INTEGRITY_OK}), 200
    # Integrity check happens inside _is_update_available (effective vs indicated)
    available = _is_update_available()
    # If project integrity failed in development mode, update checks are disabled
    if not _PROJECT_INTEGRITY_OK and DEVELOPMENT:
        log_warn("Check for updates skipped due to project integrity failure in development mode")
        return jsonify({"updateAvailable": False, "currentVersion": _get_current_project_version(), "integrityOk": False}), 200
    # Repeat the startup checks (development is off here): integrity failure crashes,
    # and so does a local hash matching no known release (latest or previous).
    if not _PROJECT_INTEGRITY_OK:
        log_error("Project integrity check failed on manual update check — not continuing")
        exit(1)
    if available:
        local_hash = _get_local_project_hash() or _compute_local_project_hash()
        if not _is_known_release_hash(local_hash):
            log_error("Project version unknown: local hash matches no Akupara release (illicit interaction?)", {"local": local_hash})
            try:
                audio.get_audio_orchestrator().start()
            except Exception:
                pass
            try:
                audio.play_sound("error")
            except Exception:
                pass
            exit(1)
    # Cache the result so the server-rendered button stays consistent with the check
    _UPDATE_AVAILABLE = available
    _UPDATE_AVAILABLE_AT_STARTUP = available
    log_info("Check for updates", {"client": request.remote_addr, "available": available, "current": _get_current_project_version()})
    return jsonify({"updateAvailable": available, "currentVersion": _get_current_project_version(), "integrityOk": _PROJECT_INTEGRITY_OK}), 200


@app.route("/api/update-now", methods=["POST", "HEAD", "OPTIONS"])
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def update_now() -> tuple:
    # Not network callable — only localhost can reach (no @network decorator)
    # Extra localhost check: ensure request is from local device
    if request.remote_addr not in _get_local_device_addresses():
        log_warn("Update rejected: not localhost", {"client": request.remote_addr})
        return jsonify({"error": "Local device access only."}), 403
    # Manual updates never run in development mode (the button is disabled there)
    if DEVELOPMENT:
        log_warn("Update rejected: updates are disabled in development mode", {"client": request.remote_addr})
        return jsonify({"error": "Updates are disabled in development mode."}), 403
    # Integrity check before update
    effective = _compute_local_project_hash()
    indicated = _get_local_project_hash()
    if effective and indicated and effective.strip().lower() != indicated.strip().lower():
        if not DEVELOPMENT:
            log_error("Update rejected: project integrity check failed and development is false")
            return jsonify({"error": "Project integrity check failed."}), 500
        else:
            log_warn("Update rejected: project integrity check failed in development mode")
            return jsonify({"error": "Project integrity check failed."}), 400
    if not _is_update_available():
        return jsonify({"error": "No update available."}), 400
    log_info("Update now requested", {"client": request.remote_addr, "current": _get_current_project_version()})
    # Perform update in background then restart
    def do_update():
        if _perform_project_update():
            _restart()
    threading.Timer(0.5, do_update).start()
    return jsonify({"status": "ok", "message": "Updating and restarting."}), 200


@app.route("/api/check-for-plugin-updates", methods=["POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def check_for_plugin_updates() -> tuple:
    global _PLUGIN_UPDATE_AVAILABLE, _PLUGIN_UPDATE_AVAILABLE_AT_STARTUP
    # Integrity check happens inside _is_plugin_update_available (effective vs indicated)
    available = _is_plugin_update_available()
    if not _PLUGIN_INTEGRITY_OK:
        log_warn("Check for plugin library updates skipped due to integrity failure")
        return jsonify({"updateAvailable": False, "currentVersion": _get_current_plugins_lib_version(), "integrityOk": False}), 200
    _PLUGIN_UPDATE_AVAILABLE = available
    _PLUGIN_UPDATE_AVAILABLE_AT_STARTUP = available
    log_info("Check for plugin library updates", {"client": request.remote_addr, "available": available, "current": _get_current_plugins_lib_version()})
    return jsonify({"updateAvailable": available, "currentVersion": _get_current_plugins_lib_version(), "integrityOk": True}), 200


@app.route("/api/update-plugins-now", methods=["POST", "HEAD", "OPTIONS"])
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def update_plugins_now() -> tuple:
    if request.remote_addr not in _get_local_device_addresses():
        log_warn("Plugin library update rejected: not localhost", {"client": request.remote_addr})
        return jsonify({"error": "Local device access only."}), 403
    # Integrity check before update
    effective = plugin_bridge._compute_plugins_lib_hash()
    indicated = plugin_bridge._read_stored_hash()
    if effective and indicated and effective.strip().lower() != indicated.strip().lower():
        log_error("Plugin library update rejected: integrity check failed", {"effective": effective, "indicated": indicated})
        return jsonify({"error": "Plugin library integrity check failed."}), 400
    if not _is_plugin_update_available():
        return jsonify({"error": "No update available."}), 400
    log_info("Plugin library update now requested", {"client": request.remote_addr, "current": _get_current_plugins_lib_version()})
    def do_update():
        if _perform_plugins_lib_update():
            _restart()
    threading.Timer(0.5, do_update).start()
    return jsonify({"status": "ok", "message": "Updating and restarting."}), 200


@app.route("/api/check-for-plugin-upgrades", methods=["POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def check_for_plugin_upgrades() -> tuple:
    global _INSTALLED_PLUGINS_PENDING_UPGRADES
    # Manual availability check only — never applies automatic upgrades,
    # independently of AUTOMATIC_PLUGIN_UPGRADE.
    try:
        _, pending = plugin_bridge.get_plugin_bridge().discover_installed_plugins(development=DEVELOPMENT, auto_upgrade=False)
    except Exception as exc:
        log_warn("Manual installed plugins upgrade check failed", {"client": request.remote_addr, "error": str(exc)})
        return jsonify({"error": "Upgrade check failed."}), 500
    _INSTALLED_PLUGINS_PENDING_UPGRADES = pending
    log_info("Manual installed plugins upgrade check", {"client": request.remote_addr, "pending": len(pending)})
    return jsonify({
        "upgradesAvailable": bool(pending),
        "plugins": [{"hash": item.get("hash"), "name": item.get("name"), "installedVersion": item.get("installed_version"), "latestTag": item.get("latest_tag")} for item in pending],
    }), 200


@app.route("/api/upgrade-all-plugins", methods=["POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def upgrade_all_plugins() -> tuple:
    global _INSTALLED_PLUGINS_PENDING_UPGRADES
    try:
        _, pending = plugin_bridge.get_plugin_bridge().discover_installed_plugins(development=DEVELOPMENT, auto_upgrade=True)
    except Exception as exc:
        log_warn("Upgrade all plugins failed", {"client": request.remote_addr, "error": str(exc)})
        return jsonify({"error": "Upgrade failed."}), 500
    _INSTALLED_PLUGINS_PENDING_UPGRADES = pending
    log_info("Upgrade all plugins requested", {"client": request.remote_addr, "stillPending": len(pending)})
    return jsonify({
        "status": "ok",
        "stillPending": bool(pending),
        "plugins": [{"hash": item.get("hash"), "name": item.get("name"), "installedVersion": item.get("installed_version"), "latestTag": item.get("latest_tag")} for item in pending],
    }), 200


def _resolve_installed_plugin_folder(folder: str) -> Path | None:
    """Resolve an installed plugin folder name to its path (None when invalid/missing)."""
    name = (folder or "").strip()
    if not name or "/" in name or "\\" in name or ":" in name or "\x00" in name or name in {".", ".."}:
        return None
    plugins_dir = plugin_bridge._plugins_dir()
    path = plugins_dir / name
    try:
        if path.parent != plugins_dir or not path.is_dir():
            return None
    except Exception:
        return None
    return path


def _list_installed_plugins() -> list[dict]:
    """List installed plugins (dev ones included) with names and hashes (disk-only, no network)."""
    result: list[dict] = []
    plugins_dir = plugin_bridge._plugins_dir()
    if not plugins_dir.is_dir():
        return result
    try:
        catalog = {str(e.get("hash", "")).strip().lower(): str(e.get("name", "")).strip() for e in plugin_bridge._load_plugins()}
    except Exception:
        catalog = {}
    for entry in sorted(plugins_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        folder = entry.name
        if folder.startswith("dev-"):
            result.append({"folder": folder, "name": folder, "dev": True, "effective": "", "indicated": ""})
            continue
        try:
            effective = plugin_bridge._compute_plugin_folder_hash(entry) or ""
        except Exception:
            effective = ""
        try:
            stored = plugin_bridge._read_plugin_hash_file(entry)
        except Exception:
            stored = None
        result.append({
            "folder": folder,
            "name": catalog.get(folder.strip().lower(), folder),
            "dev": False,
            "effective": effective,
            "indicated": stored or folder,
        })
    result.sort(key=lambda item: (bool(item["dev"]), str(item["name"]).casefold()))
    return result


def _delete_installed_plugin(folder: str) -> bool:
    """Stop and delete an installed plugin folder (dev ones included)."""
    path = _resolve_installed_plugin_folder(folder)
    if path is None:
        return False
    import shutil
    shutil.rmtree(path)
    audio.play_audio("success")()
    return True


@app.route("/api/installed-plugins", methods=["GET", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def installed_plugins() -> tuple:
    log_info("Installed plugins list read", {"client": request.remote_addr})
    return jsonify({"plugins": _list_installed_plugins()}), 200


@app.route("/api/installed-plugins/<path:folder>", methods=["POST", "DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "DELETE", "OPTIONS")
def installed_plugin_item(folder: str) -> tuple:
    global _INSTALLED_PLUGINS_PENDING_UPGRADES
    if request.method == "DELETE":
        try:
            deleted = _delete_installed_plugin(folder)
        except OSError as exc:
            log_warn("Installed plugin deletion failed", {"client": request.remote_addr, "folder": folder, "error": str(exc)})
            return jsonify({"error": "Deletion failed."}), 500
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        _INSTALLED_PLUGINS_PENDING_UPGRADES = [item for item in _INSTALLED_PLUGINS_PENDING_UPGRADES if item.get("folder") != folder.strip()]
        log_info("Installed plugin deleted", {"client": request.remote_addr, "folder": folder})
        return jsonify({"status": "ok"}), 200
    # POST upgrades only the singular plugin (dev plugins cannot be upgraded)
    if (folder or "").strip().startswith("dev-"):
        return jsonify({"error": "Dev plugins cannot be upgraded."}), 400
    path = _resolve_installed_plugin_folder(folder)
    if path is None:
        return jsonify({"error": "Not found."}), 404
    only = folder.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", only):
        try:
            file_hash = plugin_bridge._read_plugin_hash_file(path)
        except Exception:
            file_hash = None
        if file_hash and re.fullmatch(r"[0-9a-fA-F]{64}", file_hash.strip()):
            only = file_hash.strip()
    try:
        _, pending_before = plugin_bridge.get_plugin_bridge().discover_installed_plugins(development=DEVELOPMENT, auto_upgrade=False)
    except Exception as exc:
        log_warn("Single plugin upgrade check failed", {"client": request.remote_addr, "folder": folder, "error": str(exc)})
        return jsonify({"error": "Upgrade check failed."}), 500
    if not any(item.get("folder") == folder.strip() for item in pending_before):
        log_info("Single plugin upgrade not needed", {"client": request.remote_addr, "folder": folder})
        audio.play_audio("acknowledge")()
        return jsonify({"status": "ok", "upgraded": False, "upToDate": True, "stillPending": bool(_INSTALLED_PLUGINS_PENDING_UPGRADES)}), 200
    try:
        _, pending_after = plugin_bridge.get_plugin_bridge().discover_installed_plugins(development=DEVELOPMENT, auto_upgrade=True, only_hash=only)
    except Exception as exc:
        log_warn("Single plugin upgrade failed", {"client": request.remote_addr, "folder": folder, "error": str(exc)})
        return jsonify({"error": "Upgrade failed."}), 500
    _INSTALLED_PLUGINS_PENDING_UPGRADES = [item for item in _INSTALLED_PLUGINS_PENDING_UPGRADES if item.get("folder") != folder.strip()] + pending_after
    upgraded = not any(item.get("folder") == folder.strip() for item in pending_after)
    if upgraded:
        audio.play_audio("success")()
    log_info("Single plugin upgrade requested", {"client": request.remote_addr, "folder": folder, "upgraded": upgraded})
    return jsonify({"status": "ok", "upgraded": upgraded, "upToDate": False, "stillPending": bool(_INSTALLED_PLUGINS_PENDING_UPGRADES)}), 200


_FORBIDDEN_KEY_NAME_CHARS = set(" ,;:\\/%\"'")


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


def _api_key_id() -> str:
    return secrets.token_hex(16)


def _api_key_cipher() -> Fernet:
    key = _read_env_var("API_KEY_ENCRYPTION_KEY")
    if not key:
        raise ValueError("API key encryption key not configured.")
    return Fernet(key.encode("utf-8"))


def _api_key_encrypt(key: str) -> str:
    return _api_key_cipher().encrypt(key.encode("utf-8")).decode("utf-8")


def _api_key_decrypt(token: str) -> str:
    try:
        return _api_key_cipher().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def _load_api_keys() -> list[dict]:
    value = _read_env_var("API_KEYS")
    keys: list[dict] = []
    if not value:
        return keys
    try:
        data = json.loads(value)
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                key = str(entry.get("key") or "").strip()
                if not name or not key:
                    continue
                key_id = str(entry.get("id") or "").strip() or _api_key_id()
                keys.append({"name": name, "key": key, "id": key_id})
            return keys
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Legacy comma-separated fallback ("name:key:id,...")
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        fields = part.split(":")
        if len(fields) < 2:
            continue
        name = fields[0].strip()
        key = fields[1].strip()
        if not name or not key:
            continue
        key_id = fields[2].strip() if len(fields) > 2 and fields[2].strip() else _api_key_id()
        keys.append({"name": name, "key": key, "id": key_id})
    return keys


# In-memory store of API keys in plaintext, loaded once at startup. The .env file
# holds only the encrypted (Fernet) tokens; the cipher layer never reaches the UI.
_api_key_store: list[dict] = []


def _refresh_api_key_store() -> None:
    """Load the API keys from .env and decrypt them into the in-memory plaintext store."""
    global _api_key_store
    _api_key_store = []
    for entry in _load_api_keys():
        plain = _api_key_decrypt(entry["key"])
        if not plain:
            continue
        _api_key_store.append({"name": entry["name"], "key": plain, "id": entry["id"]})


def _save_api_keys(entries: list[dict]) -> None:
    """Persist plaintext API key entries by encrypting them into .env, then refresh the store."""
    global _api_key_store
    encrypted = [
        {"name": e["name"], "key": _api_key_encrypt(e["key"]), "id": e.get("id") or _api_key_id()}
        for e in entries
    ]
    _write_env_var("API_KEYS", json.dumps(encrypted, ensure_ascii=False))
    _api_key_store = [{"name": e["name"], "key": e["key"], "id": e.get("id") or _api_key_id()} for e in entries]


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _list_api_keys() -> list[dict]:
    _require_api_keys_enabled()
    return [{"name": entry["name"], "key": entry["key"]} for entry in _api_key_store]


@audio.play_audio("success")
def _create_api_key(name: str) -> dict:
    _require_api_keys_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    key = _generate_api_key()
    if any(entry["name"].lower() == name.lower() for entry in _api_key_store):
        raise DuplicateNameError("An API key with this name already exists.")
    entry = {"name": name, "key": key, "id": _api_key_id()}
    _save_api_keys(_api_key_store + [entry])
    return {"name": entry["name"], "key": entry["key"]}


def _delete_api_key(key: str) -> bool:
    _require_api_keys_enabled()
    remaining = [entry for entry in _api_key_store if entry["key"] != key]
    if len(remaining) == len(_api_key_store):
        return False
    _save_api_keys(remaining)
    audio.play_audio("success")()
    return True


@audio.play_audio("acknowledge")
def _rename_api_key(key: str, name: str) -> dict | None:
    _require_api_keys_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    target = next((entry for entry in _api_key_store if entry["key"] == key), None)
    if not target:
        return None
    if any(entry["name"].lower() == name.lower() and entry is not target for entry in _api_key_store):
        raise DuplicateNameError("An API key with this name already exists.")
    target["name"] = name
    _save_api_keys(_api_key_store)
    return {"name": target["name"], "key": target["key"]}


@app.route("/api/api-keys", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def api_keys() -> tuple:
    if request.method == "GET":
        try:
            keys = _list_api_keys()
        except FeatureDisabledError:
            return jsonify({"error": "Functionality disabled."}), 403
        log_info("API keys read", {"client": request.remote_addr})
        return jsonify({"apiKeys": keys}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not API_KEYS_ENABLED:
        return jsonify({"error": "Functionality disabled."}), 403
    try:
        entry = _create_api_key(name)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    except DuplicateNameError:
        return jsonify({"error": "Already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("API key generated", {"client": request.remote_addr, "name": name})
    return jsonify(entry), 201


@app.route("/api/api-keys/<path:key>", methods=["PATCH", "DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def api_key_item(key: str) -> tuple:
    if request.method == "DELETE":
        try:
            deleted = _delete_api_key(key)
        except FeatureDisabledError:
            return jsonify({"error": "Functionality disabled."}), 403
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        log_info("API key deleted", {"client": request.remote_addr})
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    try:
        target = _rename_api_key(key, name)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    except DuplicateNameError:
        return jsonify({"error": "Already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if target is None:
        return jsonify({"error": "Not found."}), 404
    log_info("API key renamed", {"client": request.remote_addr, "name": name})
    return jsonify(target), 200


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
    raw = _read_env_var("SHARED_MEMORY")
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
    _write_env_var("SHARED_MEMORY", json.dumps(variables))


def _list_shared_memory() -> list[dict]:
    _require_shared_memory_enabled()
    return _load_shared_memory()


@audio.play_audio("success")
def _create_shared_variable(name: str, value, value_type: str) -> dict:
    _require_shared_memory_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid shared variable name.")
    value = _normalize_shared_value(value, value_type)
    value = _validate_plaintext_value(value, "shared variable value")
    variables = _load_shared_memory()
    if any(entry["name"].lower() == name.lower() for entry in variables):
        raise DuplicateNameError("A shared variable with this name already exists.")
    entry = {"name": name, "type": value_type, "value": value}
    variables.append(entry)
    _save_shared_memory(variables)
    return entry


@audio.play_audio("acknowledge")
def _update_shared_variable(name: str, value, value_type=None) -> dict | None:
    _require_shared_memory_enabled()
    variables = _load_shared_memory()
    target = next((entry for entry in variables if entry["name"] == name), None)
    if not target:
        return None
    new_type = value_type if value_type is not None else target["type"]
    target["value"] = _normalize_shared_value(value, new_type)
    target["value"] = _validate_plaintext_value(target["value"], "shared variable value")
    target["type"] = new_type
    _save_shared_memory(variables)
    return target


def _delete_shared_variable(name: str) -> bool:
    _require_shared_memory_enabled()
    variables = _load_shared_memory()
    remaining = [entry for entry in variables if entry["name"] != name]
    if len(remaining) == len(variables):
        return False
    _save_shared_memory(remaining)
    audio.play_audio("success")()
    return True


@app.route("/api/shared-memory", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def shared_memory() -> tuple:
    if request.method == "GET":
        try:
            variables = _list_shared_memory()
        except FeatureDisabledError:
            return jsonify({"error": "Functionality disabled."}), 403
        log_info("Shared memory read", {"client": request.remote_addr})
        return jsonify({"sharedMemory": variables}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    value = data.get("value")
    value_type = data.get("type")
    if not _effective_shared_memory_enabled():
        return jsonify({"error": "Functionality disabled."}), 403
    try:
        entry = _create_shared_variable(name, value, value_type)
    except FeatureDisabledError:
        return jsonify({"error": "The internal interactions functionality is disabled."}), 403
    except DuplicateNameError:
        return jsonify({"error": "A shared variable with this name already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Shared variable created", {"client": request.remote_addr, "name": name, "type": value_type})
    return jsonify(entry), 201


@app.route("/api/shared-memory/<path:name>", methods=["DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("DELETE", "OPTIONS")
def shared_memory_delete(name: str) -> tuple:
    try:
        deleted = _delete_shared_variable(name)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    if not deleted:
        return jsonify({"error": "Not found."}), 404
    log_info("Shared variable deleted", {"client": request.remote_addr, "name": name})
    return jsonify({"status": "ok"}), 200


@app.route("/api/shared-memory/<path:name>", methods=["PATCH", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("PATCH", "OPTIONS")
def shared_memory_edit(name: str) -> tuple:
    data = request.get_json(silent=True) or {}
    value = data.get("value")
    value_type = data.get("type")
    try:
        target = _update_shared_variable(name, value, value_type)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if target is None:
        return jsonify({"error": "Not found."}), 404
    log_info("Shared variable updated", {"client": request.remote_addr, "name": name})
    return jsonify(target), 200



def _load_plugin_event_subscriptions() -> dict[str, list[str]]:
    raw = _read_env_var("PLUGIN_EVENT_SUBSCRIPTIONS")
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
        # Validate event name: allow alphanumeric, underscore, hyphen, dot
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", event.strip()):
            continue
        cleaned: list[str] = []
        for p in plugins:
            if not isinstance(p, str) or not p.strip():
                continue
            # Validate plugin name
            if not _is_valid_key_name(p):
                continue
            cleaned.append(p.strip())
        # Deduplicate case-insensitive but preserve original case
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
    # Normalize and sort for determinism
    normalized: dict[str, list[str]] = {}
    for event in sorted(data.keys()):
        plugins = data[event]
        if not isinstance(plugins, list):
            plugins = []
        # Sort plugins case-insensitive
        normalized[event] = sorted(plugins, key=lambda x: x.casefold())
    _write_env_var("PLUGIN_EVENT_SUBSCRIPTIONS", json.dumps(normalized, ensure_ascii=False))


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
    audio.play_audio("success")()
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
    # Check duplicate (case-insensitive)
    if any(p.casefold() == plugin.casefold() for p in data[event]):
        raise DuplicateNameError("Plugin already subscribed to this event.")
    # Check plugin exists in library (optional, but helpful)
    # We don't enforce strict existence to allow future plugins, but validate name
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
    audio.play_audio("success")()
    return True


@app.route("/api/plugin-events", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def plugin_events() -> tuple:
    if not INTERNAL_INTERACTIONS:
        return jsonify({"error": "Internal interactions disabled."}), 403
    if request.method == "GET":
        data = _load_plugin_event_subscriptions()
        log_info("Plugin events read", {"client": request.remote_addr})
        return jsonify({"pluginEvents": data}), 200

    data = request.get_json(silent=True) or {}
    event = data.get("event")
    plugin = data.get("plugin")
    try:
        _add_plugin_event(event, plugin)
    except DuplicateNameError:
        return jsonify({"error": "Already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Plugin event created", {"client": request.remote_addr, "event": event})
    return jsonify({"event": event, "plugins": [plugin] if plugin else []}), 201


@app.route("/api/plugin-events/<path:event>", methods=["DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("DELETE", "OPTIONS")
def plugin_event_delete(event: str) -> tuple:
    if not INTERNAL_INTERACTIONS:
        return jsonify({"error": "Internal interactions disabled."}), 403
    try:
        deleted = _remove_plugin_event(event)
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if not deleted:
        return jsonify({"error": "Not found."}), 404
    log_info("Plugin event deleted", {"client": request.remote_addr, "event": event})
    return jsonify({"status": "ok"}), 200


@app.route("/api/plugin-events/<path:event>/plugins", methods=["POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("POST", "HEAD", "OPTIONS")
def plugin_event_plugins(event: str) -> tuple:
    if not INTERNAL_INTERACTIONS:
        return jsonify({"error": "Internal interactions disabled."}), 403
    if request.method == "HEAD":
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    plugin = data.get("plugin")
    try:
        _add_plugin_to_event(event, plugin)
    except DuplicateNameError:
        return jsonify({"error": "Already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Plugin added to event", {"client": request.remote_addr, "event": event, "plugin": plugin})
    return jsonify({"event": event, "plugin": plugin}), 201


@app.route("/api/plugin-events/<path:event>/plugins/<path:plugin>", methods=["DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("DELETE", "OPTIONS")
def plugin_event_plugin_delete(event: str, plugin: str) -> tuple:
    if not INTERNAL_INTERACTIONS:
        return jsonify({"error": "Internal interactions disabled."}), 403
    try:
        deleted = _remove_plugin_from_event(event, plugin)
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if not deleted:
        return jsonify({"error": "Not found."}), 404
    log_info("Plugin removed from event", {"client": request.remote_addr, "event": event, "plugin": plugin})
    return jsonify({"status": "ok"}), 200


@app.route("/api/external-interactions-incoming-ips", methods=["GET", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def external_interactions_incoming_ips() -> tuple:
    if not EXTERNAL_INTERACTIONS:
        return jsonify({"error": "Functionality disabled."}), 403
    try:
        entries = _list_external_interactions_entries("incoming")
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("Incoming external interactions IPs read", {"client": request.remote_addr})
    return jsonify({"incomingIps": entries}), 200


@app.route("/api/external-interactions-outgoing-ips", methods=["GET", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def external_interactions_outgoing_ips() -> tuple:
    if not EXTERNAL_INTERACTIONS:
        return jsonify({"error": "Functionality disabled."}), 403
    try:
        entries = _list_external_interactions_entries("outgoing")
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("Outgoing external interactions IPs read", {"client": request.remote_addr})
    return jsonify({"outgoingIps": entries}), 200


def _external_interactions_ip_item(direction: str, ip: str) -> tuple:
    if request.method == "DELETE":
        try:
            deleted = _delete_external_interactions_entry(direction, ip)
        except FeatureDisabledError:
            return jsonify({"error": "Functionality disabled."}), 403
        except ValueError:
            return jsonify({"error": "Invalid request."}), 400
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        log_info("External interactions firewall entry deleted", {"client": request.remote_addr, "direction": direction, "ip": ip})
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    note = data.get("note")
    new_ip = data.get("new_ip")
    remove_plugin = data.get("remove_plugin")
    add_plugin = data.get("add_plugin")
    if action is None and note is None and new_ip is None and remove_plugin is None and add_plugin is None:
        return jsonify({"error": "Invalid request."}), 400
    if action is not None and not isinstance(action, str):
        return jsonify({"error": "Invalid request."}), 400
    if note is not None and not isinstance(note, str):
        return jsonify({"error": "Invalid request."}), 400
    if new_ip is not None and not isinstance(new_ip, str):
        return jsonify({"error": "Invalid request."}), 400
    if remove_plugin is not None and not isinstance(remove_plugin, str):
        return jsonify({"error": "Invalid request."}), 400
    if add_plugin is not None and not isinstance(add_plugin, str):
        return jsonify({"error": "Invalid request."}), 400
    try:
        entry = _update_external_interactions_entry(direction, ip, action, note, new_ip, remove_plugin, add_plugin)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    except DuplicateNameError:
        return jsonify({"error": "Already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if entry is None:
        return jsonify({"error": "Not found."}), 404
    log_info("External interactions firewall entry updated", {"client": request.remote_addr, "direction": direction, "ip": ip})
    return jsonify(entry), 200


@app.route("/api/external-interactions-incoming-ips/<path:ip>", methods=["PATCH", "DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def external_interactions_incoming_ip_item(ip: str) -> tuple:
    return _external_interactions_ip_item("incoming", ip)


@app.route("/api/external-interactions-outgoing-ips/<path:ip>", methods=["PATCH", "DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def external_interactions_outgoing_ip_item(ip: str) -> tuple:
    return _external_interactions_ip_item("outgoing", ip)


@app.route("/api/external-interactions-allow-new", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def external_interactions_allow_new() -> tuple:
    if request.method == "GET":
        log_info("External interactions allow new setting read", {"client": request.remote_addr})
        return jsonify({"externalInteractionsAllowNew": _read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", EXTERNAL_INTERACTIONS_ALLOW_NEW)}), 200

    data = request.get_json(silent=True) or {}
    if "externalInteractionsAllowNew" not in data or not isinstance(data["externalInteractionsAllowNew"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["externalInteractionsAllowNew"]
    try:
        _set_external_interactions_allow_new(value)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("External interactions allow new set", {"client": request.remote_addr, "externalInteractionsAllowNew": EXTERNAL_INTERACTIONS_ALLOW_NEW})
    return jsonify({"externalInteractionsAllowNew": EXTERNAL_INTERACTIONS_ALLOW_NEW}), 200


@app.route("/api/external-interactions-allow-new-outgoing", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def external_interactions_allow_new_outgoing() -> tuple:
    if request.method == "GET":
        log_info("Outgoing external interactions allow new setting read", {"client": request.remote_addr})
        return jsonify({"externalInteractionsAllowNewOutgoing": _read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING", EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING)}), 200

    data = request.get_json(silent=True) or {}
    if "externalInteractionsAllowNewOutgoing" not in data or not isinstance(data["externalInteractionsAllowNewOutgoing"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["externalInteractionsAllowNewOutgoing"]
    try:
        _set_external_interactions_allow_new_outgoing(value)
    except FeatureDisabledError:
        return jsonify({"error": "Functionality disabled."}), 403
    log_info("Outgoing external interactions allow new set", {"client": request.remote_addr, "externalInteractionsAllowNewOutgoing": EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING})
    return jsonify({"externalInteractionsAllowNewOutgoing": EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING}), 200


@app.route("/api/plugins/search", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def search_plugins() -> tuple:
    if request.method == "GET":
        pattern = request.args.get("q", request.args.get("query", request.args.get("pattern", "")))
        if pattern is None:
            pattern = ""
        if not isinstance(pattern, str):
            return jsonify({"error": "Invalid request."}), 400
    else:
        data = request.get_json(silent=True) or {}
        pattern = data.get("query", data.get("pattern", data.get("q", "")))
        if pattern is None:
            pattern = ""
        if not isinstance(pattern, str):
            return jsonify({"error": "Invalid request."}), 400
    try:
        results = plugin_bridge._search_plugins(pattern)
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Plugins search", {"client": request.remote_addr, "pattern": pattern, "count": len(results)})
    return jsonify({"plugins": results}), 200


@network.external_interactions_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def index():
    log_info("Serving UI", {"client": request.remote_addr})
    web_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (web_dir / "index.html").read_text(encoding="utf-8")
    session = _active_session()
    return render_template_string(
        template,
        display_promotion=_read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
        is_admin=bool(session and session["admin"]),
        development=DEVELOPMENT,
    )


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_css(filename: str):
    css_dir = Path(__file__).resolve().parent.parent / "ui" / "css"
    return send_from_directory(css_dir, filename)


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_font(filename: str):
    fonts_dir = Path(__file__).resolve().parent.parent / "ui" / "fonts"
    return send_from_directory(fonts_dir, filename)


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_icon(filename: str):
    icons_dir = Path(__file__).resolve().parent.parent / "ui" / "icons"
    return send_from_directory(icons_dir, filename)


@network.external_interactions_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_page(filename: str):
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    return send_from_directory(pages_dir, filename)


# ---------------------------------------------------------------------------
# Settings page: decoupled card indicators + on-demand card fragments
# ---------------------------------------------------------------------------
# The settings page is served as a light shell: the page title, the card
# indicators (always present, independently of whether the corresponding card
# has been loaded) and the page actions (terminate/restart, which are not part
# of a card and are never subject to on-demand loading). Each card lives in its
# own fragment under ``ui/cards/settings/`` and is loaded by the client only
# when it is relevant to the viewport (see ``ui/pages/index.html``). The
# fragment endpoint enforces the same authorization as the old single page, so
# card *contents* are still sent only to users allowed to see them.

_SETTINGS_CARDS = [
    {
        "id": "general",
        "title": "General",
        "fragment": "general.html",
        "roles": ("all",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    },
    {
        "id": "plugins",
        "title": "Plugins",
        "fragment": "plugins.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"/></svg>',
    },
    {
        "id": "api-keys",
        "title": "API Keys",
        "fragment": "api-keys.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
    },
    {
        "id": "internal-interactions",
        "title": "Internal Interactions",
        "fragment": "internal-interactions.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 10a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 14.286V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/><path d="M20 9a2 2 0 0 1 2 2v10.286a.71.71 0 0 1-1.212.502l-2.202-2.202A2 2 0 0 0 17.172 19H10a2 2 0 0 1-2-2v-1"/></svg>',
    },
    {
        "id": "external-interactions",
        "title": "External Interactions",
        "fragment": "external-interactions.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16.247 7.761a6 6 0 0 1 0 8.478"/><path d="M19.075 4.933a10 10 0 0 1 0 14.134"/><path d="M4.925 19.067a10 10 0 0 1 0-14.134"/><path d="M7.753 16.239a6 6 0 0 1 0-8.478"/><circle cx="12" cy="12" r="2"/></svg>',
    },
    {
        "id": "users",
        "title": "Users",
        "fragment": "users.html",
        "roles": ("admin",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    },
    {
        "id": "account",
        "title": "Account",
        "fragment": "account.html",
        "roles": ("all",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 20a6 6 0 0 0-6-6 6 6 0 0 0-6 6"/><circle cx="12" cy="10" r="4"/><circle cx="12" cy="12" r="10"/></svg>',
    },
    {
        "id": "customisation",
        "title": "Customisation",
        "fragment": "customisation.html",
        "roles": ("all",),
        "icon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg>',
    },
]


def _settings_static_context() -> dict:
    """Return the session-independent settings context, cached briefly.

    Building this context is expensive (project/plugin-library hash
    computation, installed-plugins folder hashing). The on-demand card
    fragments are fetched immediately after the shell, so caching it for a
    short TTL avoids recomputing it once per fragment.
    """
    global _SETTINGS_STATIC_CACHE, _SETTINGS_STATIC_TS
    now = time.time()
    if _SETTINGS_STATIC_CACHE is not None and (now - _SETTINGS_STATIC_TS) < _SETTINGS_STATIC_TTL:
        return _SETTINGS_STATIC_CACHE
    api_keys = sorted(_api_key_store, key=lambda k: (k.get("name") or "").lower())
    shared_memory = _load_shared_memory()
    incoming_interactions_ips = _load_external_interactions_entries("incoming")
    outgoing_interactions_ips = _load_external_interactions_entries("outgoing")
    users = _list_users()
    installed_plugins_list = _list_installed_plugins()
    ctx: dict = {
        "api_keys": api_keys,
        "has_api_keys": bool(api_keys),
        "api_keys_json": json.dumps(api_keys),
        "shared_memory": shared_memory,
        "has_shared_memory": bool(shared_memory),
        "shared_memory_json": json.dumps(shared_memory),
        "has_external_interactions_ips": bool(incoming_interactions_ips),
        "external_interactions_ips_json": json.dumps(incoming_interactions_ips),
        "has_external_interactions_outgoing_ips": bool(outgoing_interactions_ips),
        "external_interactions_outgoing_ips_json": json.dumps(outgoing_interactions_ips),
        "users": users,
        "has_users": bool(users),
        "users_json": json.dumps(users),
        "installed_plugins_list": installed_plugins_list,
        "has_installed_plugins": bool(installed_plugins_list),
        "installed_plugins_json": json.dumps(installed_plugins_list),
        "current_version": _get_current_project_version(),
        "effective_version": _get_effective_project_version(),
        "indicated_version": _get_indicated_project_version(),
        "current_plugins_lib_version": _get_current_plugins_lib_version(),
        "effective_plugins_lib_version": _get_effective_plugins_lib_version(),
        "indicated_plugins_lib_version": _get_indicated_plugins_lib_version(),
        "sounds": {event: _read_env_var(audio.SOUND_ENV_VARS[event], audio.DEFAULT_SOUND_FILES.get(event, "")) for event in audio.SOUND_EVENTS},
        "available_audios": audio.list_audio_files(),
        "sound_events": [(event, event.capitalize()) for event in audio.SOUND_EVENTS],
    }
    _SETTINGS_STATIC_CACHE = ctx
    _SETTINGS_STATIC_TS = now
    return ctx


def _settings_render_context(session: dict | None) -> dict:
    """Return the full Jinja context for the settings shell and card fragments."""
    ctx = dict(_settings_static_context())
    is_admin = bool(session and session["admin"])
    ctx.update({
        "is_admin": is_admin,
        "is_root": bool(session and session.get("root", False)),
        "account_username": session["username"] if session else "",
        "current_username": session["username"] if session else "",
        "internal_interactions": _read_env_bool("INTERNAL_INTERACTIONS", INTERNAL_INTERACTIONS),
        "api_keys_enabled": _read_env_bool("API_KEYS_ENABLED", API_KEYS_ENABLED),
        "display_promotion": _read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
        "play_audios": _read_env_bool("PLAY_AUDIOS", PLAY_AUDIOS),
        "play_log_sounds": _read_env_bool("PLAY_LOG_SOUNDS", PLAY_LOG_SOUNDS),
        "play_startup_sound": _read_env_bool("PLAY_STARTUP_SOUND", PLAY_STARTUP_SOUND),
        "shared_memory_enabled": _read_env_bool("SHARED_MEMORY_ENABLED", SHARED_MEMORY_ENABLED),
        "external_interactions_allow_new": _read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", EXTERNAL_INTERACTIONS_ALLOW_NEW),
        "external_interactions_allow_new_outgoing": _read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING", EXTERNAL_INTERACTIONS_ALLOW_NEW_OUTGOING),
        "external_interactions_enabled": _external_interactions_enabled(),
        "external_interactions_worker_bind": _external_interactions_worker_bind_address(),
        "automatic_update": _read_env_bool("AUTOMATIC_UPDATE", AUTOMATIC_UPDATE),
        "update_available": _UPDATE_AVAILABLE_AT_STARTUP,
        "project_integrity_ok": _PROJECT_INTEGRITY_OK,
        "development": DEVELOPMENT,
        "project_update_disabled": DEVELOPMENT,
        "automatic_plugin_library_update": _read_env_bool("AUTOMATIC_PLUGIN_LIBRARY_UPDATE", AUTOMATIC_PLUGIN_LIBRARY_UPDATE),
        "automatic_plugin_upgrade": _read_env_bool("AUTOMATIC_PLUGIN_UPGRADE", AUTOMATIC_PLUGIN_UPGRADE),
        "installed_plugins_upgrade_available": bool(_INSTALLED_PLUGINS_PENDING_UPGRADES),
        "plugins_lib_update_available": _PLUGIN_UPDATE_AVAILABLE_AT_STARTUP,
        "plugin_integrity_ok": _PLUGIN_INTEGRITY_OK,
    })
    return ctx


def _settings_card_indicators(session: dict | None) -> list[dict]:
    """Return the card-indicator metadata for the shell (all cards, role tagged).

    Every card's indicator is sent to the client regardless of whether the card
    is actually loaded; the client filters by role. The ``hidden`` flag mirrors
    the feature-enabled state (e.g. API Keys card hidden when the functionality
    is disabled), which is a separate concern from the loading state.
    """
    is_admin = bool(session and session["admin"])
    indicators: list[dict] = []
    for card in _SETTINGS_CARDS:
        hidden = False
        if card["id"] == "api-keys" and not _read_env_bool("API_KEYS_ENABLED", API_KEYS_ENABLED):
            hidden = True
        elif card["id"] == "internal-interactions" and not _read_env_bool("INTERNAL_INTERACTIONS", INTERNAL_INTERACTIONS):
            hidden = True
        elif card["id"] == "external-interactions" and not _read_env_bool("EXTERNAL_INTERACTIONS", EXTERNAL_INTERACTIONS):
            hidden = True
        indicators.append({
            "id": card["id"],
            "title": card["title"],
            "icon": card["icon"],
            "role": "admin" if "admin" in card["roles"] else "all",
            "hidden": hidden,
            "admin": not is_admin and "admin" in card["roles"],
        })
    return indicators


_SETTINGS_STATIC_CACHE: dict | None = None
_SETTINGS_STATIC_TS: float = 0.0
_SETTINGS_STATIC_TTL: float = 30.0


def _settings_cards_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "ui" / "cards" / "settings"


@network.external_interactions_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_settings_card(card_id: str) -> tuple:
    """Serve a single settings card fragment, authorized for the session.

    Card contents are sent only when the user is allowed to see them (the
    same authorization the monolithic settings page used to apply). Unknown
    card ids and unauthorized cards return a small JSON error that the client
    turns into an unobtrusive placeholder.
    """
    session = _active_session()
    if session is None:
        return _unauthorized_response()
    entry = next((card for card in _SETTINGS_CARDS if card["id"] == card_id), None)
    if entry is None:
        return jsonify({"error": "Unknown card."}), 404
    if "admin" in entry["roles"] and not session["admin"]:
        log_warn("Settings card denied: logged-in user is not an admin", {"client": request.remote_addr, "card": card_id})
        return jsonify({"error": "Admin privileges required."}), 403
    fragment = (_settings_cards_dir() / entry["fragment"]).read_text(encoding="utf-8")
    ctx = _settings_render_context(session)
    rendered = render_template_string(fragment, **ctx)
    return rendered, 200


@network.external_interactions_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_settings_page():
    global _PROJECT_INTEGRITY_OK, _PLUGIN_INTEGRITY_OK
    # Re-verify integrity on every settings page load (whenever recomputed)
    try:
        eff_pl = plugin_bridge._compute_plugins_lib_hash()
        ind_pl = plugin_bridge._read_stored_hash()
        if eff_pl and ind_pl and eff_pl.strip().lower() != ind_pl.strip().lower():
            _PLUGIN_INTEGRITY_OK = False
            log_error("Plugin library integrity check failed before rendering settings", {"effective": eff_pl, "indicated": ind_pl})
            try:
                audio.get_audio_orchestrator().start()
            except Exception:
                pass
            try:
                audio.play_sound("error")
            except Exception:
                pass
            try:
                plugin_bridge.get_plugin_bridge().stop()
            except Exception:
                pass
        else:
            # Only mark ok if bridge can start (or is already started)
            if not plugin_bridge.get_plugin_bridge().is_started():
                try:
                    plugin_bridge.get_plugin_bridge().start()
                    _PLUGIN_INTEGRITY_OK = plugin_bridge.get_plugin_bridge().is_started()
                except Exception:
                    pass
            else:
                _PLUGIN_INTEGRITY_OK = True
    except Exception:
        pass
    try:
        eff_pr = _compute_local_project_hash()
        ind_pr = _get_local_project_hash()
        if eff_pr and ind_pr and eff_pr.strip().lower() != ind_pr.strip().lower():
            _PROJECT_INTEGRITY_OK = False
            if not DEVELOPMENT:
                log_error("Project integrity check failed before rendering settings and development is false", {"effective": eff_pr, "indicated": ind_pr})
                try:
                    audio.get_audio_orchestrator().start()
                except Exception:
                    pass
                try:
                    audio.play_sound("error")
                except Exception:
                    pass
            else:
                log_warn("Project integrity check failed before rendering settings but development is true — continuing", {"effective": eff_pr, "indicated": ind_pr})
        else:
            _PROJECT_INTEGRITY_OK = True
    except Exception:
        pass
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (pages_dir / "settings.html").read_text(encoding="utf-8")
    session = _active_session()
    ctx = _settings_render_context(session)
    ctx["card_indicators"] = _settings_card_indicators(session)
    return render_template_string(template, **ctx)


@network.external_interactions_worker_callable
@admin_session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_plugins_page():
    global _PLUGIN_INTEGRITY_OK
    try:
        eff_pl = plugin_bridge._compute_plugins_lib_hash()
        ind_pl = plugin_bridge._read_stored_hash()
        if eff_pl and ind_pl and eff_pl.strip().lower() != ind_pl.strip().lower():
            _PLUGIN_INTEGRITY_OK = False
            log_error("Plugin library integrity check failed before rendering plugins page", {"effective": eff_pl, "indicated": ind_pl})
            try:
                audio.get_audio_orchestrator().start()
            except Exception:
                pass
            try:
                audio.play_sound("error")
            except Exception:
                pass
            try:
                plugin_bridge.get_plugin_bridge().stop()
            except Exception:
                pass
        else:
            if not plugin_bridge.get_plugin_bridge().is_started():
                try:
                    plugin_bridge.get_plugin_bridge().start()
                    _PLUGIN_INTEGRITY_OK = plugin_bridge.get_plugin_bridge().is_started()
                except Exception:
                    pass
            else:
                _PLUGIN_INTEGRITY_OK = True
    except Exception:
        pass
    if not _PLUGIN_INTEGRITY_OK:
        log_warn("Plugins page disabled due to integrity failure")
        return render_template_string("<section class=\"page-content\" style=\"opacity:0.6\"><h2 class=\"page-title\">Plugins</h2><div class=\"page-card\" style=\"opacity:0.6; border-color:#dc2626; pointer-events:none;\"><p style=\"color:#b91c1c;\">Plugin library integrity check failed — plugins disabled.</p></div></section>"), 200
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (pages_dir / "plugins.html").read_text(encoding="utf-8")
    return render_template_string(template)


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def login_page():
    if _is_valid_session_cookie():
        return redirect("/")
    web_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (web_dir / "login.html").read_text(encoding="utf-8")
    return render_template_string(template, development=DEVELOPMENT)


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_argon2_script():
    js_dir = Path(__file__).resolve().parent.parent / "ui" / "js" / "argon2"
    return send_from_directory(js_dir, "argon2-bundled.min.js")


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_js(filename: str):
    js_dir = Path(__file__).resolve().parent.parent / "ui" / "js"
    return send_from_directory(js_dir, filename)


@network.external_interactions_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_login_icon():
    icons_dir = Path(__file__).resolve().parent.parent / "ui" / "icons"
    return send_from_directory(icons_dir, "akupara.svg")


@app.route("/api/login", methods=["POST", "OPTIONS"])
@network.external_interactions_worker_callable
@standard_endpoint("POST", "OPTIONS")
def login() -> tuple:
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password_hash = data.get("password_hash")
    if not isinstance(username, str) or not isinstance(password_hash, str):
        return jsonify({"error": "Invalid request."}), 400
    if not _login_credentials_configured():
        log_warn("Login rejected: USERS not configured in .env", {"client": request.remote_addr})
        return jsonify({"error": "Login is not configured."}), 403
    user = _authenticate_user(username, password_hash)
    if user is not None:
        response = jsonify({"status": "ok"})
        _issue_session_cookie(response, user["username"], user["admin"], user.get("root", False))
        _reset_failed_login_attempts()
        log_info("Login successful for user", {"username": user["username"], "client": request.remote_addr})
        audio.play_audio("success")()
        return response, 200
    play_warn = _register_failed_login_attempt()
    log_warn("Login failed", {"client": request.remote_addr}, silent=not play_warn)
    if play_warn:
        audio.play_audio("warn")()
    return jsonify({"error": "Invalid credentials."}), 401


@app.route("/api/logout", methods=["POST", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@session_authenticated
@standard_endpoint("POST", "OPTIONS")
def logout() -> tuple:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = _active_session()
    username = session["username"] if session else "unknown"
    if token:
        with _SESSION_LOCK:
            _SESSION_STORE.pop(token, None)
        log_info("Logout", {"username": username, "client": request.remote_addr})
    response = jsonify({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response, 200


@app.route("/api/change-password", methods=["POST", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@session_authenticated
@standard_endpoint("POST", "OPTIONS")
def change_password() -> tuple:
    data = request.get_json(silent=True) or {}
    current_password_hash = data.get("current_password_hash")
    new_password_hash = data.get("new_password_hash")
    if (
        not isinstance(current_password_hash, str)
        or not current_password_hash
        or not isinstance(new_password_hash, str)
        or not new_password_hash
    ):
        return jsonify({"error": "Invalid request."}), 400
    if not new_password_hash.startswith("$argon2id$"):
        return jsonify({"error": "Invalid new password hash."}), 400
    session = _active_session()
    if session is None:
        return _unauthorized_response()
    keep_token = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        _change_password(session["username"], current_password_hash, new_password_hash, keep_token)
    except AccountNotFoundError as exc:
        log_warn("Password change rejected", {"client": request.remote_addr, "error": str(exc)})
        return jsonify({"error": "Account not found."}), 404
    except CurrentPasswordError as exc:
        log_warn("Password change rejected", {"client": request.remote_addr, "error": str(exc)})
        audio.play_audio("warn")()
        return jsonify({"error": "Current password is incorrect."}), 403
    audio.play_audio("success")()
    return jsonify({"status": "ok"}), 200


@app.route("/api/users", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def users() -> tuple:
    if request.method == "GET":
        users_list = _list_users()
        log_info("Users list read", {"client": request.remote_addr})
        return jsonify({"users": users_list}), 200

    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password_hash = data.get("password_hash")
    admin = data.get("admin", False)
    if not isinstance(username, str) or not username.strip():
        return jsonify({"error": "Invalid request."}), 400
    if any(ch in username for ch in _FORBIDDEN_KEY_NAME_CHARS):
        return jsonify({"error": "Invalid request."}), 400
    if len(username.strip()) < 8:
        return jsonify({"error": "Invalid request."}), 400
    if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$"):
        return jsonify({"error": "Invalid password hash."}), 400
    if not isinstance(admin, bool):
        return jsonify({"error": "Invalid request."}), 400
    try:
        _register_user(username, password_hash, admin)
    except UsernameTakenError as exc:
        log_warn("User registration rejected", {"client": request.remote_addr, "error": str(exc)})
        return jsonify({"error": "Already exists."}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("User registered", {"username": username.strip(), "client": request.remote_addr})
    return jsonify({"status": "ok"}), 201


@app.route("/api/users/<path:username>", methods=["PATCH", "DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@admin_session_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def user_item(username: str) -> tuple:
    session = _active_session()
    if session is None:
        return _unauthorized_response()
    if request.method == "DELETE":
        if username.casefold() == session["username"].casefold():
            log_warn("Self-deletion rejected", {"client": request.remote_addr, "user": username})
            return jsonify({"error": "You cannot delete your own account."}), 403
        try:
            deleted = _delete_user(username)
        except ValueError as exc:
            log_warn("Root deletion rejected", {"client": request.remote_addr, "user": username})
            return jsonify({"error": "Invalid request."}), 403
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        log_info("User deleted", {"client": request.remote_addr, "username": username})
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    new_username = data.get("new_username")
    admin = data.get("admin")
    if new_username is not None:
        if _is_root_username(username) and not session.get("root", False):
            log_warn("Non-root rename of root rejected", {"client": request.remote_addr, "user": username})
            return jsonify({"error": "Only the root user can change its username."}), 403
        try:
            target = _rename_user(username, new_username)
        except UsernameTakenError as exc:
            log_warn("User rename rejected", {"client": request.remote_addr, "error": str(exc)})
            return jsonify({"error": "Already exists."}), 409
        except ValueError:
            return jsonify({"error": "Invalid request."}), 400
        if target is None:
            return jsonify({"error": "Not found."}), 404
        log_info("User renamed", {"client": request.remote_addr, "old_username": username, "new_username": new_username})
        return jsonify(target), 200
    if admin is not None:
        if not isinstance(admin, bool):
            return jsonify({"error": "Invalid request."}), 400
        if username.casefold() == session["username"].casefold():
            log_warn("Self admin change rejected", {"client": request.remote_addr, "user": username})
            return jsonify({"error": "You cannot change your own admin status."}), 403
        try:
            target = _set_user_admin(username, admin)
        except ValueError as exc:
            log_warn("Root admin change rejected", {"client": request.remote_addr, "user": username})
            return jsonify({"error": "Invalid request."}), 403
        if target is None:
            return jsonify({"error": "Not found."}), 404
        log_info("Admin status updated", {"client": request.remote_addr, "username": username, "admin": admin})
        return jsonify(target), 200
    return jsonify({"error": "Invalid request."}), 400


@app.route("/api/session", methods=["GET", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def session_info() -> tuple:
    session = _active_session()
    if session is None:
        return _unauthorized_response()
    return jsonify({"username": session["username"], "admin": session["admin"], "root": bool(session.get("root", False))}), 200


def _register_ui_routes(app_instance: Flask) -> None:
    if not GUI_ENABLED:
        return
    app_instance.add_url_rule("/login", methods=["GET", "HEAD", "OPTIONS"], view_func=login_page)
    app_instance.add_url_rule(
        "/ui/js/argon2/argon2-bundled.min.js",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_argon2_script,
    )
    app_instance.add_url_rule(
        "/ui/icons/akupara.svg",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_login_icon,
    )
    app_instance.add_url_rule("/", methods=["GET", "HEAD", "OPTIONS"], view_func=index)
    app_instance.add_url_rule(
        "/ui/pages/settings.html",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_settings_page,
    )
    app_instance.add_url_rule(
        "/ui/pages/plugins.html",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_plugins_page,
    )
    app_instance.add_url_rule(
        "/ui/cards/settings/<string:card_id>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_settings_card,
    )
    app_instance.add_url_rule(
        "/ui/css/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_css,
    )
    app_instance.add_url_rule(
        "/ui/fonts/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_font,
    )
    app_instance.add_url_rule(
        "/ui/icons/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_icon,
    )
    app_instance.add_url_rule(
        "/ui/pages/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_page,
    )
    app_instance.add_url_rule(
        "/ui/js/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_js,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Akupara")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args, _ = parser.parse_known_args()

    init_logging("Akupara", debug=args.debug)

    try:
        _verify_disabled_styles()
    except Exception as exc:
        log_warn("Disabled styles check failed", {"error": str(exc)})

    try:
        _initialize_service_config()
        _register_ui_routes(app)
    except Exception as exc:
        log_error("Failed to load configuration", {"error": str(exc), "traceback": _format_exc()})
        exit(1)

    # Plugin loader starts immediately — verifies plugins-lib integrity (effective vs indicated)
    try:
        plugin_bridge.get_plugin_bridge().start()
        _PLUGIN_INTEGRITY_OK = plugin_bridge.get_plugin_bridge().is_started()
        if not _PLUGIN_INTEGRITY_OK:
            log_error("Plugin loader failed to start — check previous errors (illicit plugins-lib interaction?)")
    except Exception as exc:
        _PLUGIN_INTEGRITY_OK = False
        log_error("Plugin loader start failed", {"error": str(exc), "traceback": _format_exc()})

    # Local project integrity check — before any update check
    try:
        _PROJECT_INTEGRITY_OK = _verify_local_project_integrity()
        if not _PROJECT_INTEGRITY_OK:
            if not DEVELOPMENT:
                log_error("Project integrity check failed and development is false — not starting", {"development": DEVELOPMENT})
                try:
                    audio.get_audio_orchestrator().start()
                except Exception:
                    pass
                try:
                    audio.play_sound("error")
                except Exception:
                    pass
                exit(1)
            else:
                log_warn("Project integrity check failed but development is true — continuing without project update checks", {"development": DEVELOPMENT})
                _UPDATE_AVAILABLE = False
                _UPDATE_AVAILABLE_AT_STARTUP = False
    except Exception as exc:
        _PROJECT_INTEGRITY_OK = False
        log_warn("Project integrity check failed", {"error": str(exc), "traceback": _format_exc()})
        if not DEVELOPMENT:
            exit(1)
        else:
            _UPDATE_AVAILABLE = False
            _UPDATE_AVAILABLE_AT_STARTUP = False

    # Startup check for updates — skipped in development mode (and when integrity failed)
    if _PROJECT_INTEGRITY_OK and not DEVELOPMENT:
        try:
            _UPDATE_AVAILABLE = _is_update_available()
            _UPDATE_AVAILABLE_AT_STARTUP = _UPDATE_AVAILABLE
            current = _get_current_project_version()
            if _UPDATE_AVAILABLE:
                log_info("Update available at startup", {"current": current, "automaticUpdate": AUTOMATIC_UPDATE, "development": DEVELOPMENT})
                if AUTOMATIC_UPDATE and not DEVELOPMENT:
                    log_info("Automatic update enabled — updating now and restarting", {"current": current})
                    if _perform_project_update():
                        _restart()
                    # Update failed but the process continues — fall through to the known-release check
                elif AUTOMATIC_UPDATE and DEVELOPMENT:
                    log_info("Automatic update skipped in development mode", {"current": current})
                # Still not on the latest release (auto-update off or failed): the local
                # hash must belong to a known release (latest or previous); otherwise crash.
                local_hash = _get_local_project_hash() or _compute_local_project_hash()
                if not _is_known_release_hash(local_hash):
                    log_error("Project version unknown: local hash matches no Akupara release (illicit interaction?)", {"local": local_hash})
                    try:
                        audio.get_audio_orchestrator().start()
                    except Exception:
                        pass
                    try:
                        audio.play_sound("error")
                    except Exception:
                        pass
                    exit(1)
            else:
                log_info("No update available at startup", {"current": current})
        except Exception as exc:
            log_warn("Startup update check failed", {"error": str(exc)})
    else:
        if _PROJECT_INTEGRITY_OK:
            log_info("Skipping project update check in development mode")
        elif DEVELOPMENT:
            log_info("Skipping project update check due to integrity failure in development mode")

    # Startup check for plugin library updates — always (even if integrity failed, to allow recovery update)
    try:
        _PLUGIN_UPDATE_AVAILABLE = _is_plugin_update_available()
        _PLUGIN_UPDATE_AVAILABLE_AT_STARTUP = _PLUGIN_UPDATE_AVAILABLE
        current_pl = _get_current_plugins_lib_version()
        if _PLUGIN_UPDATE_AVAILABLE:
            log_info("Plugin library update available at startup", {"current": current_pl, "automaticPluginLibraryUpdate": AUTOMATIC_PLUGIN_LIBRARY_UPDATE})
            if AUTOMATIC_PLUGIN_LIBRARY_UPDATE:
                log_info("Automatic plugin library update enabled — updating now and restarting", {"current": current_pl})
                if _perform_plugins_lib_update():
                    _restart()
        else:
            # No update needed; if integrity failed, bridge remains stopped until manual update
            if not _PLUGIN_INTEGRITY_OK:
                log_warn("Plugin library integrity failed — bridge remains stopped, update may be required")
            else:
                log_info("No plugin library update available at startup", {"current": current_pl})
    except Exception as exc:
        log_warn("Startup plugin library update check failed", {"error": str(exc)})

    # Startup discovery of installed plugins — applies automatic upgrades only
    # when AUTOMATIC_PLUGIN_UPGRADE is on; otherwise pending upgrades are cached
    # so the settings page can offer them via Update All.
    try:
        _, _INSTALLED_PLUGINS_PENDING_UPGRADES = plugin_bridge.get_plugin_bridge().discover_installed_plugins(development=DEVELOPMENT)
        if _INSTALLED_PLUGINS_PENDING_UPGRADES:
            log_info("Installed plugins have available upgrades", {"count": len(_INSTALLED_PLUGINS_PENDING_UPGRADES)})
        else:
            log_info("No installed plugin upgrades available at startup")
    except Exception as exc:
        log_warn("Startup installed plugins discovery failed", {"error": str(exc)})

    if EXTERNAL_INTERACTIONS:
        _start_external_interactions_worker()

    # Every loading operation is done — play the startup sound when enabled
    # (only when PLAY_AUDIOS is on; _play_startup_sound checks both flags).
    try:
        _play_startup_sound()
    except Exception:
        pass

    try:
        log_info("Akupara starting", {"bind": f"http://{SERVICE_HOST}:{SERVICE_PORT}", "gui": GUI_ENABLED, "port": SERVICE_PORT, "guiEnabled": GUI_ENABLED, "internalInteractions": INTERNAL_INTERACTIONS, "apiKeysEnabled": API_KEYS_ENABLED, "externalInteractions": EXTERNAL_INTERACTIONS})

        app.run(host=SERVICE_HOST, port=SERVICE_PORT, debug=False, threaded=True)

    except OSError as exc:
        if "Address already in use" in str(exc):
            log_error("Port already in use", {"port": SERVICE_PORT, "hint": "Change the port in resources/configuration.json"})
        elif "Permission denied" in str(exc):
            log_error("Permission denied to bind to port", {"port": SERVICE_PORT, "hint": "Use a port >= 1024 or run with elevated privileges."})
        else:
            log_error("Network binding failed", {"error": str(exc)})

    except KeyboardInterrupt:
        log_info("Akupara stopped")

    except Exception as exc:
        log_error("Server startup failed", {"error": str(exc)})
