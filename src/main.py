"""Akupara local web service template."""

from __future__ import annotations

import argparse
import functools
import ipaddress
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from datetime import datetime
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
from flask import Flask, jsonify, request, send_from_directory, render_template_string
from werkzeug.exceptions import NotFound

import audio

logger = logging.getLogger(__name__)

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = None

GUI_ENABLED: bool = True

ALLOW_DISCOVERY: bool = False

API_KEYS_ENABLED: bool = False

DISPLAY_PROMOTION: bool = True

PLAY_AUDIOS: bool = True

SHARED_MEMORY_ENABLED: bool = True

_CONFIG_CACHE: dict | None = None

SESSION_COOKIE_NAME = "akupara-session"
_SESSION_TOKEN: str | None = None
_SESSION_ISSUED: bool = False
_SESSION_LOCK = threading.Lock()


def _load_configuration() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        logger.debug("Configuration loaded from cache")
        return _CONFIG_CACHE

    config_path = Path(__file__).resolve().parent.parent / "resources" / "configuration.json"
    if not config_path.exists():
        logger.warning(f"Configuration file not found at {config_path}")
        raise FileNotFoundError("Configuration file not found.")

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        logger.warning(f"Configuration file contains invalid JSON: {exc}")
        raise ValueError("Configuration file contains invalid JSON") from exc

    _CONFIG_CACHE = config
    logger.debug(f"Configuration loaded from {config_path}")
    return config


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _initialize_service_config() -> None:
    global SERVICE_PORT, GUI_ENABLED, ALLOW_DISCOVERY, API_KEYS_ENABLED, DISPLAY_PROMOTION, PLAY_AUDIOS, SHARED_MEMORY_ENABLED, _SESSION_TOKEN, _SESSION_ISSUED
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    config = _load_configuration()

    configured_port = config.get("port", 49150)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        logger.warning(f"Invalid port value in configuration ({configured_port!r}); defaulting to 49150")
        configured_port = 49150
    SERVICE_PORT = configured_port

    GUI_ENABLED = config.get("guiEnabled", True)

    ALLOW_DISCOVERY = _parse_bool(os.getenv("ALLOW_DISCOVERY"), False)

    API_KEYS_ENABLED = _parse_bool(os.getenv("API_KEYS_ENABLED"), False)

    DISPLAY_PROMOTION = _parse_bool(os.getenv("DISPLAY_PROMOTION"), True)

    PLAY_AUDIOS = _parse_bool(os.getenv("PLAY_AUDIOS"), True)
    audio.set_audio_worker_enabled(PLAY_AUDIOS)

    SHARED_MEMORY_ENABLED = _parse_bool(os.getenv("SHARED_MEMORY_ENABLED"), True)

    _SESSION_TOKEN = _get_or_create_session_token()
    _SESSION_ISSUED = False

    logger.debug(f"Resolved config values: port={SERVICE_PORT}, guiEnabled={GUI_ENABLED}, allowDiscovery={ALLOW_DISCOVERY}, apiKeysEnabled={API_KEYS_ENABLED}")
    logger.info("Service configuration initialized")


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
            logger.debug(f"getaddrinfo failed for {candidate_name}")
        try:
            local_addresses.update(socket.gethostbyname_ex(candidate_name)[2])
        except OSError:
            logger.debug(f"gethostbyname_ex failed for {candidate_name}")

    for probe_address in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as socket_handle:
                socket_handle.connect((probe_address, 80))
                local_addresses.add(socket_handle.getsockname()[0])
        except OSError:
            logger.debug(f"UDP probe to {probe_address} failed")

    normalized_addresses: set[str] = set()
    for address_value in local_addresses:
        try:
            normalized_addresses.add(ipaddress.ip_address(address_value).compressed)
        except ValueError:
            logger.debug(f"Invalid local address value ignored: {address_value}")
            continue

    normalized_addresses.update({"127.0.0.1", "::1"})
    logger.debug(f"Local device address cache populated: {len(normalized_addresses)} address(es)")
    return normalized_addresses


def _is_local_request() -> bool:
    remote_address = request.remote_addr
    if not isinstance(remote_address, str) or not remote_address.strip():
        return False
    try:
        client_ip = ipaddress.ip_address(remote_address.strip())
    except ValueError:
        logger.debug(f"Non-IP remote address rejected: {remote_address!r}")
        return False
    if client_ip.is_loopback:
        return True
    return client_ip.compressed in _get_local_device_addresses()


