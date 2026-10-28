"""Unit tests for service configuration."""

import pytest

import audio
import auth
import config
import env_store
import state


@pytest.mark.parametrize("development", [True, False])
def test_initialize_service_config_sets_port_and_flags(monkeypatch, development):
    monkeypatch.setattr(
        config,
        "_load_configuration",
        lambda: {"port": 49155, "guiEnabled": True, "development": development},
    )
    config._initialize_service_config()
    assert state.SERVICE_PORT == 49155
    assert state.DEVELOPMENT is development
    assert state.GUI_ENABLED is True


def test_initialize_service_config_propagates_a_missing_pepper(monkeypatch):
    # The pepper policy is fatal by design, so the startup path must not swallow
    # the error: main.__main__ turns it into a logged crash.
    monkeypatch.setattr(
        config,
        "_load_configuration",
        lambda: {"port": 49155, "guiEnabled": True, "development": True},
    )
    auth._password_pepper_path().unlink()
    auth._reset_password_pepper_cache()
    with pytest.raises(auth.PasswordStorageError):
        config._initialize_service_config()


def test_initialize_service_config_does_not_create_a_default_root_user(monkeypatch):
    monkeypatch.setattr(
        config,
        "_load_configuration",
        lambda: {"port": 49155, "guiEnabled": True, "development": True},
    )
    config._initialize_service_config()
    assert auth._load_users() == []


def test_initialize_service_config_can_skip_the_default_root_user(monkeypatch):
    monkeypatch.setattr(
        config,
        "_load_configuration",
        lambda: {"port": 49155, "guiEnabled": True, "development": True},
    )
    config._initialize_service_config(seed_default_root=False)
    assert auth._load_users() == []


def test_initialize_service_config_fills_the_missing_sound_defaults(monkeypatch):
    # An absent key used to be written as an empty value, which silently muted the
    # event; every event must fall back to the file shipped in resources/audios/.
    monkeypatch.setattr(
        config,
        "_load_configuration",
        lambda: {"port": 49155, "guiEnabled": True, "development": True},
    )
    config._initialize_service_config(seed_default_root=False)
    for event, env_name in audio.SOUND_ENV_VARS.items():
        assert env_store.read_env_var(env_name) == audio.DEFAULT_SOUND_FILES[event], event
    assert env_store.read_env_var("WARN_SOUND") == "warn.wav"
    assert env_store.read_env_var("PROCESS_SOUND") == "process.wav"


def test_get_local_device_addresses_includes_loopback():
    addresses = config._get_local_device_addresses()
    assert "127.0.0.1" in addresses
    assert "::1" in addresses
