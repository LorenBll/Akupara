"""Unit tests for plugin-event subscription helpers."""

import pytest

import plugin_events

from auth import DuplicateNameError


def test_add_event():
    assert plugin_events._add_plugin_event("event1", "plugin1") is True
    assert plugin_events._load_plugin_event_subscriptions()["event1"] == ["plugin1"]


def test_add_event_duplicate():
    plugin_events._add_plugin_event("event1")
    with pytest.raises(DuplicateNameError):
        plugin_events._add_plugin_event("event1")


def test_add_event_invalid_name():
    with pytest.raises(ValueError):
        plugin_events._add_plugin_event("bad event")


def test_add_plugin_to_event():
    plugin_events._add_plugin_event("event1")
    plugin_events._add_plugin_to_event("event1", "plugin1")
    assert "plugin1" in plugin_events._load_plugin_event_subscriptions()["event1"]


def test_add_plugin_to_missing_event():
    with pytest.raises(ValueError):
        plugin_events._add_plugin_to_event("missing", "plugin1")


def test_add_plugin_duplicate():
    plugin_events._add_plugin_event("event1", "plugin1")
    with pytest.raises(DuplicateNameError):
        plugin_events._add_plugin_to_event("event1", "plugin1")


def test_remove_event():
    plugin_events._add_plugin_event("event1")
    assert plugin_events._remove_plugin_event("event1") is True
    assert plugin_events._remove_plugin_event("event1") is False


def test_remove_plugin_from_event():
    plugin_events._add_plugin_event("event1", "plugin1")
    assert plugin_events._remove_plugin_from_event("event1", "plugin1") is True
    assert plugin_events._remove_plugin_from_event("event1", "plugin1") is False
