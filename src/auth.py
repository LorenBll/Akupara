"""Authentication, sessions, users and API keys."""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import state
import env_store as _env_store
from argon2.low_level import Type as _Argon2Type, hash_secret as _argon2_hash_secret
from logginglib import log_debug, log_error, log_info, log_warn
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


class PasswordStorageError(RuntimeError):
    """Raised when the password store (its pepper or a stored verifier) is unusable."""


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
                log_info("Change recorded", {"kind": kind, "id": actor_id, "method": req.method, "path": req.path, "status": status, "client": req.remote_addr})
        return result
    return wrapper


# ---------------------------------------------------------------------------
# Password storage
#
# The browser stretches the password with Argon2id and sends only the encoded
# hash. That hash is still password-equivalent, so it is never stored as-is:
# each stored verifier is ``akupara$2$<wrap salt>$<tag>`` where
# ``tag = HMAC-SHA256(pepper, wrap salt || client hash)``. The pepper lives in
# ``resources/password-pepper`` (outside ``.env``), so a leaked ``.env`` cannot
# be attacked offline and its values cannot be replayed at the login endpoint.
# Each account stores its own random client salt (``USERS[].client_salt``), so
# the salt is unique per account and survives a rename unchanged.
#
# The pepper is scoped to one installation, and an installation is one setup
# run: ``resources/installation.json`` marks that it has started and the setup
# scripts delete it, so the next start counts as a new installation and replaces
# the pepper. See :func:`_initialize_password_storage` for the four cases.
#
# The peppered format is the only one accepted. A ``USERS`` entry that still
# holds a raw Argon2id hash, or that lacks a client salt, is unusable: the
# stored password must be regenerated with ``scripts/generate-password-hash.js``.
# ---------------------------------------------------------------------------

_PASSWORD_WRAP_PREFIX = "akupara$2$"
_PASSWORD_PEPPER_BYTES = 32
_PASSWORD_SALT_BYTES = 16
_PASSWORD_PEPPER_CACHE: bytes | None = None

# Argon2id parameters the browser uses (``ui/js/argon2``). The server never
# sees a password during normal use, but the bootstrap account below has to be
# hashed here, and the result must match the browser byte for byte.
_PASSWORD_ARGON2_TIME_COST = 2
_PASSWORD_ARGON2_MEMORY_COST = 65536
_PASSWORD_ARGON2_PARALLELISM = 1
_PASSWORD_ARGON2_HASH_LENGTH = 32

# Bootstrap account created when no user is configured, so a fresh installation
# can be logged into. Its password is public: change it immediately after the
# first login, or delete the account once another root exists.
_DEFAULT_ROOT_USERNAME = "admin"
_DEFAULT_ROOT_PASSWORD = "admin123"


def _password_pepper_path() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "password-pepper"


def _installation_marker_path() -> Path:
    """Return the file recording that this installation has already run.

    It is written when the installation is first prepared — by its first start
    or by ``scripts/generate-password-hash.js`` — and removed by
    ``setup.bat``/``setup.sh``, because one installation corresponds to one
    setup run. Its presence is what separates a fresh installation, which mints
    its own pepper, from one that has run before, which must find the pepper it
    minted.
    """
    return Path(__file__).resolve().parent.parent / "resources" / "installation.json"


def _reset_password_pepper_cache() -> None:
    """Forget the cached pepper (used by tests that relocate the pepper file)."""
    global _PASSWORD_PEPPER_CACHE
    _PASSWORD_PEPPER_CACHE = None


def _decode_password_pepper(raw: str) -> bytes:
    try:
        pepper = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PasswordStorageError("The password pepper is not valid base64.") from exc
    if len(pepper) < _PASSWORD_PEPPER_BYTES:
        raise PasswordStorageError("The password pepper is too short.")
    return pepper


def _read_password_pepper() -> bytes | None:
    """Return the stored pepper, or ``None`` when there is no pepper file."""
    path = _password_pepper_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PasswordStorageError("The password pepper cannot be read.") from exc
    if not raw.strip():
        raise PasswordStorageError("The password pepper is empty.")
    return _decode_password_pepper(raw)


def _write_password_pepper(secret: bytes) -> None:
    """Store *secret* as this installation's pepper, replacing any existing file."""
    path = _password_pepper_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as exc:
        raise PasswordStorageError("The password pepper cannot be created.") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(base64.b64encode(secret).decode("ascii") + "\n")
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise PasswordStorageError("The password pepper cannot be written.") from exc
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _installation_has_run() -> bool:
    return _installation_marker_path().exists()


