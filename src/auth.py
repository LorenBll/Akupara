"""Authentication, sessions, users and API keys."""

from __future__ import annotations

import functools
import hmac
import json
import secrets
import time

import state
import env_store as _env_store
from logginglib import log_debug, log_info, log_warn
from validation import _is_valid_key_name

try:
    from flask import Flask, jsonify, redirect, request  # type: ignore
except ImportError:
    Flask = jsonify = redirect = request = None  # type: ignore


class FeatureDisabledError(RuntimeError):
    """Raised when a feature is disabled and its functionality is unavailable."""


class DuplicateNameError(RuntimeError):
    """Raised when creating/renaming an entity whose name is already taken."""


class AccountNotFoundError(RuntimeError):
    """Raised when the account is not found in the configured credentials."""


class CurrentPasswordError(RuntimeError):
    """Raised when the current password provided does not match the stored one."""


class UsernameTakenError(RuntimeError):
    """Raised when registering a username that already exists."""


def _get_flask_request():
    try:
        from flask import request as _req  # type: ignore
        return _req
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def _prune_expired_sessions() -> None:
    cutoff = time.time() - state._SESSION_MAX_AGE
    with state._SESSION_LOCK:
        expired = [token for token, session in state._SESSION_STORE.items() if session["last_refresh"] < cutoff]
        for token in expired:
            del state._SESSION_STORE[token]
    if expired:
        log_debug("Pruned expired sessions", {"count": len(expired)})


def _active_session() -> dict | None:
    req = _get_flask_request()
    if req is None:
        return None
    provided_token = req.cookies.get(state.SESSION_COOKIE_NAME)  # type: ignore
    if not provided_token:
        return None
    with state._SESSION_LOCK:
        session = state._SESSION_STORE.get(provided_token)
        if session is None:
            return None
        if time.time() - session["last_refresh"] > state._SESSION_MAX_AGE:
            state._SESSION_STORE.pop(provided_token, None)
            return None
        return {"username": session["username"], "admin": session["admin"], "root": bool(session.get("root", False))}


def _is_valid_session_cookie() -> bool:
    return _active_session() is not None


def _issue_session_cookie(response, username: str, admin: bool, root: bool = False) -> None:
    _prune_expired_sessions()
    token = _generate_session_token()
    with state._SESSION_LOCK:
        state._SESSION_STORE[token] = {"username": username, "admin": admin, "root": bool(root), "last_refresh": time.time()}
    response.set_cookie(
        state.SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Lax",
        max_age=state._SESSION_MAX_AGE,
        path="/",
    )


def _renew_session_cookie(response) -> None:
    req = _get_flask_request()
    if req is None:
        return
    provided_token = req.cookies.get(state.SESSION_COOKIE_NAME)  # type: ignore
    if not provided_token:
        return
    with state._SESSION_LOCK:
        session = state._SESSION_STORE.get(provided_token)
        if session is None:
            return
        session["last_refresh"] = time.time()
        new_token = _generate_session_token()
        state._SESSION_STORE[new_token] = session
    response.set_cookie(
        state.SESSION_COOKIE_NAME,
        new_token,
        httponly=True,
        samesite="Lax",
        max_age=state._SESSION_MAX_AGE,
        path="/",
    )


def _unauthorized_response():
    req = _get_flask_request()
    from flask import jsonify as _jsonify, redirect as _redirect  # type: ignore
    if req is not None and req.path.startswith("/api/"):
        log_warn("Rejected API request: missing or invalid refresh cookie", {"client": req.remote_addr})
        return _jsonify({"error": "API key required."}), 401
    client = req.remote_addr if req is not None else "unknown"
    log_warn("Redirecting unauthenticated request to /login", {"client": client})
    return _redirect("/login")


def _refresh_cookie_when_valid(func, *args, **kwargs):
    app = state.app
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
    req = _get_flask_request()
    if req is None:
        return False
    provided_key = req.headers.get("X-Api-Key")
    if not provided_key:
        return False
    return any(hmac.compare_digest(provided_key, entry["key"]) for entry in state._api_key_store)


