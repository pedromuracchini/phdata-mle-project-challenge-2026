"""Missing-value imputation for a prediction request. Pure: no FastAPI, no I/O.

Two steps, in this order (CLAUDE.md "Imputation pipeline", ADR 003):

1. Deterministic sqft fill: when exactly one of ``sqft_living``, ``sqft_above`` and
   ``sqft_basement`` is missing, it is computed from ``sqft_living = sqft_above +
   sqft_basement`` (true for every row of the training data).
2. KNN for whatever is still missing: the persisted, already-fitted ``KNNImputer`` from the
   notebook (raw values, no scaling, no rounding), only ever used with ``transform`` here.
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

SQFT_LIVING, SQFT_ABOVE, SQFT_BASEMENT = "sqft_living", "sqft_above", "sqft_basement"
SQFT_IDENTITY = (SQFT_LIVING, SQFT_ABOVE, SQFT_BASEMENT)


class ImputationError(ValueError):
    """The input cannot be imputed (wrong columns, or a non-imputable column is missing)."""


@dataclass(frozen=True)
class ImputationResult:
    features: pd.DataFrame
    imputed_fields: list[str]


def missing_fields(features: pd.DataFrame) -> list[str]:
    """Columns with a missing value in any row, in column order."""
    return [str(column) for column in features.columns[features.isna().any(axis=0).to_numpy()]]


def fill_sqft_identity(features: pd.DataFrame) -> pd.DataFrame:
    """Fill the one missing sqft value per row from ``sqft_living = sqft_above + sqft_basement``.

    Rows with zero, two or three of the sqft values missing are left as they are. A derived
    value below 0 means the given values contradict each other; it is not used, and the field
    stays missing for KNN. Returns a float64 copy.
    """
    filled = features.astype("float64")
    living, above, basement = (filled[column] for column in SQFT_IDENTITY)
    one_missing = filled[list(SQFT_IDENTITY)].isna().sum(axis=1) == 1
    derived = {
        SQFT_LIVING: above + basement,
        SQFT_ABOVE: living - basement,
        SQFT_BASEMENT: living - above,
    }
    for column, value in derived.items():
        use = one_missing & filled[column].isna() & (value >= 0)
        filled.loc[use, column] = value[use]
    return filled


def impute_missing(
    features: pd.DataFrame, imputer: Any, columns: Sequence[str]
) -> ImputationResult:
    """Fill missing house features with the fitted imputer.

    ``features`` must have exactly ``columns`` (the imputer's, which are the model's), in that
    order. When nothing is missing the frame is returned as is, the same object, so complete
    requests are untouched. Otherwise the missing house features are filled (sqft identity
    first, KNN for the rest) and every other value is kept; the result is float64.
    ``imputed_fields`` lists every field that was missing, however it was filled.
    """
    if tuple(features.columns) != tuple(columns):
        raise ImputationError("feature columns differ from the imputer's columns (name or order)")

    missing = missing_fields(features)
    if not missing:
        return ImputationResult(features=features, imputed_fields=[])

    not_imputable = [column for column in missing if column not in HOUSE_FEATURES]
    if not_imputable:
        raise ImputationError(f"columns cannot be imputed: {not_imputable}")

    partially_filled = fill_sqft_identity(features)
    if not partially_filled.isna().to_numpy().any():
        return ImputationResult(features=partially_filled, imputed_fields=missing)

    filled = imputer.transform(partially_filled)
    imputed = pd.DataFrame(filled, columns=features.columns, index=features.index)
    if not np.isfinite(imputed.to_numpy()).all():
        raise ImputationError("imputer left missing or non-finite values")
    return ImputationResult(features=imputed, imputed_fields=missing)
