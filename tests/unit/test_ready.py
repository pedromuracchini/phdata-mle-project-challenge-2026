"""/health is liveness (no dependencies); /ready is readiness (artifacts and model_version)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_ready_reports_the_model_version(test_client: TestClient) -> None:
    response = test_client.get("/ready")

    assert response.status_code == 200
    expected = test_client.app.state.model_version  # type: ignore[attr-defined]
    assert response.json() == {"status": "ready", "model_version": expected}


@pytest.mark.parametrize("missing", ["model", "demographics", "imputer", "model_version"])
def test_not_ready_without_an_artifact(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delattr(test_client.app.state, missing)  # type: ignore[attr-defined]

    response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not ready"}


def test_health_does_not_depend_on_the_artifacts(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("model", "demographics", "imputer", "model_version"):
        monkeypatch.delattr(test_client.app.state, name)  # type: ignore[attr-defined]

    response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
