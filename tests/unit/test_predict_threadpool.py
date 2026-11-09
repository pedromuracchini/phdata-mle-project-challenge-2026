"""`/predict` is a `def` route, and concurrent requests in the threadpool get serial results.

CLAUDE.md: no CPU-bound work inside `async def` routes. As a `def` route, FastAPI runs
`predict` in its threadpool, so requests now run concurrently against the shared artifacts on
``app.state``. These tests pin both: the route is synchronous, and many threads calling it at
once get exactly what one thread gets, call by call.
"""

from __future__ import annotations

import csv
import inspect
import itertools
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient

from api.endpoints import predict
from core.config import DATA_DIR

HOUSE_FIELDS = (
    "bedrooms",
    "bathrooms",
    "sqft_living",
    "sqft_lot",
    "floors",
    "sqft_above",
    "sqft_basement",
)


def payloads() -> list[dict[str, Any]]:
    """Unseen rows complete, with one KNN field null, and with only the zipcode."""
    with (DATA_DIR / "future_unseen_examples.csv").open(newline="") as handle:
        rows = [
            {
                "bedrooms": int(row["bedrooms"]),
                **{name: float(row[name]) for name in HOUSE_FIELDS[1:]},
                "zipcode": row["zipcode"].strip(),
            }
            for row in itertools.islice(csv.DictReader(handle), 20)
        ]
    return [
        variant
        for row in rows
        for variant in (row, {**row, "bedrooms": None}, {"zipcode": row["zipcode"]})
    ]


def test_predict_is_a_sync_route() -> None:
    assert not inspect.iscoroutinefunction(predict)


def test_concurrent_requests_match_serial_ones(test_client: TestClient) -> None:
    requests = payloads() * 4

    def call(payload: dict[str, Any]) -> tuple[int, str]:
        response = test_client.post("/predict", json=payload)
        return response.status_code, response.text

    serial = [call(payload) for payload in requests]
    with ThreadPoolExecutor(max_workers=16) as pool:
        concurrent = list(pool.map(call, requests))

    assert all(status == 200 for status, _ in serial)
    assert concurrent == serial
