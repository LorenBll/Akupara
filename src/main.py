"""Akupara local web service template."""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
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

import audio

import network

from logginglib import init_logging, log_debug, log_error, log_info, log_warn

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = None

GUI_ENABLED: bool = True

ALLOW_DISCOVERY: bool = False

API_KEYS_ENABLED: bool = False

DISPLAY_PROMOTION: bool = True

PLAY_AUDIOS: bool = True

SHARED_MEMORY_ENABLED: bool = True

NETWORK_INTERACTIONS: bool = False

EXTERNAL_ACCESS: bool = False

_external_access_worker: network.ExternalAccessWorker | None = None

_CONFIG_CACHE: dict | None = None

SESSION_COOKIE_NAME = "akupara-refresh"
_SESSION_STORE: dict[str, dict] = {}
_SESSION_MAX_AGE: int = 900
_SESSION_LOCK = threading.Lock()


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
    global SERVICE_PORT, GUI_ENABLED, ALLOW_DISCOVERY, API_KEYS_ENABLED, DISPLAY_PROMOTION, PLAY_AUDIOS, SHARED_MEMORY_ENABLED, NETWORK_INTERACTIONS, EXTERNAL_ACCESS, NETWORK_ACCESS_ALLOW_NEW
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    config = _load_configuration()

    configured_port = config.get("port", 49150)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        log_warn("Invalid port value in configuration; defaulting to 49150", {"port": configured_port})
        configured_port = 49150
    SERVICE_PORT = configured_port

    GUI_ENABLED = config.get("guiEnabled", True)

    ALLOW_DISCOVERY = _parse_bool(os.getenv("ALLOW_DISCOVERY"), False)

    API_KEYS_ENABLED = _parse_bool(os.getenv("API_KEYS_ENABLED"), False)

    DISPLAY_PROMOTION = _parse_bool(os.getenv("DISPLAY_PROMOTION"), True)

    PLAY_AUDIOS = _parse_bool(os.getenv("PLAY_AUDIOS"), True)
    audio.set_audio_worker_enabled(PLAY_AUDIOS)

    SHARED_MEMORY_ENABLED = _parse_bool(os.getenv("SHARED_MEMORY_ENABLED"), True)

    NETWORK_INTERACTIONS = _parse_bool(os.getenv("NETWORK_INTERACTIONS"), False)

    EXTERNAL_ACCESS = _parse_bool(os.getenv("EXTERNAL_ACCESS"), False)

    NETWORK_ACCESS_ALLOW_NEW = _parse_bool(os.getenv("NETWORK_ACCESS_ALLOW_NEW"), False)

    for sound_event, env_name in audio.SOUND_ENV_VARS.items():
        if _read_env_var(env_name, None) is None:
            _write_env_var(env_name, audio.DEFAULT_SOUND_FILES.get(sound_event, ""))

    _SESSION_STORE.clear()

    if not _login_credentials_configured():
        log_warn("Login credentials not configured", {"hint": "set USERNAME and PASSWORD (or USERS) in .env"})

    log_debug("Resolved config values", {"port": SERVICE_PORT, "guiEnabled": GUI_ENABLED, "allowDiscovery": ALLOW_DISCOVERY, "apiKeysEnabled": API_KEYS_ENABLED, "externalAccess": EXTERNAL_ACCESS})
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
        if session is not None:
            session["last_refresh"] = time.time()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        provided_token,
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
    keys = [entry["key"] for entry in _load_api_keys()]
    return provided_key in keys


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
        for entry in _load_api_keys():
            if entry["key"] == provided_key:
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
    expected_username = _read_env_var("USERNAME")
    expected_password = _read_env_var("PASSWORD")
    if expected_username and username_key == expected_username.casefold():
        records.append(("legacy", expected_password))
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
    if any(ch in username for ch in " ,;\\/:%"):
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
    if any(ch in new_username for ch in " ,;\\/:%"):
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