def api_key_or_admin_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_valid_api_key():
            return func(*args, **kwargs)
        session = _active_session()
        if session is not None and session["admin"]:
            return _refresh_cookie_when_valid(func, *args, **kwargs)
        req = _get_flask_request()
        client = req.remote_addr if req is not None else "unknown"
        log_warn("Rejected admin request: logged-in user is not an admin", {"client": client})
        return _unauthorized_response()
    return wrapper


def _require_admin_session():
    req = _get_flask_request()
    from flask import jsonify as _jsonify  # type: ignore
    session = _active_session()
    if session is None:
        return _unauthorized_response()
    if not session["admin"]:
        client = req.remote_addr if req is not None else "unknown"
        log_warn("Rejected admin request: logged-in user is not an admin", {"client": client})
        return _jsonify({"error": "Admin privileges required."}), 403
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
    req = _get_flask_request()
    if req is None:
        return None
    provided_key = req.headers.get("X-Api-Key")
    if provided_key:
        for entry in state._api_key_store:
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
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        req = _get_flask_request()
        result = func(*args, **kwargs)
        status = result[1] if isinstance(result, tuple) and len(result) > 1 else 200
        if req is not None and req.method in ("POST", "PATCH", "DELETE") and isinstance(status, int) and 200 <= status < 300:
            actor = _resolve_actor()
            if actor and actor[1]:
                kind, actor_id = actor
                log_info("Change recorded", {"kind": kind, "id": actor_id, "method": req.method, "path": req.path, "status": status})
        return result
    return wrapper


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _login_credentials_configured() -> bool:
    return bool(_load_users())


def _load_users() -> list[dict]:
    raw = _env_store.read_env_var("USERS")
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
    with state._FAILED_LOGIN_LOCK:
        state._FAILED_LOGIN_ATTEMPTS += 1
        return state._FAILED_LOGIN_ATTEMPTS <= state._MAX_FAILED_LOGIN_WARNS


def _reset_failed_login_attempts() -> None:
    with state._FAILED_LOGIN_LOCK:
        state._FAILED_LOGIN_ATTEMPTS = 0


def _matching_stored_passwords(username: str) -> list[tuple[str, str]]:
    username_key = username.casefold()
    records: list[tuple[str, str]] = []
    for user in _load_users():
        if username_key == user["username"].casefold():
            records.append(("users", user["password"]))
    return records


def _set_user_password_env(username: str, new_password_hash: str) -> bool:
    raw = _env_store.read_env_var("USERS")
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
            _env_store.write_env_var("USERS", json.dumps(data, ensure_ascii=False))
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


def _register_user(username: str, password_hash: str, admin: bool) -> None:
    username = username.strip()
    if not username:
        raise ValueError("Username must not be empty.")
    if any(ch in username for ch in state._FORBIDDEN_KEY_NAME_CHARS):
        raise ValueError("Username contains prohibited characters.")
    if len(username) < 8:
        raise ValueError("Username must be at least 8 characters long.")
    if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$"):
        raise ValueError("Invalid password hash.")
    username_key = username.casefold()
    for user in _load_users():
        if username_key == user["username"].casefold():
            raise UsernameTakenError("A user with that username already exists.")
    raw = _env_store.read_env_var("USERS")
    try:
        data = json.loads(raw) if raw else []
    except (json.JSONDecodeError, ValueError, TypeError):
        data = []
    if not isinstance(data, list):
        data = []
    data.append({"username": username, "password": password_hash, "admin": bool(admin), "root": False, "id": secrets.token_hex(16)})
    _env_store.write_env_var("USERS", json.dumps(data, ensure_ascii=False))
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    log_info("User registered", {"username": username})


def _list_users() -> list[dict]:
    users = [{"username": user["username"], "admin": user["admin"], "root": user["root"]} for user in _load_users()]
    users.sort(key=lambda user: (not user["root"], not user["admin"], user["username"].casefold()))
    return users


def _is_root_username(username: str) -> bool:
    return any(user["root"] for user in _load_users() if user["username"].casefold() == username.casefold())


def _save_users(users: list[dict]) -> None:
    _env_store.write_env_var("USERS", json.dumps(users, ensure_ascii=False))


