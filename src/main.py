"""PortHandler - Web Service Registry."""

from __future__ import annotations

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


def _initialize_service_config() -> None:
    global SERVICE_HOST, SERVICE_PORT
    config = _load_configuration()

    SERVICE_HOST = "127.0.0.1"

    configured_port = config.get("port", 49155)
    if isinstance(configured_port, str) and configured_port.isdigit():
        configured_port = int(configured_port)
    if not isinstance(configured_port, int):
        configured_port = 49155

    SERVICE_PORT = configured_port


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
    if request.path in ("/",) or request.path.startswith(("/api/", "/css/")):
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


@app.route("/")
def index():
    web_dir = Path(__file__).parent.parent / "web"
    return send_from_directory(web_dir, "index.html")


@app.route("/css/<path:filename>")
def css_files(filename):
    css_dir = Path(__file__).parent.parent / "web" / "css"
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

    if pid is not None:
        if isinstance(pid, str) and pid.isdigit():
            pid = int(pid)
        if not isinstance(pid, int):
            return _error_response("PID must be a number.")

    if bind_address is not None and not isinstance(bind_address, str):
        return _error_response("Bind address must be a string.")
    if hostname_val is not None and not isinstance(hostname_val, str):
        return _error_response("Hostname must be a string.")

    client_ip = request.remote_addr or "127.0.0.1"

    if not _ping_health(client_ip, port):
        return _error_response("Client health endpoint is not reachable.", 400)

    name_to_check = name.strip()

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


@app.route("/api/health/check/<hash>", methods=["POST"])
def health_check_single(hash):
    with REGISTERED_CLIENTS_LOCK:
        client = REGISTERED_CLIENTS.get(hash)
    if not client:
        return _error_response("Client not found.", 404)
    ip = client.get("ip", "127.0.0.1")
    port = client.get("port", 0)
    healthy = _ping_health(ip, port)
    if not healthy:
        logger.info(f"Client '{client.get('name', 'unknown')}' ({hash[:8]}...) unhealthy (health check failed)")
    return _success_response({"hash": hash, "healthy": healthy})


@app.route("/api/health/check", methods=["POST"])
def health_check_trigger():
    unhealthy = _run_health_check()
    return _success_response({"checked": True, "unhealthy": unhealthy})


@app.route("/api/clients", methods=["GET", "HEAD", "OPTIONS"])
def clients():
    if request.method == "OPTIONS":
        return _options_response(["GET", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    with REGISTERED_CLIENTS_LOCK:
        client_list = list(REGISTERED_CLIENTS.values())

    return _success_response({"clients": client_list})


def _get_config_path() -> Path:
    return Path(__file__).parent.parent / "resources" / "configuration.json"


@app.route("/api/sort-settings", methods=["GET", "PUT", "HEAD", "OPTIONS"])
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
        resp = {"sort_order": order}
        if group_by is not None:
            resp["group_by"] = group_by
        if original_sort_order is not None:
            resp["original_sort_order"] = original_sort_order
        return _success_response(resp)

    payload = request.get_json(silent=True) or {}
    new_order = payload.get("sort_order") if isinstance(payload, dict) else None

    if not isinstance(new_order, list) or not new_order:
        return _error_response("sort_order must be a non-empty list.")

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except Exception:
        return _error_response("Failed to read configuration.", 500)

    config["sort_order"] = new_order

    group_by = payload.get("group_by") if isinstance(payload, dict) else None
    original_sort_order = payload.get("original_sort_order") if isinstance(payload, dict) else None

    if group_by is not None:
        config["group_by"] = group_by
    else:
        config.pop("group_by", None)

    if original_sort_order is not None:
        config["original_sort_order"] = original_sort_order
    else:
        config.pop("original_sort_order", None)

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        return _error_response("Failed to write configuration.", 500)

    resp = {"sort_order": new_order}
    if group_by is not None:
        resp["group_by"] = group_by
    if original_sort_order is not None:
        resp["original_sort_order"] = original_sort_order
    return _success_response(resp)


@app.route("/api/terminate", methods=["POST", "HEAD", "OPTIONS"])
def terminate():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
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

    return _success_response({"status": "terminated", "hash": client_hash.strip(), "pid": pid})


@app.route("/api/restart", methods=["POST", "HEAD", "OPTIONS"])
def restart():
    if request.method == "OPTIONS":
        return _options_response(["POST", "HEAD", "OPTIONS"])
    if request.method == "HEAD":
        return _head_response()

    payload = request.get_json(silent=True) or {}
    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    script_path = payload.get("starting_script") if isinstance(payload, dict) else None
    raw_pid = payload.get("pid") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    if not isinstance(script_path, str) or not script_path.strip():
        return _error_response("A starting_script path is required.")

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
    client_hash = payload.get("hash") if isinstance(payload, dict) else None
    script_path = payload.get("starting_script") if isinstance(payload, dict) else None

    if not isinstance(client_hash, str) or not client_hash.strip():
        return _error_response("A hash is required.")

    if not isinstance(script_path, str) or not script_path.strip():
        return _error_response("A starting_script path is required.")

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


if __name__ == "__main__":
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        _initialize_service_config()
        _start_health_check_loop()
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
