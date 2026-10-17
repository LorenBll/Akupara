"""Unit tests for shared-memory (internal interactions) helpers."""

import pytest

import shared_memory
import state

from auth import DuplicateNameError, FeatureDisabledError


def enable_shared():
    state.INTERNAL_INTERACTIONS = True
    state.SHARED_MEMORY_ENABLED = True


def test_normalize_plugins_list():
    assert shared_memory._normalize_plugins_list(["Beta", "alpha", "beta"]) == ["alpha", "Beta"]
    assert shared_memory._normalize_plugins_list(None) == []
    with pytest.raises(ValueError):
        shared_memory._normalize_plugins_list("not-a-list")
    with pytest.raises(ValueError):
        shared_memory._normalize_plugins_list([123])


def test_resolve_plugin_exclusivity():
    editor, reader = shared_memory._resolve_plugin_exclusivity(["p1", "p2"], ["p2", "p3"])
    assert editor == ["p1", "p2"]
    assert reader == ["p3"]


def test_normalize_shared_value_types():
    assert shared_memory._normalize_shared_value("x", "string") == "x"
    assert shared_memory._normalize_shared_value([1], "list") == [1]
    assert shared_memory._normalize_shared_value({"a": 1}, "dictionary") == {"a": 1}
    assert shared_memory._normalize_shared_value(5, "integer") == 5
    assert shared_memory._normalize_shared_value(5, "float") == 5.0
    assert shared_memory._normalize_shared_value(True, "boolean") is True
    with pytest.raises(ValueError):
        shared_memory._normalize_shared_value("x", "integer")
    with pytest.raises(ValueError):
        shared_memory._normalize_shared_value(True, "integer")  # bool is not int
    with pytest.raises(ValueError):
        shared_memory._normalize_shared_value(1, "unknown")


def test_list_shared_memory_disabled():
    state.INTERNAL_INTERACTIONS = False
    state.SHARED_MEMORY_ENABLED = True
    with pytest.raises(FeatureDisabledError):
        shared_memory._list_shared_memory(privileged=True)


def test_create_shared_variable_requires_privileged():
    enable_shared()
    with pytest.raises(shared_memory.SharedVariableAccessError):
        shared_memory._create_shared_variable("v", "x", "string", ["p"], [], privileged=False)


def test_create_shared_variable():
    enable_shared()
    entry = shared_memory._create_shared_variable("myvar", "hello", "string", ["editor1"], [], privileged=True)
    assert entry["name"] == "myvar"
    assert entry["value"] == "hello"


def test_create_shared_variable_requires_plugin():
    enable_shared()
    with pytest.raises(ValueError):
        shared_memory._create_shared_variable("myvar", "hello", "string", [], [], privileged=True)


def test_create_shared_variable_duplicate():
    enable_shared()
    shared_memory._create_shared_variable("myvar", "hello", "string", ["e"], [], privileged=True)
    with pytest.raises(DuplicateNameError):
        shared_memory._create_shared_variable("myvar", "x", "string", ["e"], [], privileged=True)


def test_role_enforcement():
    enable_shared()
    shared_memory._create_shared_variable("v", "x", "string", ["editor1"], ["reader1"], privileged=True)
    assert shared_memory._shared_variable_role_for_plugin("editor1", shared_memory._get_shared_variable("v", privileged=True)) == "editor"
    # Editor can edit.
    updated = shared_memory._update_shared_variable("v", value="y", plugin_name="editor1")
    assert updated["value"] == "y"
    # Reader can read but not edit.
    assert shared_memory._get_shared_variable("v", plugin_name="reader1") is not None
    with pytest.raises(shared_memory.SharedVariableAccessError):
        shared_memory._update_shared_variable("v", value="z", plugin_name="reader1")
    # A stranger has no access.
    with pytest.raises(shared_memory.SharedVariableAccessError):
        shared_memory._get_shared_variable("v", plugin_name="stranger")


def test_update_partial_leaves_fields():
    enable_shared()
    shared_memory._create_shared_variable("v", "hello", "string", ["e"], ["r"], privileged=True)
    updated = shared_memory._update_shared_variable("v", editor=["new_editor"], privileged=True)
    assert updated["editor"] == ["new_editor"]
    assert updated["reader"] == ["r"]
    assert updated["value"] == "hello"
    assert updated["type"] == "string"


def test_update_type_change_compatible():
    enable_shared()
    shared_memory._create_shared_variable("v", 5, "integer", ["e"], [], privileged=True)
    updated = shared_memory._update_shared_variable("v", value_type="float", privileged=True)
    assert updated["type"] == "float"
    assert updated["value"] == 5.0


def test_update_type_change_incompatible():
    enable_shared()
    shared_memory._create_shared_variable("v", "hello", "string", ["e"], [], privileged=True)
    with pytest.raises(ValueError):
        shared_memory._update_shared_variable("v", value_type="integer", privileged=True)


def test_delete_shared_variable():
    enable_shared()
    shared_memory._create_shared_variable("v", "hello", "string", ["e"], [], privileged=True)
    assert shared_memory._delete_shared_variable("v", privileged=True) is True
    assert shared_memory._delete_shared_variable("v", privileged=True) is False
