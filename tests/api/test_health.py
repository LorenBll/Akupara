"""Endpoint tests for ``GET /api/health``."""


def test_health_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["service"] == "Akupara"
    assert data["bind_address"] == "127.0.0.1"
    assert data["port"] == 49155
    assert "hostname" in data
    assert "pid" in data


def test_health_head(client):
    assert client.head("/api/health").status_code == 200


def test_health_options(client):
    response = client.options("/api/health")
    assert response.status_code == 200
    assert "GET" in response.headers["Allow"]
