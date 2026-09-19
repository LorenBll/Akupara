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
    """Return True when *exc* looks like an offline / network failure."""
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


def _get_version_tags() -> list[str]:
    """Return semantic-version tags on origin sorted ascending (e.g. ['v1.0.0', ...])."""
    try:
        root = Path(__file__).resolve().parent.parent
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", "origin"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode("utf-8", errors="replace")
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Version tags check skipped: offline or network unreachable", {"error": str(exc)})
        else:
            log_warn("Version tags check failed", {"error": str(exc)})
        return []
    versions: list[tuple[tuple[int, ...], str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):].removesuffix("^{}")
        m = re.match(r"^v(\d+(?:\.\d+)*)$", name)
        if not m:
            continue
        key = tuple(int(part) for part in m.group(1).split("."))
        versions.append((key, name))
    if not versions:
        return []
    versions.sort(key=lambda item: item[0])
    tags: list[str] = []
    for _, name in versions:
        if name not in tags:
            tags.append(name)
    return tags


def _get_latest_version_tag() -> str | None:
    tags = _get_version_tags()
    return tags[-1] if tags else None


def _fetch_remote_project_hash(timeout: int = 8) -> str | None:
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
        if _is_offline_error(exc):
            log_warn("Remote project hash check skipped: offline", {"url": url, "error": str(exc)})
        else:
            log_warn("Remote project hash fetch failed", {"url": url, "error": str(exc)})
        # Fall through to fallback raw tag path
        pass
    tag = _get_latest_version_tag()
    if not tag:
        if _is_offline_error(Exception("no tag")):
            pass
        return None
    url = f"https://raw.githubusercontent.com/LorenBll/Akupara/{tag}/hash"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with github_api.github_urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()[0]
            if data:
                return data
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit exceeded while fetching remote project hash (fallback) — skipping version check", {"url": url})
        raise
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Remote project hash fallback check skipped: offline", {"url": url, "error": str(exc)})
        else:
            log_warn("Remote project hash fallback fetch failed", {"url": url, "error": str(exc)})
        return None
    return None


def _fetch_release_hash_for_tag(tag: str, timeout: int = 8) -> str | None:
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    url = f"https://raw.githubusercontent.com/LorenBll/Akupara/{tag}/hash"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with github_api.github_urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip().split()
            if data:
                return data[0]
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit exceeded while fetching release hash for tag — skipping version check", {"tag": tag, "url": url})
        raise
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Release hash for tag check skipped: offline", {"tag": tag, "url": url, "error": str(exc)})
        else:
            log_warn("Release hash for tag fetch failed", {"tag": tag, "url": url, "error": str(exc)})
        return None
    return None


def _is_known_release_hash(local_hash: str | None) -> bool:
    local = (local_hash or "").strip().lower()
    if not local:
        return False
    try:
        latest = _fetch_remote_project_hash()
    except github_api.GithubRateLimitedError:
        log_warn("GitHub rate limit during known-release check — assuming known to keep running", {"local": local})
        return True
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Known-release check skipped: offline — assuming known to keep running", {"local": local, "error": str(exc)})
            return True
        log_warn("Known-release check failed — assuming known to keep running", {"local": local, "error": str(exc)})
        return True
    if latest is None:
        log_warn("Known-release check skipped: remote unavailable (offline?) — assuming known to keep running", {"local": local})
        return True
    if latest and latest.strip().lower() == local:
        return True
    for tag in reversed(_get_version_tags()):
        try:
            known = _fetch_release_hash_for_tag(tag)
        except github_api.GithubRateLimitedError:
            log_warn("GitHub rate limit during known-release tag fetch — assuming known to keep running", {"tag": tag})
            return True
        except Exception as exc:
            if _is_offline_error(exc):
                log_warn("Known-release tag check skipped: offline — assuming known to keep running", {"tag": tag, "error": str(exc)})
                return True
            log_warn("Known-release tag check failed — assuming known to keep running", {"tag": tag, "error": str(exc)})
            return True
        if known is None:
            # Offline or missing — skip this tag, try next
            continue
        if known and known.strip().lower() == local:
            return True
    return False


def _is_update_available() -> bool:
    # Integrity: effective vs indicated before update check (local, always authoritative)
    from integrity import _compute_local_project_hash, _get_local_project_hash
    effective = _compute_local_project_hash()
    indicated = _get_local_project_hash()
    if effective and indicated and effective.strip().lower() != indicated.strip().lower():
        state._PROJECT_INTEGRITY_OK = False
        log_error("Project integrity check failed before update check", {"effective": effective, "indicated": indicated})
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
        if not state.DEVELOPMENT:
            return False
        return False
    else:
        state._PROJECT_INTEGRITY_OK = True
    # Compare the locally *indicated* hash (already signature-verified at
    # startup) with the remote hash directly — the remote hash is unsigned.
    local = indicated or effective
    if not local:
        try:
            from integrity import _get_local_project_hash as _glph
            local = _glph()
        except Exception:
            local = None
        if not local:
            return False
    try:
        remote = _fetch_remote_project_hash()
    except github_api.GithubRateLimitedError:
        log_warn("GitHub API rate limit during update check — assuming no update", {"local": local})
        return False
    except Exception as exc:
        if _is_offline_error(exc):
            log_warn("Update check skipped: offline — assuming no update", {"local": local, "error": str(exc)})
            return False
        log_warn("Update check failed — assuming no update", {"local": local, "error": str(exc)})
        return False
    if remote is None:
        log_warn("Update check skipped: remote unavailable (offline?) — assuming no update", {"local": local})
        return False
    if not local or not remote:
        return False
    return local.strip().lower() != remote.strip().lower()


def _is_plugin_update_available() -> bool:
    import plugin_bridge
    # Integrity: effective vs indicated before update check (always mandatory, even in development)
    try:
        import plugin_bridge
        effective = plugin_bridge._compute_plugins_lib_hash()
        indicated = plugin_bridge._read_stored_hash()
    except Exception:
        return False
    if effective and indicated and effective.strip().lower() != indicated.strip().lower():
        state._PLUGIN_INTEGRITY_OK = False
        log_error("Plugin library integrity check failed before update check", {"effective": effective, "indicated": indicated})
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
        try:
            import plugin_bridge
            plugin_bridge.get_plugin_bridge().stop()
        except Exception:
            pass
    else:
        state._PLUGIN_INTEGRITY_OK = True
    # Compare the locally *indicated* hash (already signature-verified by the
    # loader) with the remote (latest commit) hash directly.
    local = indicated or effective
    try:
        import plugin_bridge
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
        log_warn("Plugin library update check skipped: remote unavailable (offline?) — assuming no update")
        return False
    if not local or not remote:
        return False
    return local.strip().lower() != remote.strip().lower()


def _get_local_plugins_lib_hash() -> str | None:
    try:
        import plugin_bridge
        return plugin_bridge._read_stored_hash()
    except Exception:
        return None


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
