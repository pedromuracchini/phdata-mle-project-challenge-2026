"""Prometheus metrics (served on ``GET /metrics``).

- ``http_requests_total{method, route, status}`` and
  ``http_request_duration_seconds{method, route}``: every HTTP request, recorded by the request
  middleware. ``route`` is the matched route's path template, or ``unmatched``, so a client
  cannot create label values.
- ``prediction_missing_fields_total{feature}``: each house field that was missing (and imputed).
- ``predicted_price_dollars``: every predicted price, the basis for drift monitoring. Buckets
  are the training prices' quantiles (``PRICE_QUANTILES`` of kc_house_data.csv, rounded to
  $10,000), so a shift in the predicted price distribution shows as a shift between buckets.

With several uvicorn workers each process has its own counters. When
``PROMETHEUS_MULTIPROC_DIR`` is set (the image sets it), prometheus_client writes them to files
in that directory and a scrape, answered by any worker, aggregates every process's files. The
directory must be empty when the server starts (the image's ENTRYPOINT clears it). Without the
variable (tests, local runs) the process's own registry is served.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
PRICE_QUANTILES = (0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99)
PRICE_BUCKETS = (
    150_000.0,
    210_000.0,
    240_000.0,
    320_000.0,
    450_000.0,
    640_000.0,
    890_000.0,
    1_160_000.0,
    1_960_000.0,
)

REQUESTS = Counter("http_requests_total", "HTTP requests.", ["method", "route", "status"])
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Time from receiving a request to sending its response.",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
MISSING_FIELDS = Counter(
    "prediction_missing_fields", "House fields missing from a request (imputed).", ["feature"]
)
PREDICTED_PRICE = Histogram("predicted_price_dollars", "Predicted prices.", buckets=PRICE_BUCKETS)


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    REQUESTS.labels(method, route, str(status)).inc()
    REQUEST_DURATION.labels(method, route).observe(seconds)


def record_prediction(imputed_fields: list[str], price: float) -> None:
    for feature in imputed_fields:
        MISSING_FIELDS.labels(feature).inc()
    PREDICTED_PRICE.observe(price)


def render() -> tuple[bytes, str]:
    """The metrics in the text exposition format, aggregated across processes if needed."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
