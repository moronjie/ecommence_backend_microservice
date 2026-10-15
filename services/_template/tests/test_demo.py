from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)
KEYS = {"success", "message", "data", "error_message"}


def test_ping():
    body = client.get("/api/v1/template/ping").json()
    assert set(body) == KEYS and body["success"] is True


def test_not_found_uses_uniform_shape():
    resp = client.get("/api/v1/template/items/1")
    assert resp.status_code == 404
    assert set(resp.json()) == KEYS


def test_boom_is_500_uniform():
    resp = client.get("/api/v1/template/boom")
    assert resp.status_code == 500
    assert set(resp.json()) == KEYS
