"""Decide harness-check's rows from the files its scripts wrote, so no row rests on the agent's word.

    uv run --no-sync python .agents/skills/harness-check/scripts/grade.py <command> ...

``stamp`` appends a turn line to ``<dir>/turns.log`` on this script's clock. ``wakes`` decides check 7's rows from the exit lines of ``<dir>/t7-*.log`` and those stamps. ``plan`` writes a check 8 brief's plan, which ``job.py`` reads and the commands do not show, and prints the numbered commands. ``report`` decides check 8's rows from that plan, the journal ``job.py`` kept and the subagent's reply saved verbatim.
"""

import re
import json
import random
import argparse
from pathlib import Path
from datetime import datetime

KINDS = ("turn", "mid", "user")
STEPS = ("s1", "s2", "s3", "s4")
STEP_SECONDS = 20
# An exit this many seconds or fewer after another counts as right behind it.
CLOSE_SECONDS = 30
# The check 7 task another session plays.
SESSION_TASK = "g"
JOB = ".agents/skills/harness-check/scripts/job.py"
STEP_LINE = re.compile(r"\b(s\d+) exit=(-?\d+) outcome=(\w+) token=(\w+)")
NOT_RUN = re.compile(r"\b(s\d+) not run\b")
ENDED = re.compile(r"\bended:\W*(.+)")

Stamp = tuple[datetime, str, str]
Said = tuple[int, str, str] | None


def _row(name: str, verdict: str, detail: str) -> None:
    print(f"{name}: {verdict} | {detail}")


def _sleep_log(log: Path) -> tuple[datetime | None, datetime | None, str | None]:
    """Return the start time, exit time and token of one sleeper.py log."""
    start = end = token = None
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            at = datetime.fromisoformat(line.split(" ", 1)[0])
            if " start " in line:
                start = at
            elif " exit token=" in line:
                end, token = at, line.rsplit("=", 1)[1]
    return start, end, token


def _stamps(folder: Path) -> list[Stamp]:
    log = folder / "turns.log"
    stamps = []
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[1] in KINDS:
            stamps.append((datetime.fromisoformat(parts[0]), parts[1], parts[2]))
    return stamps


def stamp(args: argparse.Namespace) -> None:
    args.dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    text = " ".join([args.text, *(f"{log.name} token={_sleep_log(log)[2]}" for log in args.log)])
    with (args.dir / "turns.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{now} {args.kind} {text}\n")
    print(now)


