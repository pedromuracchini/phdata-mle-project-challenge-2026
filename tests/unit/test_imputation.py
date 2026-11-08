"""services/imputation.py: pure imputation on the committed imputer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics.pairwise import nan_euclidean_distances

from core.artifacts import ImputerArtifact, load_imputer
from core.config import SRC_DIR, get_settings
from services.imputation import (
    HOUSE_FEATURES,
    SQFT_IDENTITY,
    ImputationError,
    fill_sqft_identity,
    impute_missing,
    missing_fields,
)

# Features filled only by KNN (the sqft ones go through the identity when missing alone).
KNN_ONLY = tuple(f for f in HOUSE_FEATURES if f not in SQFT_IDENTITY)


@pytest.fixture(scope="module")
def artifact() -> ImputerArtifact:
    return load_imputer(get_settings())


@pytest.fixture(scope="module")
def unseen(artifact: ImputerArtifact) -> pd.DataFrame:
    """future_unseen_examples joined with demographics, in the model's column order."""
    settings = get_settings()
    houses = pd.read_csv(SRC_DIR / "data" / "future_unseen_examples.csv", dtype={"zipcode": str})
    demographics = pd.read_csv(settings.demographics_path, dtype={"zipcode": str})
    merged = houses.merge(demographics, how="left", on="zipcode")
    return merged[list(artifact.columns)]


def row(unseen: pd.DataFrame, index: int, missing: tuple[str, ...] = ()) -> pd.DataFrame:
    frame = unseen.iloc[[index]].reset_index(drop=True).copy()
    for column in missing:
        frame[column] = np.nan
    return frame


# future_unseen_examples rows are copies of kc_house_data rows. Row 3 is also in the model's
# train split (the imputer's donors), so its nearest donor is itself at distance 0; row 8 is not.
ROW_IN_FIT_DATA = 3
ROW_NOT_IN_FIT_DATA = 8


def reference_knn(artifact: ImputerArtifact, frame: pd.DataFrame, column: str) -> float:
    """Distance-weighted mean of the 5 nearest training rows (nan_euclidean), by hand.

    Weights are 1/distance; if any of the 5 is at distance 0, those donors share all the weight
    (scikit-learn's rule for weights="distance").
    """
    fit_x = artifact.imputer._fit_X
    distances = nan_euclidean_distances(frame.to_numpy(dtype=float), fit_x)[0]
    order = np.argsort(distances, kind="stable")
    assert distances[order[4]] < distances[order[5]], "tie at the 5th neighbour"
    nearest = distances[order[:5]]
    zero = nearest == 0
    weights = zero.astype(float) if zero.any() else 1.0 / nearest
    target = fit_x[order[:5], artifact.columns.index(column)]
    return float(np.sum(weights * target) / np.sum(weights))


