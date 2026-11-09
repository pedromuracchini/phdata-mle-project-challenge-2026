"""Exception handlers that keep every error response valid JSON and free of request data."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# What a 422 body keeps of each Pydantic error. ``input`` (the rejected value) and ``ctx``
# are dropped: an error response never echoes the request back (ADR 005). Dropping ``input``
# also removes the NaN/Infinity values that FastAPI's default handler failed to serialise.
ERROR_KEYS = ("type", "loc", "msg")


def public_error(error: dict[str, Any]) -> dict[str, Any]:
    """The client-facing part of one validation error: type, location and message."""
    return {
        "type": error["type"],
        "loc": [part if isinstance(part, int) else str(part) for part in error["loc"]],
        "msg": error["msg"],
    }


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """422 in FastAPI's shape (``{"detail": [...]}``) with only ``loc``, ``msg`` and ``type``."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return JSONResponse(status_code=422, content={"detail": [public_error(e) for e in errors]})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
