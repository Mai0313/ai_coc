"""Decide harness-check's rows from the files its scripts wrote, so no row rests on the agent's word.

    uv run --no-sync python .agents/skills/harness-check/scripts/grade.py <command> ...

``stamp`` appends a turn line to ``<dir>/turns.log`` on this script's clock. ``wakes`` decides how the story's timed ends reached the actor each was for, from the exit lines of their ``sleeper.py`` logs and the actors' stamps. ``plan`` writes a brief's plan, which ``job.py`` reads and the commands do not show, and prints the numbered commands. ``report`` decides a brief's rows from that plan, the journal ``job.py`` kept and the subagent's reply saved verbatim. ``table`` holds the takeover start table against the ``status`` output it was built from.
"""

import re
import json
import random
import argparse
from pathlib import Path
from datetime import datetime

KINDS = ("turn", "mid", "user")
STEP_COUNT = 4
STEP_SECONDS = 20
# Each brief's steps carry its own letter, so one reply can answer both briefs.
PREFIX = {"error": "e", "stop": "s"}
# An exit this many seconds or fewer after another counts as right behind it.
CLOSE_SECONDS = 30
# The story's timed ends, each with the actor directory whose turns.log it has to reach. The
# subagent's long command reaches the main session through the subagent's report.
ENDS = (("p.log", "."), ("q.log", "."), ("side/timer.log", "side"), ("sub/long.log", "."))
# An end relayed in a report arrives as late as the report takes to write, so it is left out of the
# rows that compare when ends arrived.
RELAYED = {"sub/long.log"}
# The main session's long foreground wait.
WAIT = "wait.log"
JOB = ".agents/skills/harness-check/scripts/job.py"
STATUS_HEADER = ["時間", "目標", "項目"]
TABLE_HEADER = [*STATUS_HEADER, "後續"]

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
    ends: dict[str, tuple[datetime, str, str]] = {}
    first: dict[str, Stamp] = {}
    missing = []
    for log, actor in ENDS:
        _, end, token = _sleep_log(args.dir / log)
        if not (end and token):
            missing.append(log)
            continue
        ends[log] = (end, token, actor)
        # A user turn cannot carry a token nobody had before the exit, so only wakes count.
        hit = next((s for s in _stamps(args.dir / actor) if s[1] != "user" and s[0] >= end and token in s[2]), None)
        if hit:
            first[log] = hit
    late = {n: int((first[n][0] - ends[n][0]).total_seconds()) for n in first}

    def how(n: str) -> str:
        if n not in first:
            return "never reached its actor"
        way = "arrived inside a turn" if first[n][1] == "mid" else "started a turn"
        return f"{way}, +{late[n]}s"

    for n in ends:
        print(f"  {n} -> {ends[n][2]}: {how(n)}")
    unreached = [n for n in ends if n not in first]
    detail = ", ".join(f"{n} +{late[n]}s" for n in first) or "none reached"
    if first:
        worst = max(first, key=lambda n: late[n])
        detail += f"; worst {worst} +{late[worst]}s"
    if unreached:
        detail += f"; not reached: {', '.join(unreached)}"
    if missing:
        detail += f"; no exit line: {', '.join(missing)}"
    _row("every end reached its actor", "fail" if unreached or missing else "pass", detail)

    by_exit = sorted(ends, key=lambda n: ends[n][0])
    direct = [n for n in by_exit if n not in RELAYED]
    swapped = []
    for actor in dict.fromkeys(ends[n][2] for n in direct):
        mine = [n for n in direct if n in first and ends[n][2] == actor]
        swapped += [f"{b} before {a}" for a, b in zip(mine, mine[1:]) if first[b][0] < first[a][0]]
    _row("reached in the order they ended", "fail" if swapped else "pass", ", ".join(swapped) or "in exit order")

    # Two close ends only test anything when the same actor has to take both.
    behind = []
    for actor in dict.fromkeys(ends[n][2] for n in direct):
        mine = [n for n in direct if ends[n][2] == actor]
        behind += [
            (a, b, int((ends[b][0] - ends[a][0]).total_seconds()))
            for a, b in zip(mine, mine[1:])
            if (ends[b][0] - ends[a][0]).total_seconds() <= CLOSE_SECONDS
        ]
    if behind:
        _row(
            "an end right behind another",
            "fail" if any(n not in first for a, b, _ in behind for n in (a, b)) else "pass",
            "; ".join(f"{b} {gap}s after {a}: {a} {how(a)}, {b} {how(b)}" for a, b, gap in behind),
        )
    else:
        _row("an end right behind another", "not exercised", f"no exit came within {CLOSE_SECONDS}s of another for the same actor")

    wait_start, wait_end, _ = _sleep_log(args.dir / WAIT)
    if not (wait_start and wait_end):
        _row("ends during the long wait", "not exercised", f"{WAIT} has no start and exit line")
        return
    # Only the main session's own ends meet its busy thread; the side session's timer wakes the side session.
    mine = [n for n in by_exit if ends[n][2] == "."]
    inside = [n for n in mine if wait_start <= ends[n][0] <= wait_end]
    parts = []
    for n in inside:
        if n not in first:
            parts.append(f"{n} never reached its actor")
            continue
        way = "came with the call's result" if first[n][1] == "mid" else "started a turn after the call"
        parts.append(f"{n} {way}, {int((first[n][0] - wait_end).total_seconds())}s after the wait returned")
    before = [n for n in mine if ends[n][0] < wait_start]
    if before:
        parts.append(f"ended before the wait began: {', '.join(before)}")
    verdict = "not exercised" if not inside else "fail" if any(n not in first for n in inside) else "pass"
    _row("ends during the long wait", verdict, "; ".join(parts) or "no end fell inside the wait")


