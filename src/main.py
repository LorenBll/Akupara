"""PortHandler - Web Service Registry."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

from models import PostResponse

logger = logging.getLogger(__name__)

SERVICE_HOST = None
SERVICE_PORT = None

CLIENTS_PATH = Path(__file__).parent.parent / "resources" / "clients.json"
REGISTERED_CLIENTS: dict[str, dict] = {}
REGISTERED_CLIENTS_LOCK = threading.Lock()

HEALTH_CHECK_INTERVAL_SECONDS = 30


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


def _save_clients() -> None:
    CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGISTERED_CLIENTS_LOCK:
        clients_list = list(REGISTERED_CLIENTS.values())
    with open(CLIENTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"clients": clients_list}, f, indent=2)


def _load_clients_from_disk() -> None:
    if not CLIENTS_PATH.exists():
        return

    try:
        with open(CLIENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    if not isinstance(data, dict):
        return

    clients_list = data.get("clients", [])
    if not isinstance(clients_list, list):
        return

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS.clear()
        for client in clients_list:
            if isinstance(client, dict) and "hash" in client:
                REGISTERED_CLIENTS[client["hash"]] = client


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

    _load_clients_from_disk()


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


def _health_check_worker() -> None:
    while True:
        try:
            with REGISTERED_CLIENTS_LOCK:
                current_clients = dict(REGISTERED_CLIENTS)

            to_remove = []
            for client_hash, client_data in current_clients.items():
                ip = client_data.get("ip", "127.0.0.1")
                port = client_data.get("port", 0)
                if not _ping_health(ip, port):
                    to_remove.append(client_hash)

            if to_remove:
                with REGISTERED_CLIENTS_LOCK:
                    for client_hash in to_remove:
                        REGISTERED_CLIENTS.pop(client_hash, None)
                _save_clients()
                for client_hash in to_remove:
                    entry = current_clients.get(client_hash, {})
                    logger.info(
                        f"Client '{entry.get('name', 'unknown')}' "
                        f"({client_hash[:8]}...) removed (health check failed)"
                    )

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
    if request.path.startswith("/api/") and not _is_local_request():
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
        _save_clients()
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
        "ip": client_ip,
        "timestamp": timestamp,
    }

    with REGISTERED_CLIENTS_LOCK:
        REGISTERED_CLIENTS[client_hash] = client_data

    _save_clients()

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

    _save_clients()

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
            "hostname": socket.gethostname(),
            "registered_clients": client_count,
        }
    )


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
        logger.info(f"Clients loaded from disk: {len(REGISTERED_CLIENTS)}")
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
