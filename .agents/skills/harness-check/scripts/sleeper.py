"""Sleep for a set time and log that it is alive, for harness-check.

    uv run --no-sync python .agents/skills/harness-check/scripts/sleeper.py --seconds 120 --log <file>
    uv run --no-sync python .agents/skills/harness-check/scripts/sleeper.py --until 21:07:30 --log <file>

The log gets a ``start`` line with the pid, an ``alive`` line every minute and an
``exit`` line with a random token, which is also printed. Each line starts with the
local time. Only the exit line holds the token, so a report that quotes it was
written after the script ended. ``--until`` takes a local clock time, so several
actors started minutes apart can aim their ends at times one plan set.
"""

import os
import time
import secrets
import argparse
from pathlib import Path
from datetime import datetime, timedelta


def _stamp(log: Path, text: str) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{now} {text}\n")


def main() -> None:
    """Sleep for ``--seconds`` or until ``--until``, logging to ``--log``, then print the exit token."""
    parser = argparse.ArgumentParser(description="Sleep and log that it is alive.")
    when = parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--seconds", type=int)
    when.add_argument("--until", help="local clock time HH:MM[:SS]")
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    if args.until:
        now = datetime.now()
        target = datetime.combine(now.date(), datetime.strptime(args.until, "%H:%M:%S" if args.until.count(":") == 2 else "%H:%M").time())
        # A time more than twelve hours off belongs to the next or the previous day; a nearer past
        # one is a plan already late.
        if now - target > timedelta(hours=12):
            target += timedelta(days=1)
        elif target - now > timedelta(hours=12):
            target -= timedelta(days=1)
        if target <= now:
            parser.error(f"--until {args.until} has already passed")
        args.seconds = int((target - now).total_seconds())
    args.log.parent.mkdir(parents=True, exist_ok=True)
    _stamp(args.log, f"start pid={os.getpid()} seconds={args.seconds}")
    end = time.monotonic() + args.seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(60.0, left))
        if end - time.monotonic() > 0:
            _stamp(args.log, "alive")
    token = secrets.token_hex(4)
    _stamp(args.log, f"exit token={token}")
    print(token)


if __name__ == "__main__":
    main()
