from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
import logging

from rich.console import Console
from rich.logging import RichHandler

if TYPE_CHECKING:
    from .models import RunLog

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


class _Configured(logging.NullHandler):
    """Marks the root logger as set up, whatever sinks this process ended up with.

    The guard used to key off the rotating file handler, which was added
    unconditionally. Keying it off the console instead reads as equivalent and
    is not: PyInstaller builds this `--windowed`, so the shipped executable has
    no stderr, that handler is never added, and every later call would fall
    through to `root.setLevel` — resetting the level on the one build where the
    在執行紀錄 panel is the only log there is, and doing it just after the user
    asked that selector for DEBUG.
    """


class _RunFileHandler(logging.FileHandler):
    """One run's own file, marked by its type so the next run can take it away.

    A subclass rather than a flag on the instance, which is how the rotating
    handler is already found below. The window opens a run per job, and without
    the removal its tenth job would still be writing into the first job's
    directory as well.
    """


def _attach_run(root: logging.Logger, run: RunLog | None) -> None:
    """Point the run-scoped sink at this run, or at nothing."""
    for handler in [item for item in root.handlers if isinstance(item, _RunFileHandler)]:
        root.removeHandler(handler)
        handler.close()
    if run is None:
        return
    # No level of its own: the window's 執行紀錄 selector lowers the root
    # logger to DEBUG at runtime, and a handler carrying the level it was built
    # with would keep exactly the material that selector exists to capture out
    # of the one file someone opens afterwards.
    handler = _RunFileHandler(run.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
    root.addHandler(handler)


def configure_logging(run: RunLog | None = None) -> None:
    """Send every module's logging to a rich stderr console, and to this run's file.

    **There is no second file collecting every run together any more.** A
    rotating `controller.log` sat beside these for a while, on the reasoning
    that it answered a different question — what this machine has been doing,
    rather than what one run did. What it cost was every record written twice,
    and what it bought is available without it: the run directories are named
    `<when>-<what>`, so a plain listing already reads as that history, and
    `grep -r ~/.ai_coc/logs/*/run.log` answers across runs while naming which
    run each hit came from, which the merged file could not.

    What says this has already run is a marker handler of its own rather than
    any of the sinks, because which sinks exist depends on the build; see
    `_Configured`.
    """
    level = getattr(logging, os.environ.get("COC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if any(isinstance(handler, _Configured) for handler in root.handlers):
        # Already set up, but a run directory asked for later still gets its
        # sink: the window configures logging once at startup and opens a run
        # for each job it runs afterwards.
        _attach_run(root, run)
        return
    root.setLevel(level)
    root.addHandler(_Configured())
    _attach_run(root, run)
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