def _generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def _get_or_create_session_token() -> str:
    token_file = Path(__file__).resolve().parent.parent / "resources" / "session.token"
    try:
        if token_file.exists():
            stored = token_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError:
        pass
    token = _generate_session_token()
    try:
        token_file.write_text(token, encoding="utf-8")
    except OSError:
        pass
    return token


def localhost_only(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not _is_local_request():
            logger.warning(f"Blocked non-local request from {request.remote_addr} for {request.path}")
            return NotFound().get_response()
        return func(*args, **kwargs)
    return wrapper


def _is_valid_session_cookie() -> bool:
    provided_token = request.cookies.get(SESSION_COOKIE_NAME)
    return _SESSION_TOKEN is not None and provided_token == _SESSION_TOKEN


def _unauthorized_response():
    global _SESSION_ISSUED
    first = False
    with _SESSION_LOCK:
        if not _SESSION_ISSUED:
            _SESSION_ISSUED = True
            first = True
    if first:
        response = NotFound().get_response()
        response.set_cookie(SESSION_COOKIE_NAME, _SESSION_TOKEN, httponly=True, samesite="Lax", max_age=31536000)
        logger.info("Issued akupara-session cookie to establish a new runtime session")
        return response
    logger.warning(f"Rejected request from {request.remote_addr}: missing or invalid akupara-session cookie")
    return NotFound().get_response()


def cookie_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_valid_session_cookie():
            return func(*args, **kwargs)
        return _unauthorized_response()
    return wrapper


def _is_valid_api_key() -> bool:
    provided_key = request.headers.get("X-Api-Key")
    if not provided_key:
        return False
    keys = [entry["key"] for entry in _load_api_keys()]
    keys.extend(_load_configuration().get("apiKeys", []))
    return provided_key in keys


def api_key_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_valid_api_key():
            return func(*args, **kwargs)
        logger.warning(f"Rejected request from {request.remote_addr}: invalid or missing API key")
        return jsonify({"error": "API key required."}), 401
    return wrapper


def api_key_or_cookie_authenticated(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _is_valid_api_key():
            return func(*args, **kwargs)
        if _is_valid_session_cookie():
            return func(*args, **kwargs)
        return _unauthorized_response()
    return wrapper


class FeatureDisabledError(RuntimeError):
    """Raised when a feature is disabled and its functionality is unavailable."""


def _require_api_keys_enabled() -> None:
    if not API_KEYS_ENABLED:
        raise FeatureDisabledError("The API keys functionality is disabled.")


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
        logger.debug(f"Connection set to keep-alive for {request.path}")
    else:
        response.headers["Connection"] = "close"
        logger.debug(f"Connection set to close for {request.path}")
    return response


def standard_endpoint(*methods: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                logger.debug(f"OPTIONS request handled for {request.path}")
                return _options_response(list(methods))
            if request.method == "HEAD":
                logger.debug(f"HEAD request handled for {request.path}")
                return _head_response()
            return func(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/api/health", methods=["GET", "HEAD", "OPTIONS"])
@localhost_only
@standard_endpoint("GET", "HEAD", "OPTIONS")
def health() -> tuple:
    logger.info(f"Health check from {request.remote_addr}")

    return jsonify({
        "status": "ok",
        "service": "Akupara",
        "bind_address": SERVICE_HOST,
        "port": SERVICE_PORT,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }), 200


def _terminate() -> None:
    logger.info("Akupara terminating")
    os.kill(os.getpid(), signal.SIGTERM)


def _restart() -> None:
    logger.info("Akupara restarting")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    os._exit(0)


@app.route("/api/terminate", methods=["POST", "OPTIONS"])
@localhost_only
@api_key_or_cookie_authenticated
@standard_endpoint("POST", "OPTIONS")
def terminate() -> tuple:
    logger.info(f"Terminate requested by {request.remote_addr}")
    threading.Timer(0.5, _terminate).start()
    return jsonify({"status": "ok", "message": "Akupara is terminating."}), 200


@app.route("/api/restart", methods=["POST", "OPTIONS"])
@localhost_only
@api_key_or_cookie_authenticated
@standard_endpoint("POST", "OPTIONS")
def restart() -> tuple:
    logger.info(f"Restart requested by {request.remote_addr}")
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


def _set_display_promotion(value: bool) -> None:
    global DISPLAY_PROMOTION
    DISPLAY_PROMOTION = value
    _write_env_bool("DISPLAY_PROMOTION", value)


def _set_play_audios(value: bool) -> None:
    global PLAY_AUDIOS
    PLAY_AUDIOS = value
    _write_env_bool("PLAY_AUDIOS", value)
    audio.set_audio_worker_enabled(value)


@audio.play_audio("acknowledge")
def _set_shared_memory_enabled(value: bool) -> None:
    global SHARED_MEMORY_ENABLED
    SHARED_MEMORY_ENABLED = value
    _write_env_bool("SHARED_MEMORY_ENABLED", value)


@app.route("/api/settings", methods=["GET", "POST", "HEAD", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def settings() -> tuple:
    if request.method == "GET":
        logger.info(f"Settings read by {request.remote_addr}")
        return jsonify({
            "allowDiscovery": _read_env_bool("ALLOW_DISCOVERY", ALLOW_DISCOVERY),
            "apiKeysEnabled": _read_env_bool("API_KEYS_ENABLED", API_KEYS_ENABLED),
            "displayPromotion": _read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
        }), 200
    data = request.get_json(silent=True) or {}
    keys = [key for key in ("allowDiscovery", "apiKeysEnabled", "displayPromotion") if key in data]
    if not keys:
        return jsonify({"error": "No known setting provided."}), 400
    for key in keys:
        value = data[key]
        if not isinstance(value, bool):
            return jsonify({"error": f"{key} must be a boolean."}), 400
        if key == "allowDiscovery":
            _set_allow_discovery(value)
        elif key == "apiKeysEnabled":
            _set_api_keys_enabled(value)
        else:
            _set_display_promotion(value)
    logger.info(f"Settings updated by {request.remote_addr}: allowDiscovery={ALLOW_DISCOVERY}, apiKeysEnabled={API_KEYS_ENABLED}, displayPromotion={DISPLAY_PROMOTION}")
    return jsonify({
        "allowDiscovery": ALLOW_DISCOVERY,
        "apiKeysEnabled": API_KEYS_ENABLED,
        "displayPromotion": DISPLAY_PROMOTION,
    }), 200


@app.route("/api/audio", methods=["GET", "POST", "HEAD", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def audio_playback() -> tuple:
    if request.method == "GET":
        logger.info(f"Audio playback setting read by {request.remote_addr}")
        return jsonify({"playAudios": _read_env_bool("PLAY_AUDIOS", PLAY_AUDIOS)}), 200

    data = request.get_json(silent=True) or {}
    if "playAudios" not in data or not isinstance(data["playAudios"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["playAudios"]
    _set_play_audios(value)
    logger.info(f"Audio playback set by {request.remote_addr}: playAudios={PLAY_AUDIOS}")
    return jsonify({"playAudios": PLAY_AUDIOS}), 200


@app.route("/api/shared-memory-enabled", methods=["GET", "POST", "HEAD", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def shared_memory_enabled() -> tuple:
    if request.method == "GET":
        logger.info(f"Shared memory enabled setting read by {request.remote_addr}")
        return jsonify({"sharedMemoryEnabled": SHARED_MEMORY_ENABLED}), 200

    data = request.get_json(silent=True) or {}
    if "sharedMemoryEnabled" not in data or not isinstance(data["sharedMemoryEnabled"], bool):
        return jsonify({"error": "Invalid request."}), 400
    value = data["sharedMemoryEnabled"]
    _set_shared_memory_enabled(value)
    logger.info(f"Shared memory enabled set by {request.remote_addr}: sharedMemoryEnabled={SHARED_MEMORY_ENABLED}")
    return jsonify({"sharedMemoryEnabled": SHARED_MEMORY_ENABLED}), 200


_FORBIDDEN_KEY_NAME_CHARS = set(" ,;:\\/")


def _is_valid_key_name(name: str) -> bool:
    return bool(name) and not any(ch in name for ch in _FORBIDDEN_KEY_NAME_CHARS)


def _load_api_keys() -> list[dict]:
    value = _read_env_var("API_KEYS")
    keys: list[dict] = []
    for part in value.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, key = part.partition(":")
        keys.append({"name": name.strip(), "key": key.strip()})
    return keys


def _save_api_keys(keys: list[dict]) -> None:
    value = ",".join(f"{entry['name']}:{entry['key']}" for entry in keys)
    _write_env_var("API_KEYS", value)


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _list_api_keys() -> list[dict]:
    _require_api_keys_enabled()
    return _load_api_keys()


@audio.play_audio("acknowledge")
def _create_api_key(name: str) -> dict:
    _require_api_keys_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid API key name.")
    key = _generate_api_key()
    keys = _load_api_keys()
    entry = {"name": name, "key": key}
    keys.append(entry)
    _save_api_keys(keys)
    return entry


@audio.play_audio("acknowledge")
def _delete_api_key(key: str) -> bool:
    _require_api_keys_enabled()
    keys = _load_api_keys()
    remaining = [entry for entry in keys if entry["key"] != key]
    if len(remaining) == len(keys):
        return False
    _save_api_keys(remaining)
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
    target["name"] = name
    _save_api_keys(keys)
    return target


@app.route("/api/api-keys", methods=["GET", "POST", "HEAD", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def api_keys() -> tuple:
    if request.method == "GET":
        try:
            keys = _list_api_keys()
        except FeatureDisabledError:
            return jsonify({"error": "Forbidden"}), 403
        logger.info(f"API keys read by {request.remote_addr}")
        return jsonify({"apiKeys": keys}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    try:
        entry = _create_api_key(name)
    except FeatureDisabledError:
        return jsonify({"error": "Forbidden"}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    logger.info(f"API key generated by {request.remote_addr}: name={name!r}")
    return jsonify(entry), 201


@app.route("/api/api-keys/<path:key>", methods=["PATCH", "DELETE", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("PATCH", "DELETE", "OPTIONS")
def api_key_item(key: str) -> tuple:
    if request.method == "DELETE":
        try:
            deleted = _delete_api_key(key)
        except FeatureDisabledError:
            return jsonify({"error": "Forbidden"}), 403
        if not deleted:
            return jsonify({"error": "Not found."}), 404
        logger.info(f"API key deleted by {request.remote_addr}")
        return jsonify({"status": "ok"}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    try:
        target = _rename_api_key(key, name)
    except FeatureDisabledError:
        return jsonify({"error": "Forbidden"}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if target is None:
        return jsonify({"error": "Not found."}), 404
    logger.info(f"API key renamed by {request.remote_addr}: name={name!r}")
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


@audio.play_audio("acknowledge")
def _create_shared_variable(name: str, value, value_type: str) -> dict:
    _require_shared_memory_enabled()
    if not isinstance(name, str) or not _is_valid_key_name(name):
        raise ValueError("Invalid shared variable name.")
    value = _normalize_shared_value(value, value_type)
    variables = _load_shared_memory()
    if any(entry["name"].lower() == name.lower() for entry in variables):
        raise ValueError("A shared variable with this name already exists.")
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


@audio.play_audio("acknowledge")
def _delete_shared_variable(name: str) -> bool:
    _require_shared_memory_enabled()
    variables = _load_shared_memory()
    remaining = [entry for entry in variables if entry["name"] != name]
    if len(remaining) == len(variables):
        return False
    _save_shared_memory(remaining)
    return True


@app.route("/api/shared-memory", methods=["GET", "POST", "HEAD", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
def shared_memory() -> tuple:
    if request.method == "GET":
        try:
            variables = _list_shared_memory()
        except FeatureDisabledError:
            return jsonify({"error": "Forbidden"}), 403
        logger.info(f"Shared memory read by {request.remote_addr}")
        return jsonify({"sharedMemory": variables}), 200

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    value = data.get("value")
    value_type = data.get("type")
    try:
        entry = _create_shared_variable(name, value, value_type)
    except FeatureDisabledError:
        return jsonify({"error": "Forbidden"}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    logger.info(f"Shared variable created by {request.remote_addr}: name={name!r} type={value_type!r}")
    return jsonify(entry), 201


@app.route("/api/shared-memory/<path:name>", methods=["DELETE", "OPTIONS"])
@localhost_only
@cookie_authenticated
@standard_endpoint("DELETE", "OPTIONS")
def shared_memory_delete(name: str) -> tuple:
    try:
        deleted = _delete_shared_variable(name)
    except FeatureDisabledError:
        return jsonify({"error": "Forbidden"}), 403
    if not deleted:
        return jsonify({"error": "Not found."}), 404
    logger.info(f"Shared variable deleted by {request.remote_addr}: name={name!r}")
    return jsonify({"status": "ok"}), 200


@app.route("/api/shared-memory/<path:name>", methods=["PATCH", "OPTIONS"])
@localhost_only
@cookie_authenticated
@api_key_authenticated
@standard_endpoint("PATCH", "OPTIONS")
def shared_memory_edit(name: str) -> tuple:
    data = request.get_json(silent=True) or {}
    value = data.get("value")
    value_type = data.get("type")
    try:
        target = _update_shared_variable(name, value, value_type)
    except FeatureDisabledError:
        return jsonify({"error": "Forbidden"}), 403
    except ValueError:
        return jsonify({"error": "Invalid request."}), 400
    if target is None:
        return jsonify({"error": "Not found."}), 404
    logger.info(f"Shared variable updated by {request.remote_addr}: name={name!r}")
    return jsonify(target), 200


@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def index():
    logger.info(f"Serving UI to {request.remote_addr}")
    web_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (web_dir / "index.html").read_text(encoding="utf-8")
    return render_template_string(
        template,
        display_promotion=_read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
    )


@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_css(filename: str):
    css_dir = Path(__file__).resolve().parent.parent / "ui" / "css"
    return send_from_directory(css_dir, filename)


@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_icon(filename: str):
    icons_dir = Path(__file__).resolve().parent.parent / "resources" / "icons"
    return send_from_directory(icons_dir, filename)


@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_page(filename: str):
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    return send_from_directory(pages_dir, filename)


@localhost_only
@cookie_authenticated
@standard_endpoint("GET", "HEAD", "OPTIONS")
def ui_settings_page():
    pages_dir = Path(__file__).resolve().parent.parent / "ui" / "pages"
    template = (pages_dir / "settings.html").read_text(encoding="utf-8")
    api_keys = sorted(_load_api_keys(), key=lambda k: (k.get("name") or "").lower())
    shared_memory = _load_shared_memory()
    return render_template_string(
        template,
        allow_discovery=_read_env_bool("ALLOW_DISCOVERY", ALLOW_DISCOVERY),
        api_keys_enabled=_read_env_bool("API_KEYS_ENABLED", API_KEYS_ENABLED),
        display_promotion=_read_env_bool("DISPLAY_PROMOTION", DISPLAY_PROMOTION),
        play_audios=_read_env_bool("PLAY_AUDIOS", PLAY_AUDIOS),
        shared_memory_enabled=_read_env_bool("SHARED_MEMORY_ENABLED", SHARED_MEMORY_ENABLED),
        has_api_keys=bool(api_keys),
        api_keys_json=json.dumps(api_keys),
        has_shared_memory=bool(shared_memory),
        shared_memory_json=json.dumps(shared_memory),
    )


def _register_ui_routes(app_instance: Flask) -> None:
    if not GUI_ENABLED:
        return
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
    parser.add_argument("--verbose", action="store_true", help="Enable verbose/debug logging")
    args, _ = parser.parse_known_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%d-%m-%Y_%H.%M.%S')}.log"
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logger.info(f"Log file: {log_file}")

    try:
        _initialize_service_config()
        _register_ui_routes(app)
    except Exception as exc:
        logger.error(f"Failed to load configuration: {exc}", exc_info=True)
        exit(1)

    try:
        logger.info("=" * 50)
        logger.info("  Akupara")
        logger.info("=" * 50)
        logger.info(f"Binding to: http://{SERVICE_HOST}:{SERVICE_PORT}")
        logger.info(f"Mode: private (local only)")
        if GUI_ENABLED:
            logger.info("GUI: enabled")
        else:
            logger.info("GUI: disabled")
        logger.info(f"Config: port={SERVICE_PORT}, guiEnabled={GUI_ENABLED}, allowDiscovery={ALLOW_DISCOVERY}, apiKeysEnabled={API_KEYS_ENABLED}")
        logger.info("Server starting...")

        app.run(host=SERVICE_HOST, port=SERVICE_PORT, debug=False, threaded=True)

    except OSError as exc:
        if "Address already in use" in str(exc):
            logger.error(f"Port {SERVICE_PORT} is already in use. Change the port in resources/configuration.json")
        elif "Permission denied" in str(exc):
            logger.error(f"Permission denied to bind to port {SERVICE_PORT}. Use a port >= 1024 or run with elevated privileges.")
        else:
            logger.error(f"Network binding failed: {exc}")

    except KeyboardInterrupt:
        logger.info("=" * 50)
        logger.info("  Server Stopped")
        logger.info("=" * 50)

    except Exception as exc:
        logger.error(f"Server startup failed: {exc}")
