"""Turn a validated request into the model's input frame. Pure: no FastAPI, no I/O.

The demographics are looked up in the table ``core.artifacts.load_demographics`` built at
startup; this module only fills in the request's house fields.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from core.artifacts import DemographicsLookup


def build_features(
    lookup: DemographicsLookup, zipcode: str, house_values: Mapping[str, float | None]
) -> pd.DataFrame | None:
    """The one-row model input (float64, model feature order), or None for an unknown zipcode.

    ``house_values`` maps every house field to its value, ``None`` meaning missing; missing
    fields become NaN for the imputer.
    """
    row = lookup.row_for(zipcode)
    if row is None:
        return None
    for name, value in house_values.items():
        row[lookup.house_positions[name]] = np.nan if value is None else value
    return pd.DataFrame(row[np.newaxis, :], columns=list(lookup.features))
