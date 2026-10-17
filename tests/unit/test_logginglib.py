"""Unit tests for the JSON logging library (logging standard)."""

import json
from datetime import date
from pathlib import Path

import logginglib


def test_canonical_hash_deterministic():
    first = logginglib._canonical_hash("ts", "title", {"a": 1})
    second = logginglib._canonical_hash("ts", "title", {"a": 1})
    assert first == second
    assert len(first) == 64
    # Different data must produce a different hash.
    third = logginglib._canonical_hash("ts", "title", {"a": 2})
    assert first != third


def test_debug_gated_by_default(tmp_path):
    logginglib.init_logging("T", debug=False, log_dir=tmp_path)
    logginglib.log_debug("hidden", {"x": 1})
    logginglib.log_info("visible", {"x": 2})
    data = json.loads(logginglib._file.read_text(encoding="utf-8"))
    titles = [event["title"] for event in data]
    assert "hidden" not in titles
    assert "visible" in titles


def test_debug_enabled_writes_debug(tmp_path):
    logginglib.init_logging("T", debug=True, log_dir=tmp_path)
    logginglib.log_debug("hidden", {"x": 1})
    data = json.loads(logginglib._file.read_text(encoding="utf-8"))
    assert any(event["title"] == "hidden" for event in data)


def test_event_shape(tmp_path):
    logginglib.init_logging("T", debug=False, log_dir=tmp_path)
    logginglib.log_error("boom", {"a": 1})
    data = json.loads(logginglib._file.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    event = data[0]
    assert set(event.keys()) == {"timestamp", "type", "title", "data", "hash"}
    assert event["type"] == "ERROR"
    assert event["title"] == "boom"
    assert event["data"] == {"a": 1}
    assert event["hash"] == logginglib._canonical_hash(event["timestamp"], event["title"], event["data"])


def test_prune_expired(tmp_path):
    logginglib.init_logging("T", debug=False, log_dir=tmp_path)
    old = tmp_path / "01-01-2000_00.00.00.json"
    old.write_text("[]", encoding="utf-8")
    future = tmp_path / "01-01-2030_00.00.00.json"
    future.write_text("[]", encoding="utf-8")
    logginglib._prune_expired(tmp_path)
    assert not old.exists()
    assert future.exists()


def test_parse_file_date():
    assert logginglib._parse_file_date(Path("01-01-2000_00.00.00.json")) == date(2000, 1, 1)