def test_complete_frame_is_returned_untouched(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    frame = row(unseen, 0)

    result = impute_missing(frame, artifact.imputer, artifact.columns)

    assert result.features is frame
    assert result.imputed_fields == []


@pytest.mark.parametrize("column", HOUSE_FEATURES)
def test_single_missing_field_is_filled_and_nothing_else_changes(
    artifact: ImputerArtifact, unseen: pd.DataFrame, column: str
) -> None:
    complete = row(unseen, 0)
    frame = row(unseen, 0, missing=(column,))

    result = impute_missing(frame, artifact.imputer, artifact.columns)

    assert result.imputed_fields == [column]
    assert np.isfinite(result.features.to_numpy()).all()
    others = [name for name in artifact.columns if name != column]
    pd.testing.assert_frame_equal(
        result.features[others], complete[others].astype("float64"), check_exact=True
    )


@pytest.mark.parametrize("index", [ROW_IN_FIT_DATA, ROW_NOT_IN_FIT_DATA])
@pytest.mark.parametrize("column", KNN_ONLY)
def test_imputed_value_matches_the_notebook_knn_by_hand(
    artifact: ImputerArtifact, unseen: pd.DataFrame, column: str, index: int
) -> None:
    frame = row(unseen, index, missing=(column,))

    result = impute_missing(frame, artifact.imputer, artifact.columns)

    assert result.features.loc[0, column] == pytest.approx(
        reference_knn(artifact, frame, column), rel=1e-12
    )


def test_a_row_in_the_fit_data_recovers_its_own_value(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    # Documents a property of the data (and why evaluation must not use these rows): a masked
    # value of a training row is recovered exactly from the row itself.
    complete = row(unseen, ROW_IN_FIT_DATA)
    frame = row(unseen, ROW_IN_FIT_DATA, missing=("sqft_lot",))

    result = impute_missing(frame, artifact.imputer, artifact.columns)

    assert result.features.loc[0, "sqft_lot"] == complete.loc[0, "sqft_lot"]


def test_all_house_features_missing_are_all_filled(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    frame = row(unseen, 0, missing=HOUSE_FEATURES)

    result = impute_missing(frame, artifact.imputer, artifact.columns)

    assert result.imputed_fields == list(HOUSE_FEATURES)
    assert np.isfinite(result.features.to_numpy()).all()


def test_none_values_from_a_request_are_treated_as_missing(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    frame = row(unseen, 0).astype(object)
    frame.loc[0, "bathrooms"] = None

    result = impute_missing(frame, artifact.imputer, artifact.columns)

    assert result.imputed_fields == ["bathrooms"]
    assert np.isfinite(result.features.to_numpy(dtype=float)).all()


def test_missing_demographics_are_rejected(artifact: ImputerArtifact, unseen: pd.DataFrame) -> None:
    frame = row(unseen, 0, missing=("ppltn_qty",))

    with pytest.raises(ImputationError, match="ppltn_qty"):
        impute_missing(frame, artifact.imputer, artifact.columns)


def test_columns_in_another_order_are_rejected(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    frame = row(unseen, 0)
    reordered = frame[list(reversed(frame.columns))]

    with pytest.raises(ImputationError, match="order"):
        impute_missing(reordered, artifact.imputer, artifact.columns)


def test_missing_fields_lists_columns_in_order() -> None:
    frame = pd.DataFrame([{"a": 1.0, "b": np.nan, "c": None, "d": 2.0}])

    assert missing_fields(frame) == ["b", "c"]


def test_service_does_not_import_fastapi() -> None:
    code = (
        "import sys, services.imputation; "
        "sys.exit(any(m.split('.')[0] in {'fastapi', 'starlette'} for m in sys.modules))"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(SRC_DIR))}
    completed = subprocess.run([sys.executable, "-c", code], env=env, check=False)

    assert completed.returncode == 0


# --- deterministic sqft fill -----------------------------------------------------------------


def cell(frame: pd.DataFrame, column: str) -> float:
    """The single row's value in ``column``, typed as float."""
    return float(frame[column].to_numpy(dtype=float)[0])


class NoKnn:
    """Stands in for the imputer where KNN must not be reached."""

    def transform(self, frame: pd.DataFrame) -> None:
        raise AssertionError("KNN was called")


class RecordingKnn:
    """Wraps the real imputer and records what KNN received."""

    def __init__(self, imputer: object) -> None:
        self.imputer = imputer
        self.received: list[pd.DataFrame] = []

    def transform(self, frame: pd.DataFrame) -> object:
        self.received.append(frame.copy())
        return self.imputer.transform(frame)  # type: ignore[attr-defined]


@pytest.mark.parametrize("index", [ROW_IN_FIT_DATA, ROW_NOT_IN_FIT_DATA])
@pytest.mark.parametrize("column", SQFT_IDENTITY)
def test_one_missing_sqft_value_comes_from_the_identity_without_knn(
    artifact: ImputerArtifact, unseen: pd.DataFrame, column: str, index: int
) -> None:
    complete = row(unseen, index)
    frame = row(unseen, index, missing=(column,))

    result = impute_missing(frame, NoKnn(), artifact.columns)

    assert result.imputed_fields == [column]
    pd.testing.assert_frame_equal(result.features, complete.astype("float64"), check_exact=True)


def test_two_missing_sqft_values_go_to_knn(artifact: ImputerArtifact, unseen: pd.DataFrame) -> None:
    knn = RecordingKnn(artifact.imputer)
    frame = row(unseen, ROW_NOT_IN_FIT_DATA, missing=("sqft_above", "sqft_basement"))

    result = impute_missing(frame, knn, artifact.columns)

    assert result.imputed_fields == ["sqft_above", "sqft_basement"]
    assert knn.received[0][["sqft_above", "sqft_basement"]].isna().all(axis=None)


def test_identity_runs_before_knn_and_knn_sees_the_filled_value(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    knn = RecordingKnn(artifact.imputer)
    complete = row(unseen, ROW_NOT_IN_FIT_DATA)
    frame = row(unseen, ROW_NOT_IN_FIT_DATA, missing=("bedrooms", "sqft_basement"))

    result = impute_missing(frame, knn, artifact.columns)

    assert result.imputed_fields == ["bedrooms", "sqft_basement"]
    sent = knn.received[0]
    assert cell(sent, "sqft_basement") == cell(complete, "sqft_basement")
    assert np.isnan(cell(sent, "bedrooms"))
    assert cell(result.features, "sqft_basement") == cell(complete, "sqft_basement")


def test_contradictory_sqft_values_fall_back_to_knn(
    artifact: ImputerArtifact, unseen: pd.DataFrame
) -> None:
    # sqft_above larger than sqft_living: the identity would give a negative basement.
    knn = RecordingKnn(artifact.imputer)
    frame = row(unseen, ROW_NOT_IN_FIT_DATA, missing=("sqft_basement",))
    frame.loc[0, "sqft_above"] = cell(frame, "sqft_living") + 100

    result = impute_missing(frame, knn, artifact.columns)

    assert len(knn.received) == 1
    assert result.imputed_fields == ["sqft_basement"]
    assert cell(result.features, "sqft_basement") >= 0


def test_fill_sqft_identity_on_several_rows() -> None:
    nan = np.nan
    frame = pd.DataFrame(
        {
            "sqft_living": [nan, 2000.0, 2000.0, nan, 2000.0, 1000.0],
            "sqft_above": [1500.0, nan, 1500.0, nan, 1500.0, 1500.0],
            "sqft_basement": [500.0, 500.0, nan, 500.0, 500.0, nan],
        }
    )

    filled = fill_sqft_identity(frame)

    expected = pd.DataFrame(
        {
            "sqft_living": [2000.0, 2000.0, 2000.0, nan, 2000.0, 1000.0],
            "sqft_above": [1500.0, 1500.0, 1500.0, nan, 1500.0, 1500.0],
            "sqft_basement": [500.0, 500.0, 500.0, 500.0, 500.0, nan],  # last: would be -500
        }
    )
    pd.testing.assert_frame_equal(filled, expected)
