# Observability: Prometheus Metrics & Request Tracing

LightRAG ships with an optional observability layer for the FastAPI server.
It adds a Prometheus scrape endpoint at `/metrics` and attaches a correlation
id (`X-Request-ID`) to every request.

## Enabling the feature

The metrics endpoint uses the small [`prometheus_client`](https://github.com/prometheus/client_python)
package. Install it via the optional extra:

```bash
pip install -e ".[observability]"
```

If the package is **not** installed the server still works:

- request-id correlation (`X-Request-ID`) keeps working;
- `/metrics` returns a clear `503` JSON response explaining how to enable it.

## Exposed metrics

All metrics are prefixed with `lightrag_`.

| Metric | Type | Labels | Description |
| --- | --- | --- | --- |
| `lightrag_http_requests_total` | Counter | `method`, `route`, `status` | Total HTTP requests handled. |
| `lightrag_http_request_duration_seconds` | Histogram | `method`, `route` | Server-side request latency in seconds. |
| `lightrag_http_requests_in_progress` | Gauge | `method` | Requests currently being processed. |

### Low-cardinality route labels

The `route` label uses the matched **route template** (e.g. `/documents/{doc_id}`)
rather than the raw path, so individual resource ids do not explode the label
space. Requests that match no route (404s) collapse to a single `unmatched`
bucket.

## Scraping with Prometheus

The endpoint is public by default (it is part of the default
`WHITELIST_PATHS`: `/health,/metrics,/api/*`) so a scraper needs no
credentials. Example scrape config:

```yaml
scrape_configs:
  - job_name: lightrag
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets: ["lightrag:9621"]
```

To lock the endpoint down, remove `metrics` from `WHITELIST_PATHS` and/or
restrict it at your network edge.

## Request correlation ids

Every response carries an `X-Request-ID` header:

- if the inbound request already provides `X-Request-ID`, it is preserved
  (allowing a trace to span multiple services);
- otherwise a random id is generated (`uuid4().hex`).

The id is also published through a `ContextVar`, so application code and
structured log lines can read it:

```python
from lightrag.api.observability import get_request_id

request_id = get_request_id()
```

## Middleware ordering

The middleware is registered as the **outermost** layer of the ASGI stack, so
it measures the full request cost (including time spent in CORS, body-limit and
admission-control layers) and captures every status code. It only appends the
`X-Request-ID` response header and never strips headers set by the inner stack.
