"""Sleep for a set time and log that it is alive, for agent-capability-check.

    uv run --no-sync python .agents/skills/agent-capability-check/scripts/sleeper.py --seconds 120 --log <file>

The log gets a ``start`` line with the pid, an ``alive`` line every minute and an
``exit`` line with a random token, which is also printed. Each line starts with the
local time. Only the exit line holds the token, so a report that quotes it was
written after the script ended.
"""

import os
import time
import secrets
import argparse
from pathlib import Path
from datetime import datetime


def _stamp(log: Path, text: str) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{now} {text}\n")


def main() -> None:
    """Sleep for ``--seconds``, logging to ``--log``, then print the exit token."""
    parser = argparse.ArgumentParser(description="Sleep and log that it is alive.")
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
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