def _mark_installation_started() -> None:
    """Record that this installation has run, so its pepper may never vanish."""
    path = _installation_marker_path()
    if path.exists():
        return
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"started": started}) + "\n", encoding="utf-8")
    except OSError as exc:
        raise PasswordStorageError("The installation marker cannot be written.") from exc


def _initialize_password_storage() -> str:
    """Apply the installation-scoped pepper policy and say what happened.

    The pepper belongs to one installation — the tree produced by one setup run
    — so a stored verifier is only meaningful to the installation that minted
    it. A new installation therefore mints a fresh pepper and replaces any
    pepper it finds, which can only belong to a previous installation. An
    installation that has already run must find its own pepper: without it no
    stored password can be verified, so the caller must not start.

    Returns ``"created"``, ``"replaced"`` or ``"kept"``, and raises
    ``PasswordStorageError`` when the pepper is missing or unusable on an
    installation that has already run.
    """
    global _PASSWORD_PEPPER_CACHE
    if not _installation_has_run():
        # New installation: whatever pepper is present is not ours.
        already_present = _password_pepper_path().exists()
        secret = secrets.token_bytes(_PASSWORD_PEPPER_BYTES)
        _write_password_pepper(secret)
        _PASSWORD_PEPPER_CACHE = secret
        _mark_installation_started()
        return "replaced" if already_present else "created"
    stored = _read_password_pepper()
    if stored is None:
        raise PasswordStorageError(
            "The password pepper is missing on an installation that has already run; "
            "no stored password can be verified."
        )
    _PASSWORD_PEPPER_CACHE = stored
    return "kept"


def _ensure_password_pepper() -> bytes:
    """Return this installation's pepper, loading it once.

    Minting a pepper is the startup policy's job (:func:`_initialize_password_storage`),
    so a missing file here is an error rather than something to create.
    """
    global _PASSWORD_PEPPER_CACHE
    if _PASSWORD_PEPPER_CACHE is not None:
        return _PASSWORD_PEPPER_CACHE
    stored = _read_password_pepper()
    if stored is None:
        raise PasswordStorageError("The password pepper is missing.")
    _PASSWORD_PEPPER_CACHE = stored
    return stored


def _derive_decoy_salt(username: str) -> str:
    """Return a stable, random-looking salt for a username that does not exist.

    Returning a decoy instead of an error keeps the salt endpoint from becoming
    a username-enumeration oracle for accounts on the current scheme: an unknown
    account answers with a stable value of the same shape.
    """
    pepper = _ensure_password_pepper()
    message = b"akupara/client-salt/decoy\x00" + str(username).casefold().encode("utf-8")
    digest = hmac.new(pepper, message, hashlib.sha256).digest()
    return _b64url_encode(digest[:_PASSWORD_SALT_BYTES])


def _new_client_salt() -> str:
    """Return a fresh random Argon2 salt for a new password."""
    return _b64url_encode(secrets.token_bytes(_PASSWORD_SALT_BYTES))


