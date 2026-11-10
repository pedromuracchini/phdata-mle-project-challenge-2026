"""X-Request-ID: a safe incoming ID is kept, anything else is replaced by a UUID4 (ADR 005)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from api.middleware import RequestIDMiddleware


def assert_uuid4(value: str) -> None:
    assert uuid.UUID(value).version == 4, value


def test_generates_a_uuid4_without_an_incoming_id(test_client: TestClient) -> None:
    response = test_client.get("/health")

    assert_uuid4(response.headers["x-request-id"])


def test_each_request_gets_its_own_id(test_client: TestClient) -> None:
    ids = {test_client.get("/health").headers["x-request-id"] for _ in range(20)}

    assert len(ids) == 20


@pytest.mark.parametrize("incoming", ["abc", "req-123_4.5", "A" * 64, str(uuid.uuid4())])
def test_keeps_a_safe_incoming_id(test_client: TestClient, incoming: str) -> None:
    response = test_client.get("/health", headers={"X-Request-ID": incoming})

    assert response.headers["x-request-id"] == incoming


@pytest.mark.parametrize(
    "incoming", ["", "A" * 65, "a b", "<script>", "id;drop", "a/b", "ä", "id\t1", "x" * 10_000]
)
def test_replaces_an_unsafe_incoming_id(test_client: TestClient, incoming: str) -> None:
    response = test_client.get("/health", headers=[(b"X-Request-ID", incoming.encode())])

    assert response.headers["x-request-id"] != incoming
    assert_uuid4(response.headers["x-request-id"])


def test_response_has_exactly_one_request_id_header(test_client: TestClient) -> None:
    response = test_client.get("/health", headers={"X-Request-ID": "abc"})

    assert response.headers.get_list("x-request-id") == ["abc"]


def test_errors_carry_the_id_too(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    invalid = test_client.post("/predict", json={**sample_home_features, "zipcode": "1"})
    not_found = test_client.get("/nope")

    assert invalid.status_code == 422
    assert not_found.status_code == 404
    for response in (invalid, not_found):
        assert_uuid4(response.headers["x-request-id"])


def test_id_is_available_to_handlers_in_request_state() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/id")
    def read_id(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    with TestClient(app) as client:
        response = client.get("/id", headers={"X-Request-ID": "abc"})
        generated = client.get("/id")

    assert response.json() == {"request_id": "abc"}
    assert generated.json()["request_id"] == generated.headers["x-request-id"]


def test_is_pure_asgi_middleware() -> None:
    assert not issubclass(RequestIDMiddleware, BaseHTTPMiddleware)
