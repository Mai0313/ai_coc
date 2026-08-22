from __future__ import annotations

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from .constants import LOG_PATH

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


def configure_logging() -> None:
    """Send every module's logging to the rotating file and to stderr."""
    level = getattr(logging, os.environ.get("COC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT, TIME_FORMAT)
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)
    # google-genai logs the whole request body, including the base64 screenshot.
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