def _require_allow_discovery_enabled() -> None:
    if not ALLOW_DISCOVERY:
        raise FeatureDisabledError("The shared memory functionality is disabled.")


def _effective_shared_memory_enabled() -> bool:
    return ALLOW_DISCOVERY and SHARED_MEMORY_ENABLED


def _require_shared_memory_enabled() -> None:
    if not _effective_shared_memory_enabled():
        raise FeatureDisabledError("The shared memory functionality is disabled.")


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
@network.network_worker_callable
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
@network.network_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("POST", "OPTIONS")
def terminate() -> tuple:
    log_info("Terminate requested", {"client": request.remote_addr})
    threading.Timer(0.5, _terminate).start()
    return jsonify({"status": "ok", "message": "Akupara is terminating."}), 200


@app.route("/api/restart", methods=["POST", "OPTIONS"])
@network.network_worker_callable
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
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
def _set_allow_discovery(value: bool) -> None:
    global ALLOW_DISCOVERY
    ALLOW_DISCOVERY = value
    _write_env_bool("ALLOW_DISCOVERY", value)


@audio.play_audio("acknowledge")
def _set_api_keys_enabled(value: bool) -> None:
    global API_KEYS_ENABLED
    API_KEYS_ENABLED = value
    _write_env_bool("API_KEYS_ENABLED", value)


def _external_interactions_enabled() -> bool:
    return bool(NETWORK_INTERACTIONS) and bool(EXTERNAL_ACCESS)


@audio.play_audio("acknowledge")
def _set_external_interactions(value: bool) -> None:
    global NETWORK_INTERACTIONS, EXTERNAL_ACCESS
    if value:
        NETWORK_INTERACTIONS = True
        EXTERNAL_ACCESS = True
        _write_env_bool("NETWORK_INTERACTIONS", True)
        _write_env_bool("EXTERNAL_ACCESS", True)
        _start_external_access_worker()
    else:
        EXTERNAL_ACCESS = False
        NETWORK_INTERACTIONS = False
        _stop_external_access_worker()
        _write_env_bool("EXTERNAL_ACCESS", False)
        _write_env_bool("NETWORK_INTERACTIONS", False)


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


def _network_worker_bind_address() -> dict:
    if _external_access_worker is None:
        return {"address": None, "port": SERVICE_PORT}
    host = _resolve_external_host()
    return {"address": host, "port": SERVICE_PORT}


def _start_external_access_worker() -> None:
    global _external_access_worker
    if _external_access_worker is not None:
        return
    host = _resolve_external_host()
    if host is None:
        log_warn("External access worker not started: no non-loopback device address available")
        return
    worker = network.ExternalAccessWorker(app, host, SERVICE_PORT, ip_policy=_network_worker_ip_policy)
    try:
        worker.start()
    except OSError as exc:
        log_error("External access worker failed to start", {"host": host, "port": SERVICE_PORT, "error": str(exc)})
        return
    _external_access_worker = worker


def _stop_external_access_worker() -> None:
    global _external_access_worker
    worker = _external_access_worker
    _external_access_worker = None
    if worker is not None:
        worker.stop()


def _require_network_interactions_enabled() -> None:
    if not NETWORK_INTERACTIONS:
        raise FeatureDisabledError("The network interactions functionality is disabled.")


@audio.play_audio("acknowledge")
def _set_external_access(value: bool) -> None:
    _require_network_interactions_enabled()
    global EXTERNAL_ACCESS
    EXTERNAL_ACCESS = value
    _write_env_bool("EXTERNAL_ACCESS", value)
    if value:
        _start_external_access_worker()
    else:
        _stop_external_access_worker()


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


_NETWORK_ACCESS_ACTIONS = ("allow", "unknown", "block")


