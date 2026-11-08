"""FastAPI application: artifacts are loaded once in the lifespan and kept on ``app.state``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.endpoints import router as api_router
from core.artifacts import load_imputer
from core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load artifacts before the first request; fail startup if any is invalid."""
    app.state.imputer = load_imputer(get_settings())
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

app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
