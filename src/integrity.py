"""Project and plugin-library integrity helpers.

Keeps the effective vs indicated hash logic (local) while remote checks are
handled in ``updates`` and ``plugin_bridge``. When the internet is not
reachable the remote checks must not crash the process — that tolerance lives
in the update / bridge layers, not here.

Text-file CRLF→LF normalisation is applied so a Windows checkout (core.autocrlf)
hashes identically to the Linux CI checkout.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import state
from logginglib import log_error, log_info, log_warn


def _should_exclude_from_project_hash(rel: str) -> bool:
    if rel == "hash":
        return True
    if rel == "resources/configuration.json":
        return True
    if rel.startswith("resources/plugins-lib/"):
        return True
    if rel.startswith("resources/audios/"):
        return True
    parts = rel.split("/")
    for part in parts:
        if part in state._PROJECT_HASH_EXCLUDE_DIRS or part == "__pycache__":
            return True
        if part.endswith(".egg-info"):
            return True
    if Path(rel).suffix in state._PROJECT_HASH_EXCLUDE_SUFFIXES:
        return True
    if Path(rel).suffix == ".so":
        return True
    return False


def _get_local_project_hash() -> str | None:
    try:
        p = Path(__file__).resolve().parent.parent / "hash"
        return p.read_text(encoding="utf-8").strip().split()[0]
    except Exception:
        return None


def _get_current_project_version() -> str:
    root = Path(__file__).resolve().parent.parent
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode("utf-8", errors="replace").strip()
    except Exception:
        return "unknown"
    if not tag:
        return "unknown"
    try:
        v = subprocess.check_output(
            ["git", "show", f"{tag}:hash"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode("utf-8", errors="replace").strip().split()[0]
    except Exception:
        return "unknown"
    if len(v) < 7:
        return "unknown"
    return v[:12]


def _get_effective_project_version() -> str:
    try:
        h = _compute_local_project_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_indicated_project_version() -> str:
    try:
        h = _get_local_project_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_current_plugins_lib_version() -> str:
    try:
        import plugin_bridge
        h = plugin_bridge._read_stored_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
        h = plugin_bridge._compute_plugins_lib_hash()
        if h and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_effective_plugins_lib_version() -> str:
    try:
        import plugin_bridge
        h = plugin_bridge._compute_plugins_lib_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_indicated_plugins_lib_version() -> str:
    try:
        import plugin_bridge
        h = plugin_bridge._read_stored_hash()
        if h and h.strip() and len(h.strip()) >= 7:
            return h.strip().split()[0][:12]
    except Exception:
        pass
    return "unknown"


def _get_local_plugins_lib_hash() -> str | None:
    try:
        import plugin_bridge
        return plugin_bridge._read_stored_hash()
    except Exception:
        return None


def _compute_local_project_hash() -> str | None:
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return None
        rels = [line for line in out.stdout.splitlines() if line]
    except Exception:
        return None
    entries: list[tuple[str, str]] = []
    for rel in rels:
        if _should_exclude_from_project_hash(rel):
            continue
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" not in data:
            data = data.replace(b"\r\n", b"\n")
        h = hashlib.sha256(data).hexdigest()
        entries.append((rel, h))
    entries.sort(key=lambda x: x[0])
    merged = "".join(h for _, h in entries)
    return hashlib.sha256(merged.encode("utf-8")).hexdigest()


def _verify_local_project_integrity() -> bool:
    import signing
    stored_path = Path(__file__).resolve().parent.parent / "hash"
    stored_hex, signed = signing.read_signed_hash_file(stored_path)
    if not signed:
        _crash("Project integrity check failed: stored hash has an invalid or missing signature", {"path": str(stored_path)})
    if not stored_hex:
        _crash("Project integrity check failed: local hash file missing", {"path": str(stored_path)})
    computed = _compute_local_project_hash()
    if computed is None:
        _crash("Project integrity check failed: cannot enumerate tracked files (not a git checkout?)")
    if computed.strip().lower() != stored_hex.strip().lower():
        log_error("Project integrity check failed: local files differ from recorded project hash (illicit interaction?)", {"computed": computed, "stored": stored_hex})
        return False
    log_info("Project integrity check passed", {"hash": stored_hex})
    return True


def _is_project_functionality_disabled() -> bool:
    return (not state._PROJECT_INTEGRITY_OK and state.DEVELOPMENT)


def _crash(message: str, data=None) -> None:
    """Log an error, play the error sound and exit(1) — 'crash the project'."""
    log_error(message, data)
    try:
        import audio
        audio.get_audio_orchestrator().start()
    except Exception:
        pass
    try:
        import audio
        audio.play_sound("error")
    except Exception:
        pass
    sys.exit(1)


def _verify_disabled_styles() -> bool:
    css_path = Path(__file__).resolve().parent.parent / "ui" / "css" / "index.css"
    try:
        css = css_path.read_text(encoding="utf-8")
    except Exception as exc:
        log_warn("Disabled styles check skipped: cannot read CSS", {"error": str(exc)})
        return True
    import re
    pattern = re.compile(r"([^{]+:disabled[^{]*)\{([^}]+)\}", re.MULTILINE)
    missing = []
    for selector, block in pattern.findall(css):
        if "cursor" not in block or "not-allowed" not in block:
            if any(k in selector for k in ("button", "input", "select", "textarea", "toggle", "generate-api-key-btn", "api-key-name-input", "random-name-btn", "page-action-btn", "icon-btn", "external-interactions-segment", "sound-select", "pill-action-btn")):
                missing.append(selector.strip())
    if missing:
        log_error("Disabled styles check failed: missing cursor: not-allowed", {"selectors": missing})
        return False
    log_info("Disabled styles check passed")
    return True
