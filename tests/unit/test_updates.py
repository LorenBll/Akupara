"""Unit tests for update-check helpers (offline classification only)."""

import urllib.error

import updates


def test_is_offline_error_url_error():
    assert updates._is_offline_error(urllib.error.URLError("dns fail")) is True


def test_is_offline_error_timeout():
    assert updates._is_offline_error(TimeoutError()) is True


def test_is_offline_error_oserror_network():
    assert updates._is_offline_error(OSError("network is unreachable")) is True


def test_is_offline_error_http_error_not_offline():
    e = urllib.error.HTTPError("u", 404, "n", {}, None)
    assert updates._is_offline_error(e) is False


def test_is_offline_error_unrelated():
    assert updates._is_offline_error(ValueError("x")) is False
