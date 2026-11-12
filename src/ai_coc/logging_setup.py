from __future__ import annotations

import os
import sys
import json
from typing import TYPE_CHECKING
import logging
from datetime import datetime

from filelock import Timeout, FileLock
from rich.console import Console
from rich.logging import RichHandler

from .models import Warned
from .constants import JOURNAL_PATH

if TYPE_CHECKING:
    from .models import RunLog, JournalEvent

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"

# How long one line waits on another process's append before it is dropped.
# An append is a few hundred bytes, so a holder past this is not writing.
JOURNAL_LOCK_WAIT = 5


class Journal(logging.Handler):
    """Copies a run's milestones into `runs.jsonl`, the history that outlives `logs/`.

    A record carries one when it is logged with `extra={"journal": <event>}`,
    so its line in `run.log` and its line here are one call; anything at
    WARNING or above is copied whatever it carries. The start and the end are
    written directly (`write`), the end because a last line in `run.log` after
    the run's own would bury the lines saying how it ended.

    **Writing here never fails the run.** Each line is one append under a file
    lock, since a farming loop, a sleeping watch and an agent's `tap` all write
    at once; a line that cannot be written is said on stderr and dropped.
    """

    def __init__(self, run: RunLog) -> None:
        super().__init__()
        self.run = run.directory.name

    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, "journal", None)
        if event is None and record.levelno < logging.WARNING:
            return
        if event is None:
            trace = None
            if record.exc_info:
                trace = logging.Formatter().formatException(record.exc_info)
            event = Warned(
                level=record.levelname, logger=record.name, text=record.getMessage(), trace=trace
            )
        self.write(event, datetime.fromtimestamp(record.created).astimezone())

    def write(self, event: JournalEvent, at: datetime | None = None) -> None:
        """Append one line: this run, when, and the event's own fields."""
        line = {"run": self.run, "at": (at or datetime.now().astimezone()).isoformat()}
        line |= event.model_dump(mode="json")
        try:
            with (
                FileLock(str(JOURNAL_PATH.with_name(f"{JOURNAL_PATH.name}.lock"))).acquire(
                    timeout=JOURNAL_LOCK_WAIT
                ),
                JOURNAL_PATH.open("a", encoding="utf-8") as journal,
            ):
                journal.write(json.dumps(line, ensure_ascii=False) + "\n")
        except (OSError, Timeout) as error:
            sys.stderr.write(f"{JOURNAL_PATH} 沒寫進去: {error}\n")


def _attach_run(root: logging.Logger, run: RunLog) -> None:
    """Add this run's own file to the logger."""
    # No level of its own: `--debug` lowers the root logger to DEBUG after this
    # runs, and a handler carrying the level it was built with would keep
    # exactly the material that flag exists to capture out of the one file
    # someone opens afterwards.
    handler = logging.FileHandler(run.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
    root.addHandler(handler)


def configure_logging(run: RunLog) -> None:
    """Send every module's logging to a rich stderr console, to this run's file, and to the journal.

    The file is plain lines rather than JSON or a logfire sink, chosen because
    the agents that read it search it with grep.

    **The journal is not a second copy of the log.** A rotating `controller.log`
    once sat beside these, every record written twice, and was removed: the
    directory listing already reads as that history, and `grep -r` across the
    run logs names which run each hit came from. `runs.jsonl` keeps only the
    milestones (`Journal`), and keeps them after `logs/` is wiped, which the
    listing and the grep cannot.
    """
    level = getattr(logging, os.environ.get("COC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    _attach_run(root, run)
    root.addHandler(Journal(run))
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
    # filelock logs four lines for every journal append, and under `--debug`
    # they came ahead of the line that names the run's directory.
    logging.getLogger("filelock").setLevel(logging.INFO)
