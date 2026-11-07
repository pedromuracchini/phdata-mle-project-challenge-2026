"""Characterization test: complete requests must reproduce the v0-original predictions.

The fixture ``tests/fixtures/baseline_predictions.json`` was recorded from the
``mle-api:v0-original`` image (see ``scripts/generate_baseline.py``). Each recorded payload is
sent to the *current* app in-process and the prediction must match within ``rtol=1e-6``.

The fixture's ``meta`` records where the baseline came from and is never edited: its
``versions`` are those of the recording environment (Python 3.10.22). Two separate tests fail
with an explicit message when the environment drifts, so a mismatch is not mistaken for a
behaviour change:

- the libraries behind ``model.pkl`` (scikit-learn, numpy, pandas) must equal the recorded
  versions, because they decide whether the model unpickles and predicts the same;
- Python must equal the project's pin in ``.python-version``. Phase 2 moved the project from
  the recorded 3.10.22 to 3.12 and found all 100 predictions bit-for-bit identical
  (docs/adr/001-uv-and-python-version.md); a further interpreter change must be verified the
  same way before ``.python-version`` moves.

Never regenerate or edit the fixture to make this test pass (CLAUDE.md, hard rules).
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
PYTHON_VERSION_PATH = Path(__file__).parent.parent / ".python-version"
RTOL = 1e-6

FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
PREDICTIONS: list[dict[str, Any]] = FIXTURE["predictions"]

pytestmark = pytest.mark.characterization


def test_fixture_covers_every_csv_row() -> None:
    assert len(PREDICTIONS) == 100
    assert [record["row_id"] for record in PREDICTIONS] == list(range(100))


def test_model_libraries_match_baseline() -> None:
    actual = {
        "scikit-learn": sklearn.__version__,
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
    }
    recorded = FIXTURE["meta"]["versions"]
    expected = {name: recorded[name] for name in actual}
    assert actual == expected, (
        "The libraries behind model.pkl differ from the ones the baseline was recorded with "
        f"(image {FIXTURE['meta']['image']}). Prediction mismatches in this run may come from "
        "the environment, not from code changes. Install the locked versions (`make setup`) "
        f"or run `make test-docker`. expected={expected} actual={actual}"
    )


def test_python_matches_project_pin() -> None:
    expected = PYTHON_VERSION_PATH.read_text().strip()
    actual = platform.python_version()
    assert actual == expected, (
        f"Running on Python {actual}, but the project pins {expected} in .python-version "
        f"(baseline recorded on {FIXTURE['meta']['versions']['python']}). Use `make setup` / "
        "`make test`, or run `make test-docker`."
    )


@pytest.mark.parametrize(
    "record", PREDICTIONS, ids=[f"row-{record['row_id']}" for record in PREDICTIONS]
)
def test_complete_payload_matches_baseline(test_client: TestClient, record: dict[str, Any]) -> None:
    response = test_client.post("/predict", json=record["input"])

    assert response.status_code == 200, response.text
    assert response.json()["predicted_price"] == pytest.approx(record["predicted_price"], rel=RTOL)
