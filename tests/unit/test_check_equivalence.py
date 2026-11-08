"""Pure helpers of scripts/check_equivalence.py (the bit-for-bit imputation gate)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_equivalence.py"


@pytest.fixture(scope="module")
def equivalence() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_equivalence", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_null_pattern_once(equivalence: ModuleType) -> None:
    patterns = equivalence.null_patterns()

    assert len(patterns) == 128
    assert len(set(patterns)) == 128
    assert patterns[0] == ()
    assert patterns[-1] == equivalence.HOUSE_FIELDS


def test_house_fields_match_the_service(equivalence: ModuleType) -> None:
    from services.imputation import HOUSE_FEATURES

    assert equivalence.HOUSE_FIELDS == HOUSE_FEATURES


def test_record_compares_values_not_dtypes(equivalence: ModuleType) -> None:
    response = {"predicted_price": 253980.0, "imputed_fields": []}
    mixed = pd.DataFrame({"bedrooms": [3], "bathrooms": [2.0]})  # int64 + float64, as in v0
    floats = pd.DataFrame({"bedrooms": [3.0], "bathrooms": [2.0]})

    assert equivalence.record_bytes(mixed, response) == equivalence.record_bytes(floats, response)


def test_record_sees_a_one_ulp_difference(equivalence: ModuleType) -> None:
    response = {"predicted_price": 1.0, "imputed_fields": ["bedrooms"]}
    value = np.array([[3.0]])
    nudged = np.array([[np.nextafter(3.0, 4.0)]])

    assert equivalence.record_bytes(value, response) != equivalence.record_bytes(nudged, response)


def test_record_includes_prediction_and_imputed_fields(equivalence: ModuleType) -> None:
    values = np.array([[3.0]])
    base = equivalence.record_bytes(values, {"predicted_price": 1.0, "imputed_fields": []})

    other_price = {"predicted_price": np.nextafter(1.0, 2.0), "imputed_fields": []}
    other_fields = {"predicted_price": 1.0, "imputed_fields": ["bedrooms"]}
    assert equivalence.record_bytes(values, other_price) != base
    assert equivalence.record_bytes(values, other_fields) != base
