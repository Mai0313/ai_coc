"""Play one step of a check 8 brief, standing in for one `ai_coc` command.

Each run leaves the files a real run does: `<runs>/<label>/result.json`, the stand-in state file `<runs>/state.json`, and a start and an end line in `<runs>/journal.jsonl`. How the step ends comes from `<runs>/plan.json`, which `grade.py plan` wrote and which the commands do not show: an ordinary step exits 0 with `done`, the failing one exits 1 with `error`, and the stopped one ends early and still exits 0 with `done`, leaving the stop only in the state file's `stop_by`, as a stopped `attack` does.
"""

import os
import json
import time
import secrets
import argparse
from pathlib import Path
from datetime import datetime


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _journal(runs: Path, **event: object) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with (runs / "journal.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now, **event}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Play one step of a brief.")
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    plan = json.loads((args.runs / "plan.json").read_text(encoding="utf-8"))
    fate = plan["fate"] if args.label == plan["fated"] else "ordinary"
    state = args.runs / "state.json"
    # A new run clears the last stop, as a real command's claim overwrites stop_by.
    _write(state, {"status": "running", "label": args.label, "stop_by": None})
    _journal(args.runs, event="start", label=args.label, seconds=args.seconds, pid=os.getpid())
    stop_by = None
    if fate == "stop":
        time.sleep(args.seconds / 2)
        stop_by = f"tester-{secrets.token_hex(2)}"
    else:
        time.sleep(args.seconds)
    outcome = "error" if fate == "error" else "done"
    code = 1 if fate == "error" else 0
    token = secrets.token_hex(4)
    _write(args.runs / args.label / "result.json", {"outcome": outcome, "token": token})
    _write(state, {"status": "idle", "label": args.label, "stop_by": stop_by})
    _journal(
        args.runs,
        event="end",
        label=args.label,
        exit=code,
        outcome=outcome,
        token=token,
        stop_by=stop_by,
    )
    print(f"outcome={outcome} token={token}")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