def _load_network_access_ips() -> list[dict]:
    raw = _read_env_var("NETWORK_ACCESS_IPS")
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
        for ip, action in entry.items():
            if action not in _NETWORK_ACCESS_ACTIONS:
                continue
            try:
                canonical = _canonical_network_ip(ipaddress.ip_address(ip))
            except ValueError:
                continue
            entries.append({canonical: action})
    return entries


def _save_network_access_ips(entries: list[dict]) -> None:
    _write_env_var("NETWORK_ACCESS_IPS", json.dumps(entries))


def _list_network_access_ips() -> list[dict]:
    _require_network_interactions_enabled()
    return _load_network_access_ips()


def _set_network_access_ip(ip: str, action: str) -> tuple[dict, bool]:
    _require_network_interactions_enabled()
    if action not in _NETWORK_ACCESS_ACTIONS:
        raise ValueError("The value must be 'allow', 'unknown' or 'block'.")
    canonical = _maximize_network_ip(ip)
    entries = _load_network_access_ips()
    existing = next((entry for entry in entries if canonical in entry), None)
    entry = {canonical: action}
    remaining = [item for item in entries if canonical not in item]
    remaining.append(entry)
    _save_network_access_ips(remaining)
    if existing is None:
        audio.play_audio("success")()
    else:
        audio.play_audio("acknowledge")()
    return entry, existing is None


@audio.play_audio("acknowledge")
def _update_network_access_ip(ip: str, action: str) -> dict | None:
    _require_network_interactions_enabled()
    if action not in _NETWORK_ACCESS_ACTIONS:
        raise ValueError("The value must be 'allow', 'unknown' or 'block'.")
    canonical = _maximize_network_ip(ip)
    entries = _load_network_access_ips()
    if not any(canonical in entry for entry in entries):
        return None
    entry = {canonical: action}
    remaining = [item for item in entries if canonical not in item]
    remaining.append(entry)
    _save_network_access_ips(remaining)
    return entry


def _delete_network_access_ip(ip: str) -> bool:
    _require_network_interactions_enabled()
    canonical = _maximize_network_ip(ip)
    entries = _load_network_access_ips()
    remaining = [item for item in entries if canonical not in item]
    if len(remaining) == len(entries):
        return False
    _save_network_access_ips(remaining)
    audio.play_audio("success")()
    return True


@audio.play_audio("acknowledge")
def _set_network_access_allow_new(value: bool) -> None:
    _require_network_interactions_enabled()
    global NETWORK_ACCESS_ALLOW_NEW
    NETWORK_ACCESS_ALLOW_NEW = value
    _write_env_bool("NETWORK_ACCESS_ALLOW_NEW", value)


def _record_network_access_ip_automatic(canonical: str, action: str) -> None:
    entries = _load_network_access_ips()
    if any(canonical in entry for entry in entries):
        return
    entries.append({canonical: action})
    _save_network_access_ips(entries)
    audio.play_audio("acknowledge")()


def _network_worker_ip_policy(remote_addr: str) -> bool:
    """Per-request access decision for the network worker.

    Each IP in the list carries one of three actions: ``"allow"`` (requests
    pass through), ``"block"`` (requests are refused) and ``"unknown"``. New
    IPs are always recorded in the list with ``"unknown"``. Requests from IPs
    whose action is ``"unknown"``, and requests from IPs not yet in the list,
    are decided by ``NETWORK_ACCESS_ALLOW_NEW``. Recordings made here are
    automatic and play the acknowledge sound.
    """
    try:
        canonical = _canonical_network_ip(ipaddress.ip_address(remote_addr))
    except ValueError:
        return False
    entries = _load_network_access_ips()
    for entry in entries:
        if canonical in entry:
            action = entry[canonical]
            if action == "allow":
                return True
            if action == "block":
                return False
            break
    _record_network_access_ip_automatic(canonical, "unknown")
    return _read_env_bool("NETWORK_ACCESS_ALLOW_NEW", NETWORK_ACCESS_ALLOW_NEW)


