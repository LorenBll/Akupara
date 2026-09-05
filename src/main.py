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

from logginglib import init_logging, log_debug, log_error, log_info, log_warn

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

AUTOMATIC_UPDATE: bool = False
AUTOMATIC_PLUGIN_LIBRARY_UPDATE: bool = False
AUTOMATIC_PLUGIN_UPGRADE: bool = False

_UPDATE_AVAILABLE: bool = False
_UPDATE_AVAILABLE_AT_STARTUP: bool = False
_PLUGIN_UPDATE_AVAILABLE: bool = False
_PLUGIN_UPDATE_AVAILABLE_AT_STARTUP: bool = False
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


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _initialize_service_config() -> None:
    global SERVICE_PORT, GUI_ENABLED, DEVELOPMENT, INTERNAL_INTERACTIONS, API_KEYS_ENABLED, DISPLAY_PROMOTION, PLAY_AUDIOS, PLAY_LOG_SOUNDS, PLAY_STARTUP_SOUND, SHARED_MEMORY_ENABLED, EXTERNAL_INTERACTIONS, EXTERNAL_INTERACTIONS_ALLOW_NEW, AUTOMATIC_UPDATE, AUTOMATIC_PLUGIN_LIBRARY_UPDATE, AUTOMATIC_PLUGIN_UPGRADE
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
        log_warn("Login credentials not configured", {"hint": "set USERNAME and PASSWORD (or USERS) in .env"})

    log_debug("Resolved config values", {"port": SERVICE_PORT, "guiEnabled": GUI_ENABLED, "internalInteractions": INTERNAL_INTERACTIONS, "apiKeysEnabled": API_KEYS_ENABLED, "externalInteractions": EXTERNAL_INTERACTIONS, "automaticUpdate": AUTOMATIC_UPDATE, "automaticPluginLibraryUpdate": AUTOMATIC_PLUGIN_LIBRARY_UPDATE, "automaticPluginUpgrade": AUTOMATIC_PLUGIN_UPGRADE})
    log_info("Service configuration initialized")


