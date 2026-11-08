"""Helpers of scripts/evaluate_imputation.py (the candidate deviations it measures).

The sqft fill is the served implementation; its tests are in test_imputation.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_imputation.py"


@pytest.fixture(scope="module")
def evaluation() -> ModuleType:
    spec = importlib.util.spec_from_file_location("evaluate_imputation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def house(**values: float) -> pd.DataFrame:
    base = {
        "bedrooms": 3.0,
        "bathrooms": 2.0,
        "sqft_living": 2000.0,
        "sqft_lot": 5000.0,
        "floors": 1.0,
        "sqft_above": 1500.0,
        "sqft_basement": 500.0,
    }
    return pd.DataFrame([base | values])


def test_rounding_applies_only_to_imputed_cells(evaluation: ModuleType) -> None:
    def fake_imputer(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.fillna({"bedrooms": 2.6, "floors": 1.7, "bathrooms": 2.13, "sqft_lot": -3.0})

    frame = house(bedrooms=np.nan, floors=np.nan, bathrooms=np.nan, sqft_lot=np.nan)
    frame.loc[0, "sqft_living"] = 2000.4  # observed, must not be rounded

    result = evaluation.with_rounding(fake_imputer)(frame)

    assert result.loc[0, "bedrooms"] == 3.0
    assert result.loc[0, "floors"] == 1.5  # half floors exist: nearest 0.5, not integer
    assert result.loc[0, "bathrooms"] == 2.25
    assert result.loc[0, "sqft_lot"] == 0.0  # clipped at 0
    assert result.loc[0, "sqft_living"] == 2000.4


def test_exclude_train_copies(evaluation: ModuleType) -> None:
    train = pd.concat([house(), house(bedrooms=4.0)], ignore_index=True)
    candidates = pd.concat([house(), house(bedrooms=5.0)], ignore_index=True)

    kept, _, n_copies = evaluation.exclude_train_copies(train, candidates)

    assert n_copies == 1
    assert kept["bedrooms"].tolist() == [5.0]