def _set_display_promotion(value: bool) -> None:
    global DISPLAY_PROMOTION
    DISPLAY_PROMOTION = value
    _write_env_bool("DISPLAY_PROMOTION", value)


def _set_play_audios(value: bool) -> None:
    global PLAY_AUDIOS
    PLAY_AUDIOS = value
    _write_env_bool("PLAY_AUDIOS", value)
    audio.set_audio_worker_enabled(value)


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
    _require_allow_discovery_enabled()
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
            "allowDiscovery": _read_env_bool("ALLOW_DISCOVERY", ALLOW_DISCOVERY),
            "displayPromotion": _read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
            "externalAccess": _read_env_bool("EXTERNAL_ACCESS", EXTERNAL_ACCESS),
            "networkAccessAllowNew": _read_env_bool("NETWORK_ACCESS_ALLOW_NEW", NETWORK_ACCESS_ALLOW_NEW),
        }), 200
    denied = _require_admin_session()
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    keys = [key for key in ("allowDiscovery", "displayPromotion", "externalAccess", "networkAccessAllowNew") if key in data]
    if not keys:
        return jsonify({"error": "No known setting provided."}), 400
    for key in keys:
        value = data[key]
        if not isinstance(value, bool):
            return jsonify({"error": f"{key} must be a boolean."}), 400
        if key == "allowDiscovery":
            _set_allow_discovery(value)
        elif key == "externalAccess":
            try:
                _set_external_access(value)
            except FeatureDisabledError as exc:
                return jsonify({"error": str(exc)}), 403
        elif key == "networkAccessAllowNew":
            try:
                _set_network_access_allow_new(value)
            except FeatureDisabledError as exc:
                return jsonify({"error": str(exc)}), 403
        else:
            _set_display_promotion(value)
    log_info("Settings updated", {"client": request.remote_addr, "allowDiscovery": ALLOW_DISCOVERY, "displayPromotion": DISPLAY_PROMOTION, "externalAccess": EXTERNAL_ACCESS, "networkAccessAllowNew": NETWORK_ACCESS_ALLOW_NEW})
    return jsonify({
        "allowDiscovery": ALLOW_DISCOVERY,
        "displayPromotion": DISPLAY_PROMOTION,
        "externalAccess": EXTERNAL_ACCESS,
        "networkAccessAllowNew": NETWORK_ACCESS_ALLOW_NEW,
    }), 200


