import httpx
from ecom_common.app_factory import create_service_app
from ecom_common.config import BaseServiceSettings
from ecom_common.consumer import EventDispatcher
from ecom_common.kafka import InMemoryPublisher, build_event
from ecom_common.logging import request_id_ctx
from fastapi import HTTPException
from prometheus_client import REGISTRY


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def make_app(service: str, token: str = ""):
    app = create_service_app(BaseServiceSettings(service_name=service, metrics_token=token))

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    @app.get("/teapot")
    async def teapot():
        raise HTTPException(status_code=418, detail="teapot")

    return app


def client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://svc")


# ---- request metrics --------------------------------------------------------------------------
async def test_requests_are_counted_by_route_template_not_by_real_path():
    labels = {"service": "svc-a", "method": "GET", "route": "/items/{item_id}", "status": "200"}
    before = sample("http_requests_total", **labels)
    async with client(make_app("svc-a")) as c:
        for item_id in (1, 2, 3, 4):  # four different ids ...
            await c.get(f"/items/{item_id}")
    assert sample("http_requests_total", **labels) - before == 4  # ... but ONE time series


async def test_errors_unknown_routes_and_crashes_are_recorded_with_their_status():
    base = {
        s: sample("http_requests_total", service="svc-b", method="GET", route=r, status=st)
        for s, (r, st) in {
            "418": ("/teapot", "418"),
            "500": ("/boom", "500"),
            "404": ("unmatched", "404"),
        }.items()
    }
    async with client(make_app("svc-b")) as c:
        await c.get("/teapot")
        await c.get("/boom")
        await c.get("/no/such/route/with/lots/of/ids/12345")
    for key, (route, status) in {
        "418": ("/teapot", "418"),
        "500": ("/boom", "500"),
        "404": ("unmatched", "404"),
    }.items():
        now = sample(
            "http_requests_total", service="svc-b", method="GET", route=route, status=status
        )
        assert now - base[key] == 1, key


async def test_latency_is_measured_and_health_checks_are_not_counted():
    before = sample(
        "http_request_duration_seconds_count",
        service="svc-c",
        method="GET",
        route="/items/{item_id}",
    )
    health_before = sample(
        "http_requests_total", service="svc-c", method="GET", route="/health", status="200"
    )
    async with client(make_app("svc-c")) as c:
        await c.get("/items/1")
        await c.get("/health")
        await c.get("/health")
    assert (
        sample(
            "http_request_duration_seconds_count",
            service="svc-c",
            method="GET",
            route="/items/{item_id}",
        )
        - before
        == 1
    )
    assert (
        sample("http_requests_total", service="svc-c", method="GET", route="/health", status="200")
        == health_before
    )


async def test_metrics_endpoint_serves_prometheus_text():
    async with client(make_app("svc-d")) as c:
        await c.get("/items/1")
        body = (await c.get("/metrics")).text
    assert "# TYPE http_requests_total counter" in body
    assert 'service="svc-d"' in body and "http_request_duration_seconds_bucket" in body


async def test_metrics_can_be_protected_with_a_token():
    async with client(make_app("svc-e", token="a-long-enough-metrics-token")) as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (
            await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        ok = await c.get(
            "/metrics", headers={"Authorization": "Bearer a-long-enough-metrics-token"}
        )
        assert ok.status_code == 200 and "http_requests_total" in ok.text


# ---- event metrics ------------------------------------------------------------------------------
def counts(topic: str) -> dict[str, float]:
    return {
        o: sample("events_consumed_total", service="svc-a", topic=topic, outcome=o)
        for o in ("handled", "ignored", "retried", "dead_lettered")
    }


async def test_event_outcomes_are_counted():
    make_app("svc-a")  # names this process's service for the metrics
    calls = {"n": 0}

    async def ok(event):
        pass

    async def flaky(event):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("blip")

    async def broken(event):
        raise ValueError("always")

    d = EventDispatcher(
        {"a": ok, "b": flaky, "c": broken}, InMemoryPublisher(), max_attempts=3, backoff_seconds=0
    )
    before = {t: counts(t) for t in ("t.a", "t.b", "t.c", "t.x", "t.bad")}
    await d.process("t.a", "k", build_event("a", {}))
    await d.process("t.b", "k", build_event("b", {}))
    await d.process("t.c", "k", build_event("c", {}))
    await d.process("t.x", "k", build_event("unrelated", {}))
    await d.process("t.bad", "k", b"not json")
    delta = {t: {o: counts(t)[o] - before[t][o] for o in before[t]} for t in before}
    assert delta["t.a"]["handled"] == 1
    assert delta["t.b"]["retried"] == 2 and delta["t.b"]["handled"] == 1
    assert delta["t.c"]["retried"] == 3 and delta["t.c"]["dead_lettered"] == 1
    assert delta["t.x"]["ignored"] == 1
    assert delta["t.bad"]["dead_lettered"] == 1


# ---- one correlation id across services ----
async def test_a_consumer_works_under_the_id_of_the_request_that_started_it():
    seen = {}

    async def handler(event):
        seen["during"] = request_id_ctx.get()
        # an event published in reaction carries the same id, so the chain continues
        seen["published"] = build_event("next.step", {})["correlation_id"]

    request_id_ctx.set("unrelated-id")
    d = EventDispatcher({"x": handler}, InMemoryPublisher(), backoff_seconds=0)
    await d.process("t", "k", build_event("x", {}, correlation_id="req-checkout-42"))
    assert seen == {"during": "req-checkout-42", "published": "req-checkout-42"}
    assert request_id_ctx.get() == "unrelated-id"  # restored afterwards


async def test_events_without_a_correlation_id_still_work():
    seen = {}

    async def handler(event):
        seen["id"] = request_id_ctx.get()

    d = EventDispatcher({"x": handler}, InMemoryPublisher(), backoff_seconds=0)
    event = build_event("x", {})
    event.pop("correlation_id")
    await d.process("t", "k", event)
    assert "id" in seen
