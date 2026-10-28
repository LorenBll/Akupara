"""Unit tests for integrity helpers (pure parts only)."""

import integrity


def test_should_exclude_from_project_hash():
    assert integrity._should_exclude_from_project_hash("hash") is True
    assert integrity._should_exclude_from_project_hash("resources/configuration.json") is True
    assert integrity._should_exclude_from_project_hash("resources/plugins-lib/x.json") is True
    assert integrity._should_exclude_from_project_hash("resources/plugins-lib/lorenbll-akupara-pub") is False
    assert integrity._should_exclude_from_project_hash("resources/audios/a.wav") is True
    assert integrity._should_exclude_from_project_hash("logs/x.json") is True
    assert integrity._should_exclude_from_project_hash(".venv/x.py") is True
    assert integrity._should_exclude_from_project_hash("src/__pycache__/x.pyc") is True
    assert integrity._should_exclude_from_project_hash("foo.egg-info/x") is True
    assert integrity._should_exclude_from_project_hash("x.pyc") is True
    assert integrity._should_exclude_from_project_hash("x.so") is True
    assert integrity._should_exclude_from_project_hash("src/main.py") is False


def test_verify_disabled_styles():
    assert integrity._verify_disabled_styles() is True