def plan(args: argparse.Namespace) -> None:
    # A fresh directory per brief keeps its journal to that brief alone.
    args.runs.mkdir(parents=True)
    steps = [f"{PREFIX[args.fate]}{i}" for i in range(1, STEP_COUNT + 1)]
    fated = random.choice(steps[1:-1])
    plan = {"fate": args.fate, "fated": fated, "steps": steps, "seconds": STEP_SECONDS}
    (args.runs / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    for i, label in enumerate(steps, 1):
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
    prefix = PREFIX[fate]
    step_line = re.compile(rf"\b({prefix}\d+) exit=(-?\d+) outcome=(\w+) token=(\w+)")
    not_run = re.compile(rf"\b({prefix}\d+) not run\b")
    ended_line = re.compile(rf"\bended {prefix}:\W*(.+)")
    journal = args.runs / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []
    reply = args.report.read_text(encoding="utf-8") if args.report.exists() else ""
    said: dict[str, Said] = {}
    ended = None
    # Markdown and a closing full stop are formatting, not a wrong report.
    for line in reply.replace("`", "").replace("*", "").splitlines():
        if m := step_line.search(line):
            said[m[1]] = (int(m[2]), m[3], m[4])
        elif m := not_run.search(line):
            said[m[1]] = None
        if m := ended_line.search(line):
            ended = m[1].strip(" .")
    ends = {e["label"]: e for e in events if e["event"] == "end"}
    print(f"{fate} brief, {args.runs.as_posix()}")

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
        wrong.append(f"ended {prefix}: {ended or 'no line'}, the files say {truth}")
    if reply.strip():
        _row("reported each step as it ended", "fail" if wrong else "pass", "; ".join(wrong) or "every line matches the journal")
    else:
        _row("reported each step as it ended", "fail", "no reply saved")

    at = next((i for i, e in enumerate(events) if e["event"] == "end" and e["label"] == fated), None)
    # A later step counts whenever it started, since one run alongside the fated step ran past it too.
    later = steps[steps.index(fated) + 1 :]
    past = []
    if at is not None:
        past = [e["label"] for i, e in enumerate(events) if e["event"] == "start" and (i > at or e["label"] in later)]
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
    running: list[str] = []
    for e in events:
        if e["event"] == "start":
            if running:
                issues.append(f"{e['label']} started before {', '.join(running)} ended")
            running.append(e["label"])
        elif e["label"] in running:
            running.remove(e["label"])
    gaps = [
        int((datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])).total_seconds())
        for a, b in zip(events, events[1:])
        if a["event"] == "end" and b["event"] == "start"
    ]
    detail = "; ".join(issues) or "each step up to the end point ran once as written"
    gap_text = ", ".join(f"{g}s" for g in gaps) or "none"
    detail += f"; gaps between steps: {gap_text}; merged steps are read from the subagent's record"
    _row("ran the brief as written", "fail" if issues else "pass", detail)


def _tables(text: str) -> list[list[list[str]]]:
    """Return every markdown table in ``text`` as rows of cells, header first, separator dropped."""
    tables: list[list[list[str]]] = []
    rows: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        line = line.strip()
        if line.startswith("|"):
            if not re.fullmatch(r"\|[\s:|-]+\|", line):
                rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif rows:
            tables.append(rows)
            rows = []
    return tables


def table(args: argparse.Namespace) -> None:
    status = args.status.read_text(encoding="utf-8") if args.status.exists() else ""
    want = [t for t in _tables(status) if t[0] == STATUS_HEADER]
    got = _tables(args.table.read_text(encoding="utf-8")) if args.table.exists() else []
    wrong = []
    if not got:
        wrong.append(f"no table in {args.table.as_posix()}")
    elif len(got) != len(want):
        wrong.append(f"{len(got)} tables, status printed {len(want)}")
    for i, (w, g) in enumerate(zip(want, got), 1):
        if g[0] != TABLE_HEADER:
            wrong.append(f"table {i} header {' | '.join(g[0])}")
        if [row[:3] for row in g[1:]] != w[1:]:
            wrong.append(f"table {i} rows differ from status")
        empty = [row[0] for row in g[1:] if len(row) != 4 or not row[3]]
        if empty:
            wrong.append(f"table {i} has no 後續 on {', '.join(empty)}")
    _row("takeover start table", "fail" if wrong else "pass","; ".join(wrong) or f"{len(got)} tables match status")


def main() -> None:
    parser = argparse.ArgumentParser(description="Decide harness-check's rows from the files its scripts wrote.")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("stamp", help="append a turn line to <dir>/turns.log")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("kind", choices=KINDS)
    p.add_argument("text")
    p.add_argument("--log", type=Path, action="append", default=[], help="a sleeper.py log whose exit token to add")
    p.set_defaults(run=stamp)
    p = sub.add_parser("wakes", help="decide how the timed ends reached their actors")
    p.add_argument("--dir", type=Path, required=True)
    p.set_defaults(run=wakes)
    p = sub.add_parser("plan", help="write a brief's plan and print its commands")
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--fate", choices=tuple(PREFIX), required=True)
    p.set_defaults(run=plan)
    p = sub.add_parser("report", help="decide one brief's rows")
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.set_defaults(run=report)
    p = sub.add_parser("table", help="hold the takeover start table against status")
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--table", type=Path, required=True)
    p.set_defaults(run=table)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
