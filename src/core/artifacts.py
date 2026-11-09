"""Load and validate artifacts once at startup (FastAPI ``lifespan``).

Startup fails loudly instead of serving wrong answers: a missing file, an artifact that does not
match its metadata, columns that differ from the model's features, or library versions other
than the ones the artifact was produced with all raise ``ArtifactError``.

Nothing here runs on the request path: the route reads what these functions return from
``app.state``.
"""

from __future__ import annotations

import json
import pickle
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import joblib
import numpy as np
import numpy.typing as npt
import pandas as pd
import sklearn

from core.config import Settings
from core.imputer_artifact import read_model_features, sha256_file
from services.imputation import HOUSE_FEATURES

# Libraries that decide whether the pickled imputer loads and behaves as it was fitted.
PINNED_LIBRARIES = {
    "scikit-learn": sklearn.__version__,
    "joblib": joblib.__version__,
    "numpy": np.__version__,
}


class ArtifactError(RuntimeError):
    """An artifact is missing, corrupt or incompatible with this environment."""


@dataclass(frozen=True)
class ImputerArtifact:
    """The fitted imputer with the column order it expects and its metadata."""

    imputer: Any
    columns: tuple[str, ...]
    metadata: dict[str, Any]


def load_imputer(settings: Settings) -> ImputerArtifact:
    """Load ``imputer.joblib`` and verify it against its metadata and the model's features."""
    for path in (settings.imputer_path, settings.imputer_metadata_path):
        if not path.is_file():
            raise ArtifactError(f"missing artifact {path}; run `make fit-imputer`")

    metadata: dict[str, Any] = json.loads(settings.imputer_metadata_path.read_text())

    actual_sha = sha256_file(settings.imputer_path)
    if actual_sha != metadata.get("artifact_sha256"):
        raise ArtifactError(
            f"{settings.imputer_path} sha256 {actual_sha} does not match its metadata "
            f"({metadata.get('artifact_sha256')})"
        )

    recorded = metadata.get("versions", {})
    mismatched = {
        name: (recorded.get(name), version)
        for name, version in PINNED_LIBRARIES.items()
        if recorded.get(name) != version
    }
    if mismatched:
        raise ArtifactError(
            f"imputer was produced with other library versions (recorded, installed): "
            f"{mismatched}; use the locked environment or refit with `make fit-imputer`"
        )

    columns = tuple(metadata.get("columns", ()))
    model_features = tuple(read_model_features(settings))
    if columns != model_features:
        raise ArtifactError("imputer columns differ from the model's features (name or order)")

    imputer = joblib.load(settings.imputer_path)
    fitted_columns = tuple(getattr(imputer, "feature_names_in_", ()))
    if fitted_columns != columns:
        raise ArtifactError("imputer's fitted feature names differ from its metadata columns")

    return ImputerArtifact(imputer=imputer, columns=columns, metadata=metadata)


@dataclass(frozen=True)
class ModelArtifact:
    """The trained model and the feature order it was trained on."""

    model: Any
    features: tuple[str, ...]


def load_model(settings: Settings) -> ModelArtifact:
    """Unpickle ``model.pkl`` and check it against ``model_features.json``."""
    for path in (settings.model_path, settings.model_features_path):
        if not path.is_file():
            raise ArtifactError(f"missing artifact {path}")

    features = tuple(read_model_features(settings))
    with settings.model_path.open("rb") as model_file:
        model = pickle.load(model_file)
    fitted = tuple(getattr(model, "feature_names_in_", ()))
    if fitted != features:
        raise ArtifactError("model's fitted feature names differ from model_features.json")
    return ModelArtifact(model=model, features=features)


@dataclass(frozen=True)
class DemographicsLookup:
    """Zipcode -> a full model-order feature row, read once from the demographics CSV.

    Each row holds the zipcode's demographic values at their model positions and NaN at the
    positions of the house fields (``house_positions``), which the request fills in. Rows are
    float64 and read-only; copy one before writing to it.
    """

    features: tuple[str, ...]
    house_positions: Mapping[str, int]
    rows: Mapping[str, npt.NDArray[np.float64]]

    def row_for(self, zipcode: str) -> npt.NDArray[np.float64] | None:
        """A writable copy of the zipcode's row, or None if the zipcode is unknown."""
        row = self.rows.get(zipcode)
        return None if row is None else row.copy()


def load_demographics(settings: Settings, features: tuple[str, ...]) -> DemographicsLookup:
    """Build the zipcode lookup in the model's feature order.

    Every model feature must be either a demographic column or one of the request's house
    fields; zipcodes must be unique 5-digit strings (kept as strings: leading zeros matter).
    """
    path = settings.demographics_path
    if not path.is_file():
        raise ArtifactError(f"missing artifact {path}")
    frame = pd.read_csv(path, dtype={"zipcode": str})

    zipcodes = frame["zipcode"]
    if not zipcodes.is_unique:
        raise ArtifactError(f"{path}: duplicated zipcodes")
    if not zipcodes.str.fullmatch(r"[0-9]{5}").all():
        raise ArtifactError(f"{path}: zipcodes must be 5-digit strings")

    demographic_columns = [name for name in features if name not in HOUSE_FEATURES]
    absent = [name for name in demographic_columns if name not in frame.columns]
    if absent:
        raise ArtifactError(f"{path} lacks model features: {absent}")
    if not set(HOUSE_FEATURES) <= set(features):
        raise ArtifactError("model features lack some house fields")

    positions = {name: index for index, name in enumerate(features)}
    demographic_positions = [positions[name] for name in demographic_columns]
    values = frame[demographic_columns].to_numpy(dtype=np.float64)

    rows: dict[str, npt.NDArray[np.float64]] = {}
    for zipcode, demographic_values in zip(zipcodes, values, strict=True):
        row = np.full(len(features), np.nan, dtype=np.float64)
        row[demographic_positions] = demographic_values
        row.flags.writeable = False
        rows[str(zipcode)] = row
    return DemographicsLookup(
        features=features,
        house_positions=MappingProxyType({name: positions[name] for name in HOUSE_FEATURES}),
        rows=MappingProxyType(rows),
    )


def compute_model_version(settings: Settings) -> str:
    """Identify the artifacts that decide a prediction: ``<model>-<imputer>``.

    The first part is the sha256 of ``model.pkl``, the second the sha256 of
    ``imputer_metadata.json``, which records the sha256 of ``imputer.joblib`` (checked by
    ``load_imputer``) and the imputer's configuration. 12 hex digits each. Computed once at
    startup; any change to either artifact changes it.
    """
    model = sha256_file(settings.model_path)[:12]
    imputer = sha256_file(settings.imputer_metadata_path)[:12]
    return f"{model}-{imputer}"