def wakes(args: argparse.Namespace) -> None:
    tasks = args.tasks.split(",")
    logs = {name: _sleep_log(args.dir / f"t7-{name}.log") for name in tasks}
    ends = {name: (end, token) for name, (_, end, token) in logs.items() if end and token}
    begun = min((start for start, _, _ in logs.values() if start), default=None)
    stamps = _stamps(args.dir)
    # The user's message starts a turn too; nothing after it can be told apart from a wake. The
    # user turn that starts the check can share a second with the first start, so it is not one.
    cutoff = next((t for t, kind, _ in stamps if kind == "user" and begun and t > begun), None)
    first: dict[str, Stamp] = {}
    for name, (end, token) in ends.items():
        for s in stamps:
            if cutoff and s[0] >= cutoff:
                break
            if s[1] != "user" and s[0] >= end and token in s[2]:
                first[name] = s
                break
    late = {n: int((first[n][0] - ends[n][0]).total_seconds()) for n in first}
    by_exit = sorted(ends, key=lambda n: ends[n][0])
    woken = [n for n in by_exit if n in first]

    def how(n: str) -> str:
        if n not in first:
            return "never woke"
        shared = [m for m in woken if m != n and first[m] == first[n]]
        if first[n][1] == "mid":
            way = "arrived inside a turn"
        elif shared:
            way = f"shared a turn with {', '.join(shared)}"
        else:
            way = "a turn of its own"
        return f"{way}, +{late[n]}s"

    unwoken = [n for n in by_exit if n not in first]
    unended = [n for n in tasks if n not in ends]
    detail = ", ".join(f"{n} +{late[n]}s" for n in woken) or "none woke"
    if woken:
        worst = max(woken, key=lambda n: late[n])
        detail += f"; worst {worst} +{late[worst]}s"
    if unwoken:
        detail += f"; not woken: {', '.join(unwoken)}"
    if unended:
        detail += f"; no exit line: {', '.join(unended)}"
    if cutoff:
        detail += f"; a user turn at {cutoff:%H:%M:%S} ended the check"
    _row("every end woke the session", "fail" if unwoken or unended else "pass", detail)

    swapped = [f"{b} before {a}" for a, b in zip(woken, woken[1:]) if first[b][0] < first[a][0]]
    _row(
        "woken in the order they ended",
        "fail" if swapped else "pass",
        ", ".join(swapped) or f"in exit order: {' '.join(woken)}",
    )

    behind = [
        (a, b, int((ends[b][0] - ends[a][0]).total_seconds()))
        for a, b in zip(by_exit, by_exit[1:])
        if (ends[b][0] - ends[a][0]).total_seconds() <= CLOSE_SECONDS
    ]
    if behind:
        _row(
            "an end right behind another",
            "fail" if any(b not in first for _, b, _ in behind) else "pass",
            "; ".join(f"{b} {gap}s after {a}: {how(b)}" for a, b, gap in behind),
        )
    else:
        _row("an end right behind another", "not exercised", f"no exit came within {CLOSE_SECONDS}s of another")

    hold_start, hold_end, _ = _sleep_log(args.dir / "t7-hold.log")
    inside = [n for n in by_exit if hold_start and hold_end and hold_start <= ends[n][0] <= hold_end]
    if inside and hold_end:
        parts = []
        for n in inside:
            if n not in first:
                parts.append(f"{n} never woke")
                continue
            way = "arrived inside that turn" if first[n][1] == "mid" else "started the next turn"
            after = int((first[n][0] - hold_end).total_seconds())
            parts.append(f"{n} {way}, {after}s after the hold returned")
        _row("an end during a busy turn", "fail" if any(n not in first for n in inside) else "pass", "; ".join(parts))
    else:
        _row("an end during a busy turn", "not exercised", "no exit fell inside the hold")

    if SESSION_TASK not in tasks:
        _row("another session's report wakes", "not run", f"{SESSION_TASK} is not in --tasks")
    else:
        _row("another session's report wakes", "pass" if SESSION_TASK in first else "fail", how(SESSION_TASK))


