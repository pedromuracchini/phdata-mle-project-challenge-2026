"""Load and validate artifacts once at startup (FastAPI ``lifespan``).

Startup fails loudly instead of serving wrong answers: a missing file, an artifact that does not
match its metadata, columns that differ from the model's features, or library versions other
than the ones the artifact was produced with all raise ``ArtifactError``.

Phase 3 loads the imputer only; the model and demographics are still read per request until
Phase 4 moves them here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import joblib
import numpy as np
import sklearn

from core.config import Settings
from core.imputer_artifact import read_model_features, sha256_file

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
