"""Startup artifact loading: valid artifacts load, invalid ones stop the app from starting."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.artifacts import ArtifactError, ImputerArtifact, load_imputer
from core.config import Settings, get_settings


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings pointing at a copy of the committed imputer artifact."""
    settings = get_settings()
    copy = Settings(
        imputer_path=tmp_path / "imputer.joblib",
        imputer_metadata_path=tmp_path / "imputer_metadata.json",
    )
    shutil.copy(settings.imputer_path, copy.imputer_path)
    shutil.copy(settings.imputer_metadata_path, copy.imputer_metadata_path)
    return copy


def edit_metadata(settings: Settings, change: Callable[[dict[str, Any]], None]) -> None:
    metadata = json.loads(settings.imputer_metadata_path.read_text())
    change(metadata)
    settings.imputer_metadata_path.write_text(json.dumps(metadata))


@pytest.fixture
def clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_committed_imputer_loads() -> None:
    artifact = load_imputer(get_settings())

    assert len(artifact.columns) == 33
    assert artifact.columns[:7] == (
        "bedrooms",
        "bathrooms",
        "sqft_living",
        "sqft_lot",
        "floors",
        "sqft_above",
        "sqft_basement",
    )


def test_missing_artifact_is_rejected(tmp_settings: Settings) -> None:
    tmp_settings.imputer_path.unlink()

    with pytest.raises(ArtifactError, match="make fit-imputer"):
        load_imputer(tmp_settings)


def test_modified_artifact_is_rejected(tmp_settings: Settings) -> None:
    with tmp_settings.imputer_path.open("ab") as handle:
        handle.write(b"\0")

    with pytest.raises(ArtifactError, match="sha256"):
        load_imputer(tmp_settings)


def test_other_library_version_is_rejected(tmp_settings: Settings) -> None:
    edit_metadata(tmp_settings, lambda m: m["versions"].update({"scikit-learn": "0.0.0"}))

    with pytest.raises(ArtifactError, match="scikit-learn"):
        load_imputer(tmp_settings)


def test_column_order_other_than_the_models_is_rejected(tmp_settings: Settings) -> None:
    def swap_first_two(metadata: dict[str, Any]) -> None:
        columns = metadata["columns"]
        columns[0], columns[1] = columns[1], columns[0]

    edit_metadata(tmp_settings, swap_first_two)

    with pytest.raises(ArtifactError, match="model's features"):
        load_imputer(tmp_settings)


def test_lifespan_puts_the_imputer_on_app_state(test_client: TestClient) -> None:
    assert isinstance(test_client.app.state.imputer, ImputerArtifact)  # type: ignore[attr-defined]


@pytest.mark.usefixtures("clear_settings_cache")
def test_app_does_not_start_without_the_imputer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from main import app

    monkeypatch.setenv("APP_IMPUTER_PATH", str(tmp_path / "missing.joblib"))

    with pytest.raises(ArtifactError), TestClient(app):
        pass
