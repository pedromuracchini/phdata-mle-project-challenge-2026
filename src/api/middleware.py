"""Pure ASGI middleware (not ``BaseHTTPMiddleware``): no extra task or body buffering per request.

``RequestLogMiddleware`` writes one structured line per request (``core/logging.py``):
request_id, method, path, status, latency_ms, imputed_fields and model_version. Never the
payload, the query string or the model's features. It also records the request's metrics
(``core/metrics.py``), labelled by the matched route template.

``RequestIDMiddleware`` gives every HTTP request an ID. A safe incoming ``X-Request-ID`` (1-64
characters from ``[A-Za-z0-9._-]``) is kept so a caller can trace its own requests; anything
else is replaced by a UUID4. The ID is stored in ``scope["state"]`` (``request.state.request_id``
in handlers) and returned in the ``X-Request-ID`` response header.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core import metrics

REQUEST_LOGGER = "api.requests"
logger = logging.getLogger(REQUEST_LOGGER)

REQUEST_ID_HEADER = b"x-request-id"
SAFE_REQUEST_ID = re.compile(rb"[A-Za-z0-9._-]{1,64}")


def resolve_request_id(scope: Scope) -> str:
    """The incoming X-Request-ID if it is safe, otherwise a new UUID4."""
    for name, value in scope["headers"]:
        if name == REQUEST_ID_HEADER:
            if SAFE_REQUEST_ID.fullmatch(value):
                return str(value, "ascii")
            break
    return str(uuid.uuid4())


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        header = (REQUEST_ID_HEADER, request_id.encode("ascii"))

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", ()), header]
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class RequestLogMiddleware:
    """Log one line per HTTP request once its response is sent (or it failed: status 500)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = 500  # unless a response starts: an exception becomes a 500 further out

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            metrics.observe_request(
                scope["method"], getattr(route, "path", "unmatched"), status, elapsed
            )
            state = scope.get("state", {})
            logger.info(
                "request",
                extra={
                    "request_id": state.get("request_id"),
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "latency_ms": round(elapsed * 1000, 3),
                    "imputed_fields": state.get("imputed_fields"),
                    "model_version": getattr(scope["app"].state, "model_version", None),
                },
            )
