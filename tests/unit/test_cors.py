"""CORS is off by default; allowed origins come from APP_CORS_ALLOWED_ORIGINS (ADR 005)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from core.config import Settings
from main import add_cors

ORIGIN = "https://app.example.com"


def app_with(settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.post("/predict")
    def predict() -> dict[str, str]:
        return {"ok": "yes"}

    add_cors(app, settings)
    return app


def test_default_allows_no_origin() -> None:
    assert Settings().cors_allowed_origins == []


def test_the_service_has_no_cors_middleware_by_default(test_client: TestClient) -> None:
    app = test_client.app
    assert not any(m.cls is CORSMiddleware for m in app.user_middleware)  # type: ignore[attr-defined]

    response = test_client.get("/health", headers={"Origin": ORIGIN})

    assert "access-control-allow-origin" not in response.headers


def test_no_cors_headers_when_no_origin_is_configured() -> None:
    client = TestClient(app_with(Settings(cors_allowed_origins=[])))

    response = client.post("/predict", headers={"Origin": ORIGIN})
    preflight = client.options(
        "/predict", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"}
    )

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in preflight.headers


def test_configured_origin_is_allowed_and_others_are_not() -> None:
    client = TestClient(app_with(Settings(cors_allowed_origins=[ORIGIN])))

    allowed = client.post("/predict", headers={"Origin": ORIGIN})
    other = client.post("/predict", headers={"Origin": "https://evil.example"})
    preflight = client.options(
        "/predict",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-request-id",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in allowed.headers
    assert "x-request-id" in allowed.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-origin" not in other.headers
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ORIGIN


def test_origins_are_read_from_the_environment_as_a_json_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}", "https://b.example"]')

    assert Settings().cors_allowed_origins == [ORIGIN, "https://b.example"]
