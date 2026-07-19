"""ServiceHandler - Web Service Registry."""

from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from flask import Flask, jsonify, request, send_from_directory

from models import GetRequest, GetResponse, PostRequest, PostResponse

logger = logging.getLogger(__name__)


def _kill_pid(pid: int) -> None:
    is_win = sys.platform.startswith("win")
    cmd = ["taskkill", "/F", "/PID", str(pid)] if is_win else ["kill", "-9", str(pid)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _resolve_pid(ip: str, port: int) -> int | None:
    is_win = sys.platform.startswith("win")
    port_str = str(port)
    try:
        if is_win:
            proc = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10
            )
            for line in proc.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and parts[3] == "LISTENING":
                    local_addr = parts[1]
                    if local_addr.endswith(f":{port_str}") and (
                        local_addr.startswith("127.")
                        or local_addr.startswith("[::1]")
                        or local_addr.startswith("0.0.0.0:")
                        or local_addr.startswith("[::]:")
                    ):
                        try:
                            return int(parts[4])
                        except (ValueError, IndexError):
                            pass
        else:
            try:
                proc = subprocess.run(
                    ["ss", "-tlnp"], capture_output=True, text=True, timeout=10
                )
                for line in proc.stdout.splitlines():
                    if f":{port_str}" in line:
                        import re
                        match = re.search(r"pid=(\d+)", line)
                        if match:
                            return int(match.group(1))
            except FileNotFoundError:
                proc = subprocess.run(
                    ["netstat", "-tlnp"], capture_output=True, text=True, timeout=10
                )
                for line in proc.stdout.splitlines():
                    if f":{port_str}" in line and "LISTEN" in line:
                        import re
                        match = re.search(r"(\d+)/", line.strip().split()[-1])
                        if match:
                            return int(match.group(1))
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(f"Failed to resolve PID for {ip}:{port}: {exc}")
    return None


SERVICE_HOST = None
SERVICE_PORT = None

REGISTERED_CLIENTS: dict[str, dict] = {}
REGISTERED_CLIENTS_LOCK = threading.Lock()

PENDING_API_KEY_REQUESTS: dict[str, dict] = {}
PENDING_API_KEY_REQUESTS_LOCK = threading.Lock()

API_KEYS_DATA: dict = {"keys": {}}
API_KEYS_LOCK = threading.Lock()
API_KEY_LOOKUP: list[str] = []

ENDPOINT_SEARCH_INDEX: dict[str, set[str]] = {}
ENDPOINT_BY_ID: dict[str, dict] = {}
ENDPOINT_INDEX_LOCK = threading.Lock()

SERVICE_NAME_INDEX: dict[str, str] = {}
SERVICE_NAME_INDEX_LOCK = threading.Lock()


def _tokenize(text: str) -> set[str]:
    text = text.lower()
    return {word for word in re.split(r"[^a-z0-9]+", text) if len(word) >= 2}


def _damerau_levenshtein(a: str, b: str) -> int:
    a_len, b_len = len(a), len(b)
    d = [[0] * (b_len + 1) for _ in range(a_len + 1)]
    for i in range(a_len + 1):
        d[i][0] = i
    for j in range(b_len + 1):
        d[0][j] = j
    for i in range(1, a_len + 1):
        for j in range(1, b_len + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + cost)
    return d[a_len][b_len]


def _add_to_endpoint_index(service_name: str, ep: dict) -> None:
    ep_id = f"{service_name}:{ep.get('verb', '')}:{ep.get('path', '')}"
    result_entry = {
        "service": service_name,
        "verb": ep.get("verb", ""),
        "path": ep.get("path", ""),
        "description": ep.get("description", ""),
        "path_variables": ep.get("path_variables", []),
        "body_schema": ep.get("body_schema", {}),
    }
    with ENDPOINT_INDEX_LOCK:
        ENDPOINT_BY_ID[ep_id] = result_entry
        for token in _tokenize(ep.get("description", "") + " " + ep.get("path", "")):
            ENDPOINT_SEARCH_INDEX.setdefault(token, set()).add(ep_id)


def _add_to_service_name_index(name: str, client_hash: str) -> None:
    with SERVICE_NAME_INDEX_LOCK:
        SERVICE_NAME_INDEX[name.lower()] = client_hash


def _remove_from_service_name_index(name: str) -> None:
    with SERVICE_NAME_INDEX_LOCK:
        SERVICE_NAME_INDEX.pop(name.lower(), None)


def _rebuild_api_key_lookup() -> None:
    API_KEY_LOOKUP.clear()
    for data in API_KEYS_DATA.get("keys", {}).values():
        if isinstance(data, dict) and "api_key" in data:
            API_KEY_LOOKUP.append(data["api_key"])

API_KEY_SESSION_READY: bool = False

HEALTH_CHECK_INTERVAL_SECONDS = 15

NO_GUI: bool = False

_PROJECT_ROOT = Path(__file__).parent.parent
ENV_PATH = _PROJECT_ROOT / ".env"


# ============================================================================
# ENVIRONMENT FILE HELPERS
# ============================================================================


def _parse_env_file() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    env_dict: dict[str, str] = {}
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key:
                    env_dict[key] = value
    except OSError:
        return {}
    return env_dict


def _write_env_file(env_dict: dict[str, str]) -> None:
    lines: list[str] = []
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            lines = []

    updated_keys: set[str] = set()
    output_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(line)
            continue
        key, _, _ = stripped.partition("=")
        key = key.strip()
        if key in env_dict:
            output_lines.append(f"{key}={env_dict[key]}\n")
            updated_keys.add(key)
        else:
            output_lines.append(line)

    for key, value in env_dict.items():
        if key not in updated_keys:
            output_lines.append(f"{key}={value}\n")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(output_lines)


