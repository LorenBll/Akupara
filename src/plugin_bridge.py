"""Plugin bridge — mediates between Akupara and plugins.

The ``resources/plugins-lib`` JSON catalog (hash-range files, full-space
``<lowest>-<highest>.json`` while small) is the authoritative library of
*all* plugins that *can* be installed. An adjacent ``reverse-index.json``
optimises research: a mapping ``word -> [plugin hash, …]`` (stored as a
dict; a list of ``{word, hashes}`` entries is also accepted for backward
compat) that associates query tokens with plugin IDs that should be shown
for that token. This module owns its research (``_load_plugins`` /
``_search_plugins`` / reverse-index helpers) and its **loader** integrity
checks (``PluginBridge.start``) and trust verification
(``verify_plugin_signature``). No plugin *handling* operations
(download / install / load / start / stop of an installed plugin) beyond
verification are exposed yet.

Naming: this unit is called **plugin bridge**, not plugin loader / handler.
It follows the worker naming standard (``start``/``stop`` idempotent).

NOTE — hash-identified plugin loading order (trust before hash comparison):
When a plugin is loaded by its ``hash`` (catalog ID), the bridge MUST first
check that the plugin entry with that ``hash`` in ``resources/plugins-lib``
has a valid trust mark (``verify_plugin_signature(entry) is True``) BEFORE
comparing the local hash (``get_local_plugin_hash(hash)``) against the
remote hash provided by the repository release (``get_remote_plugin_hash(hash)``).
Only if the trust mark is valid should the hash comparison proceed; otherwise
loading must be refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import regex
import ssl
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from logginglib import log_error, log_info, log_warn


# Remote hash for integrity check — latest commit on the Akupara repository
_REPO = "LorenBll/Akupara"
_REMOTE_COMMITS_URL = f"https://api.github.com/repos/{_REPO}/commits/main"
_REMOTE_HASH_URL = f"https://raw.githubusercontent.com/{_REPO}/{{commit}}/resources/plugins-lib/hash"

def _plugins_lib_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "plugins-lib"

def _hash_file_path() -> Path:
    return _plugins_lib_dir() / "hash"

def _gpg_pubkey_path() -> Path:
    return _plugins_lib_dir() / "lorenbll-akupara-pub"

def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())

def _compute_plugins_lib_hash() -> str:
    """Hash all files in plugins-lib except the hash file itself (sorted, merged)."""
    base = _plugins_lib_dir()
    hash_path = _hash_file_path()
    if not base.is_dir():
        return _hash_bytes(b"")
    entries: list[tuple[str, str]] = []
    for p in sorted(base.iterdir(), key=lambda x: x.name):
        if not p.is_file():
            continue
        try:
            # Exclude hash file itself (resolve + name check)
            if p.resolve() == hash_path.resolve():
                continue
        except Exception:
            pass
        if p.name == hash_path.name:
            continue
        try:
            h = _hash_file(p)
        except OSError:
            continue
        entries.append((p.name, h))
    merged = "".join(h for _, h in sorted(entries))
    return _hash_bytes(merged.encode("utf-8"))

def _read_stored_hash() -> str | None:
    p = _hash_file_path()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None

def _fetch_latest_commit_sha(timeout: int = 8) -> str | None:
    """Resolve the SHA of the latest commit on the Akupara repository (main)."""
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(
            _REMOTE_COMMITS_URL,
            headers={"User-Agent": "Akupara/1.0", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            sha = data.get("sha")
            return sha.strip() if isinstance(sha, str) and sha.strip() else None
    except Exception:
        return None


def _fetch_remote_hash(timeout: int = 8) -> str | None:
    """Fetch the plugins-lib hash file from the latest commit on the Akupara repository."""
    ctx = ssl.create_default_context()
    commit = _fetch_latest_commit_sha(timeout)
    if commit is None:
        return None
    url = _REMOTE_HASH_URL.format(commit=commit)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip()
            if data:
                return data.split()[0].strip()
            return ""
    except Exception:
        return None

def _play_error_sound():
    try:
        import audio
        try:
            audio.get_audio_orchestrator().start()
        except Exception:
            pass
        audio.play_sound("error")
    except Exception:
        pass

def _play_warn_sound():
    try:
        import audio
        try:
            audio.get_audio_orchestrator().start()
        except Exception:
            pass
        audio.play_sound("warn")
    except Exception:
        pass

class PluginBridge:
    """Bridge between Akupara and installed plugins — loader + research.

    Follows the worker naming standard (``start``/``stop`` idempotent).
    ``start`` immediately verifies plugins-lib integrity by comparing the
    recomputed local folder hash against the ``hash`` file in the latest commit
    on the Akupara repository — mismatch plays **error** sound and the loader
    stays stopped. Offline (remote unavailable) falls back to comparing the
    recomputed folder hash against the local ``hash`` file.
    This guards against illicit interaction with the ``plugins-lib`` folder.
    """

    def __init__(self) -> None:
        self._started: bool = False

    def start(self) -> None:
        """Start the bridge — immediately verifies plugins-lib integrity.

        Idempotent. Two mandatory validations, always enforced even in
        development mode:
          1) effective hash (computed folder hash) == indicated hash (stored ``hash`` file)
          2) indicated hash == hash indicated in the latest commit on the Akupara repository
        Mismatch on either → error sound and stays stopped; plugin card/page disabled.
        These checks happen before any update check. Offline (remote unavailable)
        skips the second check with a warning and falls back to the first.
        """
        if self._started:
            return
        # Ensure we start from stopped
        self._started = False

        # --- Integrity 1: effective vs indicated (always mandatory) ---
        computed = _compute_plugins_lib_hash()
        stored = _read_stored_hash()
        if stored is None or not stored:
            log_error("Plugin loader failed: local hash file missing", {"path": str(_hash_file_path())})
            _play_error_sound()
            return
        if computed.strip().lower() != stored.strip().lower():
            log_error("Plugin loader failed: plugins-lib folder hash mismatch (illicit interaction?)", {"computed": computed, "stored": stored})
            _play_error_sound()
            return

        # --- Integrity 2: indicated vs latest commit (always mandatory when online) ---
        remote = _fetch_remote_hash()
        if remote is None:
            from logginglib import log_warn
            log_warn("Plugin loader: remote hash unavailable — skipping authoritative check (offline?)", {"commits_url": _REMOTE_COMMITS_URL, "hash_url": _REMOTE_HASH_URL, "indicated": stored})
        elif stored.strip().lower() != remote.strip().lower():
            log_error("Plugin loader failed: indicated plugins-lib hash differs from latest commit", {"indicated": stored, "remote": remote, "commits_url": _REMOTE_COMMITS_URL})
            _play_error_sound()
            return

        # All checks passed — loader is started
        self._started = True
        log_info("Plugin loader started", {"hash": computed})

    def stop(self) -> None:
        """Stop the bridge (idempotent)."""
        self._started = False

    def is_started(self) -> bool:
        """Return whether the bridge has been started (integrity checks passed)."""
        return self._started

    def discover_installed_plugins(self, development: bool = False) -> list[Path]:
        """Discover installed plugin folders per execution tree steps 6 (defined part only).

        Lists ``plugins/`` subfolders (folder name is the ``hash``):
        - ``dev-*`` → loaded without checks only if ``development`` is True, else skipped.
        - Other folders → ``hash`` is folder name (identifies library entry via ``hash``),
          version from that entry indicates which GitHub release to check. Checks:
          1) trust (before any hash), 2) computed folder hash vs ``hash`` file,
          3) hash from version-specific GitHub release vs local,
          4) local hash vs latest release hash (deduplicated if same release),
             then manifest ``akupara_version`` vs local Akupara hash,
             then latest hash vs local → upgrade available.

        Returns list of ``Path`` that passed all defined checks.

        NOTE: This is where the loading of the plugins should happen — actual
        plugin execution (how plugins are implemented) is still to be defined,
        so this method stops before loading and returns the verified paths.
        """
        plugins_dir = _plugins_dir()
        if not plugins_dir.is_dir():
            return []
        verified: list[Path] = []
        # Cache local Akupara version hash for upgrade checks
        local_akupara_version = _get_local_akupara_version()
        for entry in sorted(plugins_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            folder_name = entry.name
            # dev- folders: load without checks only if development
            if folder_name.startswith("dev-"):
                if not development:
                    log_info("Skipping dev plugin (development off)", {"folder": folder_name})
                    continue
                log_info("Dev plugin (no checks, development on)", {"folder": folder_name})
                # NOTE: plugin loading should happen here for dev plugins (without checks)
                verified.append(entry)
                continue
            # Regular plugins: folder name IS the hash (identifies library entry)
            hash_value = folder_name.strip()
            # Validate hash format (64 hex)
            if not re.fullmatch(r"[0-9a-fA-F]{64}", hash_value):
                # Fallback: try reading hash file inside (legacy)
                alt = _read_plugin_hash_file(entry)
                if alt and re.fullmatch(r"[0-9a-fA-F]{64}", alt.strip()):
                    hash_value = alt.strip()
                else:
                    log_warn("Plugin folder name not a valid hash and no hash file, skipping", {"folder": folder_name})
                    continue
            # Find catalog entry for this hash to verify trust and get version/repo
            catalog_entry = None
            for e in _load_plugins():
                if str(e.get("hash", "")).strip().lower() == hash_value.lower():
                    catalog_entry = e
                    break
            if catalog_entry is None:
                log_warn("Plugin hash not in library, skipping", {"folder": folder_name, "hash": hash_value})
                continue
            if not verify_plugin_signature(catalog_entry):
                log_warn("Plugin trust mark invalid, skipping", {"folder": folder_name, "hash": hash_value})
                continue
            # Compute folder hash (hash each non-hash file, sorted, concat, hash) vs hash file
            computed = _compute_plugin_folder_hash(entry)
            if computed is None:
                log_warn("Failed to compute plugin folder hash, skipping", {"folder": folder_name})
                continue
            stored_hash = _read_plugin_hash_file(entry)
            # If no hash file, use folder name as stored_hash for comparison
            if stored_hash is None:
                stored_hash = hash_value
            if computed.lower() != stored_hash.lower():
                log_warn("Plugin folder hash mismatch, skipping", {"folder": folder_name, "computed": computed, "stored": stored_hash})
                continue
            # --- Check 1: version-specific GitHub release hash vs local ---
            plugin_name = str(catalog_entry.get("name", "")).strip()
            plugin_version = str(catalog_entry.get("version", "")).strip()
            repo_url = str(catalog_entry.get("repo", "")).strip()
            parsed = _parse_github_repo(repo_url)
            if parsed is None:
                log_warn("Invalid repo URL for plugin, skipping", {"folder": folder_name, "repo": repo_url})
                continue
            owner, repo = parsed
            # Fetch version-specific release (e.g. tag v{version} or {version})
            version_release = _fetch_github_release_by_tag(owner, repo, plugin_version)
            if version_release is None:
                # Fallback to latest if tag not found (for backward compat)
                version_release = _fetch_github_latest_release(owner, repo)
                if version_release is None:
                    log_warn("Failed to fetch version-specific release and latest, skipping", {"folder": folder_name, "version": plugin_version})
                    continue
            # Find hash attachment in that version release
            version_assets = version_release.get("assets", []) if isinstance(version_release.get("assets"), list) else []
            version_hash_asset = _find_hash_asset(version_assets)
            if version_hash_asset is None:
                log_warn("No hash attachment in version release, skipping", {"folder": folder_name, "version": plugin_version})
                continue
            version_hash_url = version_hash_asset.get("browser_download_url") or version_hash_asset.get("url")
            if not isinstance(version_hash_url, str) or not version_hash_url.strip():
                log_warn("Hash attachment has no URL, skipping", {"folder": folder_name})
                continue
            version_remote_hash = _fetch_url_text(version_hash_url.strip())
            if not version_remote_hash:
                log_warn("Failed to fetch version hash attachment or empty, skipping", {"folder": folder_name})
                continue
            version_remote_hash = version_remote_hash.split()[0].strip()
            # Compare version release hash vs local (stored_hash/computed)
            if version_remote_hash.lower() != stored_hash.lower():
                log_warn("Plugin version hash mismatch (remote vs local), skipping", {"folder": folder_name, "remote": version_remote_hash, "local": stored_hash})
                continue
            # --- Check 2: latest release hash vs local (deduplicated if same release) ---
            latest_release = _fetch_github_latest_release(owner, repo)
            if latest_release is None:
                log_warn("Failed to fetch latest release, skipping upgrade check but plugin passed", {"folder": folder_name})
                log_info("Plugin passed all checks", {"folder": folder_name, "hash": hash_value})
                verified.append(entry)
                continue
            # Check if latest release is same as version release (by tag or id)
            version_tag = version_release.get("tag_name", "") or version_release.get("tag", "")
            latest_tag = latest_release.get("tag_name", "") or latest_release.get("tag", "")
            if version_tag and latest_tag and str(version_tag).strip().lower() == str(latest_tag).strip().lower():
                # Same release, already checked, don't repeat
                log_info("Plugin is up to date (latest is same as installed version)", {"folder": folder_name, "version": plugin_version})
                verified.append(entry)
                continue
            # Also check if latest hash is same as version hash (defensive)
            latest_assets = latest_release.get("assets", []) if isinstance(latest_release.get("assets"), list) else []
            latest_hash_asset = _find_hash_asset(latest_assets)
            if latest_hash_asset is None:
                log_warn("Latest release has no hash attachment, skipping upgrade check but plugin passed", {"folder": folder_name})
                verified.append(entry)
                continue
            latest_hash_url = latest_hash_asset.get("browser_download_url") or latest_hash_asset.get("url")
            if not isinstance(latest_hash_url, str) or not latest_hash_url.strip():
                log_warn("Latest hash attachment has no URL", {"folder": folder_name})
                verified.append(entry)
                continue
            latest_hash = _fetch_url_text(latest_hash_url.strip())
            if not latest_hash:
                log_warn("Failed to fetch latest hash attachment", {"folder": folder_name})
                verified.append(entry)
                continue
            latest_hash = latest_hash.split()[0].strip()
            # If latest hash is same as version hash and we already passed, don't repeat (already handled by tag check)
            if latest_hash.lower() == version_remote_hash.lower():
                log_info("Plugin is up to date (latest hash same as installed)", {"folder": folder_name})
                verified.append(entry)
                continue
            # Now check manifest akupara_version
            manifest_akupara = _fetch_manifest_akupara_version(owner, repo, latest_release)
            if manifest_akupara is None:
                log_warn("Latest release has no manifest.json or no akupara_version, skipping upgrade check", {"folder": folder_name})
                verified.append(entry)
                continue
            if local_akupara_version is None or manifest_akupara.strip().lower() != local_akupara_version.strip().lower():
                log_info("Akupara version mismatch, not upgradeable (manifest vs local)", {"folder": folder_name, "manifest": manifest_akupara, "local": local_akupara_version})
                verified.append(entry)
                continue
            # Akupara versions match, now check local plugin hash (computed) vs latest hash
            if computed.lower() == latest_hash.lower():
                log_info("Plugin is up to date (local hash equals latest)", {"folder": folder_name})
                verified.append(entry)
                continue
            # Upgrade available!
            log_info("Plugin has available upgrade", {"folder": folder_name, "installed": plugin_version, "latest_hash": latest_hash, "latest_tag": latest_tag})
            if _is_automatic_plugin_upgrade_enabled():
                log_info("Automatic plugin upgrade enabled — upgrading", {"folder": folder_name, "old_hash": hash_value, "new_hash": latest_hash, "latest_tag": latest_tag})
                # Automatic upgrade: delete old folder and create new one named as new hash with new version's files
                try:
                    # Find the new catalog entry for the latest version to get its hash (should be latest_hash)
                    # The latest_hash is the hash of the new version's files (as per release)
                    # Download all non-hash, non-manifest assets from latest release
                    new_folder = _plugins_dir() / latest_hash
                    if new_folder.exists():
                        log_warn("New plugin folder already exists, skipping upgrade", {"folder": str(new_folder)})
                    else:
                        # Collect assets to download (exclude hash and manifest)
                        assets_to_download = []
                        for a in latest_assets:
                            if not isinstance(a, dict):
                                continue
                            n = a.get("name")
                            if not isinstance(n, str) or not n.strip():
                                continue
                            low = n.strip().lower()
                            if low in ("hash", "hash.txt") or low.endswith(".hash") or low == "manifest.json":
                                continue
                            url = a.get("browser_download_url") or a.get("url")
                            if isinstance(url, str) and url.strip():
                                assets_to_download.append((n.strip(), url.strip()))
                        # Create new folder
                        new_folder.mkdir(parents=True, exist_ok=True)
                        # Download each asset
                        for fname, url in assets_to_download:
                            # Sanitize filename
                            if "/" in fname or "\\" in fname or fname in (".", ".."):
                                log_warn("Invalid asset filename, skipping", {"fname": fname})
                                continue
                            data = _fetch_url_bytes(url)
                            if data is None:
                                log_warn("Failed to download asset for upgrade, skipping file", {"fname": fname, "url": url})
                                continue
                            (new_folder / fname).write_bytes(data)
                        # Create hash file inside new folder
                        # Use "hash" as filename (consistent with _read_plugin_hash_file)
                        (new_folder / "hash").write_text(latest_hash + "\n", encoding="utf-8")
                        log_info("Plugin upgraded: old folder kept for now, new folder created", {"old": str(entry), "new": str(new_folder)})
                        # Delete old folder
                        import shutil
                        try:
                            shutil.rmtree(entry)
                            log_info("Deleted old plugin folder after upgrade", {"old": str(entry)})
                        except Exception as exc:
                            log_warn("Failed to delete old plugin folder after upgrade", {"old": str(entry), "error": str(exc)})
                        # Verified is now the new folder (which will be discovered on next run, but also add it now)
                        verified.append(new_folder)
                        continue  # Skip adding old entry, new one will be verified next time
                except Exception as exc:
                    log_warn("Automatic plugin upgrade failed", {"folder": folder_name, "error": str(exc)})
                    # Fall back to keeping old
                    import traceback
                    log_warn("Upgrade exception", {"trace": traceback.format_exc()})
            else:
                log_info("Automatic plugin upgrade disabled — not upgrading", {"folder": folder_name})
            verified.append(entry)
        return verified


_REVERSE_INDEX_NAME = "reverse-index.json"


def _read_plugin_hash_file(plugin_path: Path) -> str | None:
    """Read the ``hash`` file inside a plugin folder (stripped first token)."""
    for fname in ("hash", "hash.txt"):
        p = plugin_path / fname
        if p.is_file():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace").strip()
                if txt:
                    return txt.split()[0].strip()
            except OSError:
                continue
    # Fallback: any *.hash
    for p in plugin_path.iterdir():
        if p.is_file() and p.name.lower().endswith(".hash"):
            try:
                txt = p.read_text(encoding="utf-8", errors="replace").strip()
                if txt:
                    return txt.split()[0].strip()
            except OSError:
                continue
    return None


def _compute_plugin_folder_hash(plugin_path: Path) -> str | None:
    """Hash a plugin folder (each non-hash file individually, sorted, concat, hash).

    Excludes the ``hash`` file itself (hash/hash.txt/*.hash). Returns hex or None on error.
    """
    if not plugin_path.is_dir():
        return None
    per_file: list[tuple[str, str]] = []
    for p in sorted(plugin_path.iterdir(), key=lambda x: x.name):
        if not p.is_file():
            continue
        # Exclude hash file(s)
        if p.name.lower() in ("hash", "hash.txt") or p.name.lower().endswith(".hash"):
            continue
        try:
            h = _hash_file(p)
        except OSError:
            continue
        per_file.append((p.name, h))
    # If no files (only hash), hash of empty
    if not per_file:
        return _hash_bytes(b"")
    per_file.sort(key=lambda x: x[0])
    merged = "".join(h for _, h in per_file)
    # Also sort by hash as secondary (already sorted by name, but spec said sorted hashes)
    # Use sorted hashes for final merge as per previous folder hash pattern (sorted by file hash)
    # For plugin folder, sort by file name is more intuitive; but we also sort hashes
    # To match execution tree: "each hashed singularly, and then the hashes are hashed together" — implies sorted hashes
    hashes_sorted = sorted(h for _, h in per_file)
    merged_sorted = "".join(hashes_sorted)
    return _hash_bytes(merged_sorted.encode("utf-8"))


def _reverse_index_path() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "plugins-lib" / _REVERSE_INDEX_NAME


def _load_reverse_index() -> dict[str, list[str]]:
    """Load ``reverse-index.json`` as ``{word: [plugin hash, ...]}``.

    The file is intentionally separate from the hash-range catalog. Each key
    is a lower-cased query token; each value is the list of plugin ``hash``
    values that should be surfaced for that token. Both the canonical
    ``{word: [hash]}`` dict and the legacy ``[{word, hashes/hashes}]`` list
    forms are accepted; invalid entries are ignored. Returns ``{}`` when the
    file is missing or malformed. No handling of installed plugins is
    performed here.
    """
    path = _reverse_index_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log_warn("Reverse index contains invalid JSON", {"path": str(path), "error": str(exc)})
        return {}
    # Canonical form: dict word -> list
    if isinstance(data, dict):
        index: dict[str, list[str]] = {}
        for word, hashes in data.items():
            if not isinstance(word, str) or not word.strip():
                continue
            if not isinstance(hashes, list):
                continue
            cleaned = [str(h) for h in hashes if isinstance(h, (str, int, float)) and str(h).strip()]
            if cleaned:
                index[word.strip().lower()] = cleaned
            elif word.strip().lower() not in index:
                # keep empty list for explicit word with no plugins
                index[word.strip().lower()] = []
        return index
    # Legacy form: list of {word, hashes} / {word, hash} / {word, plugins}
    if isinstance(data, list):
        index = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            word = entry.get("word") or entry.get("term") or entry.get("key")
            hashes = entry.get("hashes") or entry.get("hash") or entry.get("plugins") or entry.get("ids") or []
            if not isinstance(word, str) or not word.strip():
                continue
            if isinstance(hashes, str):
                hashes = [hashes]
            if not isinstance(hashes, list):
                continue
            cleaned = [str(h) for h in hashes if isinstance(h, (str, int, float)) and str(h).strip()]
            index[word.strip().lower()] = cleaned
        return index
    log_warn("Reverse index has unexpected format", {"path": str(path)})
    return {}


def _save_reverse_index(index: dict[str, list[str]]) -> None:
    """Persist ``index`` to ``reverse-index.json`` (internal helper).

    Normalises keys to lower-case and values to string lists. Not exposed
    as handling yet — kept internal for future incremental use.
    """
    path = _reverse_index_path()
    # Normalise
    normalised = {str(k).strip().lower(): [str(h) for h in v if str(h).strip()] for k, v in index.items() if str(k).strip()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalised, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _load_plugins() -> list[dict]:
    """Load the plugins catalog from hash-range JSON files (library layer).

    The ``resources/plugins-lib`` directory stores one JSON file per hash
    interval, named ``<low>-<high>.json``. Each file contains a list of
    plugin objects with the keys: ``hash`` (non-secret per-version hash,
    regenerated at every version change, determines file), ``name`` (plugin ID,
    unique case-insensitive), ``description``, ``repo`` (GitHub URL),
    ``version`` (plugin version string, e.g. ``"1.0.0"``, after ``repo``),
    ``akupara_version`` (Akupara version the plugin is for, e.g. ``"1.0.0"``,
    after ``version``) and ``trust_mark`` (armored detached GPG signature,
    ``""`` when absent). Each version change adds a **new entry** with a new
    ``hash``; division between files is still based on ``hash``.
    ``reverse_index_keys`` is **not** stored in the hash-range file — it lives
    only in ``reverse-index.json`` (word -> [hash]) and is calculated via
    ``_load_reverse_index``/``keys_for_hash``. While small a single
    full-space file is used.
    No plugin handling (download/start) is performed here — this is catalog research.

    ``reverse-index.json`` and ``lorenbll-akupara-pub`` are explicitly excluded
    from this scan.
    """
    base_dir = Path(__file__).resolve().parent.parent / "resources" / "plugins-lib"
    plugins: list[dict] = []

    # Primary: scan plugins-lib for hash-range files (any *.json except reverse-index)
    if base_dir.is_dir():
        json_files = sorted(p for p in base_dir.glob("*.json") if p.name != _REVERSE_INDEX_NAME)
        if json_files:
            for json_path in json_files:
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError) as exc:
                    log_warn("Plugins file contains invalid JSON", {"path": str(json_path), "error": str(exc)})
                    continue
                if isinstance(data, list):
                    plugins.extend(entry for entry in data if isinstance(entry, dict))
                else:
                    log_warn("Plugins file has unexpected format", {"path": str(json_path)})
            return plugins
        # No hash-range files; if the directory already uses the new layout
        # (contains reverse-index.json or other json), treat as empty catalog
        # rather than falling back to legacy single-file locations.
        if any(base_dir.glob("*.json")):
            return plugins

    # Fallback: legacy single-file locations (backward compatibility)
    candidates = [
        base_dir / "plugin-repositories.json",
        base_dir / "plugins-repositories.json",
        Path(__file__).resolve().parent.parent / "resources" / "plugin-repositories.json",
        Path(__file__).resolve().parent.parent / "resources" / "plugins-repositories.json",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log_warn("Plugins file contains invalid JSON", {"path": str(candidate), "error": str(exc)})
            continue
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        log_warn("Plugins file has unexpected format", {"path": str(candidate)})
        return []
    return plugins


_MAX_SEARCH_PATTERN_LENGTH = 100

_REGEX_SEARCH_TIMEOUT = 1.0


def _search_plugins(pattern: str) -> list[dict]:
    """Return plugins matching the regex ``pattern`` (logic layer, catalog research).

    Research is driven by ``reverse-index.json`` when possible: the pattern
    is tokenised into ``\\w+`` words (lower-cased) and each token is looked
    up in the reverse index (``word -> [hash]``). The union of hashes for
    all tokens forms the candidate set. Candidates are then filtered by the
    regex (applied to each plugin's field values and its JSON dump,
    case-insensitive). When the reverse index yields no candidates (empty
    index, no token hit, or pattern with no word tokens) the search falls
    back to a full catalog regex scan, so arbitrary regexes (``.*``, ``foo|bar``,
    etc.) remain supported. Raises ValueError for a too-long or invalid regex;
    the raised message is generic and never embeds the regex engine's error.
    No handling of installed plugins is performed.
    """
    if not isinstance(pattern, str):
        raise ValueError("Invalid request.")
    pattern = pattern.strip()
    if len(pattern) > _MAX_SEARCH_PATTERN_LENGTH:
        raise ValueError("Invalid request.")
    plugins = _load_plugins()
    if pattern == "":
        return plugins
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE)
    except regex.error:
        raise ValueError("Invalid request.") from None

    def _regex_match(plugin: dict) -> bool:
        searchable_values = " ".join(str(v) for v in plugin.values() if isinstance(v, (str, int, float)))
        searchable_json = json.dumps(plugin, ensure_ascii=False)
        try:
            return bool(
                compiled.search(searchable_values, timeout=_REGEX_SEARCH_TIMEOUT)
                or compiled.search(searchable_json, timeout=_REGEX_SEARCH_TIMEOUT)
            )
        except regex.TimeoutError:
            raise ValueError("Invalid request.") from None

    # Use reverse index to narrow candidates, but keep regex as final filter
    index = _load_reverse_index()
    if index:
        tokens = re.findall(r"\w+", pattern.lower())
        candidate_hashes: set[str] = set()
        for tok in tokens:
            if tok in index:
                candidate_hashes.update(str(h) for h in index[tok] if str(h).strip())
        if candidate_hashes:
            # Hash is the canonical plugin ID; fall back to str(plugin) if missing
            narrowed = [p for p in plugins if str(p.get("hash", "")).strip() in candidate_hashes]
            # Still apply regex so research supports both index + regex
            return [p for p in narrowed if _regex_match(p)]

    # Fallback: full catalog regex scan (index miss / empty index / no word tokens)
    return [p for p in plugins if _regex_match(p)]


def _get_trust_signature(entry: dict) -> str:
    """Extract the armored trust_mark signature from a plugin entry."""
    if not isinstance(entry, dict):
        return ""

    value = entry.get("trust_mark")

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        signature = value.get("signature")
        return signature.strip() if isinstance(signature, str) else ""

    return ""


def _keys_for_hash(plugin_hash: str) -> list[str]:
    """Return reverse-index keys for hash (computed live from reverse-index.json)."""
    idx = _load_reverse_index()
    ph = str(plugin_hash).strip()
    return sorted([k for k, hs in idx.items() if ph in hs])


def _build_trust_data(plugin: dict) -> dict:
    """Build canonical trust data for a plugin (hash, name, description, repo, version, akupara_version, reverse_index_keys)."""
    ph = str(plugin.get("hash", "")).strip()
    return {
        "hash": ph,
        "name": str(plugin.get("name", "")),
        "description": str(plugin.get("description", "")),
        "repo": str(plugin.get("repo", "") or plugin.get("link", "")),
        "version": str(plugin.get("version", "")),
        "akupara_version": str(plugin.get("akupara_version", "")),
        "reverse_index_keys": _keys_for_hash(ph),
    }


def _ensure_pubkey_imported() -> None:
    """Ensure the Akupara public key is in the GPG keyring (best-effort)."""
    pub = _gpg_pubkey_path()
    if not pub.is_file():
        return
    # Try to import; ignore errors (already imported)
    for gpg in ["gpg", r"C:\Program Files\GnuPG\bin\gpg.exe", r"C:\Program Files (x86)\GnuPG\bin\gpg.exe"]:
        try:
            subprocess.run([gpg, "--import", str(pub)], capture_output=True, timeout=5)
            return
        except Exception:
            continue


def verify_plugin_signature(plugin: dict) -> bool:
    """Verify whether a plugin's ``trust_mark`` signature is valid.

    Rebuilds the canonical JSON ``{"hash","name","description","repo","version","akupara_version","reverse_index_keys"}``
    (reverse keys computed live from ``reverse-index.json``), canonicalises with
    ``sort_keys=True, separators=(",", ":")``, then:
      * if signature is 64-char hex (fallback), compares SHA-256 hex
      * else uses ``gpg --verify`` with the public key
        ``resources/plugins-lib/lorenbll-akupara-pub``.

    Returns ``True`` iff the signature is present and cryptographically valid.
    ``trust_mark == ""`` or missing → ``False``.
    """
    sig = _get_trust_signature(plugin)
    if not sig:
        return False
    data = _build_trust_data(plugin)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # Fallback SHA-256 case (gpg not available when signing)
    if re.fullmatch(r"[0-9a-fA-F]{64}", sig.strip()):
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return sig.strip().lower() == expected.lower()

    # Ensure public key is available for verification
    _ensure_pubkey_imported()

    # GPG detached verification: write data and sig to temp files
    gpg_candidates = ["gpg", r"C:\Program Files\GnuPG\bin\gpg.exe", r"C:\Program Files (x86)\GnuPG\bin\gpg.exe"]
    for gpg in gpg_candidates:
        try:
            with tempfile.TemporaryDirectory() as td:
                td_p = Path(td)
                data_path = td_p / "data.json"
                sig_path = td_p / "sig.asc"
                data_path.write_text(canonical, encoding="utf-8")
                sig_path.write_text(sig, encoding="utf-8")
                # gpg --verify sig data
                proc = subprocess.run([gpg, "--verify", str(sig_path), str(data_path)], capture_output=True, timeout=10)
                # gpg returns 0 on good signature
                if proc.returncode == 0:
                    return True
                # Also try with --status-fd
                # If failed, try next gpg candidate
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False


def _plugins_dir() -> Path:
    """Return the local ``plugins/`` installation folder (project root)."""
    return Path(__file__).resolve().parent.parent / "plugins"


def _parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    """Parse a GitHub URL into ``(owner, repo)``.

    Accepts versioned links like ``https://github.com/owner/repo/releases/tag/v1.0.0``
    or ``https://github.com/owner/repo/tree/v1``. Only the ``owner`` and base
    ``repo`` are extracted; ``.git`` suffix and query/fragment are stripped.
    Returns ``None`` when the URL is not a GitHub repo URL.
    """
    if not isinstance(repo_url, str) or not repo_url.strip():
        return None
    m = re.search(r"github\.com/([^/]+)/([^/]+)", repo_url.strip(), re.IGNORECASE)
    if not m:
        return None
    owner = m.group(1).strip()
    repo = m.group(2).strip()
    # Strip .git, query, fragment, trailing slash artefacts
    repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
    repo = repo.split("?")[0].split("#")[0].strip().rstrip("/")
    if not owner or not repo:
        return None
    # Validate characters (GitHub owner/repo allow alnum, -, _, .)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        return None
    return owner, repo


def _fetch_github_latest_release(owner: str, repo: str, timeout: int = 10) -> dict | None:
    """Fetch the latest GitHub release JSON for ``owner/repo``.

    Uses ``GET https://api.github.com/repos/{owner}/{repo}/releases/latest``.
    Returns the decoded JSON dict on success, ``None`` on network/error/404.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Akupara/1.0", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log_warn("Failed to fetch latest GitHub release", {"owner": owner, "repo": repo, "error": str(exc)})
        return None
    return None


def _fetch_github_release_by_tag(owner: str, repo: str, tag: str, timeout: int = 10) -> dict | None:
    """Fetch a specific GitHub release by tag ``tag`` for ``owner/repo``.

    Tries ``GET /repos/{owner}/{repo}/releases/tags/{tag}`` and fallback
    ``/releases/tags/v{tag}``. Returns dict or None.
    """
    ctx = ssl.create_default_context()
    for t in (tag, f"v{tag}", tag.lstrip("vV")):
        if not t:
            continue
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{t}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Akupara/1.0", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(data, dict) and not data.get("message"):
                    return data
        except Exception:
            continue
    return None


def _get_local_akupara_version() -> str | None:
    """Return the local Akupara version hash (project hash)."""
    # Try stored hash file first (project root hash)
    try:
        p = Path(__file__).resolve().parent.parent / "hash"
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
        if txt:
            return txt.split()[0].strip()
    except OSError:
        pass
    # Fallback: compute
    try:
        # Import here to avoid circular
        from main import _compute_local_project_hash, _get_local_project_hash
        h = _compute_local_project_hash()
        if h:
            return h
        return _get_local_project_hash()
    except Exception:
        return None


def _is_automatic_plugin_upgrade_enabled() -> bool:
    """Check if automatic plugin upgrades are enabled via .env."""
    # Try reading .env directly (to avoid circular import)
    try:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "AUTOMATIC_PLUGIN_UPGRADE":
                return v.strip().lower() in {"1", "true", "yes", "on"}
    except OSError:
        pass
    # Fallback to env var
    import os
    return os.getenv("AUTOMATIC_PLUGIN_UPGRADE", "").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_manifest_akupara_version(owner: str, repo: str, release: dict, timeout: int = 10) -> str | None:
    """Fetch manifest.json from a release and return its akupara_version field."""
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return None
    manifest_asset = None
    for a in assets:
        if not isinstance(a, dict):
            continue
        n = a.get("name")
        if isinstance(n, str) and n.strip().lower() == "manifest.json":
            manifest_asset = a
            break
    if manifest_asset is None:
        return None
    url = manifest_asset.get("browser_download_url") or manifest_asset.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    txt = _fetch_url_text(url.strip(), timeout=timeout)
    if not txt:
        return None
    try:
        data = json.loads(txt)
        if isinstance(data, dict):
            # Try common keys
            for k in ("akupara_version", "akuparaVersion", "akupara", "version"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            # Fallback: first string value that looks like hash
            for v in data.values():
                if isinstance(v, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", v.strip()):
                    return v.strip()
    except Exception:
        return None
    return None


def _fetch_url_text(url: str, timeout: int = 15) -> str | None:
    """Download ``url`` and return its UTF-8 text (stripped), or ``None`` on failure."""
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip()
            return data
    except Exception as exc:
        log_warn("Failed to fetch hash asset", {"url": url, "error": str(exc)})
        return None


def _fetch_url_bytes(url: str, timeout: int = 15) -> bytes | None:
    """Download ``url`` and return bytes, or ``None`` on failure."""
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except Exception as exc:
        log_warn("Failed to download asset", {"url": url, "error": str(exc)})
        return None


def _find_hash_asset(assets: list[dict]) -> dict | None:
    """Find the hash attachment among GitHub release assets.

    Convention: the hash of the plugin is provided as an attachment whose
    name is ``hash``, ``hash.txt`` or ends with ``.hash`` (case-insensitive).
    Returns the first matching asset dict, or ``None`` when none matches.
    """
    # Priority 1: exact "hash" / "hash.txt"
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if isinstance(name, str) and name.strip().lower() in ("hash", "hash.txt"):
            return asset
    # Priority 2: ends with .hash
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if isinstance(name, str) and name.strip().lower().endswith(".hash"):
            return asset
    return None


def _hash_url_content(url: str, timeout: int = 15) -> str | None:
    """Download ``url`` and return its SHA-256 hex, or ``None`` on failure (legacy)."""
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read()
            return hashlib.sha256(data).hexdigest()
    except Exception as exc:
        log_warn("Failed to download asset", {"url": url, "error": str(exc)})
        return None


def _parse_version_tuple(v: str) -> tuple[int, ...]:
    """Parse version string (e.g. \"1.2.3\", \"v1.2\") into tuple of ints for sorting."""
    s = str(v).strip().lstrip("vV")
    parts: list[int] = []
    for p in re.split(r"[.\-+]", s):
        if not p:
            continue
        # Take leading digits
        m = re.match(r"(\d+)", p)
        if m:
            try:
                parts.append(int(m.group(1)))
            except ValueError:
                parts.append(0)
        else:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def get_plugin_data(identifier: str, version: str | None = None) -> dict | None:
    """Retrieve the stored data for a plugin by its ``name`` (ID) or ``hash``.

    Plugin ID is now ``name`` (case-insensitive); ``hash`` is per-version and
    regenerated on each version change (each version is a new entry, division
    still by ``hash``). For backward compatibility, if ``identifier`` is a
    64-char hex and matches a ``hash``, that entry is returned.

    Otherwise ``identifier`` is treated as ``name`` (case-insensitive). If
    ``version`` is given, the entry with that exact ``version`` (and matching
    name) is returned; otherwise the latest version (highest ``version`` tuple)
    is returned. Returns ``dict`` with
    ``{"hash","name","description","repo","version","akupara_version","trust_mark"}``
    or ``None`` when not found. Raises ``ValueError`` for invalid identifier.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Invalid plugin identifier.")
    ident = identifier.strip()
    # Backward compat: hash lookup (64 hex)
    if re.fullmatch(r"[0-9a-fA-F]{64}", ident):
        for entry in _load_plugins():
            if str(entry.get("hash", "")).strip().lower() == ident.lower():
                return dict(entry)
    # Name lookup (case-insensitive)
    candidates = [
        e for e in _load_plugins()
        if str(e.get("name", "")).strip().casefold() == ident.casefold()
    ]
    if not candidates:
        return None
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Invalid version.")
        v = version.strip()
        for e in candidates:
            if str(e.get("version", "")).strip() == v:
                return dict(e)
        return None
    # No version specified → return latest version (highest version tuple, then hash)
    candidates.sort(key=lambda e: (_parse_version_tuple(str(e.get("version", ""))), str(e.get("hash", ""))))
    return dict(candidates[-1])


# Alias for backwards compatibility / spec wording
def get_plugin(identifier: str, version: str | None = None) -> dict | None:
    """Alias for :func:`get_plugin_data` (now name as ID, hash per-version)."""
    return get_plugin_data(identifier, version)


# NOTE: When loading a plugin by name (ID, hash per-version), the caller MUST
# verify the catalog entry's trust mark (verify_plugin_signature) BEFORE
# comparing get_local_plugin_hash() vs get_remote_plugin_hash(). See module docstring.
def get_remote_plugin_hash(identifier: str, version: str | None = None, timeout: int = 15) -> str | None:
    """Retrieve the hash of the plugin from its latest GitHub release (remote).

    Given a plugin ``name`` (ID, case-insensitive, hash per-version) or legacy
    ``hash``, looks up its ``repo`` URL, resolves the GitHub ``owner/repo``,
    fetches ``GET /repos/{owner}/{repo}/releases/latest``, finds the hash
    attachment (asset named ``hash``/``hash.txt`` or ``*.hash``), downloads it
    and returns its stripped UTF-8 content. If ``version`` is given, the
    specific version entry is used; otherwise the latest version is used.

    The hash is assumed to have been published as an attachment of the release
    (e.g. a file containing the ``SHA-256`` hex of the plugin). This replaces
    the former generation ``hash(concat(sorted(file_hashes)))`` — now it is a
    pure retrieval.

    Returns the hash string on success, ``None`` when the release/assets cannot
    be fetched or no hash asset is present. Raises ``ValueError`` when the
    identifier is invalid or the plugin is not found/has no repo.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Invalid plugin identifier.")
    ident = identifier.strip()
    plugin = get_plugin_data(ident, version)
    if plugin is None:
        raise ValueError("Plugin not found.")
    repo_url = str(plugin.get("repo", "") or plugin.get("link", "")).strip()
    if not repo_url:
        raise ValueError("Plugin has no repo URL.")
    parsed = _parse_github_repo(repo_url)
    if parsed is None:
        raise ValueError("Invalid GitHub repo URL.")
    owner, repo = parsed
    release = _fetch_github_latest_release(owner, repo, timeout=timeout)
    if release is None:
        return None
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        assets = []
    hash_asset = _find_hash_asset(assets)
    if hash_asset is None:
        log_warn("No hash attachment found in latest release", {"plugin": ph, "owner": owner, "repo": repo})
        return None
    url = hash_asset.get("browser_download_url") or hash_asset.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    text = _fetch_url_text(url.strip(), timeout=timeout)
    if text is None or not text:
        return None
    # Return first token (hash is first word, e.g. "abc123  filename" or plain hex)
    return text.split()[0].strip()


# NOTE: When loading a plugin by name (ID, hash per-version), the caller MUST
# verify the catalog entry's trust mark (verify_plugin_signature) BEFORE
# comparing get_local_plugin_hash() vs get_remote_plugin_hash(). See module docstring.
def get_local_plugin_hash(identifier: str, version: str | None = None, timeout: int = 10) -> str | None:
    """Retrieve the hash of the plugin from the local ``plugins/`` folder.

    Given a plugin ``name`` (ID) or legacy ``hash``, fetches the latest GitHub
    release to discover the hash attachment name (``hash``/``hash.txt``/``*.hash``),
    then finds that file in the local ``plugins/`` folder (``_plugins_dir()``,
    fallback ``resources/plugins/``) and returns its stripped UTF-8 content.
    If ``version`` is given, that specific version is used; otherwise the
    latest version is used.

    This is the local counterpart to :func:`get_remote_plugin_hash` and
    together they allow an external caller to compare remote vs local hashes
    without the bridge itself implementing the comparison (per spec).

    Returns the hash string on success, ``None`` when the release cannot be
    fetched, no hash asset exists, or the local file is missing/unreadable.
    Raises ``ValueError`` when the identifier is invalid or not found.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Invalid plugin identifier.")
    ident = identifier.strip()
    plugin = get_plugin_data(ident, version)
    if plugin is None:
        raise ValueError("Plugin not found.")
    repo_url = str(plugin.get("repo", "") or plugin.get("link", "")).strip()
    if not repo_url:
        raise ValueError("Plugin has no repo URL.")
    parsed = _parse_github_repo(repo_url)
    if parsed is None:
        raise ValueError("Invalid GitHub repo URL.")
    owner, repo = parsed
    release = _fetch_github_latest_release(owner, repo, timeout=timeout)
    if release is None:
        return None
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        assets = []
    hash_asset = _find_hash_asset(assets)
    if hash_asset is None:
        log_warn("No hash attachment found in latest release for local lookup", {"plugin": ident})
        return None
    name = hash_asset.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if "/" in name or "\\" in name or name in (".", ".."):
        log_warn("Invalid hash asset name for local lookup", {"name": name, "plugin": ident})
        return None
    path = _plugins_dir() / name
    if not path.is_file():
        alt = Path(__file__).resolve().parent.parent / "resources" / "plugins" / name
        if alt.is_file():
            path = alt
        else:
            log_warn("Local hash file missing", {"plugin": ident, "name": name, "path": str(path)})
            return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return None
        return text.split()[0].strip()
    except OSError as exc:
        log_warn("Failed to read local hash file", {"path": str(path), "error": str(exc)})
        return None


# --- Legacy aliases (now hash retrieval, not generation; name as ID, hash per-version) ---
def hash_remote_plugin_release(identifier: str, version: str | None = None, timeout: int = 15) -> str | None:
    """Legacy alias for :func:`get_remote_plugin_hash` (now name as ID)."""
    return get_remote_plugin_hash(identifier, version, timeout)


def get_plugin_remote_assets_hash(identifier: str, version: str | None = None, timeout: int = 15) -> str | None:
    """Legacy alias for :func:`get_remote_plugin_hash`."""
    return get_remote_plugin_hash(identifier, version, timeout)


def hash_remote_release_assets(identifier: str, version: str | None = None, timeout: int = 15) -> str | None:
    """Legacy alias for :func:`get_remote_plugin_hash`."""
    return get_remote_plugin_hash(identifier, version, timeout)


def hash_local_plugin_release(identifier: str, version: str | None = None, timeout: int = 10) -> str | None:
    """Legacy alias for :func:`get_local_plugin_hash` (now name as ID)."""
    return get_local_plugin_hash(identifier, version, timeout)


def get_plugin_local_assets_hash(identifier: str, version: str | None = None, timeout: int = 10) -> str | None:
    """Legacy alias for :func:`get_local_plugin_hash`."""
    return get_local_plugin_hash(identifier, version, timeout)


def hash_local_release_assets(identifier: str, version: str | None = None, timeout: int = 10) -> str | None:
    """Legacy alias for :func:`get_local_plugin_hash`."""
    return get_local_plugin_hash(identifier, version, timeout)


_bridge = PluginBridge()


def get_plugin_bridge() -> PluginBridge:
    """Return the process-wide plugin bridge singleton."""
    return _bridge
