"""Structured request log: one JSON line per request, never the payload (ADR 005)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import api.endpoints
from api.middleware import REQUEST_LOGGER, RequestLogMiddleware
from core.logging import JsonFormatter, build_queue_logging

FIELDS = {"request_id", "method", "path", "status", "latency_ms", "imputed_fields", "model_version"}


@pytest.fixture
def request_records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    # Yield caplog itself: caplog.records is a new list in each test phase.
    with caplog.at_level(logging.INFO, logger=REQUEST_LOGGER):
        yield caplog


def request_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    formatter = JsonFormatter()
    return [json.loads(formatter.format(r)) for r in caplog.records if r.name == REQUEST_LOGGER]


def test_one_line_per_request_with_the_agreed_fields(
    test_client: TestClient,
    sample_home_features: dict[str, Any],
    request_records: pytest.LogCaptureFixture,
) -> None:
    response = test_client.post(
        "/predict",
        json={**sample_home_features, "bedrooms": None},
        headers={"X-Request-ID": "log-1"},
    )

    [line] = request_lines(request_records)
    assert set(line) >= FIELDS
    assert line["request_id"] == "log-1"
    assert line["method"] == "POST"
    assert line["path"] == "/predict"
    assert line["status"] == 200
    assert line["imputed_fields"] == ["bedrooms"]
    assert line["model_version"] == response.json()["model_version"]
    assert 0 < line["latency_ms"] < 60_000
    assert line["level"] == "INFO"


def test_errors_are_logged_with_their_status(
    test_client: TestClient, request_records: pytest.LogCaptureFixture
) -> None:
    test_client.post("/predict", json={"zipcode": "00000"})
    test_client.get("/nope")
    test_client.get("/health")

    lines = request_lines(request_records)
    assert [(line["path"], line["status"]) for line in lines] == [
        ("/predict", 422),
        ("/nope", 404),
        ("/health", 200),
    ]
    assert all(line["imputed_fields"] is None for line in lines)


def test_unhandled_exception_is_logged_as_500(
    monkeypatch: pytest.MonkeyPatch,
    sample_home_features: dict[str, Any],
    request_records: pytest.LogCaptureFixture,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(api.endpoints, "impute_missing", explode)
    from main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/predict", json=sample_home_features, headers={"X-Request-ID": "log-500"})

    [line] = request_lines(request_records)
    assert (line["request_id"], line["status"]) == ("log-500", 500)


def test_payload_and_features_are_never_logged(
    test_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"zipcode": "98042", "sqft_lot": 4321.987, "bedrooms": 7, "floors": None}
    with caplog.at_level(logging.DEBUG):
        test_client.post("/predict", json=payload)
        test_client.post("/predict", json={**payload, "typo_field": "SENTINEL-77"})

    formatter = JsonFormatter()
    text = "\n".join(formatter.format(record) for record in caplog.records)
    for value in ("4321.987", "SENTINEL-77", "typo_field", "sqft_lot", "98042"):
        assert value not in text


def test_formatter_writes_one_json_object_with_extras_and_traceback() -> None:
    try:
        raise ValueError("bad")
    except ValueError as error:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, None)
        record.exc_info = (type(error), error, error.__traceback__)
    record.request_id = "abc"

    line = JsonFormatter().format(record)

    assert "\n" not in line
    data = json.loads(line)
    assert data["message"] == "failed"
    assert data["level"] == "ERROR"
    assert data["request_id"] == "abc"
    assert "Traceback" in data["exc_info"] and "ValueError: bad" in data["exc_info"]
    assert data["ts"].endswith("Z")


def test_formatter_drops_uvicorn_color_message() -> None:
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "running", None, None)
    record.color_message = "\x1b[1mrunning\x1b[0m"

    assert "color_message" not in json.loads(JsonFormatter().format(record))


def test_queue_logging_writes_json_lines_from_a_thread() -> None:
    stream = io.StringIO()
    handler, listener = build_queue_logging(stream)
    logger = logging.getLogger("test.queue")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    listener.start()
    try:
        logger.info("one", extra={"status": 200})
        logger.info("two")
    finally:
        listener.stop()
        logger.removeHandler(handler)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["one", "two"]
    assert lines[0]["status"] == 200


def test_uvicorn_access_log_is_replaced(test_client: TestClient) -> None:
    assert logging.getLogger("uvicorn.access").disabled


def test_is_pure_asgi_middleware() -> None:
    assert not issubclass(RequestLogMiddleware, BaseHTTPMiddleware)
