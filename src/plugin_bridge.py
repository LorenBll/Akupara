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
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from logginglib import log_error, log_info, log_warn


# Remote hash for integrity check — latest GitHub Akupara
_REMOTE_HASH_URL = "https://raw.githubusercontent.com/LorenBll/Akupara/main/resources/plugins-lib/hash"
_REMOTE_HASH_URLS = [_REMOTE_HASH_URL]

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

def _fetch_remote_hash(timeout: int = 8) -> str | None:
    """Fetch hash file from latest GitHub Akupara (raw). Returns stripped text or None on failure."""
    ctx = ssl.create_default_context()
    for url in _REMOTE_HASH_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Akupara/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                # Accept 2xx
                data = resp.read().decode("utf-8", errors="replace").strip()
                # Hash is single hex line; take first token
                if data:
                    return data.split()[0].strip()
                return ""
        except urllib.error.HTTPError as e:
            # 404 means not found — try next URL
            if e.code == 404:
                continue
            return None
        except Exception:
            continue
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
    ``start`` immediately performs two integrity checks:
      1) local ``hash`` file vs remote GitHub ``hash`` (latest Akupara) — mismatch plays **warn** sound
      2) recomputed ``plugins-lib`` folder hash vs local ``hash`` file — mismatch plays **error** sound
    On mismatch the loader stops and logs an error.
    This guards against illicit interaction with the ``plugins-lib`` folder.
    """

    def __init__(self) -> None:
        self._started: bool = False

    def start(self) -> None:
        """Start the bridge — immediately verifies plugins-lib integrity.

        Idempotent. Checks:
          1) stored ``hash`` == remote GitHub ``hash`` (latest Akupara) — mismatch → warn sound
          2) recomputed folder hash == stored ``hash`` — mismatch → error sound
        On mismatch: logs error, plays warn/error sound respectively, stays stopped.
        """
        if self._started:
            return
        # Ensure we start from stopped
        self._started = False

        # --- Check 1: local hash vs remote GitHub hash ---
        stored = _read_stored_hash()
        if stored is None or not stored:
            log_error("Plugin loader failed: local hash file missing", {"path": str(_hash_file_path())})
            _play_error_sound()
            return
        remote = _fetch_remote_hash()
        if remote is None:
            # Network offline or fetch failed — cannot verify against GitHub latest.
            # Log a warning and fall through to local integrity check (which guards illicit local tamper).
            # Strict mode would require remote match, but offline should not brick the loader.
            from logginglib import log_warn
            log_warn("Plugin loader: remote hash unavailable — skipping GitHub check (offline?)", {"remote_urls": _REMOTE_HASH_URLS, "local_hash": stored})
        elif stored.strip().lower() != remote.strip().lower():
            log_error("Plugin loader failed: local hash differs from GitHub latest", {"local_hash": stored, "remote_hash": remote, "remote_url": _REMOTE_HASH_URL})
            _play_error_sound()
            return

        # --- Check 2: recomputed folder hash vs stored hash ---
        computed = _compute_plugins_lib_hash()
        if computed.strip().lower() != stored.strip().lower():
            log_error("Plugin loader failed: plugins-lib folder hash mismatch (illicit interaction?)", {"computed": computed, "stored": stored})
            _play_error_sound()
            return

        # All checks passed — loader is started
        self._started = True
        log_info("Plugin loader started", {"hash": stored})

    def stop(self) -> None:
        """Stop the bridge (idempotent)."""
        self._started = False

    def is_started(self) -> bool:
        """Return whether the bridge has been started (integrity checks passed)."""
        return self._started


_REVERSE_INDEX_NAME = "reverse-index.json"


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
    plugin objects with the keys: ``hash`` (non-secret ID that also
    determines the file the plugin belongs to), ``name``, ``description``,
    ``repo`` (GitHub URL) and ``trust_mark`` (secret trust mark — armored
    detached GPG signature string, ``""`` when absent; legacy ``trust_hash``
    is read with fallback). ``reverse_index_keys`` is **not** stored in the
    hash-range file — it lives only in the apposite file
    ``reverse-index.json`` (word -> [hash]) and is calculated every time via
    ``_load_reverse_index``/``keys_for_hash``. While the catalog is small a
    single file covering the full hash space is used (``<lowest>-<highest>.json``).
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
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        raise ValueError("Invalid request.") from None

    def _regex_match(plugin: dict) -> bool:
        searchable_values = " ".join(str(v) for v in plugin.values() if isinstance(v, (str, int, float)))
        searchable_json = json.dumps(plugin, ensure_ascii=False)
        return bool(regex.search(searchable_values) or regex.search(searchable_json))

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
    """Extract armored signature string from plugin entry (handles legacy formats)."""
    if not isinstance(entry, dict):
        return ""
    v = entry.get("trust_mark")
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        sig = v.get("signature")
        if isinstance(sig, str) and sig.strip():
            return sig.strip()
        return ""
    v2 = entry.get("trust_hash")
    if isinstance(v2, str) and v2.strip():
        return v2.strip()
    if isinstance(v2, dict):
        sig = v2.get("signature")
        if isinstance(sig, str) and sig.strip():
            return sig.strip()
    return ""


def _keys_for_hash(plugin_hash: str) -> list[str]:
    """Return reverse-index keys for hash (computed live from reverse-index.json)."""
    idx = _load_reverse_index()
    ph = str(plugin_hash).strip()
    return sorted([k for k, hs in idx.items() if ph in hs])


def _build_trust_data(plugin: dict) -> dict:
    """Build canonical trust data for a plugin (hash, name, description, repo, reverse_index_keys)."""
    ph = str(plugin.get("hash", "")).strip()
    return {
        "hash": ph,
        "name": str(plugin.get("name", "")),
        "description": str(plugin.get("description", "")),
        "repo": str(plugin.get("repo", "") or plugin.get("link", "")),
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

    Rebuilds the canonical JSON ``{"hash","name","description","repo","reverse_index_keys"}``
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


_bridge = PluginBridge()


def get_plugin_bridge() -> PluginBridge:
    """Return the process-wide plugin bridge singleton."""
    return _bridge
