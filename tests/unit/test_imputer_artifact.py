"""The committed imputer artifact is exactly what scripts/fit_imputer.py produces."""

from __future__ import annotations

import json
import pickle
import shutil
from pathlib import Path

import numpy as np
import pytest

from core.config import Settings, get_settings
from core.imputer_artifact import (
    N_NEIGHBORS,
    WEIGHTS,
    check_artifact,
    load_model_train_split,
    read_model_features,
)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings whose artifact paths point at a copy of the committed artifact."""
    settings = get_settings()
    copy = Settings(
        imputer_path=tmp_path / "imputer.joblib",
        imputer_metadata_path=tmp_path / "imputer_metadata.json",
    )
    shutil.copy(settings.imputer_path, copy.imputer_path)
    shutil.copy(settings.imputer_metadata_path, copy.imputer_metadata_path)
    return copy


def test_committed_artifact_matches_a_refit() -> None:
    assert check_artifact(get_settings()) == []


def test_train_split_is_the_models_own() -> None:
    settings = get_settings()
    with settings.model_path.open("rb") as handle:
        model = pickle.load(handle)
    x_train, y_train = load_model_train_split(settings)

    scaled = model.named_steps["robustscaler"].transform(x_train)
    knn = model.named_steps["kneighborsregressor"]
    assert np.array_equal(scaled, knn._fit_X)
    assert np.array_equal(y_train.to_numpy(), knn._y)


def test_metadata_records_the_notebook_configuration() -> None:
    settings = get_settings()
    metadata = json.loads(settings.imputer_metadata_path.read_text())

    assert metadata["params"]["n_neighbors"] == N_NEIGHBORS == 5
    assert metadata["params"]["weights"] == WEIGHTS == "distance"
    assert metadata["scaling"] is None
    assert metadata["columns"] == read_model_features(settings)
    assert set(metadata["versions"]) == {"python", "scikit-learn", "joblib", "numpy", "pandas"}
    for column in ("price", "id", "date", "zipcode"):
        assert column not in metadata["columns"]


def test_check_detects_a_modified_artifact(tmp_settings: Settings) -> None:
    with tmp_settings.imputer_path.open("ab") as handle:
        handle.write(b"\0")

    problems = check_artifact(tmp_settings)

    assert any("artifact sha256" in problem for problem in problems)


def test_check_detects_modified_metadata(tmp_settings: Settings) -> None:
    metadata = json.loads(tmp_settings.imputer_metadata_path.read_text())
    metadata["params"]["n_neighbors"] = 7
    tmp_settings.imputer_metadata_path.write_text(json.dumps(metadata))

    problems = check_artifact(tmp_settings)

    assert any(problem.startswith("metadata params") for problem in problems)


def test_check_ignores_fitted_at(tmp_settings: Settings) -> None:
    metadata = json.loads(tmp_settings.imputer_metadata_path.read_text())
    metadata["fitted_at"] = "2000-01-01T00:00:00Z"
    tmp_settings.imputer_metadata_path.write_text(json.dumps(metadata))

    assert check_artifact(tmp_settings) == []


def test_check_reports_a_missing_artifact(tmp_settings: Settings) -> None:
    tmp_settings.imputer_path.unlink()

    problems = check_artifact(tmp_settings)

    assert len(problems) == 1
    assert "make fit-imputer" in problems[0]
