"""Script smoke tests: run the setup and run scripts in a disposable copy.

These tests never touch the developer's real ``.env``, ``logs/``, ``.venv``,
``resources/configuration.json`` or installed plugins: the project is copied to
a temporary directory and operated on there.

The run test additionally requires ``gpg`` (the shipped ``hash`` files are
PGP-signed and ``src/main.py`` refuses to start without verification) and a git
checkout (``git ls-files`` drives the project-integrity check). It is skipped
automatically where those are unavailable.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_GPG_AVAILABLE = shutil.which("gpg") is not None
_GIT_AVAILABLE = shutil.which("git") is not None

# ``hash`` (the signed project hash) is required by the project-integrity check
# at startup; ``.gitignore`` keeps ``git add -A`` from committing runtime
# artefacts such as ``.venv``, ``logs`` and ``.env``.
_COPY_NAMES = ("src", "ui", "resources", "scripts", "requirements.txt", "hash", ".gitignore")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _copy_project(dst: Path) -> None:
    for name in _COPY_NAMES:
        src = PROJECT_ROOT / name
        if src.is_dir():
            shutil.copytree(src, dst / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst / name)


def _make_sandbox(tmp_path: Path, port: int) -> Path:
    sandbox = tmp_path / "akupara"
    _copy_project(sandbox)

    # The project-integrity check needs a git checkout (``git ls-files``).
    subprocess.run(["git", "init", "-q"], cwd=sandbox, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=sandbox, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=sandbox, check=True)
    subprocess.run(["git", "add", "-A"], cwd=sandbox, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=sandbox, check=True)

    # Development mode (lenient integrity) and a free port.
    (sandbox / "resources" / "configuration.json").write_text(
        json.dumps({"port": port, "guiEnabled": True, "development": True}),
        encoding="utf-8",
    )
    (sandbox / ".env").write_text("", encoding="utf-8")
    return sandbox


def test_setup_script(tmp_path):
    sandbox = _make_sandbox(tmp_path, _free_port())
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


@pytest.mark.skipif(not (_GPG_AVAILABLE and _GIT_AVAILABLE), reason="requires gpg and git")
def test_run_script(tmp_path):
    port = _free_port()
    sandbox = _make_sandbox(tmp_path, port)
    if os.name == "nt":
        proc = subprocess.Popen(
            ["cmd", "/c", "run.bat", "--debug"],
            cwd=sandbox / "scripts",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    else:
        proc = subprocess.Popen(
            ["bash", "scripts/run.sh", "--debug"],
            cwd=sandbox,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        _wait_for_health(port, timeout=180)
    finally:
        _terminate(proc)


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


def _wait_for_health(port: int, timeout: int) -> None:
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - retry until the server is up
            last_error = exc
        time.sleep(1)
    raise AssertionError(f"Server did not become healthy on port {port}: {last_error}")
