"""Characterization against the shipped image: every fixture payload over HTTP.

``tests/test_characterization.py`` checks the code in-process; this checks the production image
(``Dockerfile``, as built) served by uvicorn: same baseline, same tolerance. Run by
``make test-integration`` (docker-compose.test.yml), locally and in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "baseline_predictions.json").read_text()
)
PREDICTIONS: list[dict[str, Any]] = FIXTURE["predictions"]
RTOL = 1e-6  # as in tests/test_characterization.py


@pytest.mark.integration
@pytest.mark.characterization
@pytest.mark.parametrize(
    "record", PREDICTIONS, ids=[f"row-{record['row_id']}" for record in PREDICTIONS]
)
def test_image_reproduces_the_baseline(http_client: httpx.Client, record: dict[str, Any]) -> None:
    response = http_client.post("/predict", json=record["input"])

    assert response.status_code == 200, response.text
    assert response.json()["predicted_price"] == pytest.approx(record["predicted_price"], rel=RTOL)
