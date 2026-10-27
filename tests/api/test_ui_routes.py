"""Endpoint tests for UI routes."""

import main
import state


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


def _external_interactions_card(client, monkeypatch, enabled, headers=None):
    monkeypatch.setattr(state, "EXTERNAL_INTERACTIONS", enabled)
    return client.get("/ui/cards/settings/external-interactions", headers=headers or {}).get_data(as_text=True)


def test_external_interactions_card_warns_when_served_over_plain_http(client, login_admin, monkeypatch):
    login_admin(client)
    html = _external_interactions_card(client, monkeypatch, enabled=True)
    assert 'id="externalInteractionsCard"' in html
    assert "plain-http-warning" in html


def test_external_interactions_card_does_not_warn_when_the_feature_is_off(client, login_admin, monkeypatch):
    login_admin(client)
    html = _external_interactions_card(client, monkeypatch, enabled=False)
    assert "plain-http-warning" not in html


def test_external_interactions_card_does_not_warn_behind_a_tls_proxy(client, login_admin, monkeypatch):
    login_admin(client)
    html = _external_interactions_card(
        client, monkeypatch, enabled=True, headers={"X-Forwarded-Proto": "https"}
    )
    assert "plain-http-warning" not in html


def test_external_interactions_card_trusts_the_first_forwarded_proto(client, login_admin, monkeypatch):
    login_admin(client)
    html = _external_interactions_card(
        client, monkeypatch, enabled=True, headers={"X-Forwarded-Proto": "https, http"}
    )
    assert "plain-http-warning" not in html
