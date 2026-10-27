"""Unit tests for external-interactions (firewall) helpers."""

import pytest

import firewall
import state

from auth import DuplicateNameError, FeatureDisabledError


def enable_fw():
    state.EXTERNAL_INTERACTIONS = True


def test_canonical_ip():
    assert firewall._maximize_network_ip("1.2.3.4") == "1.2.3.4"


def test_invalid_ip():
    with pytest.raises(ValueError):
        firewall._maximize_network_ip("not-an-ip")


def test_sort_entry_plugins_akupara_first():
    assert firewall._sort_entry_plugins(["beta", "akupara", "alpha"]) == ["akupara", "alpha", "beta"]


def test_list_requires_enabled():
    state.EXTERNAL_INTERACTIONS = False
    with pytest.raises(FeatureDisabledError):
        firewall._list_external_interactions_entries("incoming")


def test_load_save_entries():
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": ["beta", "akupara"], "action": "allow", "Note": "hi"}
    ])
    entries = firewall._load_external_interactions_entries("incoming")
    assert len(entries) == 1
    assert entries[0]["ip"] == "1.2.3.4"
    assert entries[0]["plugins"] == ["akupara", "beta"]
    assert entries[0]["action"] == "allow"
    assert entries[0]["Note"] == "hi"


def test_update_entry():
    enable_fw()
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": [], "action": "unknown", "Note": ""}
    ])
    updated = firewall._update_external_interactions_entry(
        "incoming", "1.2.3.4", action="allow", add_plugin="akupara", note="hello"
    )
    assert updated["action"] == "allow"
    assert updated["plugins"] == ["akupara"]
    assert updated["Note"] == "hello"


def test_update_entry_invalid_action():
    enable_fw()
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": [], "action": "unknown", "Note": ""}
    ])
    with pytest.raises(ValueError):
        firewall._update_external_interactions_entry("incoming", "1.2.3.4", action="bogus")


def test_update_entry_rename_duplicate():
    enable_fw()
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": [], "action": "unknown", "Note": ""},
        {"ip": "5.6.7.8", "plugins": [], "action": "unknown", "Note": ""},
    ])
    with pytest.raises(DuplicateNameError):
        firewall._update_external_interactions_entry("incoming", "1.2.3.4", new_ip="5.6.7.8")


def test_delete_entry():
    enable_fw()
    firewall._save_external_interactions_entries("incoming", [
        {"ip": "1.2.3.4", "plugins": [], "action": "unknown", "Note": ""}
    ])
    assert firewall._delete_external_interactions_entry("incoming", "1.2.3.4") is True
    assert firewall._delete_external_interactions_entry("incoming", "1.2.3.4") is False


def test_plain_http_warning_needs_both_the_feature_and_a_plain_request():
    state.EXTERNAL_INTERACTIONS = False
    assert firewall._external_interactions_plain_http(is_secure=False) is False
    assert firewall._external_interactions_plain_http(is_secure=True) is False
    state.EXTERNAL_INTERACTIONS = True
    assert firewall._external_interactions_plain_http(is_secure=False) is True
    assert firewall._external_interactions_plain_http(is_secure=True) is False
