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
                "msg": "Unknown zipcode: no demographic data for it.",
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


def test_validation_error_body_has_loc_msg_and_type_only(
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
            }
        ]
    }


SECRET = "s3cr3t-value-7f1c"


def invalid_bodies(sample: dict[str, Any]) -> dict[str, str]:
    """Raw request bodies that fail validation, each carrying SECRET in a rejected value."""
    return {
        "wrong-type": json.dumps({**sample, "bedrooms": SECRET}),
        "bad-zipcode": json.dumps({**sample, "zipcode": SECRET}),
        "zipcode-wrong-type": json.dumps({**sample, "zipcode": [SECRET]}),
        "out-of-range": json.dumps({**sample, "sqft_lot": -7.13579}).replace(
            "-7.13579", "-" + "9" * 30
        ),
        "nan": json.dumps({**sample, "floors": "__NAN__"}).replace('"__NAN__"', "NaN"),
        "body-not-object": json.dumps([SECRET]),
        "malformed-json": '{"zipcode": "98042", "bedrooms": ' + SECRET,
    }


@pytest.mark.parametrize(
    "case",
    [
        "wrong-type",
        "bad-zipcode",
        "zipcode-wrong-type",
        "out-of-range",
        "nan",
        "body-not-object",
        "malformed-json",
    ],
)
def test_422_never_echoes_the_input(
    test_client: TestClient, sample_home_features: dict[str, Any], case: str
) -> None:
    body = invalid_bodies(sample_home_features)[case]

    response = test_client.post(
        "/predict", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert errors
    for error in errors:
        assert set(error) == {"loc", "msg", "type"}, error
    assert SECRET not in response.text
    assert "9" * 30 not in response.text


def test_unknown_zipcode_is_not_echoed(test_client: TestClient) -> None:
    response = test_client.post("/predict", json={"zipcode": "00042"})

    assert response.status_code == 422
    assert "00042" not in response.text
