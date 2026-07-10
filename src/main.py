"""PortHandler - Web Service Registry."""

from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import logging
import os
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

from flask import Flask, jsonify, request, send_from_directory

from models import PostResponse

logger = logging.getLogger(__name__)


def _kill_pid(pid: int) -> None:
    is_win = sys.platform.startswith("win")
    cmd = ["taskkill", "/F", "/PID", str(pid)] if is_win else ["kill", "-9", str(pid)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

SERVICE_HOST = None
SERVICE_PORT = None

REGISTERED_CLIENTS: dict[str, dict] = {}
REGISTERED_CLIENTS_LOCK = threading.Lock()

PENDING_API_KEY_REQUESTS: dict[str, dict] = {}
PENDING_API_KEY_REQUESTS_LOCK = threading.Lock()

API_KEYS_DATA: dict = {"keys": {}}
API_KEYS_LOCK = threading.Lock()

API_KEY_STORE_KEY_PATH: str | None = None
API_KEY_SESSION_READY: bool = False

HEALTH_CHECK_INTERVAL_SECONDS = 15


def _load_configuration() -> dict:
    script_dir = Path(__file__).parent
    config_path = script_dir.parent / "resources" / "configuration.json"
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


def _is_authorized(payload: dict) -> bool:
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    if isinstance(api_key, str) and api_key.strip():
        with API_KEYS_LOCK:
            for data in API_KEYS_DATA.get("keys", {}).values():
                if isinstance(data, dict) and data.get("api_key") == api_key.strip():
                    return True
    return _is_localhost_request()


def _initialize_service_config() -> None:
    global SERVICE_HOST, SERVICE_PORT
    global API_KEY_STORE_KEY_PATH
    config = _load_configuration()

    SERVICE_HOST = "127.0.0.1"

    configured_port = config.get("port", 49155)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        configured_port = 49155

    SERVICE_PORT = configured_port

    raw_key_path = config.get("api_key_store_key_path", "")
    if isinstance(raw_key_path, str) and raw_key_path.strip():
        API_KEY_STORE_KEY_PATH = _resolve_ultimate_path(raw_key_path.strip())


def _resolve_service(name: str, default_host: str, default_port: int) -> tuple[str, int]:
    with REGISTERED_CLIENTS_LOCK:
        for client in REGISTERED_CLIENTS.values():
            if client.get("name") == name:
                ip = client.get("ip", default_host)
                port = client.get("port", default_port)
                if isinstance(port, int) and 1 <= port <= 65535:
                    return ip, port
    return default_host, default_port


def _ping_health(ip: str, port: int, timeout: float = 5.0) -> bool:
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
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
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
    if request.path in ("/",) or request.path.startswith(("/api/", "/ui/", "/css/")):
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
    content_type = response.headers.get("Content-Type", "")
    if content_type.startswith("text/html"):
        response.headers["Connection"] = "keep-alive"
    else:
        response.headers["Connection"] = "close"
    return response


@app.route("/", methods=["GET", "HEAD", "OPTIONS"])
def index():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()
    web_dir = Path(__file__).parent.parent / "ui" / "pages"
    return send_from_directory(web_dir, "index.html")


@app.route("/css/<path:filename>", methods=["GET", "HEAD", "OPTIONS"])
def css_files(filename):
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()
    css_dir = Path(__file__).parent.parent / "ui" / "css"
    return send_from_directory(css_dir, filename)


@app.route("/api/register", methods=["POST", "HEAD", "OPTIONS"])
def register():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    name = payload.get("name") if isinstance(payload, dict) else None
    port = payload.get("port") if isinstance(payload, dict) else None
    starting_script = payload.get("starting_script") if isinstance(payload, dict) else None
    pid = payload.get("pid") if isinstance(payload, dict) else None
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

    if pid is None:
        return _error_response("A PID is required.")
    if isinstance(pid, str) and pid.isdigit():
        pid = int(pid)
    if not isinstance(pid, int):
        return _error_response("PID must be a number.")

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
        "pid": pid if isinstance(pid, int) else "",
        "bind_address": bind_address.strip() if isinstance(bind_address, str) else "",
        "hostname": hostname_val.strip() if isinstance(hostname_val, str) else "",
        "ip": client_ip,
        "timestamp": timestamp,
    }

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS[client_hash] = client_data

    logger.info(f"Client '{name}' registered on port {port} ({client_hash[:16]}...)")

    return _success_response({"hash": client_hash}, 201)


