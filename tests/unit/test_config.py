"""Settings resolve paths independently of the working directory and honour env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.config import Settings, get_settings


def test_default_paths_exist_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings()

    for path in (settings.model_path, settings.model_features_path, settings.demographics_path):
        assert path.is_absolute()
        assert path.is_file(), path


def test_env_variable_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "other.pkl"
    monkeypatch.setenv("APP_MODEL_PATH", str(override))

    assert Settings().model_path == override


def test_predict_works_outside_src(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_client: TestClient,
    sample_home_features: dict[str, object],
) -> None:
    get_settings.cache_clear()
    monkeypatch.chdir(tmp_path)

    response = test_client.post("/predict", json=sample_home_features)

    assert response.status_code == 200, response.text
