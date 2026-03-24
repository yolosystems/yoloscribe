"""Shared structured-logging initialisation for agent-runner entry points."""

from __future__ import annotations

import logging
import os

from pythonjsonlogger.json import JsonFormatter

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def configure_logging() -> None:
    """Configure root logger with JSON output and LOG_LEVEL env-var support."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))