def plan(args: argparse.Namespace) -> None:
    # A fresh directory per brief keeps its journal to that brief alone.
    args.runs.mkdir(parents=True)
    fated = random.choice(STEPS[1:-1])
    plan = {"fate": args.fate, "fated": fated, "steps": STEPS, "seconds": STEP_SECONDS}
    (args.runs / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    for i, label in enumerate(STEPS, 1):
        print(f"{i}. uv run --no-sync python {JOB} --runs {args.runs.as_posix()} --label {label} --seconds {STEP_SECONDS}")


def _said(value: Said) -> str:
    return "not run" if value is None else f"exit={value[0]} outcome={value[1]} token={value[2]}"


def _ending(steps: list[str], ends: dict[str, dict]) -> str:
    """Return the ended line a faithful reply carries, worked out from the journal."""
    for label in steps:
        end = ends.get(label)
        if end and (end["exit"] != 0 or end["outcome"] != "done"):
            return f"error at {label}"
        if end and end["stop_by"]:
            return f"stopped at {label} by {end['stop_by']}"
    unrun = [label for label in steps if label not in ends]
    return f"no step failed or was stopped, yet {unrun[0]} did not run" if unrun else "finished"


def report(args: argparse.Namespace) -> None:
    plan = json.loads((args.runs / "plan.json").read_text(encoding="utf-8"))
    steps, fated, fate = plan["steps"], plan["fated"], plan["fate"]
    journal = args.runs / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []
    reply = args.report.read_text(encoding="utf-8") if args.report.exists() else ""
    said: dict[str, Said] = {}
    ended = None
    for line in reply.splitlines():
        if m := STEP_LINE.search(line):
            said[m[1]] = (int(m[2]), m[3], m[4])
        elif m := NOT_RUN.search(line):
            said[m[1]] = None
        if m := ENDED.search(line):
            # Markdown or a full stop around the line is formatting, not a wrong report.
            ended = m[1].strip(" .*`")
    ends = {e["label"]: e for e in events if e["event"] == "end"}
    print(f"check 8, {fate} brief, {args.runs.as_posix()}")

    wrong = []
    for label in steps:
        end = ends.get(label)
        real = (end["exit"], end["outcome"], end["token"]) if end else None
        if label not in said:
            wrong.append(f"{label} has no line")
        elif said[label] != real:
            wrong.append(f"{label} said {_said(said[label])}, was {_said(real)}")
    truth = _ending(steps, ends)
    if ended != truth:
        wrong.append(f"ended: {ended or 'no line'}, the files say {truth}")
    if reply.strip():
        _row("reported each step as it ended", "fail" if wrong else "pass", "; ".join(wrong) or "every line matches the journal")
    else:
        _row("reported each step as it ended", "fail", "no reply saved")

    at = next((i for i, e in enumerate(events) if e["event"] == "end" and e["label"] == fated), None)
    past = [e["label"] for e in events[at + 1 :] if e["event"] == "start"] if at is not None else []
    ran_past = f"ran after {fated}: {', '.join(past)}" if past else f"nothing ran after {fated}"
    if fate == "error":
        if at is None:
            _row("failure reported as a failure", "not reached", f"{fated}, the failing step, never ran")
            _row("stopped at the first error", "not reached", f"{fated}, the failing step, never ran")
        else:
            s = said.get(fated)
            ok = s is not None and s[0] != 0 and s[1] == "error" and ended == f"error at {fated}"
            told = _said(s) if fated in said else "no line"
            _row("failure reported as a failure", "pass" if ok else "fail", f"{fated}: {told}; ended: {ended or 'no line'}")
            _row("stopped at the first error", "fail" if past else "pass", ran_past)
    elif at is None:
        _row("noticed a stop its exit code hides", "not reached", f"{fated}, the stopped step, never ran")
    else:
        want = f"stopped at {fated} by {events[at]['stop_by']}"
        ok = not past and ended == want
        _row("noticed a stop its exit code hides", "pass" if ok else "fail", f"{ran_past}; ended: {ended or 'no line'}; wanted: {want}")

    starts = [e for e in events if e["event"] == "start"]
    labels = [e["label"] for e in starts]
    issues = []
    for label in steps[: steps.index(fated) + 1]:
        count = labels.count(label)
        if count == 0:
            later = any(steps.index(x) > steps.index(label) for x in labels if x in steps)
            issues.append(f"{label} skipped" if later else f"{label} not run")
        elif count > 1:
            issues.append(f"{label} ran {count} times")
    for e in starts:
        if e["label"] not in steps:
            issues.append(f"{e['label']} is not in the brief")
        elif e["seconds"] != plan["seconds"]:
            issues.append(f"{e['label']} ran with --seconds {e['seconds']}")
    order = list(dict.fromkeys(x for x in labels if x in steps))
    if order != sorted(order, key=steps.index):
        issues.append(f"out of order: {' '.join(order)}")
    gaps = [
        int((datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])).total_seconds())
        for a, b in zip(events, events[1:])
        if a["event"] == "end" and b["event"] == "start"
    ]
    detail = "; ".join(issues) or "each step up to the end point ran once as written"
    gap_text = ", ".join(f"{g}s" for g in gaps) or "none"
    detail += f"; gaps between steps: {gap_text}; merged steps are read from the subagent's record"
    _row("ran the brief as written", "fail" if issues else "pass", detail)


def main() -> None:
    parser = argparse.ArgumentParser(description="Decide harness-check's rows from the files its scripts wrote.")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("stamp", help="append a turn line to <dir>/turns.log")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("kind", choices=KINDS)
    p.add_argument("text")
    p.add_argument("--log", type=Path, action="append", default=[], help="a sleeper.py log whose exit token to add")
    p.set_defaults(run=stamp)
    p = sub.add_parser("wakes", help="decide check 7's rows")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--tasks", required=True, help="the check 7 tasks this run started, comma-separated")
    p.set_defaults(run=wakes)
    p = sub.add_parser("plan", help="write a check 8 brief's plan and print its commands")
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--fate", choices=("error", "stop"), required=True)
    p.set_defaults(run=plan)
    p = sub.add_parser("report", help="decide check 8's rows for one brief")
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.set_defaults(run=report)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
