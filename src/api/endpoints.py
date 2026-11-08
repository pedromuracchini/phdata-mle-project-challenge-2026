from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import json
import pickle
import pandas as pd

from core.config import get_settings

router = APIRouter()


class HomeFeatures(BaseModel):
    bedrooms: int
    bathrooms: float
    sqft_living: float
    sqft_lot: float
    floors: float
    sqft_above: float
    sqft_basement: float
    zipcode: str


@router.get("/health")
async def health_check():
    """
    Health check endpoint for container orchestration.
    Returns 200 if API is ready to accept requests.
    """
    return {"status": "healthy"}


@router.post("/predict")
async def predict(home_features: HomeFeatures):
    settings = get_settings()

    # Load the model and features (per request until Phase 4; paths from core/config.py)
    with open(settings.model_path, "rb") as model_file:
        model = pickle.load(model_file)

    with open(settings.model_features_path) as features_file:
        model_features = json.load(features_file)
        
    input_data = pd.DataFrame([home_features.dict()])

    # Load demographic data
    demographics = pd.read_csv(settings.demographics_path, dtype={"zipcode": str})
    demographic_info = demographics[
        demographics["zipcode"] == home_features.zipcode
    ].drop(columns="zipcode").reset_index(drop=True)

    # Combine input data with demographic data
    input_data = pd.concat([input_data, demographic_info], axis=1)
    print(input_data)

    # Ensure the input data has the correct features
    input_data = input_data[model_features]

    # Make prediction
    prediction = model.predict(input_data)

    return {"predicted_price": prediction[0]}
