"""Update checks — remote hash fetches with offline tolerance.

Remote hash checks (project hash from GitHub releases, plugin-library hash from
latest commit, version tags via ``git ls-remote``) may fail when the internet
is not reachable. In that case the program must NOT crash and must keep
running; only the local effective vs indicated checks remain authoritative.

Rate-limit errors (``GithubRateLimitedError``) are handled specially as before;
all other network/offline errors are treated as "offline — skip the remote
check" with a warning, returning ``None``/``False``/``True`` as appropriate to
keep the process alive.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import state
import github_api
from logginglib import log_error, log_info, log_warn


def _is_offline_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like an offline / network failure.

    ``urllib.error.HTTPError`` is a subclass of ``URLError`` but represents an
    actual HTTP response (e.g. 404) — that is "not found", not "offline", so it
    is excluded.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    # URLError covers DNS failure, connection refused, no route, timeout wrapping
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, socket.timeout):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    # Wrapped OSError with network-ish messages
    msg = str(exc).lower()
    offline_phrases = (
        "name or service not known",
        "temporary failure in name resolution",
        "network is unreachable",
        "connection timed out",
        "timed out",
        "no route to host",
        "connection refused",
        "getaddrinfo failed",
        "offline",
    )
    return any(p in msg for p in offline_phrases)


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
    import sys
    sys.exit(1)


def _fetch_remote_project_hash(timeout: int = 8) -> str | None:
    """Fetch the unsigned project hash from the latest release's hash asset.

    Only the latest release is consulted — the locally *indicated* hash is
    already validated by its PGP signature, so no tag-scanning fallback is
    needed. Unavailable (offline / no release asset yet) → ``None``.
    """
    import ssl
    import urllib.request
    url = "https://github.com/LorenBll/Akupara/releases/latest/download/hash"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with github_api.github_urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()[0]
            if data:
                return data
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit exceeded while fetching remote project hash — skipping version check", {"url": url})
        raise
    except Exception as exc:
        log_warn("Remote project hash unavailable — skipping remote check", {"url": url, "error": str(exc)})
        return None
    return None


def _is_update_available() -> bool:
    # Local check (always authoritative): the stored project hash must be
    # validly signed, then effective vs indicated (hex only) must match.
    import signing
    from integrity import _compute_local_project_hash
    stored_path = Path(__file__).resolve().parent.parent / "hash"
    indicated, signed = signing.read_signed_hash_file(stored_path)
    if not signed or not indicated:
        _crash("Project integrity check failed: stored hash has an invalid or missing signature", {"path": str(stored_path)})
    effective = _compute_local_project_hash()
    if effective is None:
        _crash("Project integrity check failed: cannot enumerate tracked files (not a git checkout?)")
    if effective.strip().lower() != indicated.strip().lower():
        if not state.DEVELOPMENT:
            _crash("Project integrity check failed: local files differ from recorded project hash (illicit interaction?)", {"effective": effective, "indicated": indicated})
        # Development mode: mismatch is expected — skip the remote step.
        state._PROJECT_INTEGRITY_OK = False
        log_warn("Project integrity mismatch in development mode — skipping update check", {"effective": effective, "indicated": indicated})
        return False
    state._PROJECT_INTEGRITY_OK = True
    # Remote step: compare the unsigned locally indicated hash with the
    # unsigned latest-release hash directly.
    try:
        remote = _fetch_remote_project_hash()
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit during update check — assuming no update", {"local": indicated})
        return False
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Update check skipped: offline — assuming no update", {"local": indicated, "error": str(exc)})
            return False
        log_warn("Update check failed — assuming no update", {"local": indicated, "error": str(exc)})
        return False
    if remote is None:
        log_warn("Update check skipped: remote project hash unavailable (offline or no release asset) — assuming no update", {"local": indicated})
        return False
    if remote.strip().lower() == indicated.strip().lower():
        log_info("Project is up to date", {"indicated": indicated})
        return False
    log_warn("Project update available", {"indicated": indicated, "remote": remote})
    return True


def _is_plugin_update_available() -> bool:
    import signing
    import plugin_bridge
    # Local check (always authoritative): the stored plugins-lib hash must be
    # validly signed, then effective vs indicated (hex only) must match.
    stored_path = plugin_bridge._hash_file_path()
    indicated, signed = signing.read_signed_hash_file(stored_path)
    if not signed or not indicated:
        _crash("Plugin library integrity check failed: stored hash has an invalid or missing signature", {"path": str(stored_path)})
    effective = plugin_bridge._compute_plugins_lib_hash()
    if effective.strip().lower() != indicated.strip().lower():
        _crash("Plugin library integrity check failed: folder hash differs from recorded hash (illicit interaction?)", {"effective": effective, "indicated": indicated})
    state._PLUGIN_INTEGRITY_OK = True
    # Remote step: compare the unsigned locally indicated hash with the
    # unsigned latest-commit hash directly.
    try:
        remote = plugin_bridge._fetch_remote_hash()
    except github_api.GithubRateLimitedError:
        log_warn("Plugin library update check skipped: rate limited — assuming no update")
        return False
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Plugin library update check skipped: offline — assuming no update", {"error": str(exc)})
            return False
        log_warn("Plugin update check failed — assuming no update", {"error": str(exc)})
        return False
    if remote is None:
        log_warn("Plugin library update check skipped: remote hash unavailable (offline or no commit) — assuming no update", {"indicated": indicated})
        return False
    if remote.strip().lower() == indicated.strip().lower():
        log_info("Plugin library is up to date", {"indicated": indicated})
        return False
    log_warn("Plugin library update available", {"indicated": indicated, "remote": remote})
    return True


def _perform_plugins_lib_update() -> bool:
    import subprocess
    from pathlib import Path
    process_worker = None
    try:
        try:
            import audio
            fname = audio._read_sound_file("process")
            if fname:
                p = audio.AUDIOS_DIR / fname
                if p.is_file():
                    try:
                        audio.get_audio_orchestrator().start()
                    except Exception:
                        pass
                    process_worker = audio.get_audio_orchestrator().play(p, loop=True)
        except Exception:
            pass
        root = Path(__file__).resolve().parent.parent
        subprocess.run(["git", "fetch", "origin"], cwd=root, capture_output=True, timeout=30)
        proc = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, timeout=30)
        if proc.returncode != 0:
            subprocess.run(["git", "checkout", "main"], cwd=root, capture_output=True, timeout=10)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=root, capture_output=True, timeout=30)
        try:
            import plugin_bridge
            remote_hash = plugin_bridge._fetch_remote_hash()
        except github_api.GithubRateLimitedError:
            remote_hash = None
            log_warn("GitHub rate limit while fetching remote hash after plugin library update — continuing")
        except Exception as exc:
            if _is_offline_error(exc):
                remote_hash = None
                log_warn("Offline while fetching remote hash after plugin library update — continuing", {"error": str(exc)})
            else:
                remote_hash = None
        log_info("Plugin library updated to latest version", {"remote_hash": remote_hash})
        return True
    except Exception as exc:
        log_error("Plugin library update failed", {"error": str(exc)})
        return False
    finally:
        if process_worker is not None:
            try:
                process_worker.stop()
            except Exception:
                pass
            try:
                import audio
                audio.get_audio_orchestrator().reap_finished()
            except Exception:
                pass


def _perform_project_update() -> bool:
    import subprocess
    from pathlib import Path
    process_worker = None
    try:
        try:
            import audio
            fname = audio._read_sound_file("process")
            if fname:
                p = audio.AUDIOS_DIR / fname
                if p.is_file():
                    try:
                        audio.get_audio_orchestrator().start()
                    except Exception:
                        pass
                    process_worker = audio.get_audio_orchestrator().play(p, loop=True)
        except Exception:
            pass
        root = Path(__file__).resolve().parent.parent
        subprocess.run(["git", "fetch", "origin"], cwd=root, capture_output=True, timeout=30)
        proc = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, timeout=30)
        if proc.returncode != 0:
            subprocess.run(["git", "checkout", "main"], cwd=root, capture_output=True, timeout=10)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=root, capture_output=True, timeout=30)
        try:
            remote_hash = _fetch_remote_project_hash()
        except github_api.GithubRateLimitedError:
            remote_hash = None
            log_warn("GitHub rate limit while fetching remote hash after project update — continuing")
        except Exception as exc:
            if _is_offline_error(exc):
                remote_hash = None
                log_warn("Offline while fetching remote hash after project update — continuing", {"error": str(exc)})
            else:
                remote_hash = None
                log_warn("Failed to fetch remote hash after project update", {"error": str(exc)})
        log_info("Project updated to latest version", {"remote_hash": remote_hash})
        return True
    except Exception as exc:
        log_error("Project update failed", {"error": str(exc)})
        return False
    finally:
        if process_worker is not None:
            try:
                process_worker.stop()
            except Exception:
                pass
            try:
                import audio
                audio.get_audio_orchestrator().reap_finished()
            except Exception:
                pass
