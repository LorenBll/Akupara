"""Unit tests for the external-interactions worker guard."""

from flask import Flask
from werkzeug.test import Client

import network


def test_marker_is_applied():
    @network.external_interactions_worker_callable
    def view():
        return "x"

    assert getattr(view, network._MARKER, False) is True


def _build_app():
    app = Flask(__name__)

    @app.route("/marked")
    @network.external_interactions_worker_callable
    def marked():
        return "ok", 200

    @app.route("/unmarked")
    def unmarked():
        return "secret", 200

    return app


def test_guarded_app_serves_marked_only():
    guarded = network._guarded_app(_build_app())
    client = Client(guarded)
    assert client.get("/marked").status_code == 200
    assert client.get("/unmarked").status_code == 404


def test_guarded_app_ip_policy():
    guarded = network._guarded_app(_build_app(), ip_policy=lambda remote: remote != "1.2.3.4")
    client = Client(guarded)
    assert client.get("/marked", environ_overrides={"REMOTE_ADDR": "1.2.3.4"}).status_code == 404
    assert client.get("/marked", environ_overrides={"REMOTE_ADDR": "9.9.9.9"}).status_code == 200
