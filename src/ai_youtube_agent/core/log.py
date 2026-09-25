"""Structured logging contract (Prompt Pack v8, prompt #007).

Every record is one JSON object with these fields:

- ``timestamp``: UTC, ISO 8601.
- ``severity``: one of ``Severity``.
- ``logger`` and ``message``.
- ``correlation_id``, ``session_id`` and ``job_id``: taken from the active
  ``log_context``, or ``null`` when none is set.
- ``fields``: extra structured data passed as ``extra={"fields": {...}}``.
- ``exception``: the formatted traceback, when there is one.

The IDs live in context variables, so they follow asyncio tasks and never
leak between concurrent jobs. Redacting secrets from logs is #219 and is
not part of this contract.
"""

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TextIO


class Severity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


CONTEXT_FIELDS = ("correlation_id", "session_id", "job_id")

_context: dict[str, ContextVar[str | None]] = {
    name: ContextVar(name, default=None) for name in CONTEXT_FIELDS
}


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def current_context() -> dict[str, str | None]:
    return {name: var.get() for name, var in _context.items()}


@contextmanager
def log_context(
    *,
    correlation_id: str | None = None,
    session_id: str | None = None,
    job_id: str | None = None,
) -> Iterator[None]:
    """Bind IDs to every record logged inside the block.

    An ID left as ``None`` keeps the value from an outer block.
    """
    values = {
        "correlation_id": correlation_id,
        "session_id": session_id,
        "job_id": job_id,
    }
    tokens = [
        (_context[name], _context[name].set(value))
        for name, value in values.items()
        if value is not None
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **current_context(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_HANDLER_NAME = "ai_youtube_agent.json"


def configure_logging(
    level: Severity = Severity.INFO, stream: TextIO | None = None
) -> logging.Handler:
    """Install the JSON handler on the root logger.

    Calling it again replaces the previous handler instead of adding a second one.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.value)
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