def _is_valid_client_salt(value) -> bool:
    """Return whether *value* is a well-formed 16-byte base64url client salt."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        return len(_b64url_decode(value)) == _PASSWORD_SALT_BYTES
    except (binascii.Error, ValueError):
        return False


def _is_wrapped_password(value) -> bool:
    return isinstance(value, str) and value.startswith(_PASSWORD_WRAP_PREFIX)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _client_password_hash(password: str, client_salt: str) -> str:
    """Return the encoded Argon2id hash the browser would send for *password*.

    The browser passes the client salt as a string and hashes its UTF-8 bytes;
    the encoded result is the value the login form submits, so it must match
    byte for byte for a server-generated account to be usable.
    """
    return _argon2_hash_secret(
        password.encode("utf-8"),
        client_salt.encode("utf-8"),
        time_cost=_PASSWORD_ARGON2_TIME_COST,
        memory_cost=_PASSWORD_ARGON2_MEMORY_COST,
        parallelism=_PASSWORD_ARGON2_PARALLELISM,
        hash_len=_PASSWORD_ARGON2_HASH_LENGTH,
        type=_Argon2Type.ID,
    ).decode("ascii")


def _password_tag(wrap_salt: bytes, client_hash: str) -> bytes:
    """Return the keyed tag binding *client_hash* to *wrap_salt*.

    A keyed hash is the right primitive here: *client_hash* is a 256-bit
    Argon2id output, not a user-chosen password, so there is nothing left to
    stretch. What matters is that checking a guess requires the pepper.
    """
    pepper = _ensure_password_pepper()
    message = b"akupara/users/v2\x00" + wrap_salt + b"\x00" + str(client_hash).encode("utf-8")
    return hmac.new(pepper, message, hashlib.sha256).digest()


def _wrap_password(client_hash: str) -> str:
    """Wrap an Argon2id client hash into the stored, peppered verifier format."""
    wrap_salt = secrets.token_bytes(_PASSWORD_SALT_BYTES)
    tag = _password_tag(wrap_salt, client_hash)
    return f"{_PASSWORD_WRAP_PREFIX}{_b64url_encode(wrap_salt)}${_b64url_encode(tag)}"


def _verify_wrapped_password(stored: str, client_hash: str) -> bool:
    if not _is_wrapped_password(stored):
        return False
    rest = stored[len(_PASSWORD_WRAP_PREFIX):]
    try:
        salt_part, tag_part = rest.split("$", 1)
    except ValueError:
        return False
    if not salt_part or not tag_part:
        return False
    try:
        wrap_salt = _b64url_decode(salt_part)
        provided_tag = _b64url_decode(tag_part)
    except (binascii.Error, ValueError):
        return False
    if len(wrap_salt) != _PASSWORD_SALT_BYTES:
        return False
    return hmac.compare_digest(_password_tag(wrap_salt, client_hash), provided_tag)


def _verify_stored_password(entry: dict, client_hash: str) -> bool:
    """Return whether *client_hash* matches the verifier stored in *entry*.

    Only the peppered format is accepted. An entry left in the old raw-hash
    format is a configuration error rather than a rejected password, so it is
    reported instead of looking like a wrong password at the login form.
    """
    stored = entry.get("password")
    if not isinstance(stored, str) or not stored:
        raise PasswordStorageError("The stored password verifier is missing.")
    if not _is_wrapped_password(stored):
        raise PasswordStorageError("The stored password verifier is not in the supported format.")
    return _verify_wrapped_password(stored, client_hash)


def _password_client_salt(entry: dict | None, username: str) -> str:
    """Return the Argon2 salt the browser must use to hash the current password."""
    if entry is None:
        return _derive_decoy_salt(username)
    stored_salt = entry.get("client_salt")
    if not _is_valid_client_salt(stored_salt):
        raise PasswordStorageError("The stored client salt is missing or malformed.")
    return stored_salt


def _password_salts_for_username(username: str) -> dict:
    """Return ``{"salt", "newSalt"}`` for the salt endpoint.

    ``salt`` hashes the current password; ``newSalt`` must be used when setting
    a new one and is then stored with the account (never derived from the
    username, so renaming an account does not invalidate its password). ``salt``
    is stable for every username — a decoy is returned for unknown accounts — so
    the response does not reveal whether an account exists.
    """
    username_key = str(username).casefold()
    entry = next((user for user in _load_users() if user["username"].casefold() == username_key), None)
    return {
        "salt": _password_client_salt(entry, username),
        "newSalt": _new_client_salt(),
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _login_credentials_configured() -> bool:
    return bool(_load_users())


def _users_raw_list(raw: str) -> list:
    """Return ``USERS`` as the list it is stored as, refusing to replace it.

    A value that is not a JSON list is a configuration error, not an empty list:
    rewriting it would silently drop whatever the operator wrote. This is the same
    rule the bootstrap account follows, and it is why the caller must let the
    ``PasswordStorageError`` reach the endpoint (500) instead of starting a fresh
    list. An absent or blank value is genuinely empty and yields an empty list.
    """
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise PasswordStorageError("The user list is not valid JSON.") from exc
    if not isinstance(data, list):
        raise PasswordStorageError("The user list is not a JSON list.")
    return data


def _parse_users(data: list) -> list[dict]:
    """Project the raw ``USERS`` list onto the accounts the service can use.

    Entries that are not objects, lack a usable username or password, or repeat an
    earlier username are dropped. Unknown keys are not carried over, so a caller
    that writes the result back must first establish that nothing would be lost
    (see ``_users_for_change``).
    """
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
        user = {
            "username": username,
            "password": password,
            "admin": bool(entry.get("admin", False)),
            "root": bool(entry.get("root", False)),
            "id": str(entry.get("id") or ""),
        }
        # Every account carries the salt its password was stretched with.
        client_salt = entry.get("client_salt")
        if isinstance(client_salt, str) and client_salt.strip():
            user["client_salt"] = client_salt.strip()
        users.append(user)
    return users


def _load_users() -> list[dict]:
    """Return the users the service can use, ignoring anything it cannot read.

    Reading never fails: a malformed ``USERS`` value yields no users so that login
    is refused rather than crashing. Changes that rewrite the list use
    ``_users_for_change`` instead, which refuses the list outright.
    """
    try:
        data = _users_raw_list(_env_store.read_env_var("USERS"))
    except PasswordStorageError:
        return []
    return _parse_users(data)


def _users_list_is_empty() -> bool:
    """Return whether ``USERS`` is genuinely empty (not merely unusable).

    A malformed value is left untouched: replacing it with the default account
    would silently destroy whatever the operator wrote.
    """
    try:
        return not _users_raw_list(_env_store.read_env_var("USERS"))
    except PasswordStorageError:
        return False


def _users_for_change() -> list[dict]:
    """Return the users to change, refusing a list the service cannot represent.

    A change rewrites ``USERS`` from the parsed accounts, so any entry that parsing
    would drop (not an object, no usable username or password, or a repeated
    username) would be lost silently. Such a list is a configuration error: the
    change is refused and the stored value stays exactly as the operator wrote it.
    """
    data = _users_raw_list(_env_store.read_env_var("USERS"))
    users = _parse_users(data)
    if len(users) != len(data):
        raise PasswordStorageError("The user list holds an entry that cannot be read.")
    return users


def _ensure_default_root_user() -> bool:
    """Create the bootstrap root account when no user is configured.

    A fresh installation has an empty ``USERS`` list, so nobody could log in.
    The account exists only to let the operator in; its password is public, so
    it must be changed at the first login. Returns whether it was created.
    """
    if not _users_list_is_empty():
        return False
    client_salt = _new_client_salt()
    entry = {
        "username": _DEFAULT_ROOT_USERNAME,
        "password": _wrap_password(_client_password_hash(_DEFAULT_ROOT_PASSWORD, client_salt)),
        "client_salt": client_salt,
        "admin": True,
        "root": True,
        "id": secrets.token_hex(16),
    }
    _env_store.write_env_var("USERS", json.dumps([entry], ensure_ascii=False))
    log_warn(
        "Default root account created",
        {"username": _DEFAULT_ROOT_USERNAME, "hint": "change its password immediately"},
    )
    return True


def _authenticate_user(username: str, password_hash: str) -> dict | None:
    username_key = username.casefold()
    for user in _load_users():
        if username_key != user["username"].casefold():
            continue
        if not _verify_stored_password(user, password_hash):
            return None
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


def _matching_stored_passwords(username: str) -> list[dict]:
    username_key = username.casefold()
    return [user for user in _load_users() if username_key == user["username"].casefold()]


def _set_user_password_env(username: str, new_password_hash: str, client_salt: str) -> bool:
    if not _is_valid_client_salt(client_salt):
        raise ValueError("Invalid client salt.")
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
            entry["client_salt"] = client_salt
            _env_store.write_env_var("USERS", json.dumps(data, ensure_ascii=False))
            return True
    return False


def _change_password(
    username: str,
    current_password_hash: str,
    new_password_hash: str,
    new_client_salt: str,
    keep_token: str | None = None,
) -> None:
    if not _is_valid_client_salt(new_client_salt):
        raise ValueError("Invalid client salt.")
    records = _matching_stored_passwords(username)
    if not records:
        raise AccountNotFoundError("Account not found.")
    if not any(_verify_stored_password(record, current_password_hash) for record in records):
        raise CurrentPasswordError("Current password is incorrect.")
    if not _set_user_password_env(username, _wrap_password(new_password_hash), client_salt=new_client_salt):
        raise AccountNotFoundError("Account not found.")
    _revoke_other_sessions(username, keep_token)
    log_info("Password changed for user", {"username": username})


def _register_user(username: str, password_hash: str, admin: bool, client_salt: str) -> None:
    username = username.strip()
    if not username:
        raise ValueError("Username must not be empty.")
    if any(ch in username for ch in state._FORBIDDEN_KEY_NAME_CHARS):
        raise ValueError("Username contains prohibited characters.")
    if len(username) < 8:
        raise ValueError("Username must be at least 8 characters long.")
    if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$"):
        raise ValueError("Invalid password hash.")
    if not _is_valid_client_salt(client_salt):
        raise ValueError("Invalid client salt.")
    username_key = username.casefold()
    for user in _load_users():
        if username_key == user["username"].casefold():
            raise UsernameTakenError("A user with that username already exists.")
    data = _users_raw_list(_env_store.read_env_var("USERS"))
    data.append({
        "username": username,
        "password": _wrap_password(password_hash),
        "client_salt": client_salt,
        "admin": bool(admin),
        "root": False,
        "id": secrets.token_hex(16),
    })
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
    users = _users_for_change()
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
    users = _users_for_change()
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
    users = _users_for_change()
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
    try:
        import audio
        audio.play_audio("acknowledge")()
    except Exception:
        pass
    return {"name": target["name"], "key": target["key"]}
