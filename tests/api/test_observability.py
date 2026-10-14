"""Unit tests for the observability middleware (``lightrag.api.observability``).

These tests deliberately build a *minimal* FastAPI app instead of standing up
the full LightRAG server, so they run with only ``fastapi``, ``httpx`` and
``prometheus_client`` installed and need no LLM, embedding model or external
service.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry

from lightrag.api import observability as obs
from lightrag.api.observability import (
    Metrics,
    ObservabilityMiddleware,
    REQUEST_ID_HEADER,
    render_metrics,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def sample_value(registry: CollectorRegistry, name: str, labels: dict):
    """Return a metric sample's value by exact name+label match, or None."""
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None


def metric_names(registry: CollectorRegistry) -> set[str]:
    """Collect the set of all sample names exposed by a registry."""
    names: set[str] = set()
    for metric in registry.collect():
        for sample in metric.samples:
            names.add(sample.name)
    return names


def build_app(registry: CollectorRegistry) -> FastAPI:
    """Build a minimal app wired with the observability middleware."""
    app = FastAPI()
    metrics = Metrics(registry)
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)

    @app.get("/items")
    async def list_items():
        return {"ok": True}

    @app.get("/items/{item_id}")
    async def get_item(item_id: int):
        return {"id": item_id}

    @app.post("/items", status_code=201)
    async def create_item():
        return {"created": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/metrics")
    async def metrics_route():
        return render_metrics(registry)

    return app


@pytest.fixture()
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture()
def client(registry: CollectorRegistry) -> TestClient:
    return TestClient(build_app(registry), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_request_counter_increments(client: TestClient, registry: CollectorRegistry):
    assert client.get("/items").status_code == 200
    assert client.get("/items").status_code == 200
    value = sample_value(
        registry,
        "lightrag_http_requests_total",
        {"method": "GET", "route": "/items", "status": "200"},
    )
    assert value == 2.0


def test_path_parameter_collapses_to_route_template(
    client: TestClient, registry: CollectorRegistry
):
    assert client.get("/items/123").status_code == 200
    # The raw id must NOT appear as a label; the route template is used.
    value = sample_value(
        registry,
        "lightrag_http_requests_total",
        {"method": "GET", "route": "/items/{item_id}", "status": "200"},
    )
    assert value == 1.0
    names = metric_names(registry)
    assert not any("123" in n for n in names)


def test_unmatched_route_bucket(client: TestClient, registry: CollectorRegistry):
    assert client.get("/does-not-exist").status_code == 404
    value = sample_value(
        registry,
        "lightrag_http_requests_total",
        {"method": "GET", "route": "unmatched", "status": "404"},
    )
    assert value == 1.0


def test_status_code_label_reflects_outcome(
    client: TestClient, registry: CollectorRegistry
):
    assert client.post("/items").status_code == 201
    value = sample_value(
        registry,
        "lightrag_http_requests_total",
        {"method": "POST", "route": "/items", "status": "201"},
    )
    assert value == 1.0


def test_unhandled_error_counts_as_500(
    client: TestClient, registry: CollectorRegistry
):
    assert client.get("/boom").status_code == 500
    value = sample_value(
        registry,
        "lightrag_http_requests_total",
        {"method": "GET", "route": "/boom", "status": "500"},
    )
    assert value == 1.0


def test_histogram_records_latency(client: TestClient, registry: CollectorRegistry):
    assert client.get("/items").status_code == 200
    # Histogram exposes a _count sample representing the number of observations.
    count = sample_value(
        registry,
        "lightrag_http_request_duration_seconds_count",
        {"method": "GET", "route": "/items"},
    )
    assert count == 1.0
    total = sample_value(
        registry,
        "lightrag_http_request_duration_seconds_sum",
        {"method": "GET", "route": "/items"},
    )
    assert total is not None and total >= 0.0


def test_in_progress_gauge_returns_to_zero_after_request(
    client: TestClient, registry: CollectorRegistry
):
    assert client.get("/items").status_code == 200
    value = sample_value(
        registry,
        "lightrag_http_requests_in_progress",
        {"method": "GET"},
    )
    assert value == 0.0


def test_in_progress_gauge_is_one_during_request(registry: CollectorRegistry):
    """While a slow request is in flight, the in-progress gauge must be 1."""

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        app = FastAPI()
        metrics = Metrics(registry)
        app.add_middleware(ObservabilityMiddleware, metrics=metrics)

        @app.get("/slow")
        async def slow():
            entered.set()
            await release.wait()
            return {"ok": True}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            task = asyncio.create_task(ac.get("/slow"))
            await entered.wait()
            await asyncio.sleep(0.05)
            during = sample_value(
                registry,
                "lightrag_http_requests_in_progress",
                {"method": "GET"},
            )
            assert during == 1.0
            release.set()
            response = await task
            assert response.status_code == 200

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_exposes_text_format(client: TestClient):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "lightrag_http_requests_total" in body
    assert "lightrag_http_request_duration_seconds" in body


# ---------------------------------------------------------------------------
# Request id
# ---------------------------------------------------------------------------


def test_request_id_is_generated_when_absent(client: TestClient):
    response = client.get("/items")
    request_id = response.headers.get(REQUEST_ID_HEADER)
    assert request_id
    assert len(request_id) == 32  # uuid4().hex


def test_incoming_request_id_is_preserved(client: TestClient):
    custom = "corr-abc-123"
    response = client.get("/items", headers={REQUEST_ID_HEADER: custom})
    assert response.headers.get(REQUEST_ID_HEADER) == custom


def test_request_id_contextvar_is_bound(registry: CollectorRegistry):
    captured = {}
    app = FastAPI()
    app.add_middleware(
        ObservabilityMiddleware, metrics=Metrics(registry)
    )

    @app.get("/who")
    async def who():
        captured["id"] = obs.get_request_id()
        return {"id": captured["id"]}

    with TestClient(app) as c:
        r = c.get("/who", headers={REQUEST_ID_HEADER: "ctx-trace-1"})
        assert r.status_code == 200
        assert captured["id"] == "ctx-trace-1"

    # The contextvar is reset once the request finishes (outside a request).
    assert obs.get_request_id() is None


# ---------------------------------------------------------------------------
# Graceful degradation without prometheus_client
# ---------------------------------------------------------------------------


def test_degraded_mode_without_prometheus(monkeypatch, registry: CollectorRegistry):
    monkeypatch.setattr(obs, "PROMETHEUS_AVAILABLE", False)
    # Force the default-metrics singleton to recompute as "unavailable".
    monkeypatch.setattr(obs, "_default_metrics", None)
    assert obs.get_default_metrics() is None

    app = FastAPI()
    app.add_middleware(
        ObservabilityMiddleware, metrics=obs.get_default_metrics()
    )

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    @app.get("/metrics")
    async def metrics_route():
        return render_metrics(registry)

    with TestClient(app) as c:
        # Request-id correlation still works...
        r = c.get("/ping")
        assert r.status_code == 200
        assert r.headers.get(REQUEST_ID_HEADER)
        # ...but /metrics cleanly reports it is unavailable.
        m = c.get("/metrics")
        assert m.status_code == 503
        assert "prometheus_client" in m.json()["detail"]