def _set_env_var(key: str, value: str) -> None:
    os.environ[key] = value
    env_dict = _parse_env_file()
    env_dict[key] = value
    _write_env_file(env_dict)


def _load_env_file() -> None:
    env_dict = _parse_env_file()
    for key, value in env_dict.items():
        os.environ[key] = value


_load_env_file()


class _SimpleCache:
    def __init__(self, default_ttl: float = 30.0):
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> object | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: object, ttl: float | None = None) -> None:
        expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            self._data[key] = (expires_at, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_CONFIG_CACHE = _SimpleCache(default_ttl=300.0)
_HEALTH_CACHE = _SimpleCache(default_ttl=7.5)


def _load_configuration() -> dict:
    cached = _CONFIG_CACHE.get("config")
    if cached is not None:
        return cached
    config_path = _PROJECT_ROOT / "resources" / "configuration.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found at {config_path}."
        )

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Configuration file at {config_path} contains invalid JSON: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read configuration file at {config_path}: {exc}"
        ) from exc

    _CONFIG_CACHE.set("config", config)
    return config



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
            pass

        try:
            local_addresses.update(socket.gethostbyname_ex(candidate_name)[2])
        except OSError:
            pass

    for probe_address in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as socket_handle:
                socket_handle.connect((probe_address, 80))
                local_addresses.add(socket_handle.getsockname()[0])
        except OSError:
            pass

    normalized_addresses: set[str] = set()
    for address_value in local_addresses:
        try:
            normalized_addresses.add(ipaddress.ip_address(address_value).compressed)
        except ValueError:
            continue

    normalized_addresses.update({"127.0.0.1", "::1"})
    return normalized_addresses


def _is_local_request() -> bool:
    remote_address = request.remote_addr
    if not isinstance(remote_address, str) or not remote_address.strip():
        return False

    try:
        client_ip = ipaddress.ip_address(remote_address.strip())
    except ValueError:
        return False

    if client_ip.is_loopback:
        return True

    return client_ip.compressed in _get_local_device_addresses()


def _is_localhost_request() -> bool:
    return request.remote_addr == "127.0.0.1" or request.remote_addr == "::1"


def _is_authorized_strict(payload: dict) -> bool:
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    if isinstance(api_key, str) and api_key.strip():
        with API_KEYS_LOCK:
            return api_key.strip() in API_KEY_LOOKUP
    return False


def _is_authorized(payload: dict) -> bool:
    return _is_authorized_strict(payload) or _is_localhost_request()


def _is_self_request(payload: dict) -> bool:
    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    if not isinstance(client_hash, str) or not client_hash.strip():
        return False
    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(client_hash.strip())
    return client_data is not None


def _check_authorization(payload):
    """Returns (is_authorized, has_invalid_key) tuple.
    is_authorized: True if valid key provided or request is from localhost.
    has_invalid_key: True if an api_key was provided but didn't match.
    """
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    if isinstance(api_key, str) and api_key.strip():
        with API_KEYS_LOCK:
            if api_key.strip() in API_KEY_LOOKUP:
                return (True, False)
        return (False, True)
    return (_is_localhost_request(), False)


def _is_protected(client_hash: str) -> bool:
    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(client_hash)
    return client_data is not None and client_data.get("protected", False) is True


def _check_authorization_all(payload):
    """Returns (is_allowed, has_invalid_key) tuple.
    is_allowed: True if valid API key, localhost, or self-service (hash exists).
    has_invalid_key: True if an api_key was provided but didn't match.
    """
    allowed, invalid_key = _check_authorization(payload)
    if allowed or invalid_key:
        return allowed, invalid_key
    return (_is_self_request(payload), False)


def _initialize_service_config() -> None:
    global SERVICE_HOST, SERVICE_PORT
    global NO_GUI
    config = _load_configuration()

    SERVICE_HOST = "127.0.0.1"

    configured_port = config.get("port", 49155)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        configured_port = 49155

    SERVICE_PORT = configured_port

    env_no_gui = os.getenv("SH_NO_GUI")
    if env_no_gui is not None:
        NO_GUI = env_no_gui.strip().lower() in ("true", "1", "yes")
    else:
        NO_GUI = config.get("noGUI", False)


def _extract_pid(client_data: dict) -> int | None:
    stored = client_data.get("pid")
    if isinstance(stored, int):
        return stored
    if isinstance(stored, str) and stored.strip().isdigit():
        return int(stored)
    return None


