import pytest
from ecom_common import Conflict, NotFound, success_response
from ecom_common.app_factory import create_service_app
from ecom_common.config import BaseServiceSettings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

KEYS = {"success", "message", "data", "error_message"}


class Item(BaseModel):
    name: str


@pytest.fixture
def client() -> TestClient:
    app = create_service_app(BaseServiceSettings(service_name="test"))

    @app.get("/ok")
    async def ok():
        return success_response({"a": 1}, "Fetched")

    @app.get("/missing")
    async def missing():
        raise NotFound("Item 7 does not exist")

    @app.get("/conflict")
    async def conflict():
        raise Conflict("duplicate")

    @app.get("/http")
    async def http():
        raise HTTPException(status_code=418, detail="teapot")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    @app.post("/items")
    async def create(item: Item):
        return success_response(item, "Created", 201)

    return TestClient(app, raise_server_exceptions=False)


def check(resp, status, success):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == KEYS
    assert body["success"] is success
    assert isinstance(body["data"], dict)
    return body


def test_success(client):
    body = check(client.get("/ok"), 200, True)
    assert body["data"] == {"a": 1}
    assert body["message"] == "Fetched"
    assert body["error_message"] == ""


def test_created(client):
    body = check(client.post("/items", json={"name": "x"}), 201, True)
    assert body["data"] == {"name": "x"}


def test_not_found(client):
    body = check(client.get("/missing"), 404, False)
    assert body["error_message"] == "Item 7 does not exist"


def test_conflict(client):
    check(client.get("/conflict"), 409, False)


def test_http_exception(client):
    body = check(client.get("/http"), 418, False)
    assert body["error_message"] == "teapot"


def test_unknown_route(client):
    check(client.get("/nope"), 404, False)


def test_validation_error(client):
    body = check(client.post("/items", json={}), 422, False)
    assert body["data"]["errors"][0]["field"] == "name"
    assert "name" in body["error_message"]


def test_unhandled_error_hides_internals(client):
    body = check(client.get("/boom"), 500, False)
    assert "secret" not in str(body)


def test_health_and_request_id(client):
    resp = client.get("/health")
    check(resp, 200, True)
    assert resp.headers["X-Request-ID"]
    assert client.get("/health", headers={"X-Request-ID": "abc"}).headers["X-Request-ID"] == "abc"


def test_ready_reports_failed_dependency():
    async def bad():
        raise ConnectionError("db down")

    app = create_service_app(BaseServiceSettings(service_name="t"), readiness_checks={"db": bad})
    body = check(TestClient(app).get("/ready"), 503, False)
    assert "db" in body["data"]["failed"]
