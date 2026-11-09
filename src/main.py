"""FastAPI application: artifacts are loaded once in the lifespan and kept on ``app.state``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import State

from api.endpoints import router as api_router
from api.errors import register_error_handlers
from core.artifacts import compute_model_version, load_demographics, load_imputer, load_model
from core.config import Settings, get_settings


def load_artifacts(state: State, settings: Settings) -> None:
    """Load every artifact the route reads onto ``state``; raises ``ArtifactError`` if invalid."""
    model = load_model(settings)
    state.model = model
    state.demographics = load_demographics(settings, model.features)
    state.imputer = load_imputer(settings)
    state.model_version = compute_model_version(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load every artifact once, before the first request; fail startup if any is invalid."""
    load_artifacts(app.state, get_settings())
    yield


app = FastAPI(lifespan=lifespan)

# Unchanged from v0; Phase 5 reviews CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)
app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
