"""Characterization test: complete requests must reproduce the v0-original predictions.

The fixture ``tests/fixtures/baseline_predictions.json`` was recorded from the
``mle-api:v0-original`` image (see ``scripts/generate_baseline.py``). Each recorded payload is
sent to the *current* app in-process and the prediction must match within ``rtol=1e-6``.

Library versions change predictions or break unpickling, so this test must run in an
environment matching the fixture's ``meta.versions``: ``make test-docker``. A separate test
fails with an explicit message when the versions differ, so a mismatch is not mistaken for a
behaviour change.

Never regenerate the fixture to make this test pass (CLAUDE.md, hard rules).
"""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

import numpy
import pandas
import pytest
import sklearn
from fastapi.testclient import TestClient

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "baseline_predictions.json"
RTOL = 1e-6

FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
PREDICTIONS: list[dict[str, Any]] = FIXTURE["predictions"]

pytestmark = pytest.mark.characterization


def test_fixture_covers_every_csv_row() -> None:
    assert len(PREDICTIONS) == 100
    assert [record["row_id"] for record in PREDICTIONS] == list(range(100))


def test_environment_matches_baseline() -> None:
    expected = FIXTURE["meta"]["versions"]
    actual = {
        "python": platform.python_version(),
        "scikit-learn": sklearn.__version__,
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
    }
    assert actual == expected, (
        "Runtime library versions differ from the ones the baseline was recorded with "
        f"(image {FIXTURE['meta']['image']}). Prediction mismatches in this run may come from "
        "the environment, not from code changes. Run `make test-docker`, or pin the versions "
        f"in meta.versions. expected={expected} actual={actual}"
    )


@pytest.mark.parametrize(
    "record", PREDICTIONS, ids=[f"row-{record['row_id']}" for record in PREDICTIONS]
)
def test_complete_payload_matches_baseline(test_client: TestClient, record: dict[str, Any]) -> None:
    response = test_client.post("/predict", json=record["input"])

    assert response.status_code == 200, response.text
    assert response.json()["predicted_price"] == pytest.approx(record["predicted_price"], rel=RTOL)
