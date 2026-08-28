from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
import logging
from logging.handlers import RotatingFileHandler

from rich.console import Console
from rich.logging import RichHandler

from .constants import LOG_PATH

if TYPE_CHECKING:
    from .models import RunLog

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


class _RunFileHandler(logging.FileHandler):
    """One run's own file, marked by its type so the next run can take it away.

    A subclass rather than a flag on the instance, which is how the rotating
    handler is already found below. The window opens a run per job, and without
    the removal its tenth job would still be writing into the first job's
    directory as well.
    """


def _attach_run(root: logging.Logger, run: RunLog | None, level: int) -> None:
    """Point the run-scoped sink at this run, or at nothing."""
    for handler in [item for item in root.handlers if isinstance(item, _RunFileHandler)]:
        root.removeHandler(handler)
        handler.close()
    if run is None:
        return
    handler = _RunFileHandler(run.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
    handler.setLevel(level)
    root.addHandler(handler)


def configure_logging(run: RunLog | None = None) -> None:
    """Send every module's logging to the rotating file and a rich stderr console.

    With a `RunLog`, a third sink is added: that run's own `run.log`, holding
    this execution and nothing else. The rotating file stays because it answers
    the other question — what this machine has been doing — and picking one run
    out of it means reading past every run before it.
    """
    level = getattr(logging, os.environ.get("COC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        # Already set up, but a run directory asked for later still gets its
        # sink: the window configures logging once at startup and opens a run
        # for each job it runs afterwards.
        _attach_run(root, run, level)
        return
    root.setLevel(level)
    _attach_run(root, run, level)
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
