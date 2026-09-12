"""Centralised .env persistence with atomic writes and concurrency guard."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

_ENV_LOCK = threading.Lock()


def _env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def write_env_var(key: str, value: str) -> None:
    """Atomically write key=value to .env (thread-safe, preserves permissions)."""
    if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
        raise ValueError("Newline not allowed in .env key/value.")
    env_path = _env_path()
    with _ENV_LOCK:
        if not env_path.exists():
            env_path.touch()
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = False
        for index, line in enumerate(lines):
            # Match only exact key before '=', ignore surrounding whitespace
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, _ = stripped.partition("=")
            if name.strip() == key:
                lines[index] = f"{key}={value}"
                updated = True
                break
        if not updated:
            # Check also non-stripped? fallback linear scan
            found = any(l.strip().startswith(key + "=") for l in lines)
            if not found:
                lines.append(f"{key}={value}")
            else:
                # Replace first occurrence that startswith key= (already handled above, but keep parity)
                for i, line in enumerate(lines):
                    if line.strip().startswith(key + "="):
                        lines[i] = f"{key}={value}"
                        break
        new_content = "\n".join(lines) + "\n"
        try:
            mode = env_path.stat().st_mode & 0o777
        except OSError:
            mode = None
        fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), prefix=".env.", suffix=".tmp", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            if mode is not None:
                os.chmod(tmp_path, mode)
            os.replace(tmp_path, str(env_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # Mirror to process env for immediate reads via os.getenv
        try:
            os.environ[key] = value
        except Exception:
            pass


def write_env_bool(key: str, value: bool) -> None:
    write_env_var(key, str(value).lower())


def read_env_var(key: str, default: str = "") -> str:
    env_path = _env_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, val = stripped.partition("=")
        if name.strip() == key:
            return val.strip()
    return default


def read_env_bool(key: str, default: bool = False) -> bool:
    env_path = _env_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, val = stripped.partition("=")
        if name.strip() == key:
            return _parse_bool(val, default)
    return default
