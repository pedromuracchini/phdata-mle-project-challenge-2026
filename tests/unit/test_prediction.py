"""Building the model input from the startup lookup (services/prediction.py).

The route used to rebuild the frame on every request the v0 way: read the demographics CSV,
filter it, concat it with the request and select the model's columns. ``v0_frame`` below is
that code, kept as the reference. The lookup must hand the model and the imputer exactly the
same values (float64; v0's int64 bedrooms column is converted to float64 by scikit-learn).
"""

from __future__ import annotations

import csv
import itertools
from typing import Any

import numpy as np
import pandas as pd
import pytest

from core.artifacts import DemographicsLookup, load_demographics, load_model
from core.config import DATA_DIR, get_settings
from services.imputation import HOUSE_FEATURES
from services.prediction import build_features


@pytest.fixture(scope="module")
def features() -> tuple[str, ...]:
    return load_model(get_settings()).features


@pytest.fixture(scope="module")
def lookup(features: tuple[str, ...]) -> DemographicsLookup:
    return load_demographics(get_settings(), features)


@pytest.fixture(scope="module")
def demographics_csv() -> pd.DataFrame:
    return pd.read_csv(get_settings().demographics_path, dtype={"zipcode": str})


@pytest.fixture(scope="module")
def unseen_payloads() -> list[dict[str, Any]]:
    with (DATA_DIR / "future_unseen_examples.csv").open(newline="") as handle:
        return [
            {
                "bedrooms": int(row["bedrooms"]),
                **{name: float(row[name]) for name in HOUSE_FEATURES[1:]},
                "zipcode": row["zipcode"].strip(),
            }
            for row in csv.DictReader(handle)
        ]


def v0_frame(
    payload: dict[str, Any], demographics: pd.DataFrame, features: tuple[str, ...]
) -> pd.DataFrame:
    """The route's frame before Phase 4 (endpoints.py at 3595962), minus the file reads."""
    input_data = pd.DataFrame([payload])
    demographic_info = (
        demographics[demographics["zipcode"] == payload["zipcode"]]
        .drop(columns="zipcode")
        .reset_index(drop=True)
    )
    return pd.concat([input_data, demographic_info], axis=1)[list(features)]


def assert_same_model_input(new: pd.DataFrame | None, old: pd.DataFrame) -> None:
    assert new is not None
    assert list(new.columns) == list(old.columns)
    assert (new.dtypes == np.float64).all()
    assert np.array_equal(new.to_numpy(), old.astype(np.float64).to_numpy(), equal_nan=True)
    assert list(new.columns[new.isna().any()]) == list(old.columns[old.isna().any()])


def house_values(payload: dict[str, Any]) -> dict[str, Any]:
    return {name: payload.get(name) for name in HOUSE_FEATURES}


def test_complete_unseen_rows_match_v0(
    lookup: DemographicsLookup,
    demographics_csv: pd.DataFrame,
    features: tuple[str, ...],
    unseen_payloads: list[dict[str, Any]],
) -> None:
    for payload in unseen_payloads:
        new = build_features(lookup, payload["zipcode"], house_values(payload))
        assert_same_model_input(new, v0_frame(payload, demographics_csv, features))


@pytest.mark.parametrize(
    "pattern",
    [c for size in range(1, 8) for c in itertools.combinations(HOUSE_FEATURES, size)],
    ids=",".join,
)
def test_every_null_pattern_matches_v0(
    pattern: tuple[str, ...],
    lookup: DemographicsLookup,
    demographics_csv: pd.DataFrame,
    features: tuple[str, ...],
    unseen_payloads: list[dict[str, Any]],
) -> None:
    for payload in unseen_payloads[:5]:
        nulled = {**payload, **dict.fromkeys(pattern)}
        new = build_features(lookup, payload["zipcode"], house_values(nulled))
        assert_same_model_input(new, v0_frame(nulled, demographics_csv, features))


@pytest.mark.parametrize("bedrooms", [0, 2**53 + 1, 2**63 - 1, -(2**63)])
def test_large_bedroom_counts_round_like_v0(
    bedrooms: int,
    lookup: DemographicsLookup,
    demographics_csv: pd.DataFrame,
    features: tuple[str, ...],
    unseen_payloads: list[dict[str, Any]],
) -> None:
    payload = {**unseen_payloads[0], "bedrooms": bedrooms}
    new = build_features(lookup, payload["zipcode"], house_values(payload))
    assert_same_model_input(new, v0_frame(payload, demographics_csv, features))


def test_unknown_zipcode_gives_none(lookup: DemographicsLookup) -> None:
    assert build_features(lookup, "00000", dict.fromkeys(HOUSE_FEATURES)) is None


def test_requests_do_not_write_into_the_lookup(
    lookup: DemographicsLookup, unseen_payloads: list[dict[str, Any]]
) -> None:
    payload = unseen_payloads[0]
    before = lookup.rows[payload["zipcode"]].copy()

    build_features(lookup, payload["zipcode"], house_values(payload))

    assert np.array_equal(lookup.rows[payload["zipcode"]], before, equal_nan=True)
    assert not lookup.rows[payload["zipcode"]].flags.writeable
