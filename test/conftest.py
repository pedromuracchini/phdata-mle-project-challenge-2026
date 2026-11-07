import pytest
from fastapi.testclient import TestClient
import httpx
import os


@pytest.fixture
def test_client():
    """Fixture for unit tests using FastAPI TestClient."""
    from src.main import app
    return TestClient(app)


@pytest.fixture
def api_base_url():
    """Fixture providing API base URL for integration tests."""
    return os.getenv("API_BASE_URL", "http://localhost:8000")


@pytest.fixture
def http_client(api_base_url):
    """Fixture for integration tests using httpx."""
    return httpx.Client(base_url=api_base_url, timeout=10.0)


@pytest.fixture
def sample_home_features():
    """Fixture providing sample input data for tests."""
    return {
        "bedrooms": 3,
        "bathrooms": 2.0,
        "sqft_living": 1500.0,
        "sqft_lot": 5000.0,
        "floors": 1.0,
        "sqft_above": 1200.0,
        "sqft_basement": 300.0,
        "zipcode": "98042"
    }
