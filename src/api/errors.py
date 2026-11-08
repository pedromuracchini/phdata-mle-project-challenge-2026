"""Exception handlers that keep every error response valid JSON."""

from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def json_safe(value: Any) -> Any:
    """Replace non-finite floats (not representable in JSON) with their names as strings."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return value


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422 body, unchanged except that echoed NaN/Infinity inputs become strings.

    The default handler fails to serialise them and turns a 422 into a 500.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return JSONResponse(status_code=422, content={"detail": json_safe(jsonable_encoder(errors))})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
