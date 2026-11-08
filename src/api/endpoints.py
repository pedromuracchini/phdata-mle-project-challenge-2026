"""HTTP routes. Thin: validation here, imputation logic in ``services/imputation.py``."""

from __future__ import annotations

import json
import pickle
from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StringConstraints

from core.artifacts import ImputerArtifact
from core.config import get_settings
from services.imputation import impute_missing

router = APIRouter()

# Five ASCII digits, kept as a string: leading zeros matter for the demographics lookup.
ZipCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{5}$")]


class HomeFeatures(BaseModel):
    """Request body. Every field except ``zipcode`` may be omitted or ``null`` (imputed)."""

    bedrooms: int | None = None
    bathrooms: float | None = None
    sqft_living: float | None = None
    sqft_lot: float | None = None
    floors: float | None = None
    sqft_above: float | None = None
    sqft_basement: float | None = None
    zipcode: ZipCode


class PredictionResponse(BaseModel):
    predicted_price: float
    # Fields that were missing and filled by the imputer, in model order; empty if none.
    imputed_fields: list[str]


def unknown_zipcode_error(zipcode: str) -> HTTPException:
    """422 in the same shape as FastAPI's validation errors, so clients parse one format."""
    return HTTPException(
        status_code=422,
        detail=[
            {
                "type": "unknown_zipcode",
                "loc": ["body", "zipcode"],
                "msg": f"Unknown zipcode {zipcode}: no demographic data for it.",
                "input": zipcode,
            }
        ],
    )


def imputer_from(request: Request) -> ImputerArtifact:
    artifact: ImputerArtifact = request.app.state.imputer
    return artifact


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for container orchestration."""
    return {"status": "healthy"}


# Still `async def` with per-request file reads, as in v0: Phase 4 moves the model and
# demographics loading to the lifespan and makes this a `def` route, measuring each change.
@router.post("/predict", response_model=PredictionResponse)
async def predict(home_features: HomeFeatures, request: Request) -> dict[str, Any]:
    settings = get_settings()

    with open(settings.model_path, "rb") as model_file:  # noqa: ASYNC230 (Phase 4)
        model = pickle.load(model_file)

    with open(settings.model_features_path) as features_file:  # noqa: ASYNC230 (Phase 4)
        model_features = json.load(features_file)

    input_data = pd.DataFrame([home_features.model_dump()])

    demographics = pd.read_csv(settings.demographics_path, dtype={"zipcode": str})
    demographic_info = (
        demographics[demographics["zipcode"] == home_features.zipcode]
        .drop(columns="zipcode")
        .reset_index(drop=True)
    )
    if demographic_info.empty:
        raise unknown_zipcode_error(home_features.zipcode)

    input_data = pd.concat([input_data, demographic_info], axis=1)
    input_data = input_data[model_features]

    artifact = imputer_from(request)
    imputation = impute_missing(input_data, artifact.imputer, artifact.columns)

    prediction = model.predict(imputation.features)

    return {
        "predicted_price": float(prediction[0]),
        "imputed_fields": imputation.imputed_fields,
    }
