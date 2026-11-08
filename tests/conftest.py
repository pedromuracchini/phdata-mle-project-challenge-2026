"""Shared fixtures for the unit, characterization and integration tests.

The app resolves its artifact paths from ``core/config.py`` (relative to the source tree), so
the tests run from any working directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def test_client() -> Iterator[TestClient]:
    """In-process client for the app, with its lifespan run on enter and exit."""
    from main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def api_base_url() -> str:
    """Base URL of the running API for integration tests."""
    return os.getenv("API_BASE_URL", "http://localhost:8000")


@pytest.fixture
def http_client(api_base_url: str) -> Iterator[httpx.Client]:
    """HTTP client for integration tests against a running API."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        yield client


@pytest.fixture
def sample_home_features() -> dict[str, Any]:
    """A complete, valid request payload."""
    return {
        "bedrooms": 3,
        "bathrooms": 2.0,
        "sqft_living": 1500.0,
        "sqft_lot": 5000.0,
        "floors": 1.0,
        "sqft_above": 1200.0,
        "sqft_basement": 300.0,
        "zipcode": "98042",
    }
