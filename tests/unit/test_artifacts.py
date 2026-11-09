"""Startup artifact loading: valid artifacts load, invalid ones stop the app from starting."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from core.artifacts import (
    ArtifactError,
    DemographicsLookup,
    ImputerArtifact,
    ModelArtifact,
    load_demographics,
    load_imputer,
    load_model,
)
from core.config import SRC_DIR, Settings, get_settings
from services.imputation import HOUSE_FEATURES


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


# --- model and demographics (loaded once at startup, Phase 4) --------------------------------


def test_committed_model_loads_with_its_feature_order() -> None:
    settings = get_settings()

    artifact = load_model(settings)

    assert artifact.features == tuple(json.loads(settings.model_features_path.read_text()))
    assert len(artifact.features) == 33


def test_model_with_another_feature_order_is_rejected(tmp_path: Path) -> None:
    settings = get_settings()
    features = json.loads(settings.model_features_path.read_text())
    features[0], features[1] = features[1], features[0]
    reordered = tmp_path / "model_features.json"
    reordered.write_text(json.dumps(features))

    with pytest.raises(ArtifactError, match="feature names"):
        load_model(Settings(model_features_path=reordered))


def test_missing_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="missing artifact"):
        load_model(Settings(model_path=tmp_path / "model.pkl"))


@pytest.fixture
def model_features() -> tuple[str, ...]:
    return load_model(get_settings()).features


def test_demographics_lookup_holds_every_zipcode_in_model_order(
    model_features: tuple[str, ...],
) -> None:
    settings = get_settings()
    csv = pd.read_csv(settings.demographics_path, dtype={"zipcode": str}).set_index("zipcode")

    lookup = load_demographics(settings, model_features)

    assert set(lookup.rows) == set(csv.index)
    for zipcode, row in lookup.rows.items():
        for position, name in enumerate(model_features):
            if name in HOUSE_FEATURES:
                assert np.isnan(row[position])
                assert lookup.house_positions[name] == position
            else:
                assert row[position] == csv.loc[zipcode, name]


def write_demographics(tmp_path: Path, change: Callable[[pd.DataFrame], pd.DataFrame]) -> Settings:
    settings = get_settings()
    frame = change(pd.read_csv(settings.demographics_path, dtype={"zipcode": str}))
    path = tmp_path / "zipcode_demographics.csv"
    frame.to_csv(path, index=False)
    return Settings(demographics_path=path)


def test_duplicated_zipcodes_are_rejected(tmp_path: Path, model_features: tuple[str, ...]) -> None:
    settings = write_demographics(tmp_path, lambda f: pd.concat([f, f.head(1)]))

    with pytest.raises(ArtifactError, match="duplicated"):
        load_demographics(settings, model_features)


def test_zipcodes_that_lost_their_leading_digits_are_rejected(
    tmp_path: Path, model_features: tuple[str, ...]
) -> None:
    def shorten(frame: pd.DataFrame) -> pd.DataFrame:
        frame.loc[0, "zipcode"] = "8042"
        return frame

    with pytest.raises(ArtifactError, match="5-digit"):
        load_demographics(write_demographics(tmp_path, shorten), model_features)


def test_demographics_without_a_model_feature_are_rejected(
    tmp_path: Path, model_features: tuple[str, ...]
) -> None:
    demographic = next(name for name in model_features if name not in HOUSE_FEATURES)
    settings = write_demographics(tmp_path, lambda f: f.drop(columns=demographic))

    with pytest.raises(ArtifactError, match="lacks model features"):
        load_demographics(settings, model_features)


def test_lifespan_puts_model_and_demographics_on_app_state(test_client: TestClient) -> None:
    state = test_client.app.state  # type: ignore[attr-defined]

    assert isinstance(state.model, ModelArtifact)
    assert isinstance(state.demographics, DemographicsLookup)


def test_requests_read_nothing_from_disk(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After startup, /predict reads no artifact or data file: everything is on app.state.

    Reads outside ``src/`` stay allowed: scikit-learn's KNN calls threadpoolctl on every
    predict, which reads ``/proc/self/maps`` (procfs, not an artifact).
    """
    import builtins
    import pickle

    import joblib

    def guarded(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            target = args[0] if args else kwargs.get("file", kwargs.get("filepath_or_buffer"))
            if isinstance(target, str | Path) and Path(target).resolve().is_relative_to(SRC_DIR):
                raise AssertionError(f"disk read on the request path: {target}")
            return original(*args, **kwargs)

        return wrapper

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("artifact deserialized on the request path")

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(pd, "read_csv", guarded(pd.read_csv))
    for path_method in ("open", "read_text", "read_bytes"):
        original = getattr(Path, path_method)
        monkeypatch.setattr(
            Path, path_method, lambda self, *a, _o=original, **k: guarded(_o)(self, *a, **k)
        )
    monkeypatch.setattr(pickle, "load", never)
    monkeypatch.setattr(joblib, "load", never)

    # The guard catches what the v0 route did on every request.
    settings = get_settings()
    with pytest.raises(AssertionError, match="disk read"):
        open(settings.model_path, "rb")  # noqa: SIM115
    with pytest.raises(AssertionError, match="disk read"):
        pd.read_csv(settings.demographics_path)
    with pytest.raises(AssertionError, match="disk read"):
        settings.model_features_path.read_text()

    complete = {
        "bedrooms": 3,
        "bathrooms": 2.0,
        "sqft_living": 1500.0,
        "sqft_lot": 5000.0,
        "floors": 1.0,
        "sqft_above": 1200.0,
        "sqft_basement": 300.0,
        "zipcode": "98042",
    }
    for payload in (complete, {**complete, "bedrooms": None}, {"zipcode": "98042"}):
        response = test_client.post("/predict", json=payload)
        assert response.status_code == 200, response.text