def _launch_script(script_path: str) -> subprocess.Popen:
    path = script_path.strip()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        cmd = [sys.executable, path]
    elif ext in (".bat", ".cmd"):
        cmd = ["cmd", "/c", path]
    elif ext == ".ps1":
        cmd = ["powershell", "-File", path]
    else:
        cmd = [path]
    return subprocess.Popen(
        cmd,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def _resolve_service(name: str, default_host: str, default_port: int) -> tuple[str, int]:
    with REGISTERED_CLIENTS_LOCK:
        for client in REGISTERED_CLIENTS.values():
            if client.get("name") == name:
                ip = client.get("ip", default_host)
                port = client.get("port", default_port)
                if isinstance(port, int) and 1 <= port <= 65535:
                    return ip, port
    return default_host, default_port


def _send_get_request(request: GetRequest) -> GetResponse:
    try:
        req = urllib.request.Request(request.url, method="GET", headers=request.headers)
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            body = resp.read().decode("utf-8")
            body_size = len(body)
            headers = dict(resp.headers)
            json_body = None
            try:
                json_body = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                pass
            return GetResponse(
                status_code=resp.status,
                reason=resp.reason,
                body=body,
                body_size=body_size,
                headers=headers,
                json_body=json_body,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        body_size = len(body)
        headers = dict(exc.headers)
        json_body = None
        try:
            json_body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            pass
        return GetResponse(
            status_code=exc.code,
            reason=str(exc.reason),
            body=body,
            body_size=body_size,
            headers=headers,
            json_body=json_body,
        )


def _send_post_request(request: PostRequest) -> PostResponse:
    try:
        req = urllib.request.Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            body = resp.read().decode("utf-8")
            body_size = len(body)
            headers = dict(resp.headers)
            json_body = None
            try:
                json_body = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                pass
            return PostResponse(
                status_code=resp.status,
                reason=resp.reason,
                body=body,
                body_size=body_size,
                headers=headers,
                json_body=json_body,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        body_size = len(body)
        headers = dict(exc.headers)
        json_body = None
        try:
            json_body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            pass
        return PostResponse(
            status_code=exc.code,
            reason=str(exc.reason),
            body=body,
            body_size=body_size,
            headers=headers,
            json_body=json_body,
        )


def _ping_health(ip: str, port: int, timeout: float = 5.0) -> bool:
    cache_key = f"health:{ip}:{port}"
    cached = _HEALTH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not addr.is_loopback:
        return False
    if not isinstance(port, int) or port < 1 or port > 65535:
        return False
    url = f"http://{ip}:{port}/api/health"
    try:
        resp = _send_get_request(GetRequest(url=url, timeout=timeout))
        result = resp.status_code == 200
        _HEALTH_CACHE.set(cache_key, result)
        return result
    except (urllib.error.URLError, OSError, ValueError):
        _HEALTH_CACHE.set(cache_key, False)
        return False


def _run_health_check() -> list[dict]:
    with REGISTERED_CLIENTS_LOCK:
        current_clients = dict(REGISTERED_CLIENTS)

    unhealthy = []
    for client_hash, client_data in current_clients.items():
        ip = client_data.get("ip", "127.0.0.1")
        port = client_data.get("port", 0)
        if not _ping_health(ip, port):
            unhealthy.append(client_data)
            logger.info(
                f"Client '{client_data.get('name', 'unknown')}' "
                f"({client_hash[:8]}...) unhealthy (health check failed)"
            )

    return unhealthy


def _health_check_worker() -> None:
    while True:
        try:
            _run_health_check()
        except Exception as exc:
            logger.error(f"Health check error: {exc}")

        time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)


def _start_health_check_loop() -> None:
    worker = threading.Thread(
        target=_health_check_worker,
        name="health-check",
        daemon=True,
    )
    worker.start()


app = Flask(__name__)


@app.before_request
def restrict_to_local_device() -> tuple | None:
    if request.path.startswith("/api/") or (not NO_GUI and (request.path in ("/",) or request.path.startswith(("/ui/", "/css/")))):
        if not _is_local_request():
            return _error_response("Local device access only.", 403)

    return None


def _error_response(message: str, status_code: int = 400) -> tuple:
    data = {"error": message}
    body = json.dumps(data)
    resp = PostResponse(
        status_code=status_code,
        reason="error",
        body=body,
        body_size=len(body),
        headers={"Content-Type": "application/json"},
        json_body=data,
    )
    return jsonify(resp.json_body), resp.status_code


def _success_response(data: dict, status_code: int = 200) -> tuple:
    body = json.dumps(data)
    resp = PostResponse(
        status_code=status_code,
        reason="OK",
        body=body,
        body_size=len(body),
        headers={"Content-Type": "application/json"},
        json_body=data,
    )
    return jsonify(resp.json_body), resp.status_code


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
    response.headers["Connection"] = "close"
    return response


def index():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()
    web_dir = _PROJECT_ROOT / "ui" / "pages"
    return send_from_directory(web_dir, "index.html")


def css_files(filename):
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()
    css_dir = _PROJECT_ROOT / "ui" / "css"
    return send_from_directory(css_dir, filename)


@app.route("/api/register/service", methods=["POST", "HEAD", "OPTIONS"])
def register():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    name = payload.get("name") if isinstance(payload, dict) else None
    port = payload.get("port") if isinstance(payload, dict) else None
    starting_script = payload.get("starting_script") if isinstance(payload, dict) else None
    bind_address = payload.get("bind_address") if isinstance(payload, dict) else None
    hostname_val = payload.get("hostname") if isinstance(payload, dict) else None

    if not isinstance(name, str) or not name.strip():
        return _error_response("A non-empty name is required.")

    if port is None:
        return _error_response("A port number is required.")

    if isinstance(port, str) and port.isdigit():
        port = int(port)

    if not isinstance(port, int) or port < 1 or port > 65535:
        return _error_response("Port must be a number between 1 and 65535.")

    if starting_script is not None and not isinstance(starting_script, str):
        return _error_response("Starting script must be a string.")

    if not isinstance(bind_address, str) or not bind_address.strip():
        return _error_response("A bind address is required.")
    if not isinstance(hostname_val, str) or not hostname_val.strip():
        return _error_response("A hostname is required.")

    client_ip = request.remote_addr or "127.0.0.1"
    name_to_check = name.strip()
    hostname_to_check = hostname_val.strip()

    with REGISTERED_CLIENTS_LOCK:
        for existing in REGISTERED_CLIENTS.values():
            existing_name = existing.get("name")
            existing_ip = existing.get("ip")
            existing_port = existing.get("port")
            existing_hostname = existing.get("hostname")

            if (
                existing_name == name_to_check
                and existing_ip == client_ip
                and existing_port == port
            ):
                return _error_response(
                    f"A service with name '{name_to_check}', IP '{client_ip}' "
                    f"and port '{port}' is already registered.", 409
                )

            if existing_ip == client_ip and existing_hostname != hostname_to_check:
                return _error_response(
                    f"IP '{client_ip}' is already associated with hostname "
                    f"'{existing_hostname}', cannot register with hostname "
                    f"'{hostname_to_check}'.", 409
                )

    if not _ping_health(client_ip, port):
        return _error_response("Client health endpoint is not reachable.", 400)

    resolved_pid = _resolve_pid(client_ip, port)
    if resolved_pid is None:
        return _error_response("Could not determine the PID of the process.", 400)

    with REGISTERED_CLIENTS_LOCK:
        existing_client = None
        for existing in list(REGISTERED_CLIENTS.values()):
            if existing.get("name") == name_to_check:
                existing_client = existing
                break

    if existing_client is not None:
        old_ip = existing_client.get("ip", "127.0.0.1")
        old_port = existing_client.get("port", 0)
        if _ping_health(old_ip, old_port):
            return _error_response(
                f"A client with name '{name}' is already registered.", 409
            )
        _remove_from_service_name_index(name_to_check)
        with REGISTERED_CLIENTS_LOCK:
            REGISTERED_CLIENTS.pop(existing_client["hash"], None)
        logger.info(
            f"Removed stale registration for '{name_to_check}' "
            f"({existing_client['hash'][:8]}...)"
        )

    timestamp = datetime.now(timezone.utc).isoformat()

    raw = f"{name.strip()}:{port}:{timestamp}"
    client_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    client_data = {
        "hash": client_hash,
        "name": name.strip(),
        "port": port,
        "starting_script": starting_script.strip() if isinstance(starting_script, str) else "",
        "pid": resolved_pid,
        "bind_address": bind_address.strip() if isinstance(bind_address, str) else "",
        "hostname": hostname_val.strip() if isinstance(hostname_val, str) else "",
        "ip": client_ip,
        "timestamp": timestamp,
        "endpoints": [],
        "protected": False,
    }

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS[client_hash] = client_data

    _add_to_service_name_index(name.strip(), client_hash)

    logger.info(f"Client '{name}' registered on port {port} ({client_hash[:16]}...)")

    return _success_response({"hash": client_hash}, 201)


@app.route("/api/question/service", defaults={"name": None}, methods=["POST", "HEAD", "OPTIONS"])
@app.route("/api/question/service/<name>", methods=["POST", "HEAD", "OPTIONS"])
def question(name=None):
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    target_name = name if isinstance(name, str) and name.strip() else (payload.get("name") if isinstance(payload, dict) else None)

    if not isinstance(target_name, str) or not target_name.strip():
        return _error_response("The name of the target client is required.")

    authorized, invalid_key = _check_authorization(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)

    target_name_stripped = target_name.strip()
    with SERVICE_NAME_INDEX_LOCK:
        client_hash = SERVICE_NAME_INDEX.get(target_name_stripped.lower())

    if client_hash is None:
        return _error_response(f"No client found with name '{target_name_stripped}'.", 404)

    with REGISTERED_CLIENTS_LOCK:
        target = REGISTERED_CLIENTS.get(client_hash)

    if target is None:
        return _error_response(f"No client found with name '{target_name}'.", 404)

    if authorized:
        return _success_response(target)

    return _success_response({"name": target["name"], "port": target["port"]})


@app.route("/api/unregister/service", methods=["DELETE", "HEAD", "OPTIONS"])
def unregister():
    if request.method == "OPTIONS":
        return _options_response(["DELETE", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization_all(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required to unregister.")

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.pop(client_hash.strip(), None)

    if client_data is not None:
        _remove_from_service_name_index(client_data.get("name", ""))

    if client_data is None:
        return _error_response("Hash not found.", 404)

    logger.info(
        f"Client '{client_data.get('name')}' unregistered ({client_hash[:16]}...)"
    )

    return _success_response({"status": "unregistered", "hash": client_hash.strip()})


@app.route("/api/health", methods=["GET", "HEAD", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    with REGISTERED_CLIENTS_LOCK:
        client_count = len(REGISTERED_CLIENTS)

    return _success_response(
        {
            "status": "ok",
            "service": "ServiceHandler",
            "bind_address": SERVICE_HOST,
            "port": SERVICE_PORT,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "registered_clients": client_count,
        }
    )


@app.route("/api/services/healthcheck", methods=["POST", "HEAD", "OPTIONS"])
def health_check():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization_all(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if client_hash:
        with REGISTERED_CLIENTS_LOCK:
            client = REGISTERED_CLIENTS.get(client_hash)
        if not client:
            return _error_response("Client not found.", 404)
        ip = client.get("ip", "127.0.0.1")
        port = client.get("port", 0)
        healthy = _ping_health(ip, port)
        if not healthy:
            logger.info(f"Client '{client.get('name', 'unknown')}' ({client_hash[:8]}...) unhealthy (health check failed)")
        return _success_response({"hash": client_hash, "healthy": healthy})

    unhealthy = _run_health_check()
    return _success_response({"checked": True, "unhealthy": unhealthy})


@app.route("/api/services", methods=["GET", "HEAD", "OPTIONS"])
def clients():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    authorized, invalid_key = _check_authorization(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)

    with REGISTERED_CLIENTS_LOCK:
        client_list = [
            {k: v for k, v in client.items() if k != "endpoints"}
            if authorized
            else {k: v for k, v in client.items() if k not in ("endpoints", "hash")}
            for client in REGISTERED_CLIENTS.values()
        ]

    return _success_response({"clients": client_list})


@app.route("/api/register/endpoint", methods=["POST", "HEAD", "OPTIONS"])
def register_endpoint():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)

    if client_data is None:
        return _error_response("Service not found.", 404)

    verb = payload.get("verb") if isinstance(payload, dict) else None
    path = payload.get("path") if isinstance(payload, dict) else None
    path_variables = payload.get("path_variables") if isinstance(payload, dict) else None
    body_schema = payload.get("body_schema") if isinstance(payload, dict) else None
    description = payload.get("description") if isinstance(payload, dict) else None

    if not isinstance(verb, str) or not verb.strip():
        return _error_response("A non-empty HTTP verb is required.")
    if not isinstance(path, str) or not path.strip():
        return _error_response("A non-empty endpoint path is required.")
    if path_variables is not None and not isinstance(path_variables, list):
        return _error_response("path_variables must be a list.")
    if body_schema is not None and not isinstance(body_schema, dict):
        return _error_response("body_schema must be a JSON schema object.")
    if not isinstance(description, str) or not description.strip():
        return _error_response("A non-empty description is required.")

    endpoint = {
        "verb": verb.strip().upper(),
        "path": path.strip(),
        "path_variables": path_variables if isinstance(path_variables, list) else [],
        "body_schema": body_schema if isinstance(body_schema, dict) else {},
        "description": description.strip(),
    }

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS[hash_val].setdefault("endpoints", []).append(endpoint)

    _add_to_endpoint_index(client_data.get("name", ""), endpoint)

    logger.info(
        f"Endpoint '{verb} {path}' registered for '{client_data.get('name', 'unknown')}' "
        f"({hash_val[:8]}...)"
    )

    return _success_response({"status": "registered", "endpoint": endpoint}, 201)


@app.route("/api/service/endpoints", defaults={"name": None}, methods=["POST", "HEAD", "OPTIONS"])
@app.route("/api/service/endpoints/<name>", methods=["POST", "HEAD", "OPTIONS"])
@app.route("/api/endpoints/service", defaults={"name": None}, methods=["POST", "HEAD", "OPTIONS"])
@app.route("/api/endpoints/service/<name>", methods=["POST", "HEAD", "OPTIONS"])
def get_endpoints(name=None):
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    target_name = name if isinstance(name, str) and name.strip() else (payload.get("name") if isinstance(payload, dict) else None)

    if not isinstance(target_name, str) or not target_name.strip():
        return _error_response("The name of the service is required.")

    target_name_stripped = target_name.strip()
    with SERVICE_NAME_INDEX_LOCK:
        client_hash = SERVICE_NAME_INDEX.get(target_name_stripped.lower())

    if client_hash is None:
        return _error_response(f"No service found with name '{target_name}'.", 404)

    with REGISTERED_CLIENTS_LOCK:
        target = REGISTERED_CLIENTS.get(client_hash)

    if target is None:
        return _error_response(f"No service found with name '{target_name}'.", 404)

    endpoints = target.get("endpoints", [])
    return _success_response({"name": target_name_stripped, "endpoints": endpoints})


@app.route("/api/services/endpoints", methods=["GET", "HEAD", "OPTIONS"])
def clients_details():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    with REGISTERED_CLIENTS_LOCK:
        client_list = []
        for client in REGISTERED_CLIENTS.values():
            client_list.append({
                "name": client.get("name", ""),
                "ip": client.get("ip", ""),
                "port": client.get("port", 0),
                "endpoints": client.get("endpoints", []),
            })

    return _success_response({"clients": client_list})


@app.route("/api/services/search-endpoints", methods=["POST", "HEAD", "OPTIONS"])
def search_endpoints():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    query = payload.get("query") if isinstance(payload, dict) else None

    if not isinstance(query, str) or not query.strip():
        return _error_response("A non-empty query is required.")

    query_lower = query.strip().lower()
    query_tokens = [t for t in re.split(r"[^a-z0-9]+", query_lower) if len(t) >= 2]

    results = []
    with ENDPOINT_INDEX_LOCK:
        for ep_id, entry in ENDPOINT_BY_ID.items():
            search_text = (entry.get("description", "") + " " + entry.get("path", "")).lower()
            if not search_text.strip():
                continue
            if query_lower in search_text:
                results.append(entry)
                continue
            if not query_tokens:
                continue
            all_match = True
            for qt in query_tokens:
                if qt in search_text:
                    continue
                words = [w for w in re.split(r"[^a-z0-9]+", search_text) if len(w) >= 2]
                found = False
                for word in words:
                    dist = _damerau_levenshtein(qt, word)
                    if dist <= 2 or dist <= len(qt) * 0.3:
                        found = True
                        break
                if not found:
                    all_match = False
                    break
            if all_match:
                results.append(entry)

    return _success_response({"query": query.strip(), "results": results})


@app.route("/api/validate/json-body", methods=["POST", "HEAD", "OPTIONS"])
def validate_json_body():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    service_name = payload.get("service") if isinstance(payload, dict) else None
    verb = payload.get("verb") if isinstance(payload, dict) else None
    path = payload.get("path") if isinstance(payload, dict) else None
    json_body = payload.get("json_body") if isinstance(payload, dict) else None

    if not isinstance(service_name, str) or not service_name.strip():
        return _error_response("A non-empty service name is required.")
    if not isinstance(verb, str) or not verb.strip():
        return _error_response("A non-empty HTTP verb is required.")
    if not isinstance(path, str) or not path.strip():
        return _error_response("A non-empty endpoint path is required.")
    if json_body is None:
        return _error_response("A json_body is required.")

    verb_stripped = verb.strip().upper()
    path_stripped = path.strip()
    service_name_stripped = service_name.strip()
    ep_id = f"{service_name_stripped}:{verb_stripped}:{path_stripped}"

    with ENDPOINT_INDEX_LOCK:
        target_endpoint = ENDPOINT_BY_ID.get(ep_id)

    if target_endpoint is None:
        return _error_response(
            f"No endpoint found with verb '{verb_stripped}' and path "
            f"'{path_stripped}' for service '{service_name_stripped}'.",
            404,
        )

    schema = target_endpoint.get("body_schema", {})
    if not schema:
        return _success_response({
            "valid": True,
            "schema_exists": False,
            "message": "No JSON schema defined for this endpoint.",
        })

    try:
        jsonschema.validate(instance=json_body, schema=schema)
        return _success_response({
            "valid": True,
            "schema_exists": True,
            "message": "JSON body is valid against the endpoint schema.",
        })
    except jsonschema.ValidationError as exc:
        return _success_response({
            "valid": False,
            "schema_exists": True,
            "message": "JSON body is not valid against the endpoint schema.",
            "errors": [{"path": list(exc.absolute_path), "message": exc.message}],
        })


def _get_env_json_list(key: str, default: list) -> list:
    val = os.getenv(key)
    if not val:
        return default
    try:
        parsed = json.loads(val)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return default


def sort_order():
    if request.method == "OPTIONS":
        return _options_response(["GET", "PUT", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    if request.method == "GET":
        sort_order_val = _get_env_json_list("SH_SORT_ORDER", ["name", "port", "pid", "bind_address", "hostname", "status", "protected"])
        group_by_val = os.getenv("SH_GROUP_BY") or None
        original_sort_order_val = _get_env_json_list("SH_ORIGINAL_SORT_ORDER", sort_order_val)
        accuracy_val = int(os.getenv("SH_ACCURACY", "30"))

        resp = {"sort_order": sort_order_val, "accuracy": accuracy_val}
        if group_by_val:
            resp["group_by"] = group_by_val
        if group_by_val and original_sort_order_val:
            resp["original_sort_order"] = original_sort_order_val
        return _success_response(resp)

    payload = request.get_json(silent=True) or {}

    if isinstance(payload, dict):
        if "sort_order" in payload:
            new_order = payload["sort_order"]
            if not isinstance(new_order, list) or not new_order:
                return _error_response("sort_order must be a non-empty list.")
            _set_env_var("SH_SORT_ORDER", json.dumps(new_order))

        if "group_by" in payload:
            group_by = payload["group_by"]
            if group_by is not None:
                _set_env_var("SH_GROUP_BY", str(group_by))
            else:
                _set_env_var("SH_GROUP_BY", "")

        if "original_sort_order" in payload:
            original = payload["original_sort_order"]
            if original is not None:
                _set_env_var("SH_ORIGINAL_SORT_ORDER", json.dumps(original))
            else:
                _set_env_var("SH_ORIGINAL_SORT_ORDER", "")

        if "accuracy" in payload:
            acc_val = payload["accuracy"]
            if acc_val is not None:
                _set_env_var("SH_ACCURACY", str(acc_val))

    sort_order_val = _get_env_json_list("SH_SORT_ORDER", ["name", "port", "pid", "bind_address", "hostname", "status", "protected"])
    group_by_val = os.getenv("SH_GROUP_BY") or None
    original_sort_order_val = _get_env_json_list("SH_ORIGINAL_SORT_ORDER", sort_order_val)
    accuracy_val = int(os.getenv("SH_ACCURACY", "30"))

    resp = {"sort_order": sort_order_val, "accuracy": accuracy_val}
    if group_by_val:
        resp["group_by"] = group_by_val
    if group_by_val and original_sort_order_val:
        resp["original_sort_order"] = original_sort_order_val
    return _success_response(resp)


@app.route("/api/service/terminate", methods=["POST", "HEAD", "OPTIONS"])
def terminate():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization_all(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    raw_pid = payload.get("pid") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    if _is_protected(client_hash.strip()):
        return _error_response("Service is protected and cannot be terminated.", 403)

    pid = None
    if raw_pid is not None and str(raw_pid).strip().isdigit():
        pid = int(raw_pid)
    else:
        with REGISTERED_CLIENTS_LOCK:
            client_data = REGISTERED_CLIENTS.get(client_hash.strip())
        if client_data is not None:
            pid = _extract_pid(client_data)

    if pid is not None:
        try:
            _kill_pid(pid)
        except subprocess.CalledProcessError as exc:
            logger.error(f"Failed to kill PID {pid}: {exc.stderr}")

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS.pop(client_hash.strip(), None)

    logger.info(f"Terminated client {client_hash[:16]}...")

    return _success_response({"status": "terminated", "hash": client_hash.strip(), "pid": pid})


@app.route("/api/service/restart", methods=["POST", "HEAD", "OPTIONS"])
def restart():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization_all(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    raw_pid = payload.get("pid") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    if _is_protected(client_hash.strip()):
        return _error_response("Service is protected and cannot be restarted.", 403)

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(client_hash.strip())

    if client_data is None:
        return _error_response("Client not found.", 404)

    script_path = client_data.get("starting_script", "")
    if not isinstance(script_path, str) or not script_path.strip():
        return _error_response("No starting script available for this service.", 400)

    pid = None
    if raw_pid is not None and str(raw_pid).strip().isdigit():
        pid = int(raw_pid)
    else:
        pid = _extract_pid(client_data)

    if pid is None:
        return _error_response("No PID available for this service.", 400)

    try:
        _kill_pid(pid)
    except subprocess.CalledProcessError as exc:
        logger.error(f"Failed to kill PID {pid}: {exc.stderr}")
        return _error_response(f"Failed to terminate process: {exc.stderr}", 500)

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS.pop(client_hash.strip(), None)

    logger.info(f"Terminated PID {pid} (hash {client_hash[:16]}...)")

    try:
        _launch_script(script_path)
    except Exception as exc:
        return _error_response(f"Failed to start script: {exc}", 500)

    logger.info(f"Restarted with script '{script_path}' (hash {client_hash[:16]}...)")

    return _success_response({"status": "restarted", "hash": client_hash.strip()})


@app.route("/api/broken/forget", methods=["POST", "HEAD", "OPTIONS"])
def broken_forget():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization_all(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    if _is_protected(hash_val):
        return _error_response("Service is protected and cannot be forgotten.", 403)

    pid = None
    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)
        if client_data is not None:
            pid = _extract_pid(client_data)
            REGISTERED_CLIENTS.pop(hash_val, None)

    if pid is not None:
        try:
            _kill_pid(pid)
        except subprocess.CalledProcessError:
            logger.warning(f"Could not kill PID {pid} during forget (hash {hash_val[:16]}...)")

    logger.info(f"Forgotten broken service hash {hash_val[:16]}...")

    return _success_response({"status": "forgotten", "hash": hash_val})


@app.route("/api/broken/restart", methods=["POST", "HEAD", "OPTIONS"])
def broken_restart():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization_all(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    if _is_protected(hash_val):
        return _error_response("Service is protected and cannot be restarted.", 403)

    script_path = ""
    pid = None
    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)
        if client_data is not None:
            script_path = client_data.get("starting_script", "")
            pid = _extract_pid(client_data)
            REGISTERED_CLIENTS.pop(hash_val, None)

    if not isinstance(script_path, str) or not script_path.strip():
        return _error_response("No starting script available for this service.", 400)

    if pid is not None:
        try:
            _kill_pid(pid)
        except subprocess.CalledProcessError:
            logger.warning(f"Could not kill PID {pid} during broken restart (hash {hash_val[:16]}...)")

    try:
        _launch_script(script_path)
    except Exception as exc:
        return _error_response(f"Failed to start script: {exc}", 500)

    logger.info(f"Restarted broken service with script '{script_path}' (hash {hash_val[:16]}...)")

    return _success_response({"status": "restarted", "hash": hash_val})


@app.route("/api/service/protect", methods=["POST", "HEAD", "OPTIONS"])
def protect():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)

    if client_data is None:
        return _error_response("Client not found.", 404)

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS[hash_val]["protected"] = True

    logger.info(f"Protected service hash {hash_val[:16]}...")

    return _success_response({"status": "protected", "hash": hash_val})


@app.route("/api/service/unprotect", methods=["POST", "HEAD", "OPTIONS"])
def unprotect():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    allowed, invalid_key = _check_authorization(payload)
    if invalid_key:
        return _error_response("API key is not valid.", 403)
    if not allowed:
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)

    if client_data is None:
        return _error_response("Client not found.", 404)

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS[hash_val]["protected"] = False

    logger.info(f"Unprotected service hash {hash_val[:16]}...")

    return _success_response({"status": "unprotected", "hash": hash_val})


# ============================================================================
# API KEY MANAGEMENT
# ============================================================================


def _init_api_keys() -> None:
    global API_KEYS_DATA
    with API_KEYS_LOCK:
        API_KEYS_DATA = {"keys": {}}
    logger.info(
        "API key store initialized. "
        "Keys will be loaded from SH_API_KEYS environment variable on first request."
    )


def _ensure_api_key_session() -> str | None:
    global API_KEY_SESSION_READY, API_KEYS_DATA

    if API_KEY_SESSION_READY:
        return None

    env_data = os.getenv("SH_API_KEYS")
    loaded: dict = {}
    if env_data:
        try:
            parsed = json.loads(env_data)
            if isinstance(parsed, dict):
                for service_name, api_key in parsed.items():
                    if isinstance(api_key, str) and api_key.strip():
                        loaded[service_name] = {
                            "api_key": api_key.strip(),
                            "source": "env_var",
                        }
        except json.JSONDecodeError:
            logger.warning("SH_API_KEYS env var contains invalid JSON.")

    with API_KEYS_LOCK:
        API_KEYS_DATA["keys"] = loaded
        _rebuild_api_key_lookup()

    API_KEY_SESSION_READY = True
    logger.info(
        f"API key session ready. Loaded {len(loaded)} key(s) from SH_API_KEYS."
    )
    return None


def _generate_api_key_value() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def involving_api_keys(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        error = _ensure_api_key_session()
        if error:
            return _error_response(error, 503)
        return func(*args, **kwargs)
    return wrapper


# ============================================================================
# API KEY ENDPOINTS
# ============================================================================


@app.route("/api/api-key/request", methods=["POST", "HEAD", "OPTIONS"])
@involving_api_keys
def api_key_request():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)

    if client_data is None:
        return _error_response("Client not found.", 404)

    with PENDING_API_KEY_REQUESTS_LOCK:
        if hash_val in PENDING_API_KEY_REQUESTS:
            return _success_response(
                {"status": "already_pending", "message": "API key request is already pending."}
            )
        PENDING_API_KEY_REQUESTS[hash_val] = {
            "hash": hash_val,
            "name": client_data.get("name", ""),
            "port": client_data.get("port", 0),
            "ip": client_data.get("ip", "127.0.0.1"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    logger.info(
        f"API key request received from '{client_data.get('name', 'unknown')}' "
        f"({hash_val[:8]}...)"
    )

    return _success_response(
        {"status": "pending", "message": "API key request registered. Awaiting approval."},
        201,
    )


@app.route("/api/api-key/pending", methods=["GET", "HEAD", "OPTIONS"])
def api_key_pending():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    if not _is_authorized(payload):
        return _error_response("API key is not valid.", 403)

    with PENDING_API_KEY_REQUESTS_LOCK:
        pending_list = list(PENDING_API_KEY_REQUESTS.values())
        hashes = list(PENDING_API_KEY_REQUESTS.keys())

    return _success_response({"pending": pending_list, "hashes": hashes})


@app.route("/api/api-key/grant", methods=["POST", "HEAD", "OPTIONS"])
@involving_api_keys
def api_key_grant():
    try:
        if request.method == "OPTIONS":
            return _options_response(["POST", "HEAD", "OPTIONS"])
        if request.method == "HEAD":
            return _head_response()

        payload = request.get_json(silent=True) or {}
        if not _is_authorized(payload):
            return _error_response("API key is not valid.", 403)

        client_hash = payload.get("hash") if isinstance(payload, dict) else None

        if not isinstance(client_hash, str) or not client_hash.strip():
            return _error_response("A hash is required.")

        hash_val = client_hash.strip()

        with PENDING_API_KEY_REQUESTS_LOCK:
            request_info = PENDING_API_KEY_REQUESTS.pop(hash_val, None)

        if request_info is None:
            return _error_response("No pending API key request for this client.", 404)

        api_key = _generate_api_key_value()

        with API_KEYS_LOCK:
            service_name = request_info.get("name", "unknown")
            API_KEYS_DATA.setdefault("keys", {})[service_name] = {
                "api_key": api_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "service_hash": hash_val,
            }
            API_KEY_LOOKUP.append(api_key)

        service_ip = request_info.get("ip", "127.0.0.1")
        service_port = request_info.get("port", 0)
        notified = False
        if service_port:
            try:
                notify_payload = json.dumps(
                    {"api_key": api_key, "status": "granted"}
                ).encode("utf-8")
                req = PostRequest(
                    url=f"http://{service_ip}:{service_port}/api/key/granted",
                    body=notify_payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                resp = _send_post_request(req)
                notified = resp.status_code == 200
            except Exception as exc:
                logger.warning(
                    f"Failed to notify service '{request_info.get('name', 'unknown')}' "
                    f"about granted API key: {exc}"
                )

        logger.info(
            f"API key granted to '{request_info.get('name', 'unknown')}' "
            f"({hash_val[:8]}...) notified={notified}"
        )

        return _success_response(
            {
                "status": "granted",
                "api_key": api_key,
                "env_var_entry": f'SH_API_KEYS={json.dumps({service_name: api_key})}',
                "service": request_info.get("name", ""),
                "notified": notified,
            }
        )
    except Exception as exc:
        logger.error(f"Error in api_key_grant: {exc}", exc_info=True)
        return _error_response(f"Internal error: {exc}", 500)


@app.route("/api/api-key/reject", methods=["POST", "HEAD", "OPTIONS"])
@involving_api_keys
def api_key_reject():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    if not _is_authorized(payload):
        return _error_response("API key is not valid.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    with PENDING_API_KEY_REQUESTS_LOCK:
        request_info = PENDING_API_KEY_REQUESTS.pop(hash_val, None)

    if request_info is None:
        return _error_response("No pending API key request for this client.", 404)

    service_ip = request_info.get("ip", "127.0.0.1")
    service_port = request_info.get("port", 0)
    notified = False
    if service_port:
            try:
                notify_payload = json.dumps(
                    {"status": "rejected", "reason": "API key registration refused by the device owner."}
                ).encode("utf-8")
                req = PostRequest(
                    url=f"http://{service_ip}:{service_port}/api/key/rejected",
                    body=notify_payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                resp = _send_post_request(req)
                notified = resp.status_code == 200
            except Exception as exc:
                logger.warning(
                    f"Failed to notify service '{request_info.get('name', 'unknown')}' "
                    f"about rejected API key: {exc}"
                )

    logger.info(
        f"API key request rejected for '{request_info.get('name', 'unknown')}' "
        f"({hash_val[:8]}...) notified={notified}"
    )

    return _success_response(
        {
            "status": "rejected",
            "service": request_info.get("name", ""),
            "notified": notified,
        }
    )


@app.route("/api/shutdown", methods=["POST", "HEAD", "OPTIONS"])
def shutdown():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    if not _is_authorized(payload):
        return _error_response("API key is not valid.", 403)

    environ = request.environ

    def _shutdown():
        time.sleep(0.5)
        func = environ.get("werkzeug.server.shutdown")
        if func:
            func()
        else:
            os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()
    return _success_response({"status": "shutdown"})


def _register_ui_routes(app_instance: Flask) -> None:
    """Conditionally register UI-related routes when NO_GUI is False."""
    if NO_GUI:
        return
    app_instance.add_url_rule("/", methods=["GET", "HEAD", "OPTIONS"], view_func=index)
    app_instance.add_url_rule(
        "/css/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=css_files,
    )
    app_instance.add_url_rule(
        "/ui/sort-settings",
        methods=["GET", "PUT", "HEAD", "OPTIONS"],
        view_func=sort_order,
    )


if __name__ == "__main__":
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        _initialize_service_config()
        _register_ui_routes(app)
        _start_health_check_loop()
        _init_api_keys()
    except Exception as exc:
        logger.error(f"Failed to initialize: {exc}")
        exit(1)

    try:
        logger.info("=" * 50)
        logger.info("  ServiceHandler - Web Service Registry")
        logger.info("=" * 50)
        logger.info(f"Binding to: http://{SERVICE_HOST}:{SERVICE_PORT}")
        logger.info(f"Clients registered in memory: {len(REGISTERED_CLIENTS)}")
        logger.info("Server starting...")

        app.run(host=SERVICE_HOST, port=SERVICE_PORT, debug=False, threaded=True)

    except KeyboardInterrupt:
        logger.info("=" * 50)
        logger.info("  Server Stopped")
        logger.info("=" * 50)

    except OSError as exc:
        if "Address already in use" in str(exc):
            logger.error(
                f"Port {SERVICE_PORT} is already in use. "
                f"Change the port in resources/configuration.json"
            )
        elif "Permission denied" in str(exc):
            logger.error(
                f"Permission denied to bind to port {SERVICE_PORT}. "
                f"Use a port >= 1024 or run with elevated privileges."
            )
        else:
            logger.error(f"Network binding failed: {exc}")

    except Exception as exc:
        logger.error(f"Server startup failed: {exc}")
