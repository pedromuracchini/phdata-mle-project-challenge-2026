"""Structured JSON logging on stdout, written by a background thread.

Every record becomes one JSON object per line: ``ts``, ``level``, ``logger``, ``message``, any
``extra={...}`` fields and, for exceptions, ``exc_info`` (the formatted traceback).

The line is built where the record is logged (on the event loop for the request log, a few
microseconds) and put on a queue; a ``QueueListener`` thread does the write, so a slow stdout
never blocks the event loop. ``configure_logging`` runs once per process, from the lifespan:
uvicorn's own loggers then go through the same handler, and ``uvicorn.access`` is turned off
because the request log (``api.middleware``) replaces it.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import queue
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# Attributes every LogRecord has; anything else on a record came from ``extra``. uvicorn adds
# ``color_message`` (the message with ANSI colour codes), which has no place in JSON.
STANDARD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in STANDARD_ATTRIBUTES:
                data[key] = value
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def build_queue_logging(
    stream: TextIO,
) -> tuple[logging.handlers.QueueHandler, logging.handlers.QueueListener]:
    """A handler that formats JSON and enqueues, and the listener that writes the lines."""
    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = logging.handlers.QueueHandler(log_queue)
    handler.setFormatter(JsonFormatter())  # formatted before enqueueing (QueueHandler.prepare)
    writer = logging.StreamHandler(stream)
    writer.setFormatter(logging.Formatter("%(message)s"))  # already JSON
    return handler, logging.handlers.QueueListener(log_queue, writer)


_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record to stdout as JSON. Idempotent (each worker process calls it)."""
    global _configured
    if _configured:
        return
    handler, listener = build_queue_logging(sys.stdout)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    listener.start()
    atexit.register(listener.stop)
    _configured = True
