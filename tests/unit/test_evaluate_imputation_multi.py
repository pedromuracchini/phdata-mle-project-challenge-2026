"""Helpers of scripts/evaluate_imputation_multi.py (combinations and the reference check)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def multi() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the script imports evaluate_imputation from its folder
    spec = importlib.util.spec_from_file_location(
        "evaluate_imputation_multi", SCRIPTS / "evaluate_imputation_multi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_all_127_combinations_once_each_by_size(multi: ModuleType) -> None:
    combos = multi.combinations()
    assert len(combos) == 127
    assert len(set(combos)) == 127
    assert [len(c) for c in combos] == sorted(len(c) for c in combos)
    counts = {k: sum(len(c) == k for c in combos) for k in range(1, 8)}
    assert counts == {1: 7, 2: 21, 3: 35, 4: 35, 5: 21, 6: 7, 7: 1}


def test_combinations_keep_feature_order(multi: ModuleType) -> None:
    order = list(multi.HOUSE_FEATURES)
    for combo in multi.combinations():
        assert [order.index(f) for f in combo] == sorted(order.index(f) for f in combo)


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        (("sqft_living",), False),
        (("sqft_living", "bedrooms"), False),
        (("sqft_living", "sqft_above"), True),
        (("sqft_above", "sqft_basement"), True),
        (("sqft_living", "sqft_above", "sqft_basement"), True),
    ],
)
def test_loses_sqft_fill(multi: ModuleType, combo: tuple[str, ...], expected: bool) -> None:
    assert multi.loses_sqft_fill(combo) is expected


def test_three_pairs_lose_the_sqft_fill(multi: ModuleType) -> None:
    pairs = [c for c in multi.combinations() if len(c) == 2]
    assert sum(multi.loses_sqft_fill(c) for c in pairs) == 3


def _row(k: int, combo: list[str], value: float) -> dict[str, Any]:
    metrics = dict.fromkeys(multi_metrics(), value)
    return {"k": k, "combination": combo, "served": metrics, "median": dict(metrics)}


def multi_metrics() -> tuple[str, ...]:
    return ("price_mape_pct", "shift_median_pct", "shift_p90_pct")


def _reference(value: float) -> dict[str, Any]:
    metrics = dict.fromkeys(multi_metrics(), value)
    return {
        "scenarios": {
            "bedrooms": {"knn+sqft_fill": metrics, "median": metrics},
            "all 7 (zipcode only)": {"knn+sqft_fill": metrics, "median": metrics},
        }
    }


def test_reference_check_passes_on_equal_and_ignores_k2(multi: ModuleType) -> None:
    rows = {
        "bedrooms": _row(1, ["bedrooms"], 1.0),
        "a+b": _row(2, ["bedrooms", "bathrooms"], 99.0),
        "all": _row(7, list(multi.HOUSE_FEATURES), 1.0),
    }
    assert multi.check_against_reference(rows, _reference(1.0)) == []


def test_reference_check_reports_any_difference(multi: ModuleType) -> None:
    rows = {"bedrooms": _row(1, ["bedrooms"], 1.0)}
    rows["bedrooms"]["served"]["shift_p90_pct"] = 1.0000001
    problems = multi.check_against_reference(rows, _reference(1.0))
    assert problems == ["bedrooms served shift_p90_pct: 1.0000001 != 1.0"]
