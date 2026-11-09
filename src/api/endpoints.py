"""HTTP routes. Thin: validation here, imputation logic in ``services/imputation.py``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core.artifacts import ImputerArtifact, ModelArtifact
from services.imputation import impute_missing
from services.prediction import build_features

router = APIRouter()

# Five ASCII digits, kept as a string: leading zeros matter for the demographics lookup.
ZipCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{5}$")]

# Sanity bounds (ADR 005). Counts and areas cannot be negative. The upper bounds only reject
# values that are clearly impossible: 10x the largest value in src/data/kc_house_data.csv
# (tests/unit/test_input_bounds.py recomputes them). Without them, values near the float
# maximum overflowed the sqft_living = sqft_above + sqft_basement fill to inf (500).
UPPER_BOUNDS: dict[str, float] = {
    "bedrooms": 330,  # max 33
    "bathrooms": 80.0,  # max 8
    "sqft_living": 135_400.0,  # max 13,540
    "sqft_lot": 16_513_590.0,  # max 1,651,359
    "floors": 35.0,  # max 3.5
    "sqft_above": 94_100.0,  # max 9,410
    "sqft_basement": 48_200.0,  # max 4,820
}


def bounded(field: str) -> Any:
    return Field(default=None, ge=0, le=UPPER_BOUNDS[field])


class HomeFeatures(BaseModel):
    """Request body. Every field except ``zipcode`` may be omitted or ``null`` (imputed)."""

    # NaN and +/-Infinity (accepted by Python's JSON parser) are invalid input, not "missing":
    # Infinity crashed the model and NaN was silently imputed. Missing means null or omitted.
    # Unknown fields are rejected (ADR 005): every field is nullable, so an ignored misspelled
    # key ("bathroom") would silently turn into an imputed one.
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    bedrooms: int | None = bounded("bedrooms")
    bathrooms: float | None = bounded("bathrooms")
    sqft_living: float | None = bounded("sqft_living")
    sqft_lot: float | None = bounded("sqft_lot")
    floors: float | None = bounded("floors")
    sqft_above: float | None = bounded("sqft_above")
    sqft_basement: float | None = bounded("sqft_basement")
    zipcode: ZipCode


class PredictionResponse(BaseModel):
    predicted_price: float
    # Fields that were missing and filled by the imputer, in model order; empty if none.
    imputed_fields: list[str]
    # The artifacts that produced the prediction (core.artifacts.compute_model_version).
    model_version: str


def unknown_zipcode_error() -> HTTPException:
    """422 in the same shape as the validation errors, so clients parse one format.

    Like them, it does not echo the request: the client knows which zipcode it sent.
    """
    return HTTPException(
        status_code=422,
        detail=[
            {
                "type": "unknown_zipcode",
                "loc": ["body", "zipcode"],
                "msg": "Unknown zipcode: no demographic data for it.",
            }
        ],
    )


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for container orchestration."""
    return {"status": "healthy"}


# `def`, not `async def`: imputation and inference are CPU-bound, so FastAPI runs the route in
# its threadpool instead of on the event loop, which would otherwise serve one request at a
# time. The artifacts on app.state are only read, and each request gets its own feature row.
@router.post("/predict", response_model=PredictionResponse)
def predict(home_features: HomeFeatures, request: Request) -> dict[str, Any]:
    state = request.app.state
    model: ModelArtifact = state.model
    imputer: ImputerArtifact = state.imputer

    features = build_features(
        state.demographics,
        home_features.zipcode,
        home_features.model_dump(exclude={"zipcode"}),
    )
    if features is None:
        raise unknown_zipcode_error()

    imputation = impute_missing(features, imputer.imputer, imputer.columns)
    prediction = model.model.predict(imputation.features)

    return {
        "predicted_price": float(prediction[0]),
        "imputed_fields": imputation.imputed_fields,
        "model_version": state.model_version,
    }
