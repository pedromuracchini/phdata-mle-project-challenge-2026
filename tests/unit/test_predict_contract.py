"""POST /predict contract: nullable fields, imputed_fields, zipcode validation."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.imputation import HOUSE_FEATURES


def assert_prediction(response: Any, imputed: list[str]) -> None:
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"predicted_price", "imputed_fields"}
    assert body["imputed_fields"] == imputed
    assert math.isfinite(body["predicted_price"])
    assert body["predicted_price"] > 0


def test_complete_payload_imputes_nothing(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    response = test_client.post("/predict", json=sample_home_features)

    assert_prediction(response, imputed=[])
    assert response.json()["predicted_price"] == 253980.0


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_each_null_field_is_imputed(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    payload = {**sample_home_features, field: None}

    assert_prediction(test_client.post("/predict", json=payload), imputed=[field])


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_each_omitted_field_is_imputed(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    payload = {name: value for name, value in sample_home_features.items() if name != field}

    assert_prediction(test_client.post("/predict", json=payload), imputed=[field])


def test_null_and_omitted_give_the_same_prediction(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    null = test_client.post("/predict", json={**sample_home_features, "sqft_lot": None})
    omitted = {name: value for name, value in sample_home_features.items() if name != "sqft_lot"}

    assert null.json() == test_client.post("/predict", json=omitted).json()


@pytest.mark.parametrize("field", ["sqft_living", "sqft_above", "sqft_basement"])
def test_one_missing_sqft_field_gives_the_complete_prediction(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    # sample_home_features satisfies sqft_living = sqft_above + sqft_basement (1500 = 1200 + 300).
    response = test_client.post("/predict", json={**sample_home_features, field: None})

    assert_prediction(response, imputed=[field])
    assert response.json()["predicted_price"] == 253980.0


def test_only_zipcode_imputes_every_house_feature(test_client: TestClient) -> None:
    response = test_client.post("/predict", json={"zipcode": "98042"})

    assert_prediction(response, imputed=list(HOUSE_FEATURES))


def test_several_null_fields_are_listed_in_model_order(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    payload = {**sample_home_features, "sqft_basement": None, "bedrooms": None}

    assert_prediction(
        test_client.post("/predict", json=payload), imputed=["bedrooms", "sqft_basement"]
    )


def test_extra_fields_are_still_ignored(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    payload = {**sample_home_features, "waterfront": 1, "grade": 13, "lat": 47.5}

    response = test_client.post("/predict", json=payload)

    assert_prediction(response, imputed=[])
    assert response.json()["predicted_price"] == 253980.0


def test_missing_zipcode_is_rejected(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    payload = {name: value for name, value in sample_home_features.items() if name != "zipcode"}

    response = test_client.post("/predict", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "zipcode"]


@pytest.mark.parametrize("zipcode", [None, 98042, "9804", "980421", "98O42", "98 42", "٩٨٠٤٢"])
def test_invalid_zipcode_is_rejected(
    test_client: TestClient, sample_home_features: dict[str, Any], zipcode: object
) -> None:
    response = test_client.post("/predict", json={**sample_home_features, "zipcode": zipcode})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "zipcode"]


def test_unknown_zipcode_is_a_json_422(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    response = test_client.post("/predict", json={**sample_home_features, "zipcode": "00000"})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "detail": [
            {
                "type": "unknown_zipcode",
                "loc": ["body", "zipcode"],
                "msg": "Unknown zipcode 00000: no demographic data for it.",
                "input": "00000",
            }
        ]
    }


def test_unknown_zipcode_with_null_fields_is_still_a_422(test_client: TestClient) -> None:
    response = test_client.post("/predict", json={"zipcode": "00000"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "unknown_zipcode"


@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_wrong_type_is_still_rejected(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str
) -> None:
    response = test_client.post("/predict", json={**sample_home_features, field: "many"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", HOUSE_FEATURES)
def test_non_finite_numbers_are_rejected(
    test_client: TestClient, sample_home_features: dict[str, Any], field: str, token: str
) -> None:
    # Python's JSON parser accepts these tokens. Infinity used to crash the model (500) and NaN
    # was silently imputed; "missing" must be written as null or by omitting the field.
    body = json.dumps({**sample_home_features, field: "__TOKEN__"}).replace('"__TOKEN__"', token)

    response = test_client.post(
        "/predict", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", field]


def test_validation_error_body_is_unchanged_for_ordinary_errors(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    response = test_client.post("/predict", json={**sample_home_features, "zipcode": 98042})

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "string_type",
                "loc": ["body", "zipcode"],
                "msg": "Input should be a valid string",
                "input": 98042,
            }
        ]
    }


def test_route_still_prints_the_input_frame_as_v0_did(
    test_client: TestClient,
    sample_home_features: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # v0 printed the 1x34 input frame on every request (71 % of handler time, Phase 1 profile).
    # Kept so Phase 4 removes it as a measured optimization; delete this test with it.
    response = test_client.post("/predict", json=sample_home_features)

    assert response.status_code == 200
    assert "[1 rows x 34 columns]" in capsys.readouterr().out
