"""Endpoint tests for UI routes."""

import main


def _mock_versions(monkeypatch):
    for name in (
        "_get_effective_project_version",
        "_get_indicated_project_version",
        "_get_current_project_version",
        "_get_effective_plugins_lib_version",
        "_get_indicated_plugins_lib_version",
        "_get_current_plugins_lib_version",
    ):
        monkeypatch.setattr(main, name, lambda: "0123456789ab")


def test_index_redirects_anonymous(client):
    assert client.get("/").status_code == 302


def test_login_page(client):
    assert client.get("/login").status_code == 200


def test_index_with_session(client, make_user, login):
    make_user("alice1234")
    login(client, "alice1234")
    assert client.get("/").status_code == 200


def test_static_css(client):
    assert client.get("/ui/css/index.css").status_code == 200


def test_login_icon(client):
    assert client.get("/ui/icons/akupara.svg").status_code == 200


def test_settings_page_admin(client, login_admin, monkeypatch):
    _mock_versions(monkeypatch)
    login_admin(client)
    assert client.get("/ui/pages/settings.html").status_code == 200


def test_settings_page_anonymous(client):
    assert client.get("/ui/pages/settings.html").status_code == 302


def test_settings_card_general(client, login_admin, monkeypatch):
    _mock_versions(monkeypatch)
    login_admin(client)
    assert client.get("/ui/cards/settings/general").status_code == 200


def test_settings_card_admin_only(client, make_user, login, monkeypatch):
    _mock_versions(monkeypatch)
    make_user("alice1234", admin=False)
    login(client, "alice1234")
    assert client.get("/ui/cards/settings/users").status_code == 403
