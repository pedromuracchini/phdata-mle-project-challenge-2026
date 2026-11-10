"""Exception handlers: every error response is JSON and free of request data and internals."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ``loc`` names the offending key, and for unknown fields that key comes from the client: cap
# each part so a 422 cannot carry back an arbitrarily long string.
MAX_LOC_LENGTH = 64


def public_error(error: dict[str, Any]) -> dict[str, Any]:
    """The client-facing part of one validation error: type, location and message.

    ``input`` (the rejected value) and ``ctx`` are dropped: an error response never echoes the
    request back (ADR 005). That also drops the NaN/Infinity inputs FastAPI's default handler
    failed to serialise.
    """
    return {
        "type": error["type"],
        "loc": [
            part if isinstance(part, int) else str(part)[:MAX_LOC_LENGTH] for part in error["loc"]
        ],
        "msg": error["msg"],
    }


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """422 in FastAPI's shape (``{"detail": [...]}``) with only ``loc``, ``msg`` and ``type``."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return JSONResponse(status_code=422, content={"detail": [public_error(e) for e in errors]})


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """JSON 500 with the request ID and nothing about the error; the traceback is logged.

    Starlette runs this in ``ServerErrorMiddleware``, outside ``RequestIDMiddleware``, so the
    ID is read from the request state (the scope is shared) and the header is set here.
    """
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    logger.error("unhandled exception", exc_info=exc, extra={"request_id": request_id})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
