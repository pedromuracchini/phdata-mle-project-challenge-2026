"""The persisted KNN imputer: how it is fitted, described and verified.

Used by ``scripts/fit_imputer.py`` (fit, save, ``--check``) and its test. Nothing here runs on
the request path.

Configuration is the notebook's (``notebooks/imputation_experiment.ipynb``):
``KNNImputer(n_neighbors=5, weights="distance")`` on raw values, no scaling. Columns are the
model's 33 features in the model's order. Fit data is the model's own train split, rebuilt
exactly as ``create_model.py`` does it (see ``load_model_train_split``).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.impute import KNNImputer
from sklearn.model_selection import train_test_split

from core.config import SRC_DIR, Settings

N_NEIGHBORS = 5
WEIGHTS = "distance"

# create_model.py: the sales columns it reads and the split it trains on.
SALES_COLUMNS = [
    "price",
    "bedrooms",
    "bathrooms",
    "sqft_living",
    "sqft_lot",
    "floors",
    "sqft_above",
    "sqft_basement",
    "zipcode",
]
TARGET = "price"
SPLIT_RANDOM_STATE = 42

# Fitted attributes of KNNImputer that define its behaviour (sklearn 1.7).
FITTED_ATTRIBUTES = ("_fit_X", "_mask_fit_X", "_valid_mask", "n_features_in_", "feature_names_in_")
# Metadata keys that legitimately change on every refit.
VOLATILE_METADATA_KEYS = frozenset({"fitted_at"})


def read_model_features(settings: Settings) -> list[str]:
    """Feature names in the order the model was trained on."""
    features: list[str] = json.loads(settings.model_features_path.read_text())
    return features


def load_model_train_split(settings: Settings) -> tuple[pd.DataFrame, pd.Series]:
    """The model's training rows and prices, rebuilt exactly as ``create_model.py`` does.

    Sales are left-joined with demographics on ``zipcode`` (kept as a string), ``zipcode`` and
    ``price`` are dropped from the features, then ``train_test_split(random_state=42)`` keeps
    the default 75 % train part. Columns are returned in the model's feature order.
    """
    sales = pd.read_csv(settings.sales_path, usecols=SALES_COLUMNS, dtype={"zipcode": str})
    demographics = pd.read_csv(settings.demographics_path, dtype={"zipcode": str})
    merged = sales.merge(demographics, how="left", on="zipcode").drop(columns="zipcode")
    y = merged.pop(TARGET)
    x_train, _x_test, y_train, _y_test = train_test_split(
        merged, y, random_state=SPLIT_RANDOM_STATE
    )
    features = read_model_features(settings)
    return x_train[features], y_train


def fit_imputer(x_train: pd.DataFrame) -> Any:
    """Fit the notebook's KNN imputer on ``x_train`` (no scaling)."""
    return KNNImputer(n_neighbors=N_NEIGHBORS, weights=WEIGHTS).fit(x_train)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    """Path relative to src/ when possible, so metadata carries no machine-specific paths."""
    try:
        return path.resolve().relative_to(SRC_DIR).as_posix()
    except ValueError:
        return str(path)


def environment_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "scikit-learn": sklearn.__version__,
        "joblib": joblib.__version__,
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }


