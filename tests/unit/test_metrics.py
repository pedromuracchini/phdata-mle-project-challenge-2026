"""Prometheus metrics on /metrics, aggregated across worker processes (ADR 005)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

from core.config import get_settings
from core.metrics import PRICE_BUCKETS, PRICE_QUANTILES

SRC = Path(__file__).resolve().parents[2] / "src"


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_serves_the_text_format(test_client: TestClient) -> None:
    response = test_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "prediction_missing_fields_total",
        "predicted_price_dollars",
    ):
        assert f"# TYPE {name}" in response.text, name


def test_requests_are_counted_by_route_method_and_status(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    ok = {"method": "POST", "route": "/predict", "status": "200"}
    invalid = {**ok, "status": "422"}
    before_ok, before_invalid = (
        value("http_requests_total", **ok),
        value("http_requests_total", **invalid),
    )
    before_latency = value("http_request_duration_seconds_count", method="POST", route="/predict")

    test_client.post("/predict", json=sample_home_features)
    test_client.post("/predict", json={"zipcode": "00000"})

    assert value("http_requests_total", **ok) == before_ok + 1
    assert value("http_requests_total", **invalid) == before_invalid + 1
    assert (
        value("http_request_duration_seconds_count", method="POST", route="/predict")
        == before_latency + 2
    )


def test_unmatched_paths_share_one_label(test_client: TestClient) -> None:
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = value("http_requests_total", **labels)

    for path in ("/nope", "/a/b/c", "/predict/../x"):
        test_client.get(path)

    assert value("http_requests_total", **labels) == before + 3


def test_missing_fields_are_counted_per_feature(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    before = {
        f: value("prediction_missing_fields_total", feature=f) for f in ("bedrooms", "floors")
    }

    test_client.post("/predict", json={**sample_home_features, "bedrooms": None, "floors": None})
    test_client.post("/predict", json={**sample_home_features, "bedrooms": None})
    test_client.post("/predict", json=sample_home_features)

    assert value("prediction_missing_fields_total", feature="bedrooms") == before["bedrooms"] + 2
    assert value("prediction_missing_fields_total", feature="floors") == before["floors"] + 1


def test_predicted_price_histogram_observes_every_prediction(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    count, total = value("predicted_price_dollars_count"), value("predicted_price_dollars_sum")

    price = test_client.post("/predict", json=sample_home_features).json()["predicted_price"]
    test_client.post("/predict", json={"zipcode": "00000"})  # no prediction, not observed

    assert value("predicted_price_dollars_count") == count + 1
    assert value("predicted_price_dollars_sum") == pytest.approx(total + price)


def test_price_buckets_are_the_rounded_training_price_quantiles() -> None:
    prices = pd.read_csv(get_settings().sales_path)["price"]

    expected = [round(prices.quantile(q), -4) for q in PRICE_QUANTILES]

    assert list(PRICE_BUCKETS) == expected


WRITER = """
import sys
from core import metrics
for _ in range(int(sys.argv[1])):
    metrics.observe_request("POST", "/predict", 200, 0.01)
    metrics.record_prediction(["bedrooms"], 450000.0)
"""
READER = "import sys; from core.metrics import render; sys.stdout.write(render()[0].decode())"


def test_multiprocess_scrape_aggregates_every_process(tmp_path: Path) -> None:
    """Two worker processes write; a scrape from a third process sees the sum of both."""
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "PYTHONPATH": str(SRC)}
    for count in (3, 5):
        subprocess.run([sys.executable, "-c", WRITER, str(count)], env=env, check=True)

    scrape = subprocess.run(
        [sys.executable, "-c", READER], env=env, check=True, capture_output=True, text=True
    ).stdout

    samples = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(scrape)
        for sample in family.samples
    }
    request = (("method", "POST"), ("route", "/predict"), ("status", "200"))
    assert samples[("http_requests_total", request)] == 8
    assert samples[("prediction_missing_fields_total", (("feature", "bedrooms"),))] == 8
    assert samples[("predicted_price_dollars_count", ())] == 8
    assert len(list(tmp_path.glob("counter_*.db"))) == 2  # one file per writer process