@app.route("/api/question", methods=["POST", "HEAD", "OPTIONS"])
def question():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    target_name = payload.get("name") if isinstance(payload, dict) else None

    if not isinstance(target_name, str) or not target_name.strip():
        return _error_response("The name of the target client is required.")

    with REGISTERED_CLIENTS_LOCK:
        target = None
        for client in REGISTERED_CLIENTS.values():
            if client.get("name") == target_name.strip():
                target = client
                break

    if target is None:
        return _error_response(f"No client found with name '{target_name}'.", 404)

    return _success_response({"name": target["name"], "port": target["port"]})


@app.route("/api/unregister", methods=["DELETE", "HEAD", "OPTIONS"])
def unregister():
    if request.method == "OPTIONS":
        return _options_response(["DELETE", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required to unregister.")

    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.pop(client_hash.strip(), None)

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
            "service": "PortHandler",
            "bind_address": SERVICE_HOST,
            "port": SERVICE_PORT,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "registered_clients": client_count,
        }
    )


@app.route("/api/health/check", methods=["POST", "HEAD", "OPTIONS"])
def health_check():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    if not _is_authorized(payload):
        return _error_response("Only localhost requests are allowed.", 403)

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


@app.route("/api/clients", methods=["GET", "HEAD", "OPTIONS"])
def clients():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    include_hash = _is_localhost_request()
    with REGISTERED_CLIENTS_LOCK:
        client_list = [
            client if include_hash else {k: v for k, v in client.items() if k != "hash"}
            for client in REGISTERED_CLIENTS.values()
        ]

    return _success_response({"clients": client_list})


def _get_config_path() -> Path:
    return Path(__file__).parent.parent / "resources" / "configuration.json"


def _get_api_keys_path() -> Path:
    return Path(__file__).parent.parent / "resources" / "api_keys.json"


