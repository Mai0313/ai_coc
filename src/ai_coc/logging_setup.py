from __future__ import annotations

import os
from typing import TYPE_CHECKING
import logging

from rich.console import Console
from rich.logging import RichHandler

from .constants import FRAME_RETENTION_DAYS

if TYPE_CHECKING:
    from .models import RunLog

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


def _attach_run(root: logging.Logger, run: RunLog) -> None:
    """Add this run's own file to the logger."""
    # No level of its own: `--debug` lowers the root logger to DEBUG after this
    # runs, and a handler carrying the level it was built with would keep
    # exactly the material that flag exists to capture out of the one file
    # someone opens afterwards.
    handler = logging.FileHandler(run.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
    root.addHandler(handler)
    # Said here rather than where the deleting happens, because that runs inside
    # `RunLog.open` — before this function, so before any handler exists. The one
    # destructive thing in the project should not be the one thing that leaves
    # no trace: a session following a path out of an old report to a directory
    # that is gone would otherwise be unable to tell a cull from a bug.
    if run.culled:
        logging.getLogger(__name__).info(
            "Deleted the frames of %d run(s) older than %d days; their logs are untouched",
            run.culled,
            FRAME_RETENTION_DAYS,
        )


def configure_logging(run: RunLog) -> None:
    """Send every module's logging to a rich stderr console, and to this run's file.

    The file is plain lines rather than JSON or a logfire sink, chosen because
    the agents that read it search it with grep.

    **There is no second file collecting every run together any more.** A
    rotating `controller.log` sat beside these for a while, on the reasoning
    that it answered a different question — what this machine has been doing,
    rather than what one run did. What it cost was every record written twice,
    and what it bought is available without it: the run directories are named
    `<when>-<what>`, so a plain listing already reads as that history, and
    `grep -r ~/.ai_coc/logs/*/run.log` answers across runs while naming which
    run each hit came from, which the merged file could not.
    """
    level = getattr(logging, os.environ.get("COC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    _attach_run(root, run)
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
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Pillow logs two chunk lines per PNG it decodes, and every parser decodes
    # its frame again: under `--debug` they buried the project's own lines.
    logging.getLogger("PIL").setLevel(logging.INFO)
