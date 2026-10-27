"""Script smoke tests: run the setup and run scripts in a disposable copy.

These tests never touch the developer's real ``.env``, ``logs/``, ``.venv``,
``resources/configuration.json`` or installed plugins: the project is copied to
a temporary directory and operated on there.

The run test additionally requires ``gpg`` (the shipped ``hash`` files are
PGP-signed and ``src/main.py`` refuses to start without verification) and a git
checkout (``git ls-files`` drives the project-integrity check). It is skipped
automatically where those are unavailable.
"""

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

import auth

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_GPG_AVAILABLE = shutil.which("gpg") is not None
_GIT_AVAILABLE = shutil.which("git") is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _copy_project(dst: Path) -> None:
    # Copy every tracked file so the sandbox's project hash matches the signed
    # ``hash`` file, which lets the integrity check pass outside development mode.
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for rel in tracked:
        rel = rel.strip()
        if not rel:
            continue
        src = PROJECT_ROOT / rel
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _make_sandbox(tmp_path: Path, port: int) -> Path:
    sandbox = tmp_path / "akupara"
    _copy_project(sandbox)

    # The project-integrity check needs a git checkout (``git ls-files``).
    subprocess.run(["git", "init", "-q"], cwd=sandbox, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=sandbox, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=sandbox, check=True)
    subprocess.run(["git", "add", "-A"], cwd=sandbox, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=sandbox, check=True)

    # Non-development mode (strict integrity) and a free port.
    (sandbox / "resources" / "configuration.json").write_text(
        json.dumps({"port": port, "guiEnabled": True, "development": False}),
        encoding="utf-8",
    )
    (sandbox / ".env").write_text("", encoding="utf-8")
    return sandbox


def test_setup_script(tmp_path):
    sandbox = _make_sandbox(tmp_path, _free_port())
    # A setup run starts a new installation, so an existing marker must go.
    marker = sandbox / "resources" / "installation.json"
    marker.write_text('{"started": "2020-01-01T00:00:00Z"}\n', encoding="utf-8")
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "setup.bat"],
            cwd=sandbox / "scripts",
            input=b"\r\n\r\n",
            capture_output=True,
            timeout=600,
        )
    else:
        proc = subprocess.run(
            ["bash", "scripts/setup.sh"],
            cwd=sandbox,
            input=b"\n\n",
            capture_output=True,
            timeout=600,
        )
    assert proc.returncode == 0, (
        proc.stdout.decode(errors="replace") + "\n" + proc.stderr.decode(errors="replace")
    )
    assert (sandbox / ".venv").exists()
    assert not marker.exists()


@pytest.mark.skipif(not (_GPG_AVAILABLE and _GIT_AVAILABLE), reason="requires gpg and git")
def test_run_script(tmp_path):
    port = _free_port()
    sandbox = _make_sandbox(tmp_path, port)
    # Stream the server output to a file: piping it without reading would fill
    # the pipe buffer and stall the server, and the file is what lets a failed
    # boot be diagnosed (its tail is printed on assertion failure).
    log_path = sandbox / "run.log"
    with open(log_path, "wb") as log:
        if os.name == "nt":
            proc = subprocess.Popen(
                ["cmd", "/c", "run.bat", "--debug"],
                cwd=sandbox / "scripts",
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        else:
            proc = subprocess.Popen(
                ["bash", "scripts/run.sh", "--debug"],
                cwd=sandbox,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            _wait_for_health(port, timeout=180, proc=proc)
            _assert_default_root_can_log_in(sandbox, port)
        except AssertionError:
            log.flush()
            print("\n----- server stdout (tail) -----")
            print(_read_log_tail(log_path))
            print("----- end server stdout -----")
            print("----- server log (tail) -----")
            print(_read_latest_log_tail(sandbox / "logs"))
            print("----- end server log -----")
            raise
        finally:
            _terminate(proc)


def _read_users(sandbox: Path) -> list[dict]:
    for line in (sandbox / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("USERS="):
            return json.loads(stripped[len("USERS="):] or "[]")
    return []


def _assert_default_root_can_log_in(sandbox: Path, port: int) -> None:
    """The fresh installation must hand out a usable root account."""
    users = _read_users(sandbox)
    assert [user["username"] for user in users] == ["admin"], users
    assert users[0]["root"] is True, users
    password_hash = auth._client_password_hash("admin123", users[0]["client_salt"])
    body = json.dumps({"username": "admin", "password_hash": password_hash})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/api/login", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = response.read()
    finally:
        conn.close()
    assert response.status == 200, payload


def _read_log_tail(path: Path, limit: int = 8000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log captured)"
    return text[-limit:] if len(text) > limit else text


def _read_latest_log_tail(logs_dir: Path, limit: int = 12000) -> str:
    try:
        files = sorted(logs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return "(no server log captured)"
    if not files:
        return "(no server log captured)"
    return _read_log_tail(files[-1], limit)


def _terminate(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_for_health(port: int, timeout: int, proc: subprocess.Popen | None = None) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(
                f"Server process exited with code {proc.returncode} "
                f"before becoming healthy on port {port}"
            )
        try:
            # Plain http.client, never urllib: urllib routes requests through any
            # proxy advertised by the environment (on Windows that includes the
            # WinINET registry settings), so a loopback health check could be
            # sent to the proxy instead of the sandbox server and time out.
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                response.read()
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - retry until the server is up
            last_error = exc
        time.sleep(1)
    raise AssertionError(f"Server did not become healthy on port {port}: {last_error}")
