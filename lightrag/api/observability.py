"""Production-grade observability for the LightRAG FastAPI service.

This module adds two capabilities that the stock server did not expose:

1. **Prometheus metrics** at ``/metrics`` (request count, latency and
   in-flight requests), implemented with the optional ``prometheus_client``
   dependency. If that package is not installed, everything degrades
   gracefully: request-id tracking keeps working and ``/metrics`` returns a
   clear ``503`` instead of crashing the app.

2. **Request correlation ids**. Every request gets an ``X-Request-ID``
   response header. An inbound ``X-Request-ID`` header is honoured (so a
   trace can span multiple services); otherwise a random id is generated.
   The id is also published through a :class:`contextvars.ContextVar`, which
   lets structured logs and the existing exception handler attach the same
   correlation id to a request.

The middleware is written as a small, dependency-light ASGI component. It
deliberately imports only ``starlette`` / the standard library at module
import time, so it can be unit-tested in isolation without standing up the
full LightRAG stack (see ``tests/api/test_observability.py``).
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional

# ---------------------------------------------------------------------------
# Optional prometheus dependency
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised implicitly depending on the environment
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
    )
    from prometheus_client import generate_latest

    PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - prometheus_client is not installed
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    CollectorRegistry = None  # type: ignore[assignment, misc]
    Counter = Gauge = Histogram = None  # type: ignore[assignment]
    generate_latest = None  # type: ignore[assignment]
    PROMETHEUS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Request id handling
# ---------------------------------------------------------------------------

#: Header used for request correlation. Compared case-insensitively by ASGI.
REQUEST_ID_HEADER = "x-request-id"

#: Holds the current request's correlation id inside its task context.
request_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "request_id", default=None
)


def get_request_id() -> Optional[str]:
    """Return the correlation id bound to the current task, if any."""
    return request_id_ctx.get()


def _new_request_id() -> str:
    """Generate a short, URL-safe correlation id."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Metrics container
# ---------------------------------------------------------------------------


class Metrics:
    """A thin wrapper around the Prometheus objects for HTTP traffic.

    Keeping them grouped makes the middleware independent of the global
    default registry and allows tests to pass a fresh, isolated registry.
    """

    def __init__(self, registry: "CollectorRegistry") -> None:
        self.registry = registry
        # Total HTTP requests, keyed by method, route template and status.
        self.requests_total = Counter(
            "lightrag_http_requests_total",
            "Total number of HTTP requests received.",
            labelnames=("method", "route", "status"),
            registry=registry,
        )
        # Server-side request latency, in seconds.
        self.request_duration_seconds = Histogram(
            "lightrag_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            labelnames=("method", "route"),
            registry=registry,
        )
        # Requests currently being processed. Labelled by method only: the
        # matched route is not known until the endpoint resolves, and using a
        # raw path would create an unbounded (high-cardinality) label set.
        self.requests_in_progress = Gauge(
            "lightrag_http_requests_in_progress",
            "Number of HTTP requests currently being processed.",
            labelnames=("method",),
            registry=registry,
        )


# Module-level singleton registry + metrics, created lazily so importing the
# module never fails when prometheus_client is absent.
_default_registry: Optional["CollectorRegistry"] = None
_default_metrics: Optional[Metrics] = None


def get_default_metrics() -> Optional[Metrics]:
    """Return the process-wide :class:`Metrics`, creating it on first use.

    Returns ``None`` when ``prometheus_client`` is not installed.
    """
    global _default_registry, _default_metrics
    if not PROMETHEUS_AVAILABLE:
        return None
    if _default_metrics is None:
        _default_registry = CollectorRegistry()
        _default_metrics = Metrics(_default_registry)
    return _default_metrics


def _route_template(scope: dict[str, Any]) -> str:
    """Return a stable, low-cardinality path for metric labels.

    Uses the matched route's template (e.g. ``/documents/{doc_id}``) when
    available, so per-resource ids do not explode the label space. Unmatched
    requests (404s) collapse to a single ``unmatched`` bucket.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

# Type alias for the ASGI receive/send callables.
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class ObservabilityMiddleware:
    """ASGI middleware that records metrics and manages request ids.

    Parameters
    ----------
    app:
        The wrapped ASGI application.
    metrics:
        Optional pre-built :class:`Metrics` (mainly for tests). When omitted,
        the process-wide default is used, or metrics collection is skipped if
        ``prometheus_client`` is unavailable.
    """

    def __init__(self, app: Any, metrics: Optional[Metrics] = None) -> None:
        self.app = app
        self.metrics = metrics if metrics is not None else get_default_metrics()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        # Only manage HTTP (and websocket-over-http lifecycles are skipped).
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")

        # Resolve / generate the correlation id and bind it to this task.
        headers = scope.get("headers", [])
        incoming_id: Optional[str] = None
        for name, value in headers:
            if name.decode("latin-1").lower() == REQUEST_ID_HEADER:
                incoming_id = value.decode("latin-1").strip() or None
                break
        request_id = incoming_id or _new_request_id()
        token = request_id_ctx.set(request_id)
        # Expose on the request for downstream handlers / logging.
        scope.setdefault("state", {})
        if isinstance(scope.get("state"), dict):
            scope["state"]["request_id"] = request_id

        status_code = 0
        start = time.perf_counter()

        if self.metrics is not None:
            self.metrics.requests_in_progress.labels(method=method).inc()

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 0))
                # Echo the correlation id back on every response.
                headers_out = message.setdefault("headers", [])
                headers_out.append(
                    (
                        REQUEST_ID_HEADER.encode("latin-1"),
                        request_id.encode("latin-1"),
                    )
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            # Some errors never produce a response start; treat them as 500.
            if status_code == 0:
                status_code = 500
            if self.metrics is not None:
                route = _route_template(scope)
                self.metrics.requests_total.labels(
                    method=method, route=route, status=str(status_code)
                ).inc()
                self.metrics.request_duration_seconds.labels(
                    method=method, route=route
                ).observe(elapsed)
                self.metrics.requests_in_progress.labels(method=method).dec()
            request_id_ctx.reset(token)


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


def render_metrics(registry: Optional["CollectorRegistry"] = None) -> "Response":
    """Build an HTTP response containing the Prometheus text exposition.

    Returns a ``503`` JSON response when Prometheus support is unavailable.
    """
    # Imported lazily so this module does not hard-require starlette for the
    # parts that only need the standard library.
    from starlette.responses import JSONResponse, Response

    if not PROMETHEUS_AVAILABLE or generate_latest is None:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Metrics are unavailable: install the optional "
                    "'prometheus_client' dependency to enable /metrics."
                )
            },
        )

    reg = registry if registry is not None else _default_registry
    if reg is None:
        # Ensure the default metrics (and registry) exist before scraping.
        get_default_metrics()
        reg = _default_registry

    payload = generate_latest(reg)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


def create_metrics_route(
    registry: Optional["CollectorRegistry"] = None,
) -> Callable[[], "Response"]:
    """Return a FastAPI-compatible route handler for ``/metrics``."""

    def metrics_route() -> "Response":  # pragma: no cover - thin shim
        return render_metrics(registry)

    return metrics_route


def setup_observability(app: Any) -> Optional[Metrics]:
    """Register the observability middleware on a FastAPI/Starlette app.

    This is the convenience entry point for minimal apps and tests. The full
    server wires the middleware and the ``/metrics`` route explicitly so it
    can control the middleware stack ordering. Returns the metrics container
    (or ``None`` when Prometheus is not installed).
    """
    metrics = get_default_metrics()
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    return metrics