def _get_local_device_addresses() -> set[str]:
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
    if _read_env_var("USERNAME") and _read_env_var("PASSWORD"):
        return True
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
    expected_username = _read_env_var("USERNAME")
    expected_password = _read_env_var("PASSWORD")
    if expected_username and expected_password:
        if username_key == expected_username.casefold() and hmac.compare_digest(password_hash, expected_password):
            return {"username": expected_username, "admin": False, "root": False}
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
        if source == "users":
            if not _set_user_password_env(username, new_password_hash):
                raise AccountNotFoundError("Account not found.")
        else:
            _write_env_var("PASSWORD", new_password_hash)
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
    expected_username = _read_env_var("USERNAME")
    if expected_username and username_key == expected_username.casefold():
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
    expected_username = _read_env_var("USERNAME")
    if expected_username and new_key == expected_username.casefold():
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

    return jsonify({
        "status": "ok",
        "service": "Akupara",
        "bind_address": SERVICE_HOST,
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


def _write_env_var(key: str, value: str) -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        env_path.touch()
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    for index, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[index] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    new_content = "\n".join(lines) + "\n"
    try:
        mode = env_path.stat().st_mode & 0o777
    except OSError:
        mode = None
    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), prefix=".env.", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, str(env_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_env_bool(key: str, value: bool) -> None:
    _write_env_var(key, str(value).lower())


def _read_env_var(key: str, default: str = "") -> str:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return default


def _read_env_bool(key: str, default: bool = False) -> bool:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return _parse_bool(value, default)
    return default


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


@audio.play_audio("acknowledge")
def _set_external_interactions(value: bool) -> None:
    global EXTERNAL_INTERACTIONS
    if value:
        EXTERNAL_INTERACTIONS = True
        _write_env_bool("EXTERNAL_INTERACTIONS", True)
        _start_external_interactions_worker()
    else:
        EXTERNAL_INTERACTIONS = False
        _stop_external_interactions_worker()
        _write_env_bool("EXTERNAL_INTERACTIONS", False)


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
        return address.exploded
    return str(address)


def _maximize_network_ip(ip: str) -> str:
    return _canonical_network_ip(_parse_network_ip(ip))



_EXTERNAL_INTERACTIONS_ACTIONS = ("allow", "unknown", "block")


def _load_external_interactions_ips() -> list[dict]:
    raw = _read_env_var("EXTERNAL_INTERACTIONS_IPS")
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
        note = entry.get("Note")
        if not isinstance(note, str):
            note = ""
        for ip, action in entry.items():
            if ip == "Note":
                continue
            if action not in _EXTERNAL_INTERACTIONS_ACTIONS:
                continue
            try:
                canonical = _canonical_network_ip(ipaddress.ip_address(ip))
            except ValueError:
                continue
            entries.append({canonical: action, "Note": note})
    return entries



def _save_external_interactions_ips(entries: list[dict]) -> None:
    _write_env_var("EXTERNAL_INTERACTIONS_IPS", json.dumps(entries))


def _list_external_interactions_ips() -> list[dict]:
    _require_external_interactions_enabled()
    return _load_external_interactions_ips()



def _set_external_interactions_ip(ip: str, action: str, note: str | None = None) -> tuple[dict, bool]:
    _require_external_interactions_enabled()
    if action not in _EXTERNAL_INTERACTIONS_ACTIONS:
        raise ValueError("The value must be 'allow', 'unknown' or 'block'.")
    if note is not None and not isinstance(note, str):
        raise ValueError("The note must be a string.")
    if note is None:
        note = ""
    _validate_plaintext_string(note, "note")
    canonical = _maximize_network_ip(ip)
    entries = _load_external_interactions_ips()
    existing = next((entry for entry in entries if canonical in entry), None)
    entry = {canonical: action, "Note": note}
    remaining = [item for item in entries if canonical not in item]
    remaining.append(entry)
    _save_external_interactions_ips(remaining)
    if existing is None:
        audio.play_audio("success")()
    else:
        audio.play_audio("acknowledge")()
    return entry, existing is None


@audio.play_audio("acknowledge")
def _update_external_interactions_ip(ip: str, action: str, note: str | None = None) -> dict | None:
    _require_external_interactions_enabled()
    if action not in _EXTERNAL_INTERACTIONS_ACTIONS:
        raise ValueError("The value must be 'allow', 'unknown' or 'block'.")
    if note is not None and not isinstance(note, str):
        raise ValueError("The note must be a string.")
    canonical = _maximize_network_ip(ip)
    entries = _load_external_interactions_ips()
    if not any(canonical in entry for entry in entries):
        return None
    if note is None:
        existing = next(entry for entry in entries if canonical in entry)
        note = existing.get("Note", "")
    _validate_plaintext_string(note, "note")
    entry = {canonical: action, "Note": note}
    remaining = [item for item in entries if canonical not in item]
    remaining.append(entry)
    _save_external_interactions_ips(remaining)
    return entry



def _delete_external_interactions_ip(ip: str) -> bool:
    _require_external_interactions_enabled()
    canonical = _maximize_network_ip(ip)
    entries = _load_external_interactions_ips()
    remaining = [item for item in entries if canonical not in item]
    if len(remaining) == len(entries):
        return False
    _save_external_interactions_ips(remaining)
    audio.play_audio("success")()
    return True


@audio.play_audio("acknowledge")
def _set_external_interactions_allow_new(value: bool) -> None:
    _require_external_interactions_enabled()
    global EXTERNAL_INTERACTIONS_ALLOW_NEW
    EXTERNAL_INTERACTIONS_ALLOW_NEW = value
    _write_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", value)


def _record_external_interactions_ip_automatic(canonical: str, action: str) -> None:
    entries = _load_external_interactions_ips()
    if any(canonical in entry for entry in entries):
        return
    entries.append({canonical: action, "Note": ""})
    _save_external_interactions_ips(entries)
    audio.play_audio("acknowledge")()


def _external_interactions_worker_ip_policy(remote_addr: str) -> bool:
    """Per-request access decision for the external interactions worker.

    Each IP in the list carries one of three actions: ``"allow"`` (requests
    pass through), ``"block"`` (requests are refused) and ``"unknown"``. New
    IPs are always recorded in the list with ``"unknown"``. Requests from IPs
    whose action is ``"unknown"``, and requests from IPs not yet in the list,
    are decided by ``EXTERNAL_ACCESS_ALLOW_NEW``. Recordings made here are
    automatic and play the acknowledge sound.
    """
    try:
        canonical = _canonical_network_ip(ipaddress.ip_address(remote_addr))
    except ValueError:
        return False
    entries = _load_external_interactions_ips()
    for entry in entries:
        if canonical in entry:
            action = entry[canonical]
            if action == "allow":
                return True
            if action == "block":
                return False
            break
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
            "externalInteractionsAllowNew": _read_env_bool("EXTERNAL_INTERACTIONS_ALLOW_NEW", EXTERNAL_INTERACTIONS_ALLOW_NEW),
        }), 200
    denied = _require_admin_session()
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    keys = [key for key in ("internalInteractions", "displayPromotion", "externalInteractions", "externalInteractionsAllowNew") if key in data]
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
            except FeatureDisabledError as exc:
                return jsonify({"error": str(exc)}), 403
        elif key == "externalInteractionsAllowNew":
            try:
                _set_external_interactions_allow_new(value)
            except FeatureDisabledError as exc:
                return jsonify({"error": str(exc)}), 403
        else:
            _set_display_promotion(value)
    log_info("Settings updated", {"client": request.remote_addr, "internalInteractions": INTERNAL_INTERACTIONS, "displayPromotion": DISPLAY_PROMOTION, "externalInteractions": EXTERNAL_INTERACTIONS, "externalInteractionsAllowNew": EXTERNAL_INTERACTIONS_ALLOW_NEW})
    return jsonify({
        "internalInteractions": INTERNAL_INTERACTIONS,
        "displayPromotion": DISPLAY_PROMOTION,
        "externalInteractions": EXTERNAL_INTERACTIONS,
        "externalInteractionsAllowNew": EXTERNAL_INTERACTIONS_ALLOW_NEW,
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
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
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
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()[0]
            if data:
                return data
    except Exception:
        pass
    # Fallback: the raw hash file at the highest version tag (releases without an asset yet)
    tag = _get_latest_version_tag()
    if not tag:
        return None
    url = f"https://raw.githubusercontent.com/LorenBll/Akupara/{tag}/hash"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()[0]
            if data:
                return data
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
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()
            if data:
                return data[0]
    except Exception:
        return None
    return None


def _is_known_release_hash(local_hash: str | None) -> bool:
    """Return True when ``local_hash`` matches the latest or any previous release's hash."""
    local = (local_hash or "").strip().lower()
    if not local:
        return False
    latest = _fetch_remote_project_hash()
    if latest and latest.strip().lower() == local:
        return True
    for tag in reversed(_get_version_tags()):
        known = _fetch_release_hash_for_tag(tag)
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
    remote = _fetch_remote_project_hash()
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
        log_info("Project updated to latest version", {"remote_hash": _fetch_remote_project_hash()})
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
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        log_info("API keys read", {"client": request.remote_addr})
        return jsonify({"apiKeys": keys}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    try:
        entry = _create_api_key(name)
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except DuplicateNameError as exc:
        return jsonify({"error": str(exc)}), 409
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
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        log_info("API key deleted", {"client": request.remote_addr})
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    try:
        target = _rename_api_key(key, name)
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except DuplicateNameError as exc:
        return jsonify({"error": str(exc)}), 409
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
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        log_info("Shared memory read", {"client": request.remote_addr})
        return jsonify({"sharedMemory": variables}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    value = data.get("value")
    value_type = data.get("type")
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
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


def _add_plugin_event(event: str) -> bool:
    if not _is_valid_event_name(event):
        raise ValueError("Invalid event name.")
    event = event.strip()
    data = _load_plugin_event_subscriptions()
    if event in data:
        raise DuplicateNameError("An event with this name already exists.")
    data[event] = []
    _save_plugin_event_subscriptions(data)
    audio.play_audio("success")()
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
    audio.play_audio("success")()
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
    try:
        _add_plugin_event(event)
    except DuplicateNameError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    log_info("Plugin event created", {"client": request.remote_addr, "event": event})
    return jsonify({"event": event, "plugins": []}), 201


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
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
    except DuplicateNameError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not deleted:
        return jsonify({"error": "Not found."}), 404
    log_info("Plugin removed from event", {"client": request.remote_addr, "event": event, "plugin": plugin})
    return jsonify({"status": "ok"}), 200


@app.route("/api/external-interactions-ips", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def external_access_ips() -> tuple:
    if request.method == "GET":
        try:
            entries = _list_external_interactions_ips()
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        log_info("External interactions access IPs read", {"client": request.remote_addr})
        return jsonify({"externalInteractionsIps": entries}), 200

    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    action = data.get("action")
    note = data.get("note")
    try:
        entry, created = _set_external_interactions_ip(ip, action, note)
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("External interactions access IP saved", {"client": request.remote_addr, "entry": entry})
    return jsonify(entry), 201 if created else 200




@app.route("/api/external-interactions-ips/<path:ip>", methods=["PATCH", "DELETE", "OPTIONS"])
@network.external_interactions_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def external_access_ip_item(ip: str) -> tuple:
    if request.method == "DELETE":
        try:
            deleted = _delete_external_interactions_ip(ip)
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError:
            return jsonify({"error": "Invalid request."}), 400
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        log_info("External interactions access IP deleted", {"client": request.remote_addr, "ip": ip})
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    note = data.get("note")
    try:
        entry = _update_external_interactions_ip(ip, action, note)
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if entry is None:
        return jsonify({"error": "Not found."}), 404
    log_info("External interactions access IP updated", {"client": request.remote_addr, "ip": ip})
    return jsonify(entry), 200


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
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
    api_keys = sorted(_api_key_store, key=lambda k: (k.get("name") or "").lower())
    shared_memory = _load_shared_memory()
    external_interactions_ips = _load_external_interactions_ips()
    users = _list_users()
    session = _active_session()
    return render_template_string(
        template,
        is_admin=bool(session and session["admin"]),
        is_root=bool(session and session.get("root", False)),
        account_username=session["username"] if session else "",
        internal_interactions=_read_env_bool("INTERNAL_INTERACTIONS", INTERNAL_INTERACTIONS),
        api_keys_enabled=_read_env_bool("API_KEYS_ENABLED", API_KEYS_ENABLED),
        display_promotion=_read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
        play_audios=_read_env_bool("PLAY_AUDIOS", PLAY_AUDIOS),
        play_log_sounds=_read_env_bool("PLAY_LOG_SOUNDS", PLAY_LOG_SOUNDS),
        play_startup_sound=_read_env_bool("PLAY_STARTUP_SOUND", PLAY_STARTUP_SOUND),
        sounds={event: _read_env_var(audio.SOUND_ENV_VARS[event], audio.DEFAULT_SOUND_FILES.get(event, "")) for event in audio.SOUND_EVENTS},
        available_audios=audio.list_audio_files(),
        sound_events=[(event, event.capitalize()) for event in audio.SOUND_EVENTS],
        shared_memory_enabled=_read_env_bool("SHARED_MEMORY_ENABLED", SHARED_MEMORY_ENABLED),
        has_api_keys=bool(api_keys),
        api_keys_json=json.dumps(api_keys),
        has_shared_memory=bool(shared_memory),
        shared_memory_json=json.dumps(shared_memory),
        has_external_interactions_ips=bool(external_interactions_ips),
        external_interactions_ips_json=json.dumps(external_interactions_ips),
        external_interactions_enabled=_external_interactions_enabled(),
        external_interactions_worker_bind=_external_interactions_worker_bind_address(),
        automatic_update=_read_env_bool("AUTOMATIC_UPDATE", AUTOMATIC_UPDATE),
        current_version=_get_current_project_version(),
        effective_version=_get_effective_project_version(),
        indicated_version=_get_indicated_project_version(),
        update_available=_UPDATE_AVAILABLE_AT_STARTUP,
        project_integrity_ok=_PROJECT_INTEGRITY_OK,
        development=DEVELOPMENT,
        project_update_disabled=DEVELOPMENT,
        automatic_plugin_library_update=_read_env_bool("AUTOMATIC_PLUGIN_LIBRARY_UPDATE", AUTOMATIC_PLUGIN_LIBRARY_UPDATE),
        automatic_plugin_upgrade=_read_env_bool("AUTOMATIC_PLUGIN_UPGRADE", AUTOMATIC_PLUGIN_UPGRADE),
        current_plugins_lib_version=_get_current_plugins_lib_version(),
        effective_plugins_lib_version=_get_effective_plugins_lib_version(),
        indicated_plugins_lib_version=_get_indicated_plugins_lib_version(),
        plugins_lib_update_available=_PLUGIN_UPDATE_AVAILABLE_AT_STARTUP,
        plugin_integrity_ok=_PLUGIN_INTEGRITY_OK,
        has_users=bool(users),
        users_json=json.dumps(users),
        current_username=session["username"] if session else "",
    )


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
        log_warn("Login rejected: USERNAME/PASSWORD or USERS not configured in .env", {"client": request.remote_addr})
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
        return jsonify({"error": str(exc)}), 404
    except CurrentPasswordError as exc:
        log_warn("Password change rejected", {"client": request.remote_addr, "error": str(exc)})
        audio.play_audio("warn")()
        return jsonify({"error": str(exc)}), 403
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
    if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$"):
        return jsonify({"error": "Invalid password hash."}), 400
    if not isinstance(admin, bool):
        return jsonify({"error": "Invalid request."}), 400
    try:
        _register_user(username, password_hash, admin)
    except UsernameTakenError as exc:
        log_warn("User registration rejected", {"client": request.remote_addr, "error": str(exc)})
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 403
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
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
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
            return jsonify({"error": str(exc)}), 403
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
