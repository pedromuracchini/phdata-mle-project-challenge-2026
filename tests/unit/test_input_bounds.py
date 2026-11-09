"""Sanity bounds on the house fields: counts and areas are >= 0, with an upper bound only where
a value is clearly impossible (10x the largest value in the sales data, ADR 005)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.endpoints import UPPER_BOUNDS, HomeFeatures
from core.config import get_settings
from services.imputation import HOUSE_FEATURES

SALES = pd.read_csv(get_settings().sales_path, dtype={"zipcode": str})


def test_overflowing_sqft_derivation_is_rejected_not_500(test_client: TestClient) -> None:
    # Found by Hypothesis: sqft_above + sqft_basement overflowed to inf in the deterministic
    # sqft_living fill, and the model raised (500).
    payload = {
        "zipcode": "98001",
        "bedrooms": 0,
        "bathrooms": 0.0,
        "sqft_lot": 0.0,
        "floors": 0.0,
        "sqft_above": 8.988465674311579e307,
        "sqft_basement": 8.98846567431158e307,
    }

    response = test_client.post("/predict", json=payload)

    assert response.status_code == 422, response.text


def test_every_house_field_has_an_upper_bound() -> None:
    assert set(UPPER_BOUNDS) == set(HOUSE_FEATURES)


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_upper_bound_is_ten_times_the_sales_maximum(field: str) -> None:
    assert UPPER_BOUNDS[field] == 10 * SALES[field].max()


def test_every_sale_in_the_data_is_accepted() -> None:
    for record in SALES[[*HOUSE_FEATURES, "zipcode"]].to_dict(orient="records"):
        HomeFeatures.model_validate(record)


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_negative_values_are_rejected(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    response = test_client.post("/predict", json={**sample_home_features, field: -1})

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", field]
    assert response.json()["detail"][0]["type"] == "greater_than_equal"


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_values_above_the_bound_are_rejected(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    too_large = UPPER_BOUNDS[field] + 1
    response = test_client.post("/predict", json={**sample_home_features, field: too_large})

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", field]
    assert response.json()["detail"][0]["type"] == "less_than_equal"


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_bounds_are_inclusive(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    for value in (0, UPPER_BOUNDS[field]):
        response = test_client.post("/predict", json={**sample_home_features, field: value})
        assert response.status_code == 200, (field, value, response.text)
