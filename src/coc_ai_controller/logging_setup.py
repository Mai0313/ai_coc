from __future__ import annotations

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from rich.console import Console
from rich.logging import RichHandler

from .constants import LOG_PATH

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


def configure_logging() -> None:
    """Send every module's logging to the rotating file and to a rich stderr console."""
    level = getattr(logging, os.environ.get("COC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return
    root.setLevel(level)
    # The file stays plain text so it can still be read with a pager or grepped.
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console_handler = RichHandler(
            console=Console(stderr=True),
            show_path=False,
            markup=False,
            rich_tracebacks=True,
            log_time_format=f"[{TIME_FORMAT}]",
        )
        # RichHandler draws the time and level itself; the format supplies the rest.
        console_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root.addHandler(console_handler)
    # google-genai logs the whole request body, including the base64 screenshot.
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
