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

from core.config import get_settings
from services.imputation import HOUSE_FEATURES

KNOWN_ZIPCODES = sorted(
    pd.read_csv(get_settings().demographics_path, dtype={"zipcode": str})["zipcode"]
)

finite_floats = st.floats(allow_nan=False, allow_infinity=False)
# JSON integers the int64 model frame can hold.
bedrooms_values = st.integers(min_value=-(2**63), max_value=2**63 - 1)


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
            payload[field] = draw(bedrooms_values if field == "bedrooms" else finite_floats)
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
