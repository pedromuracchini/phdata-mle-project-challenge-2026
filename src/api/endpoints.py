"""HTTP routes. Thin: validation here, imputation logic in ``services/imputation.py``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from core.artifacts import ImputerArtifact, ModelArtifact
from services.imputation import impute_missing
from services.prediction import build_features

router = APIRouter()

# Five ASCII digits, kept as a string: leading zeros matter for the demographics lookup.
ZipCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{5}$")]


class HomeFeatures(BaseModel):
    """Request body. Every field except ``zipcode`` may be omitted or ``null`` (imputed)."""

    # NaN and +/-Infinity (accepted by Python's JSON parser) are invalid input, not "missing":
    # Infinity crashed the model and NaN was silently imputed. Missing means null or omitted.
    model_config = ConfigDict(allow_inf_nan=False)

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


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for container orchestration."""
    return {"status": "healthy"}


# Still `async def`, as in v0: Phase 4 makes this a `def` route as its own measured step.
@router.post("/predict", response_model=PredictionResponse)
async def predict(home_features: HomeFeatures, request: Request) -> dict[str, Any]:
    state = request.app.state
    model: ModelArtifact = state.model
    imputer: ImputerArtifact = state.imputer

    features = build_features(
        state.demographics,
        home_features.zipcode,
        home_features.model_dump(exclude={"zipcode"}),
    )
    if features is None:
        raise unknown_zipcode_error(home_features.zipcode)

    imputation = impute_missing(features, imputer.imputer, imputer.columns)
    prediction = model.model.predict(imputation.features)

    return {
        "predicted_price": float(prediction[0]),
        "imputed_fields": imputation.imputed_fields,
    }