def build_metadata(
    imputer: Any, x_train: pd.DataFrame, settings: Settings, artifact_sha256: str
) -> dict[str, Any]:
    """Describe a fitted imputer: configuration, fit data, environment and artifact hash."""
    params = imputer.get_params()
    return {
        # File name only: the metadata sits next to the artifact wherever it is deployed.
        "artifact": settings.imputer_path.name,
        "artifact_sha256": artifact_sha256,
        "estimator": f"{type(imputer).__module__}.{type(imputer).__name__}",
        "params": {
            "n_neighbors": params["n_neighbors"],
            "weights": params["weights"],
            "metric": params["metric"],
            "missing_values": "nan",
            "add_indicator": params["add_indicator"],
            "keep_empty_features": params["keep_empty_features"],
        },
        "scaling": None,
        "columns": list(x_train.columns),
        "fit_data": {
            "sources": {
                _relative(path): sha256_file(path)
                for path in (settings.sales_path, settings.demographics_path)
            },
            "selection": (
                "create_model.py rows: sales left-joined with demographics on zipcode, "
                f"train_test_split(random_state={SPLIT_RANDOM_STATE}), train part"
            ),
            "n_rows": len(x_train),
        },
        "versions": environment_versions(),
        "generator": "scripts/fit_imputer.py",
        "fitted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@dataclass(frozen=True)
class FitResult:
    imputer: Any
    metadata: dict[str, Any]


def fit_and_save(settings: Settings) -> FitResult:
    """Fit on the model's train split and write the artifact and its metadata."""
    x_train, _y_train = load_model_train_split(settings)
    imputer = fit_imputer(x_train)
    settings.imputer_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(imputer, settings.imputer_path)
    metadata = build_metadata(imputer, x_train, settings, sha256_file(settings.imputer_path))
    settings.imputer_metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return FitResult(imputer=imputer, metadata=metadata)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left):
        return math.isnan(right)
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        left_array, right_array = np.asarray(left), np.asarray(right)
        if left_array.dtype.kind == "f" and right_array.dtype.kind == "f":
            return bool(np.array_equal(left_array, right_array, equal_nan=True))
        return bool(np.array_equal(left_array, right_array))
    return bool(left == right)


def compare_fitted_state(committed: Any, refit: Any) -> list[str]:
    """Differences in parameters and fitted attributes between two imputers."""
    problems = []
    if type(committed) is not type(refit):
        problems.append(f"estimator type: {type(committed)} != {type(refit)}")
        return problems
    committed_params, refit_params = committed.get_params(), refit.get_params()
    if committed_params.keys() != refit_params.keys() or not all(
        _same_value(committed_params[key], refit_params[key]) for key in committed_params
    ):
        problems.append(f"params: {committed_params} != {refit_params}")
    for name in FITTED_ATTRIBUTES:
        if not _same_value(getattr(committed, name, None), getattr(refit, name, None)):
            problems.append(f"fitted attribute {name} differs")
    return problems


def dumped_sha256(imputer: Any) -> str:
    """sha256 of the bytes ``joblib.dump`` would write (dumps are deterministic)."""
    buffer = io.BytesIO()
    joblib.dump(imputer, buffer)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def check_artifact(settings: Settings) -> list[str]:
    """Refit in memory and list every way the committed artifact differs. Empty means OK.

    Checks the artifact file hash against its metadata, refits on the current source data and
    compares both the fitted state and the bytes a dump of the refit would produce, and
    compares the metadata (except ``fitted_at``) with what a refit in this environment would
    write. Nothing is written to disk.
    """
    for path in (settings.imputer_path, settings.imputer_metadata_path):
        if not path.is_file():
            return [f"missing artifact: {path} (run `make fit-imputer`)"]

    problems = []
    recorded: dict[str, Any] = json.loads(settings.imputer_metadata_path.read_text())
    actual_sha = sha256_file(settings.imputer_path)
    if recorded.get("artifact_sha256") != actual_sha:
        problems.append(
            f"artifact sha256 {actual_sha} != metadata {recorded.get('artifact_sha256')}"
        )

    committed = joblib.load(settings.imputer_path)
    x_train, _y_train = load_model_train_split(settings)
    refit = fit_imputer(x_train)
    problems.extend(compare_fitted_state(committed, refit))
    refit_sha = dumped_sha256(refit)
    if refit_sha != actual_sha:
        problems.append(f"a refit dumps to sha256 {refit_sha}, committed file is {actual_sha}")

    expected = build_metadata(refit, x_train, settings, actual_sha)
    for key in sorted((set(recorded) | set(expected)) - VOLATILE_METADATA_KEYS):
        if recorded.get(key) != expected.get(key):
            problems.append(f"metadata {key}: {recorded.get(key)!r} != {expected.get(key)!r}")
    return problems