@app.route("/ui/sort-settings", methods=["GET", "PUT", "HEAD", "OPTIONS"])
def sort_order():
    if request.method == "OPTIONS":
        return _options_response(["GET", "PUT", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    config_path = _get_config_path()

    if request.method == "GET":
        config = _load_configuration()
        order = config.get("sort_order", ["name", "port", "pid", "bind_address", "hostname", "status"])
        group_by = config.get("group_by")
        original_sort_order = config.get("original_sort_order")
        accuracy = config.get("accuracy", 30)
        resp = {"sort_order": order}
        if group_by is not None:
            resp["group_by"] = group_by
        if original_sort_order is not None:
            resp["original_sort_order"] = original_sort_order
        resp["accuracy"] = accuracy
        return _success_response(resp)

    payload = request.get_json(silent=True) or {}

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except Exception:
        return _error_response("Failed to read configuration.", 500)

    if isinstance(payload, dict):
        if "sort_order" in payload:
            new_order = payload["sort_order"]
            if not isinstance(new_order, list) or not new_order:
                return _error_response("sort_order must be a non-empty list.")
            config["sort_order"] = new_order

        if "group_by" in payload:
            group_by = payload["group_by"]
            if group_by is not None:
                config["group_by"] = group_by
            else:
                config.pop("group_by", None)

        if "original_sort_order" in payload:
            original_sort_order = payload["original_sort_order"]
            if original_sort_order is not None:
                config["original_sort_order"] = original_sort_order
            else:
                config.pop("original_sort_order", None)

        if "accuracy" in payload:
            acc_val = payload["accuracy"]
            if acc_val is not None:
                config["accuracy"] = acc_val
            else:
                config.pop("accuracy", None)

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        return _error_response("Failed to write configuration.", 500)

    resp = {"sort_order": config.get("sort_order", ["name", "port", "pid", "bind_address", "hostname", "status"])}
    current_group_by = config.get("group_by")
    if current_group_by is not None:
        resp["group_by"] = current_group_by
    current_original = config.get("original_sort_order")
    if current_original is not None:
        resp["original_sort_order"] = current_original
    resp["accuracy"] = config.get("accuracy", 30)
    return _success_response(resp)


@app.route("/api/terminate", methods=["POST", "HEAD", "OPTIONS"])
def terminate():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    if not _is_authorized(payload):
        return _error_response("Only localhost requests are allowed.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    raw_pid = payload.get("pid") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    pid = None
    if raw_pid is not None and str(raw_pid).strip().isdigit():
        pid = int(raw_pid)
    else:
        with REGISTERED_CLIENTS_LOCK:
            client_data = REGISTERED_CLIENTS.get(client_hash.strip())
        if client_data is not None:
            stored = client_data.get("pid")
            if isinstance(stored, int) or (isinstance(stored, str) and stored.strip().isdigit()):
                pid = int(stored)

    if pid is not None:
        try:
            _kill_pid(pid)
        except subprocess.CalledProcessError as exc:
            logger.error(f"Failed to kill PID {pid}: {exc.stderr}")

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS.pop(client_hash.strip(), None)

    logger.info(f"Terminated client {client_hash[:16]}...")

    return _success_response({"status": "terminated", "hash": client_hash.strip(), "pid": pid})


@app.route("/api/restart", methods=["POST", "HEAD", "OPTIONS"])
def restart():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    if not _is_authorized(payload):
        return _error_response("Only localhost requests are allowed.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    raw_pid = payload.get("pid") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

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
        stored = client_data.get("pid")
        if isinstance(stored, int) or (isinstance(stored, str) and stored.strip().isdigit()):
            pid = int(stored)

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
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as exc:
        logger.error(f"Failed to start script '{script_path}': {exc}")
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
    if not _is_authorized(payload):
        return _error_response("Only localhost requests are allowed.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    pid = None
    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)
        if client_data is not None:
            stored = client_data.get("pid")
            if isinstance(stored, int) or (isinstance(stored, str) and stored.strip().isdigit()):
                pid = int(stored)
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
    if not _is_authorized(payload):
        return _error_response("Only localhost requests are allowed.", 403)

    client_hash = payload.get("hash") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    hash_val = client_hash.strip()

    script_path = ""
    pid = None
    with REGISTERED_CLIENTS_LOCK:
        client_data = REGISTERED_CLIENTS.get(hash_val)
        if client_data is not None:
            script_path = client_data.get("starting_script", "")
            stored = client_data.get("pid")
            if isinstance(stored, int) or (isinstance(stored, str) and stored.strip().isdigit()):
                pid = int(stored)
            REGISTERED_CLIENTS.pop(hash_val, None)

    if not isinstance(script_path, str) or not script_path.strip():
        return _error_response("No starting script available for this service.", 400)

    if pid is not None:
        try:
            _kill_pid(pid)
        except subprocess.CalledProcessError:
            logger.warning(f"Could not kill PID {pid} during broken restart (hash {hash_val[:16]}...)")

    try:
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
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as exc:
        logger.error(f"Failed to start script '{script_path}': {exc}")
        return _error_response(f"Failed to start script: {exc}", 500)

    logger.info(f"Restarted broken service with script '{script_path}' (hash {hash_val[:16]}...)")

    return _success_response({"status": "restarted", "hash": hash_val})


# ============================================================================
# API KEY MANAGEMENT
# ============================================================================


def _resolve_ultimate_path(ultimate_path: str) -> str:
    raw = ultimate_path
    disk_id = None
    rel_path = ""
    for sep in (":", "\\"):
        if sep in raw:
            parts = raw.split(sep, 1)
            if len(parts[0]) == 64:
                disk_id = parts[0]
                rel_path = parts[1]
                break
    if not disk_id:
        return raw
    try:
        disk_host, disk_port = _resolve_service("DiskIdentifier", "127.0.0.1", 49157)
        req = urllib.request.Request(
            f"http://{disk_host}:{disk_port}/api/locate",
            data=json.dumps({"disk_identifier": disk_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            disk_root = data.get("path", "")
            if disk_root:
                return str(Path(disk_root) / rel_path.lstrip("/\\"))
    except Exception as exc:
        logger.warning(f"Failed to resolve ultimate path via DiskIdentifier: {exc}")
    return raw


def _cipher_file_operation(
    operation: str, file_path: Path, timeout_seconds: int = 120
) -> bool:
    if not API_KEY_STORE_KEY_PATH:
        logger.error("API key store key path not configured")
        return False
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        return False
    try:
        payload = json.dumps(
            {
                "key_path": API_KEY_STORE_KEY_PATH,
                "file_path": str(file_path),
                "encrypt_file_name": False,
                "decrypt_file_name": False,
                "overwrite_file": True,
            }
        ).encode("utf-8")
        endpoint = "encrypt" if operation == "encrypt" else "decrypt"
        cipher_host, cipher_port = _resolve_service("Cipher", "127.0.0.1", 49158)
        req = urllib.request.Request(
            f"http://{cipher_host}:{cipher_port}/api/{endpoint}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            task_id = result.get("task_id")
            if not task_id:
                return False
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                time.sleep(1)
                try:
                    poll_req = urllib.request.Request(
                        f"http://{cipher_host}:{cipher_port}/api/task/{task_id}",
                        method="GET",
                    )
                    with urllib.request.urlopen(poll_req, timeout=3) as poll_resp:
                        poll_result = json.loads(poll_resp.read().decode("utf-8"))
                        if poll_result.get("status") == "completed":
                            return True
                        if poll_result.get("status") == "failed":
                            return False
                except Exception:
                    continue
            return False
    except Exception as exc:
        logger.error(f"Cipher {operation} failed: {exc}")
        return False


def _load_api_keys() -> dict:
    api_keys_path = _get_api_keys_path()
    if not api_keys_path.exists():
        return {"keys": {}}
    try:
        with open(api_keys_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
            data = {"keys": {}}
    except (json.JSONDecodeError, Exception):
        data = {"keys": {}}
    return data


def _save_api_keys(data: dict) -> bool:
    api_keys_path = _get_api_keys_path()
    try:
        api_keys_path.parent.mkdir(parents=True, exist_ok=True)
        with open(api_keys_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.error(f"Failed to write API keys to {api_keys_path}: {exc}")
        return False
    return True


def _init_api_keys() -> None:
    global API_KEYS_DATA
    api_keys_path = _get_api_keys_path()
    api_keys_path.parent.mkdir(parents=True, exist_ok=True)
    if not api_keys_path.exists():
        with open(api_keys_path, "w", encoding="utf-8") as f:
            json.dump({"keys": {}}, f)
    with API_KEYS_LOCK:
        API_KEYS_DATA = _load_api_keys()
    logger.info(
        f"Loaded {len(API_KEYS_DATA.get('keys', {}))} API key(s) from store. "
        "Session initialization deferred until first request."
    )


def _ensure_api_key_session() -> str | None:
    global API_KEY_SESSION_READY, API_KEY_STORE_KEY_PATH, API_KEYS_DATA

    if API_KEY_SESSION_READY:
        return None

    if not API_KEY_STORE_KEY_PATH:
        return "API key store key path not configured in configuration.json."

    with REGISTERED_CLIENTS_LOCK:
        if not any(c.get("name") == "DiskIdentifier" for c in REGISTERED_CLIENTS.values()):
            return "DiskIdentifier service not registered. Required for API key operations."

    with REGISTERED_CLIENTS_LOCK:
        if not any(c.get("name") == "Cipher" for c in REGISTERED_CLIENTS.values()):
            return "Cipher service not registered. Required for API key operations."

    raw_path = API_KEY_STORE_KEY_PATH
    resolved = _resolve_ultimate_path(raw_path)
    raw_prefix_64 = raw_path.split("\\", 1)[0] if "\\" in raw_path else raw_path.split(":", 1)[0]
    if resolved == raw_path and len(raw_prefix_64) == 64:
        return "Failed to resolve API key store key path via DiskIdentifier."
    API_KEY_STORE_KEY_PATH = resolved

    loaded = _load_api_keys()
    with API_KEYS_LOCK:
        API_KEYS_DATA = loaded

    API_KEY_SESSION_READY = True
    logger.info(
        f"API key session ready. Loaded {len(API_KEYS_DATA.get('keys', {}))} key(s)."
    )
    return None


def _save_and_encrypt_api_keys() -> bool:
    global API_KEYS_DATA

    api_keys_path = _get_api_keys_path()
    try:
        api_keys_path.parent.mkdir(parents=True, exist_ok=True)
        if api_keys_path.exists():
            _cipher_file_operation("decrypt", api_keys_path)
        existing = _load_api_keys()
        with API_KEYS_LOCK:
            existing_keys = existing.get("keys", {})
            current_keys = API_KEYS_DATA.get("keys", {})
            merged = dict(existing_keys)
            merged.update(current_keys)
            API_KEYS_DATA["keys"] = merged
            data = dict(API_KEYS_DATA)
        with open(api_keys_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if not _cipher_file_operation("encrypt", api_keys_path):
            logger.error("Failed to encrypt api_keys.json after save.")
            return False
        logger.info("API keys saved and encrypted.")
        return True
    except Exception as exc:
        logger.error(f"Failed to save and encrypt API keys: {exc}")
        return False


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

    with PENDING_API_KEY_REQUESTS_LOCK:
        pending_list = list(PENDING_API_KEY_REQUESTS.values())

    return _success_response({"pending": pending_list})


@app.route("/api/api-key/pending-hashes", methods=["GET", "HEAD", "OPTIONS"])
def api_key_pending_hashes():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    with PENDING_API_KEY_REQUESTS_LOCK:
        hashes = list(PENDING_API_KEY_REQUESTS.keys())

    return _success_response({"hashes": hashes})


@app.route("/api/api-key/grant", methods=["POST", "HEAD", "OPTIONS"])
@involving_api_keys
def api_key_grant():
    try:
        if request.method == "OPTIONS":
            return _options_response(["POST", "HEAD", "OPTIONS"])
        if request.method == "HEAD":
            return _head_response()

        payload = request.get_json(silent=True) or {}
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

        if not _save_and_encrypt_api_keys():
            with API_KEYS_LOCK:
                API_KEYS_DATA.get("keys", {}).pop(service_name, None)
            return _error_response("Failed to persist API key.", 500)

        service_ip = request_info.get("ip", "127.0.0.1")
        service_port = request_info.get("port", 0)
        notified = False
        if service_port:
            try:
                notify_payload = json.dumps(
                    {"api_key": api_key, "status": "granted"}
                ).encode("utf-8")
                notify_req = urllib.request.Request(
                    f"http://{service_ip}:{service_port}/api/key/granted",
                    data=notify_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(notify_req, timeout=10) as notify_resp:
                    notified = notify_resp.status == 200
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
            notify_req = urllib.request.Request(
                f"http://{service_ip}:{service_port}/api/key/rejected",
                data=notify_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(notify_req, timeout=10) as notify_resp:
                notified = notify_resp.status == 200
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

if __name__ == "__main__":
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        _initialize_service_config()
        _start_health_check_loop()
        _init_api_keys()
    except Exception as exc:
        logger.error(f"Failed to initialize: {exc}")
        exit(1)

    try:
        logger.info("=" * 50)
        logger.info("  PortHandler - Web Service Registry")
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
