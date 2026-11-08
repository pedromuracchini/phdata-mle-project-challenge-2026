"""Service configuration, read from environment variables only.

Every filesystem path the service uses is defined here. Defaults are resolved from this file's
location (``src/``, or ``/app`` in the image), never from the process working directory, so the
service and the tests behave the same whatever directory they are started from.

Override any setting with an ``APP_``-prefixed environment variable, e.g.
``APP_MODEL_PATH=/models/model.pkl``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# src/ in the repository, /app in the image (the Dockerfile copies src/ to /app).
SRC_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = SRC_DIR / "model"
DATA_DIR = SRC_DIR / "data"


class Settings(BaseSettings):
    """Paths to the model artifacts and reference data."""

    # protected_namespaces=(): allow field names starting with ``model_``.
    model_config = SettingsConfigDict(env_prefix="APP_", frozen=True, protected_namespaces=())

    model_path: Path = MODEL_DIR / "model.pkl"
    model_features_path: Path = MODEL_DIR / "model_features.json"
    demographics_path: Path = DATA_DIR / "zipcode_demographics.csv"
    # Home sales the model was trained on; the imputer is fitted on the same train split.
    sales_path: Path = DATA_DIR / "kc_house_data.csv"
    imputer_path: Path = MODEL_DIR / "imputer.joblib"
    imputer_metadata_path: Path = MODEL_DIR / "imputer_metadata.json"


@lru_cache
def get_settings() -> Settings:
    """Settings for this process, read from the environment once."""
    return Settings()
