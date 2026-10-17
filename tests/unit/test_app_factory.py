"""Unit tests for the shared Flask decorators / helpers."""

from flask import Flask, Response

import app_factory


def test_options_response():
    app = Flask(__name__)
    with app.app_context():
        response, status = app_factory._options_response(["GET", "POST"])
    assert status == 200
    assert response.headers["Allow"] == "GET, POST"


def test_head_response():
    app = Flask(__name__)
    with app.app_context():
        response, status = app_factory._head_response()
    assert status == 200


def test_standard_endpoint_options_and_head():
    app = Flask(__name__)

    @app.route("/x", methods=["GET", "POST", "HEAD", "OPTIONS"])
    @app_factory.standard_endpoint("GET", "POST", "HEAD", "OPTIONS")
    def view():
        return "body", 200

    client = app.test_client()
    options = client.options("/x")
    assert options.status_code == 200
    assert "GET" in options.headers["Allow"]
    assert client.head("/x").status_code == 200
    assert client.get("/x").status_code == 200


def test_set_connection_header_html_keep_alive():
    app = Flask(__name__)
    with app.test_request_context("/"):
        response = Response("x", headers={"Content-Type": "text/html"})
        result = app_factory.set_connection_header(response)
        assert result.headers["Connection"] == "keep-alive"


def test_set_connection_header_json_close():
    app = Flask(__name__)
    with app.test_request_context("/"):
        response = Response("{}", headers={"Content-Type": "application/json"})
        result = app_factory.set_connection_header(response)
        assert result.headers["Connection"] == "close"
