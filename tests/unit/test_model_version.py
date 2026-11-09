"""model_version: derived from the artifacts once at startup, returned with every prediction."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.artifacts import compute_model_version
from core.config import Settings, get_settings

VERSION = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{12}$")


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def test_version_is_the_model_hash_then_the_imputer_metadata_hash() -> None:
    settings = get_settings()

    version = compute_model_version(settings)

    assert VERSION.match(version), version
    assert version == f"{sha12(settings.model_path)}-{sha12(settings.imputer_metadata_path)}"


@pytest.mark.parametrize("artifact", ["model_path", "imputer_metadata_path"])
def test_version_changes_when_an_artifact_changes(tmp_path: Path, artifact: str) -> None:
    settings = get_settings()
    copy = tmp_path / getattr(settings, artifact).name
    shutil.copyfile(getattr(settings, artifact), copy)
    with copy.open("ab") as handle:
        handle.write(b"\n")
    changed = Settings(**{artifact: copy})

    assert compute_model_version(changed) != compute_model_version(settings)


def test_lifespan_puts_the_version_on_app_state(test_client: TestClient) -> None:
    state = test_client.app.state  # type: ignore[attr-defined]
    assert state.model_version == compute_model_version(get_settings())


@pytest.mark.parametrize(
    "missing", [(), ("bedrooms",), ("sqft_basement",), ("bedrooms", "sqft_lot", "floors")]
)
def test_every_prediction_carries_the_version(
    test_client: TestClient, sample_home_features: dict[str, Any], missing: tuple[str, ...]
) -> None:
    payload = {**sample_home_features, **dict.fromkeys(missing)}

    response = test_client.post("/predict", json=payload)

    assert response.status_code == 200, response.text
    expected = test_client.app.state.model_version  # type: ignore[attr-defined]
    assert response.json()["model_version"] == expected
