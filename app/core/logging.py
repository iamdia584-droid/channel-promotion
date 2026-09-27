"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from app.core.config import settings

_SECRET_KEYS = re.compile(
    r"(token|secret|password|api_?key|authorization|session|destination|account_number|msisdn)",
    re.IGNORECASE,
)
_REDACTED = "***redacted***"


def _redact(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Never let a bot token, payment secret or payout destination reach a log sink."""
    for key in list(event_dict):
        if _SECRET_KEYS.search(key):
            event_dict[key] = _REDACTED
    if settings.telegram_bot_token:
        for key, value in event_dict.items():
            if isinstance(value, str) and settings.telegram_bot_token in value:
                event_dict[key] = value.replace(settings.telegram_bot_token, _REDACTED)
    return event_dict


def configure_logging() -> None:
    level = logging.INFO if settings.is_production else logging.DEBUG
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    logging.getLogger("uvicorn.access").handlers = []
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
            if settings.is_production
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