def _rename_session_username(old_username: str, new_username: str) -> None:
    old_key = old_username.casefold()
    with state._SESSION_LOCK:
        for session in state._SESSION_STORE.values():
            if session["username"].casefold() == old_key:
                session["username"] = new_username


def _set_session_admin(username: str, admin: bool) -> None:
    key = username.casefold()
    with state._SESSION_LOCK:
        for session in state._SESSION_STORE.values():
            if session["username"].casefold() == key:
                session["admin"] = admin


def _delete_session_username(username: str) -> None:
    key = username.casefold()
    with state._SESSION_LOCK:
        for token in [t for t, s in state._SESSION_STORE.items() if s["username"].casefold() == key]:
            del state._SESSION_STORE[token]


def _revoke_other_sessions(username: str, keep_token: str | None) -> None:
    key = username.casefold()
    with state._SESSION_LOCK:
        for token in [t for t, s in state._SESSION_STORE.items() if s["username"].casefold() == key and t != keep_token]:
            del state._SESSION_STORE[token]


def _rename_user(username: str, new_username: str) -> dict | None:
    if not isinstance(new_username, str) or not new_username.strip():
        raise ValueError("Username must not be empty.")
    if any(ch in new_username for ch in state._FORBIDDEN_KEY_NAME_CHARS):
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
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    log_info("User deleted", {"username": username})
    return True


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def _api_key_id() -> str:
    return secrets.token_hex(16)


def _api_key_cipher():
    from cryptography.fernet import Fernet
    key = _env_store.read_env_var("API_KEY_ENCRYPTION_KEY")
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
    value = _env_store.read_env_var("API_KEYS")
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


def _refresh_api_key_store() -> None:
    state._api_key_store[:] = []
    for entry in _load_api_keys():
        plain = _api_key_decrypt(entry["key"])
        if not plain:
            continue
        state._api_key_store.append({"name": entry["name"], "key": plain, "id": entry["id"]})


def _save_api_keys(entries: list[dict]) -> None:
    encrypted = [
        {"name": e["name"], "key": _api_key_encrypt(e["key"]), "id": e.get("id") or _api_key_id()}
        for e in entries
    ]
    _env_store.write_env_var("API_KEYS", json.dumps(encrypted, ensure_ascii=False))
    state._api_key_store[:] = [{"name": e["name"], "key": e["key"], "id": e.get("id") or _api_key_id()} for e in entries]


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _list_api_keys() -> list[dict]:
    if not state.API_KEYS_ENABLED:
        raise FeatureDisabledError("The API keys functionality is disabled.")
    return [{"name": entry["name"], "key": entry["key"]} for entry in state._api_key_store]


def _create_api_key(name: str) -> dict:
    if not state.API_KEYS_ENABLED:
        raise FeatureDisabledError("The API keys functionality is disabled.")
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    key = _generate_api_key()
    if any(entry["name"].lower() == name.lower() for entry in state._api_key_store):
        raise DuplicateNameError("An API key with this name already exists.")
    entry = {"name": name, "key": key, "id": _api_key_id()}
    _save_api_keys(state._api_key_store + [entry])
    return {"name": entry["name"], "key": entry["key"]}


def _delete_api_key(key: str) -> bool:
    if not state.API_KEYS_ENABLED:
        raise FeatureDisabledError("The API keys functionality is disabled.")
    remaining = [entry for entry in state._api_key_store if entry["key"] != key]
    if len(remaining) == len(state._api_key_store):
        return False
    _save_api_keys(remaining)
    try:
        import audio
        audio.play_audio("success")()
    except Exception:
        pass
    return True


def _rename_api_key(key: str, name: str) -> dict | None:
    if not state.API_KEYS_ENABLED:
        raise FeatureDisabledError("The API keys functionality is disabled.")
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    target = next((entry for entry in state._api_key_store if entry["key"] == key), None)
    if not target:
        return None
    if any(entry["name"].lower() == name.lower() and entry is not target for entry in state._api_key_store):
        raise DuplicateNameError("An API key with this name already exists.")
    target["name"] = name
    _save_api_keys(state._api_key_store)
    return {"name": target["name"], "key": target["key"]}
