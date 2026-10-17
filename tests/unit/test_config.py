"""Unit tests for service configuration."""

import pytest

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


def test_get_local_device_addresses_includes_loopback():
    addresses = config._get_local_device_addresses()
    assert "127.0.0.1" in addresses
    assert "::1" in addresses