@app.route("/api/audio", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.network_worker_callable
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


@app.route("/api/audio/play", methods=["POST", "HEAD", "OPTIONS"])
@network.network_worker_callable
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
@network.network_worker_callable
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
@network.network_worker_callable
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
@network.network_worker_callable
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


_FORBIDDEN_KEY_NAME_CHARS = set(" ,;:\\/%")


def _is_valid_key_name(name: str) -> bool:
    return bool(name) and not any(ch in name for ch in _FORBIDDEN_KEY_NAME_CHARS)


def _api_key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _load_api_keys() -> list[dict]:
    value = _read_env_var("API_KEYS")
    keys: list[dict] = []
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
        key_id = fields[2].strip() if len(fields) > 2 and fields[2].strip() else _api_key_id(key)
        keys.append({"name": name, "key": key, "id": key_id})
    return keys


def _save_api_keys(keys: list[dict]) -> None:
    value = ",".join(f"{entry['name']}:{entry['key']}:{entry.get('id') or _api_key_id(entry['key'])}" for entry in keys)
    _write_env_var("API_KEYS", value)


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _list_api_keys() -> list[dict]:
    _require_api_keys_enabled()
    return [{"name": entry["name"], "key": entry["key"]} for entry in _load_api_keys()]


@audio.play_audio("success")
def _create_api_key(name: str) -> dict:
    _require_api_keys_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    key = _generate_api_key()
    keys = _load_api_keys()
    if any(entry["name"].lower() == name.lower() for entry in keys):
        raise DuplicateNameError("An API key with this name already exists.")
    entry = {"name": name, "key": key, "id": _api_key_id(key)}
    keys.append(entry)
    _save_api_keys(keys)
    return {"name": entry["name"], "key": entry["key"]}


def _delete_api_key(key: str) -> bool:
    _require_api_keys_enabled()
    keys = _load_api_keys()
    remaining = [entry for entry in keys if entry["key"] != key]
    if len(remaining) == len(keys):
        return False
    _save_api_keys(remaining)
    audio.play_audio("success")()
    return True


@audio.play_audio("acknowledge")
def _rename_api_key(key: str, name: str) -> dict | None:
    _require_api_keys_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    keys = _load_api_keys()
    target = next((entry for entry in keys if entry["key"] == key), None)
    if not target:
        return None
    if any(entry["name"].lower() == name.lower() and entry is not target for entry in keys):
        raise DuplicateNameError("An API key with this name already exists.")
    target["name"] = name
    _save_api_keys(keys)
    return {"name": target["name"], "key": target["key"]}


@app.route("/api/api-keys", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.network_worker_callable
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
@network.network_worker_callable
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
@network.network_worker_callable
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
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except DuplicateNameError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Shared variable created", {"client": request.remote_addr, "name": name, "type": value_type})
    return jsonify(entry), 201


@app.route("/api/shared-memory/<path:name>", methods=["DELETE", "OPTIONS"])
@network.network_worker_callable
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
@network.network_worker_callable
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


@app.route("/api/network-access-ips", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.network_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def network_access_ips() -> tuple:
    if request.method == "GET":
        try:
            entries = _list_network_access_ips()
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        log_info("Network access IPs read", {"client": request.remote_addr})
        return jsonify({"networkAccessIps": entries}), 200

    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    action = data.get("action")
    try:
        entry, created = _set_network_access_ip(ip, action)
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    log_info("Network access IP saved", {"client": request.remote_addr, "entry": entry})
    return jsonify(entry), 201 if created else 200


@app.route("/api/network-access-ips/<path:ip>", methods=["PATCH", "DELETE", "OPTIONS"])
@network.network_worker_callable
@log_change
@api_key_or_admin_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def network_access_ip_item(ip: str) -> tuple:
    if request.method == "DELETE":
        try:
            deleted = _delete_network_access_ip(ip)
        except FeatureDisabledError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError:
            return jsonify({"error": "Invalid request."}), 400
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        log_info("Network access IP deleted", {"client": request.remote_addr, "ip": ip})
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    try:
        entry = _update_network_access_ip(ip, action)
    except FeatureDisabledError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if entry is None:
        return jsonify({"error": "Not found."}), 404
    log_info("Network access IP updated", {"client": request.remote_addr, "ip": ip})
    return jsonify(entry), 200


@network.network_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def index():
    log_info("Serving UI", {"client": request.remote_addr})
    web_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (web_dir / "index.html").read_text(encoding="utf-8")
    return render_template_string(
        template,
        display_promotion=_read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
    )


@network.network_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_css(filename: str):
    css_dir = Path(__file__).resolve().parent.parent / "ui" / "css"
    return send_from_directory(css_dir, filename)


@network.network_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_icon(filename: str):
    icons_dir = Path(__file__).resolve().parent.parent / "resources" / "icons"
    return send_from_directory(icons_dir, filename)


@network.network_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_page(filename: str):
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    return send_from_directory(pages_dir, filename)


@network.network_worker_callable
@session_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_settings_page():
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (pages_dir / "settings.html").read_text(encoding="utf-8")
    api_keys = sorted(_load_api_keys(), key=lambda k: (k.get("name") or "").lower())
    shared_memory = _load_shared_memory()
    network_access_ips = _load_network_access_ips()
    legacy_username = _read_env_var("USERNAME")
    users = _list_users()
    session = _active_session()
    return render_template_string(
        template,
        is_admin=bool(session and session["admin"]),
        is_root=bool(session and session.get("root", False)),
        account_username=session["username"] if session else "",
        allow_discovery=_read_env_bool("ALLOW_DISCOVERY", ALLOW_DISCOVERY),
        api_keys_enabled=_read_env_bool("API_KEYS_ENABLED", API_KEYS_ENABLED),
        display_promotion=_read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
        play_audios=_read_env_bool("PLAY_AUDIOS", PLAY_AUDIOS),
        sounds={event: _read_env_var(audio.SOUND_ENV_VARS[event], audio.DEFAULT_SOUND_FILES.get(event, "")) for event in audio.SOUND_EVENTS},
        available_audios=audio.list_audio_files(),
        sound_events=[(event, event.capitalize()) for event in audio.SOUND_EVENTS],
        shared_memory_enabled=_read_env_bool("SHARED_MEMORY_ENABLED", SHARED_MEMORY_ENABLED),
        has_api_keys=bool(api_keys),
        api_keys_json=json.dumps(api_keys),
        has_shared_memory=bool(shared_memory),
        shared_memory_json=json.dumps(shared_memory),
        network_access_allow_new=_read_env_bool("NETWORK_ACCESS_ALLOW_NEW", NETWORK_ACCESS_ALLOW_NEW),
        has_network_access_ips=bool(network_access_ips),
        network_access_ips_json=json.dumps(network_access_ips),
        external_interactions_enabled=_external_interactions_enabled(),
        has_users=bool(users),
        users_json=json.dumps(users),
        current_username=session["username"] if session else "",
        legacy_username=legacy_username,
        network_worker_bind=_network_worker_bind_address(),
    )


@network.network_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def login_page():
    if _is_valid_session_cookie():
        return redirect("/")
    web_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (web_dir / "login.html").read_text(encoding="utf-8")
    return render_template_string(template)


@network.network_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_argon2_script():
    js_dir = Path(__file__).resolve().parent.parent / "ui" / "js" / "argon2"
    return send_from_directory(js_dir, "argon2-bundled.min.js")


@network.network_worker_callable
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_login_icon():
    icons_dir = Path(__file__).resolve().parent.parent / "resources" / "icons"
    return send_from_directory(icons_dir, "akupara.svg")


@app.route("/api/login", methods=["POST", "OPTIONS"])
@network.network_worker_callable
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
        log_info("Login successful for user", {"username": user["username"], "client": request.remote_addr})
        audio.play_audio("success")()
        return response, 200
    log_warn("Login failed", {"client": request.remote_addr})
    audio.play_audio("error")()
    return jsonify({"error": "Invalid credentials."}), 401


@app.route("/api/change-password", methods=["POST", "OPTIONS"])
@network.network_worker_callable
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
        audio.play_audio("error")()
        return jsonify({"error": str(exc)}), 403
    audio.play_audio("success")()
    return jsonify({"status": "ok"}), 200


@app.route("/api/users", methods=["GET", "POST", "HEAD", "OPTIONS"])
@network.network_worker_callable
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
@network.network_worker_callable
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
@network.network_worker_callable
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
        "/ui/css/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_css,
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
        _initialize_service_config()
        _register_ui_routes(app)
    except Exception as exc:
        log_error("Failed to load configuration", {"error": str(exc), "traceback": _format_exc()})
        exit(1)

    if EXTERNAL_ACCESS:
        _start_external_access_worker()

    try:
        log_info("Akupara starting", {"bind": f"http://{SERVICE_HOST}:{SERVICE_PORT}", "gui": GUI_ENABLED, "port": SERVICE_PORT, "guiEnabled": GUI_ENABLED, "allowDiscovery": ALLOW_DISCOVERY, "apiKeysEnabled": API_KEYS_ENABLED, "externalAccess": EXTERNAL_ACCESS})

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
