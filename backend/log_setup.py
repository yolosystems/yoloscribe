"""Shared structured-logging initialisation for the YoloScribe backend."""

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
    level = getattr(logging, _LOG_LEVEL, logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Strands loggers default to NOTSET and propagate, but Uvicorn's logging
    # init can reset the root level before our module loads. Setting the
    # "strands" logger explicitly guarantees it honours LOG_LEVEL.
    logging.getLogger("strands").setLevel(level)
