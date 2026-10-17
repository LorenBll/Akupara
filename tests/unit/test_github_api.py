"""Unit tests for the GitHub API helper (pure classification only)."""

import urllib.error

import github_api


def test_is_github_host():
    assert github_api._is_github_host("https://api.github.com/repos/x/y") is True
    assert github_api._is_github_host("https://github.com/x/y") is True
    assert github_api._is_github_host("https://raw.githubusercontent.com/x/y") is True
    assert github_api._is_github_host("https://example.com") is False
    # Lookalike hosts must not be treated as GitHub-owned.
    assert github_api._is_github_host("https://evilgithub.com") is False
    assert github_api._is_github_host("https://evilgithubusercontent.com") is False


def test_is_rate_limited_error():
    e429 = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
    assert github_api._is_rate_limited_error(e429) is True
    e403 = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
    assert github_api._is_rate_limited_error(e403) is False
    e404 = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    assert github_api._is_rate_limited_error(e404) is False
