"""Unknown request fields are rejected (422), ADR 005.

Every field except zipcode is nullable and imputed, so an ignored misspelled key (e.g.
"bathroom") would silently become an imputed field. The payloads the project itself sends
(fixture, benchmark, equivalence check) carry only the schema's fields and stay accepted.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.endpoints import HomeFeatures
from api.errors import MAX_LOC_LENGTH

REPO_ROOT = Path(__file__).resolve().parents[2]
UNSEEN_CSV = REPO_ROOT / "src" / "data" / "future_unseen_examples.csv"
FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "baseline_predictions.json").read_text())


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_misspelled_field_is_rejected_not_imputed(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    payload = {**sample_home_features}
    payload["bathroom"] = payload.pop("bathrooms")

    response = test_client.post("/predict", json=payload)

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "extra_forbidden",
                "loc": ["body", "bathroom"],
                "msg": "Extra inputs are not permitted",
            }
        ]
    }


def test_every_unknown_field_is_reported(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    payload = {**sample_home_features, "waterfront": 1, "grade": 13, "lat": 47.5}

    response = test_client.post("/predict", json=payload)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert {tuple(error["loc"]) for error in errors} == {
        ("body", "waterfront"),
        ("body", "grade"),
        ("body", "lat"),
    }
    assert {error["type"] for error in errors} == {"extra_forbidden"}


def test_long_unknown_key_is_truncated_in_loc(
    test_client: TestClient, sample_home_features: dict[str, Any]
) -> None:
    key = "k" * 10_000

    response = test_client.post("/predict", json={**sample_home_features, key: 1})

    assert response.status_code == 422
    loc = response.json()["detail"][0]["loc"]
    assert loc[0] == "body"
    assert loc[1] == key[:MAX_LOC_LENGTH]
    assert len(response.content) < 500


def test_raw_csv_row_is_rejected(test_client: TestClient) -> None:
    """A row of future_unseen_examples.csv as is (18 columns) is not a valid request."""
    with UNSEEN_CSV.open(newline="") as handle:
        row = next(csv.DictReader(handle))

    response = test_client.post("/predict", json=row)

    assert response.status_code == 422
    rejected = {error["loc"][-1] for error in response.json()["detail"]}
    assert {"waterfront", "view", "grade", "lat", "long"} <= rejected


def test_fixture_payloads_have_no_extra_fields() -> None:
    for record in FIXTURE["predictions"]:
        HomeFeatures.model_validate(record["input"])


@pytest.mark.parametrize("profile", ["complete", "one-null", "one-null-knn", "zipcode-only"])
def test_benchmark_payloads_have_no_extra_fields(profile: str) -> None:
    benchmark = load_script("benchmark")
    for body in benchmark.load_payloads(benchmark.DEFAULT_CSV, profile):
        HomeFeatures.model_validate_json(body)


def test_equivalence_payloads_have_no_extra_fields() -> None:
    equivalence = load_script("check_equivalence")
    for row in equivalence.read_rows(UNSEEN_CSV):
        HomeFeatures.model_validate(row)
