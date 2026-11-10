"""Property-based: no combination of missing fields makes POST /predict fail with a 500."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.endpoints import UPPER_BOUNDS
from core.config import get_settings
from services.imputation import HOUSE_FEATURES

KNOWN_ZIPCODES = sorted(
    pd.read_csv(get_settings().demographics_path, dtype={"zipcode": str})["zipcode"]
)


def valid_values(field: str) -> st.SearchStrategy[Any]:
    """Any value the schema accepts for the field (sanity bounds included, ADR 005)."""
    if field == "bedrooms":
        return st.integers(min_value=0, max_value=int(UPPER_BOUNDS[field]))
    return st.floats(min_value=0, max_value=UPPER_BOUNDS[field])


def out_of_range_values(field: str) -> st.SearchStrategy[Any]:
    """Finite values below 0 or above the field's upper bound."""
    bound = UPPER_BOUNDS[field]
    if field == "bedrooms":
        return st.integers(max_value=-1) | st.integers(min_value=int(bound) + 1)
    below = st.floats(max_value=0, exclude_max=True, allow_infinity=False)
    above = st.floats(min_value=bound, exclude_min=True, allow_infinity=False)
    return below | above


@st.composite
def payloads(draw: st.DrawFn) -> tuple[dict[str, Any], list[str]]:
    """A request with any subset of house features null or omitted, and the expected list."""
    missing = draw(st.sets(st.sampled_from(HOUSE_FEATURES)))
    payload: dict[str, Any] = {"zipcode": draw(st.sampled_from(KNOWN_ZIPCODES))}
    for field in HOUSE_FEATURES:
        if field in missing:
            if draw(st.booleans()):
                payload[field] = None  # explicit null; otherwise omitted
        else:
            payload[field] = draw(valid_values(field))
    return payload, [field for field in HOUSE_FEATURES if field in missing]


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@settings(
    max_examples=int(os.environ.get("HYPOTHESIS_MAX_EXAMPLES", "300")),
    deadline=None,  # each request still loads the model from disk (until Phase 4)
    suppress_health_check=[HealthCheck.too_slow],
)
@given(case=payloads())
def test_no_missing_field_subset_returns_500(
    client: TestClient, case: tuple[dict[str, Any], list[str]]
) -> None:
    payload, expected_imputed = case

    response = client.post("/predict", json=payload)

    assert response.status_code == 200, (payload, response.status_code, response.text)
    assert response.json()["imputed_fields"] == expected_imputed


SUBSETS = [
    tuple(field for bit, field in enumerate(HOUSE_FEATURES) if mask >> bit & 1)
    for mask in range(2 ** len(HOUSE_FEATURES))
]


@pytest.mark.parametrize("missing", SUBSETS, ids=["+".join(s) or "none" for s in SUBSETS])
def test_every_missing_field_subset_returns_200(
    client: TestClient, sample_home_features: dict[str, Any], missing: tuple[str, ...]
) -> None:
    payload = {**sample_home_features, **dict.fromkeys(missing)}

    response = client.post("/predict", json=payload)

    assert response.status_code == 200, (payload, response.text)
    assert response.json()["imputed_fields"] == list(missing)


@st.composite
def out_of_range_payloads(draw: st.DrawFn) -> tuple[dict[str, Any], str]:
    """A valid request with one house field set outside its bounds."""
    payload, _ = draw(payloads())
    field = draw(st.sampled_from(HOUSE_FEATURES))
    payload[field] = draw(out_of_range_values(field))
    return payload, field


@settings(max_examples=int(os.environ.get("HYPOTHESIS_MAX_EXAMPLES", "300")), deadline=None)
@given(case=out_of_range_payloads())
def test_out_of_range_values_return_422_not_500(
    client: TestClient, case: tuple[dict[str, Any], str]
) -> None:
    payload, field = case

    response = client.post("/predict", json=payload)

    assert response.status_code == 422, (payload, response.status_code, response.text)
    assert ["body", field] in [error["loc"] for error in response.json()["detail"]]
