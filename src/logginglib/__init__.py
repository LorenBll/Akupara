"""Dedicated JSON logging library implementing the shared logging standard.

Each log event is stored as a JSON object with ``timestamp``, ``type``,
``title``, ``data`` and ``hash`` fields, appended to a JSON list inside a file
in ``logs/``. Logs are never retained for more than 14 days (compared by date
only); expired files are pruned at every start. ``--debug`` controls whether
``DEBUG`` events are written.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime
from pathlib import Path

_ALLOWED_TYPES = ("ERROR", "WARN", "INFO", "DEBUG")

_RETENTION_DAYS = 14

_name: str = "Akupara"
_debug: bool = False
_dir: Path = Path("logs")
_file: Path | None = None
_lock = threading.Lock()
_play_audios: bool = True
_play_log_sounds: bool = False
_in_log_sound: bool = False


def init_logging(
    project_name: str,
    debug: bool = False,
    log_dir: Path | None = None,
) -> None:
    """Set up the logging library for ``project_name``.

    Sets the project name, debug flag and log directory (defaulting to
    ``<project root>/logs``, created if missing), prunes logs older than the
    retention window, then opens a fresh ``logs/DD-MM-YYYY_HH.MM.SS.json`` file.
    """
    global _name, _debug, _dir, _file
    with _lock:
        _name = project_name
        _debug = bool(debug)
        _dir = (log_dir or Path(__file__).resolve().parent.parent.parent / "logs").resolve()
        _dir.mkdir(parents=True, exist_ok=True)
        _prune_expired(_dir)
        _file = _dir / f"{datetime.now().strftime('%d-%m-%Y_%H.%M.%S')}.json"


def _current_file() -> Path:
    if _file is None:
        init_logging(_name, debug=_debug, log_dir=_dir)
    assert _file is not None
    return _file


def _canonical_hash(timestamp: str, title: str, data) -> str:
    payload = json.dumps(
        [timestamp, title, data],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def set_log_sounds_config(play_audios: bool, play_log_sounds: bool) -> None:
    global _play_audios, _play_log_sounds
    _play_audios = bool(play_audios)
    _play_log_sounds = bool(play_log_sounds)


def _play_log_sound(event_type: str, title: str | None = None) -> None:
    global _in_log_sound
    if _in_log_sound:
        return
    if not _play_audios or not _play_log_sounds:
        return
    if title == "Change recorded":
        return
    mapping = {"INFO": "acknowledge", "ERROR": "error", "WARN": "warn", "DEBUG": "acknowledge"}
    sound = mapping.get(event_type)
    if not sound:
        return
    _in_log_sound = True
    try:
        import audio as _audio

        _audio.play_sound(sound, via_log=True)
    except Exception:
        pass
    finally:
        _in_log_sound = False


def _write_event(event_type: str, title: str, data=None, silent: bool = False) -> None:
    if event_type == "DEBUG" and not _debug:
        return
    timestamp = datetime.now().isoformat(timespec="seconds")
    event = {
        "timestamp": timestamp,
        "type": event_type,
        "title": title,
        "data": data,
        "hash": _canonical_hash(timestamp, title, data),
    }
    with _lock:
        path = _current_file()
        entries: list = []
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            except (OSError, json.JSONDecodeError):
                entries = []
        entries.append(event)
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    if not silent:
        _play_log_sound(event_type, title)


def log_error(title: str, data=None) -> None:
    _write_event("ERROR", title, data)


def log_warn(title: str, data=None, *, silent: bool = False) -> None:
    _write_event("WARN", title, data, silent=silent)


def log_info(title: str, data=None) -> None:
    _write_event("INFO", title, data)


def log_debug(title: str, data=None) -> None:
    _write_event("DEBUG", title, data)


def _parse_file_date(path: Path) -> date:
    try:
        return datetime.strptime(path.stem[:10], "%d-%m-%Y").date()
    except (ValueError, TypeError):
        try:
            return date.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return date.today()


def _prune_expired(log_dir: Path) -> None:
    today = date.today()
    for path in log_dir.glob("*.json"):
        file_date = _parse_file_date(path)
        if (today - file_date).days > _RETENTION_DAYS:
            try:
                path.unlink()
            except OSError:
                pass
