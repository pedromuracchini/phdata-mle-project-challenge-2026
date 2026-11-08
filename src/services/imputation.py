"""Missing-value imputation for a prediction request. Pure: no FastAPI, no I/O.

The imputer is the persisted, already-fitted ``KNNImputer`` (see ``core/imputer_artifact.py``);
it is only ever used with ``transform`` here. Configuration is the notebook's, implemented
faithfully: KNN on raw values, no deterministic fill, no rounding (CLAUDE.md, ADR 003).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

# Request fields that may be missing, in the model's feature order. zipcode never is: the
# demographic columns derived from it must always be present.
HOUSE_FEATURES = (
    "bedrooms",
    "bathrooms",
    "sqft_living",
    "sqft_lot",
    "floors",
    "sqft_above",
    "sqft_basement",
)


class ImputationError(ValueError):
    """The input cannot be imputed (wrong columns, or a non-imputable column is missing)."""


@dataclass(frozen=True)
class ImputationResult:
    features: pd.DataFrame
    imputed_fields: list[str]


def missing_fields(features: pd.DataFrame) -> list[str]:
    """Columns with a missing value in any row, in column order."""
    return [str(column) for column in features.columns[features.isna().any(axis=0).to_numpy()]]


def impute_missing(
    features: pd.DataFrame, imputer: Any, columns: Sequence[str]
) -> ImputationResult:
    """Fill missing house features with the fitted imputer.

    ``features`` must have exactly ``columns`` (the imputer's, which are the model's), in that
    order. When nothing is missing the frame is returned as is, the same object, so complete
    requests are untouched. Otherwise the missing house features are filled and every other
    value is kept; the result is float64.
    """
    if tuple(features.columns) != tuple(columns):
        raise ImputationError("feature columns differ from the imputer's columns (name or order)")

    missing = missing_fields(features)
    if not missing:
        return ImputationResult(features=features, imputed_fields=[])

    not_imputable = [column for column in missing if column not in HOUSE_FEATURES]
    if not_imputable:
        raise ImputationError(f"columns cannot be imputed: {not_imputable}")

    raw = features.astype("float64")
    filled = imputer.transform(raw)
    imputed = pd.DataFrame(filled, columns=raw.columns, index=raw.index)
    if not np.isfinite(imputed.to_numpy()).all():
        raise ImputationError("imputer left missing or non-finite values")
    return ImputationResult(features=imputed, imputed_fields=missing)
