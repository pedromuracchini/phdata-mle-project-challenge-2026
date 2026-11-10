"""Unhandled exceptions: JSON 500 with the request ID, no traceback; the traceback is logged."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api.endpoints

SECRET = "boom-internal-detail-9c2e"


@pytest.fixture
def failing_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(api.endpoints, "impute_missing", explode)
    from main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_unhandled_exception_is_a_json_500(
    failing_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    response = failing_client.post("/predict", json=sample_home_features)

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"detail", "request_id"}
    assert body["detail"] == "Internal Server Error"
    assert uuid.UUID(body["request_id"]).version == 4


def test_500_carries_the_request_id_in_body_and_header(
    failing_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    response = failing_client.post(
        "/predict", json=sample_home_features, headers={"X-Request-ID": "trace-500"}
    )

    assert response.json()["request_id"] == "trace-500"
    assert response.headers.get_list("x-request-id") == ["trace-500"]


def test_500_body_has_no_traceback_or_exception_text(
    failing_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    response = failing_client.post("/predict", json=sample_home_features)

    assert SECRET not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_traceback_and_request_id_go_to_the_log(
    failing_client: TestClient,
    sample_home_features: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="api"):
        failing_client.post(
            "/predict", json=sample_home_features, headers={"X-Request-ID": "trace-log"}
        )

    records = [r for r in caplog.records if r.name == "api.errors"]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info is not None and SECRET in str(record.exc_info[1])
    assert getattr(record, "request_id", None) == "trace-log"
