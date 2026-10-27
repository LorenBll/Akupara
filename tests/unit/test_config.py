"""Unit tests for service configuration."""

import pytest

import auth
import config
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


def test_get_local_device_addresses_includes_loopback():
    addresses = config._get_local_device_addresses()
    assert "127.0.0.1" in addresses
    assert "::1" in addresses
