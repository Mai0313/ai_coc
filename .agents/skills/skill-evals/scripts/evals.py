"""Set up and grade the skill evals: `setup` builds one sandbox per scenario, `grade` reads what each agent left.

Run from the checkout root with `uv run --no-sync python .agents/skills/skill-evals/scripts/evals.py`.
`<command> -h` lists each command's arguments.
"""

from __future__ import annotations

import re
import gzip
import sys
import json
import shutil
import tomllib
import argparse
import tempfile
import subprocess
from typing import Any, Literal
from pathlib import Path
from datetime import datetime, timedelta

from pydantic import BaseModel, Field, JsonValue

SKILL = Path(__file__).resolve().parents[1]
CHECKOUT = SKILL.parents[2]
SCENARIOS = SKILL / "scenarios"
FRAMES = CHECKOUT / "tests" / "frames"
RESULTS = CHECKOUT / ".runs" / "skill-evals"
SANDBOXES = Path(tempfile.gettempdir()) / "ai_coc-skill-evals"
# An ISO instant with its offset, the only form the fixtures write times in.
INSTANT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$")
# The files a dry run leaves for the grader, under the sandbox.
OUT = "out"
REPLY = "reply.md"

LAUNCHER = '''import subprocess
import sys


def main():
    return subprocess.call([{python!r}, {standin!r}, {scenario!r}, *sys.argv[1:]])
'''
PYPROJECT = """[project]
name = "ai-coc-eval-sandbox"
version = "0"
requires-python = ">=3.12"

[project.scripts]
ai_coc = "launcher:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
only-include = ["launcher.py"]
"""
BRIEF = """This is a dry run of this project's skills against a simulator, not the real game.

- Your checkout is `{sandbox}`. Work from there and run every `ai_coc` command the way the skills write it, `uv run --no-sync ai_coc …`, from that directory. Its `ai_coc` is the project's own command line driving a simulator that replays recorded screens and reports; no emulator is reachable from it.
- For this run `~/.ai_coc` is `{home}`: read `state.json`, `config.json`, `runs.jsonl` and the run directories there, never under your own home directory.
- Your session id for this run is `{session}`.
- Start no subagent and no session. Where a skill has you dispatch one, write the brief you would hand it as a file named `<what it is for>-brief.md` in `{out}` instead.
- Send nothing to the user's phone. Where a skill has you push a notification, write its one line to `push.md` in `{out}`; where it has you send an image, name its path in your reply.
- Change no file outside `{sandbox}` and no code.
- The simulator never sleeps: `ai_coc watch` returns at once. The dry run ends when you start a watch, when a command answers that the recording ends there, or when you would end your turn anyway. Then write your reply to the user as you would at that moment, and stop. Your final message is that reply, in full.

{situation}
"""
SAYS = "\nThe user says: 「{user}」\n"


class Select(BaseModel):
    """Which recorded calls a check is about; every field given has to hold.

    `args` are exact values of the parsed arguments, `unset` arguments left at the
    parser's default, `patterns` regular expressions over arguments, `outcome` one
    over the reported outcome, and `box` the `x0 y0 x1 y1` a tap landed inside.
    """

    command: str | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)
    patterns: dict[str, str] = Field(default_factory=dict)
    outcome: str | None = None
    box: tuple[int, int, int, int] | None = None


CheckKind = Literal[
    "called",
    "not_called",
    "nothing_claims",
    "every_call_has",
    "table_cell",
    "reply",
    "exists",
    "lines",
    "first_line",
    "last_line",
]


class Check(BaseModel):
    """One decision a scenario grades, and the rule in a skill it pins."""

    id: str
    what: str
    anchor: tuple[str, str]
    kind: CheckKind
    calls: Select | None = None
    after: Select | None = None
    fields: list[str] = Field(default_factory=list)
    row: str = ""
    pattern: str = ""
    absent: bool = False
    file: str = ""
    select: str = ""
    must: str = ""
    must_not: str = ""


class Tap(BaseModel):
    """A tap inside `box` (`x0 y0 x1 y1`) moves the scenario to screen `to`."""

    box: tuple[int, int, int, int]
    to: int


class Screen(BaseModel):
    """One recorded frame from `tests/frames/`, and the taps that lead on from it; any other input ends the recording."""

    frame: str
    taps: list[Tap] = Field(default_factory=list)


class Edit(BaseModel):
    """A change to the fixture export that stages a case; the scenario says why."""

    world: Literal["day", "night"]
    section: str
    data: int
    index: int = 0
    set: dict[str, JsonValue] = Field(default_factory=dict)
    drop: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    """One bounded slice of a skill: what the agent meets, and what it should decide."""

    skill: str
    about: str
    situation: str
    user: str = ""
    minutes_after_export: int = 1
    last_frame: str = ""
    screens: list[Screen] = Field(default_factory=list)
    replies: dict[str, list[dict[str, JsonValue]]] = Field(default_factory=dict)
    goblin_gems: int | None = None
    state: dict[str, JsonValue] = Field(default_factory=dict)
    live: list[int] = Field(default_factory=list)
    journal: list[dict[str, JsonValue]] = Field(default_factory=list)
    edits: list[Edit] = Field(default_factory=list)
    checks: list[Check]


def scenarios() -> dict[str, Scenario]:
    return {
        path.stem: Scenario.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        for path in sorted(SCENARIOS.glob("*.toml"))
    }


def shifted(value: Any, delta: timedelta) -> Any:  # noqa: ANN401 - any JSON
    """Every instant in a JSON value moved by `delta`, so a fixture reads as happening now."""
    if isinstance(value, dict):
        return {key: shifted(item, delta) for key, item in value.items()}
    if isinstance(value, list):
        return [shifted(item, delta) for item in value]
    if isinstance(value, str) and INSTANT.match(value):
        return (datetime.fromisoformat(value) + delta).isoformat()
    return value


def staged(export: dict[str, Any], edits: list[Edit]) -> dict[str, Any]:
    for edit in edits:
        rows = [row for row in export[edit.world][edit.section] if row["data"] == edit.data]
        row = rows[edit.index]
        row.update(edit.set)
        for key in edit.drop:
            row.pop(key, None)
    return export


def _copy(source: Path, target: Path, skip: tuple[str, ...] = ()) -> None:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", *skip))


def build(label: str, name: str, scenario: Scenario, root: Path, now: datetime) -> Path:
    """One sandbox: a checkout of its own whose `ai_coc` is the stand-in, and the scenario's home."""
    sandbox = root / label
    sandbox.mkdir(parents=True)
    (sandbox / "AGENTS.md").write_bytes((CHECKOUT / "AGENTS.md").read_bytes())
    _copy(CHECKOUT / ".agents", sandbox / ".agents", ("skill-evals",))
    _copy(CHECKOUT / "src", sandbox / "src")
    (sandbox / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    standin = SKILL / "scripts" / "standin.py"
    (sandbox / "launcher.py").write_text(
        LAUNCHER.format(python=sys.executable, standin=str(standin), scenario=str(sandbox)), encoding="utf-8"
    )
    subprocess.run(["uv", "sync", "--quiet"], cwd=sandbox, check=True)  # noqa: S607 - uv from PATH, as the skills run it
    stage(sandbox, name, scenario, now)
    return sandbox


def stage(sandbox: Path, name: str, scenario: Scenario, now: datetime) -> None:
    """Everything the stand-in reads and the agent meets: the home, the scene and the brief, as of `now`."""
    from ai_coc import commands
    from ai_coc.models import AppConfig, StateFile, VillageExport

    home = sandbox / "home" / ".ai_coc"
    (sandbox / OUT).mkdir(parents=True)
    export = staged(json.loads((SKILL / "village" / "export.json").read_text(encoding="utf-8")), scenario.edits)
    exported = datetime.fromisoformat(export["exported_at"])
    delta = now - exported - timedelta(minutes=scenario.minutes_after_export)
    export = shifted(export, delta)
    export["timestamp"] += round(delta.total_seconds())
    village = VillageExport.model_validate(export)
    (home / "account_json").mkdir(parents=True)
    name_on_disk = commands._export_path(village.tag, datetime.fromisoformat(export["exported_at"])).name  # noqa: SLF001
    (home / "account_json" / name_on_disk).write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    # The game's tables for the version the export was taken on, trimmed to what the
    # village holds, so `search` answers a name as it does on a machine that has them.
    # Kept compressed, since formatted they pass the repository's size limit.
    tables = home / "game_data" / export["game_version"]
    tables.mkdir(parents=True)
    (tables / "entities.json").write_bytes(gzip.decompress((SKILL / "village" / "entities.json.gz").read_bytes()))
    state = StateFile.model_validate(shifted(scenario.state, delta))
    (home / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    (home / "config.json").write_text(AppConfig().model_dump_json(indent=2), encoding="utf-8")
    if scenario.journal:
        lines = (json.dumps(shifted(line, delta), ensure_ascii=False) for line in scenario.journal)
        (home / "runs.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    status = shifted(json.loads((SKILL / "village" / "status.json").read_text(encoding="utf-8")), delta)
    (sandbox / "status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    scene = {
        "screens": [
            {"frame": str(FRAMES / screen.frame), "taps": [tap.model_dump() for tap in screen.taps]}
            for screen in scenario.screens
        ],
        "replies": shifted(scenario.replies, delta),
        "goblin_gems": scenario.goblin_gems,
        "live": scenario.live,
    }
    (sandbox / "scene.json").write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    situation = scenario.situation.strip()
    if scenario.last_frame:
        # Where the agent's last `tap` left its frame, as a real run directory would hold it.
        kept = home / "logs" / f"{now - timedelta(seconds=40):%Y-%m-%d-%H%M%S}-tap" / "frames"
        kept.mkdir(parents=True)
        shutil.copy2(FRAMES / scenario.last_frame, kept / "after.png")
        situation = situation.replace("{last_frame}", str(kept / "after.png"))
    brief = BRIEF.format(
        sandbox=sandbox, home=home, session=f"eval-{name}", out=sandbox / OUT, situation=situation
    )
    if scenario.user:
        brief += SAYS.format(user=scenario.user)
    (sandbox / "brief.md").write_text(brief, encoding="utf-8")


def _commit() -> str:
    done = subprocess.run(  # noqa: S603 - git with literal arguments
        ["git", "-C", str(CHECKOUT), "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def setup(only: list[str], repeat: int) -> None:
    every = scenarios()
    chosen = {name: every[name] for name in (only or every)}
    now = datetime.now().astimezone()
    stamp = f"{now:%Y%m%d-%H%M%S}"
    root = SANDBOXES / stamp
    results = RESULTS / stamp
    results.mkdir(parents=True)
    built: dict[str, str] = {}
    for name, scenario in chosen.items():
        for copy in range(1, repeat + 1):
            label = name if repeat == 1 else f"{name}.{copy}"
            built[label] = str(build(label, name, scenario, root, now))
    run = {"created": now.isoformat(), "commit": _commit(), "sandboxes": built}
    (results / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"run: {results}")
    for label, sandbox in built.items():
        print(f"{label}: brief {Path(sandbox) / 'brief.md'}, reply to {Path(sandbox) / REPLY}")


class Verdict(BaseModel):
    scenario: str
    sandbox: str
    check: str
    what: str
    anchor: str
    verdict: Literal["pass", "fail", "stale", "not run"]
    evidence: str


def _matches(call: dict[str, Any], select: Select) -> bool:
    args = call.get("args")
    if args is None or select.command not in (None, args.get("command")):
        return False
    if any(args.get(key) != value for key, value in select.args.items()):
        return False
    # Against the parser's own defaults: a bare `--upgrades` is `[]` where leaving it out is None.
    defaults = call.get("defaults", {})
    if any(args.get(key) != defaults.get(key) for key in select.unset):
        return False
    if select.outcome is not None and not re.search(select.outcome, str(call.get("outcome"))):
        return False
    if select.box is not None:
        left, top, right, bottom = select.box
        if not (left <= (args.get("x") or -1) <= right and top <= (args.get("y") or -1) <= bottom):
            return False
    return all(re.search(pattern, str(args.get(key) or "")) for key, pattern in select.patterns.items())


def _shown(call: dict[str, Any]) -> str:
    return "ai_coc " + " ".join(call["argv"]) + f" -> exit {call.get('exit')}, {call.get('outcome')}"


def _tables(reply: str) -> list[list[str]]:
    rows = []
    for line in reply.splitlines():
        text = line.strip()
        if text.startswith("|") and not re.fullmatch(r"[|\s:-]+", text):
            rows.append([cell.strip() for cell in text.strip("|").split("|")])
    return rows


def judge(check: Check, calls: list[dict[str, Any]], reply: str, out: Path) -> tuple[bool, str]:  # noqa: C901, PLR0911, PLR0912 - one branch per kind
    """Whether one check passed, and the evidence for it."""
    if check.kind in ("called", "not_called"):
        assert check.calls is not None
        hits = [call for call in calls if _matches(call, check.calls)]
        if check.kind == "called":
            return bool(hits), _shown(hits[0]) if hits else "no such call; ran: " + "; ".join(_shown(c) for c in calls)
        return not hits, _shown(hits[0]) if hits else "none"
    if check.kind == "nothing_claims":
        start = 0
        if check.after is not None:
            found = [index for index, call in enumerate(calls) if _matches(call, check.after)]
            if not found:
                return False, "no call to start from"
            start = found[0] + 1
        # `hold` claims the emulator and opens nothing, so it logs nobody out.
        claimed = [call for call in calls[start:] if call.get("claims") and call.get("args", {}).get("command") != "hold"]
        return not claimed, _shown(claimed[0]) if claimed else "none"
    if check.kind == "every_call_has":
        bare = [call for call in calls if call.get("args") and not all(call["args"].get(f) for f in check.fields)]
        return not bare, _shown(bare[0]) if bare else f"{len(calls)} calls"
    if check.kind == "table_cell":
        rows = [row for row in _tables(reply) if len(row) >= 4 and re.search(check.row, " | ".join(row))]
        good = [row for row in rows if re.search(check.pattern, row[-1])]
        shown = good or rows
        return bool(good), " | ".join(shown[0]) if shown else "no such row"
    if check.kind == "reply":
        found = re.search(check.pattern, reply, re.MULTILINE)
        evidence = found.group(0) if found else "not in the reply"
        return (found is None) == check.absent, evidence
    found_files = sorted(out.glob(check.file))
    path = found_files[0] if found_files else out / check.file
    if check.kind == "exists":
        return path.exists() != check.absent, path.name if path.exists() else "missing"
    if not path.exists():
        return False, f"{check.file} missing"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if re.search(check.select, line)]
    if not lines:
        return False, f"no line matching {check.select!r}"
    if check.kind in ("first_line", "last_line"):
        line = lines[0] if check.kind == "first_line" else lines[-1]
        return bool(re.search(check.must, line)), line.strip()
    bad = [
        line
        for line in lines
        if (check.must and not re.search(check.must, line)) or (check.must_not and re.search(check.must_not, line))
    ]
    return not bad, bad[0].strip() if bad else f"{len(lines)} lines"


def _anchored(check: Check) -> bool:
    file, phrase = check.anchor
    return phrase in (CHECKOUT / file).read_text(encoding="utf-8")


def grade(results: Path, against: Path | None, runtime: str, model: str) -> None:
    run = json.loads((results / "run.json").read_text(encoding="utf-8"))
    every = scenarios()
    verdicts: list[Verdict] = []
    for label, place in run["sandboxes"].items():
        sandbox = Path(place)
        name = label.split(".")[0]
        kept = results / label
        kept.mkdir(exist_ok=True)
        for item in (REPLY, "calls.jsonl"):
            if (sandbox / item).exists():
                shutil.copy2(sandbox / item, kept / item)
        if (sandbox / OUT).exists():
            shutil.copytree(sandbox / OUT, kept / OUT, dirs_exist_ok=True)
        calls_file = kept / "calls.jsonl"
        calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()] if calls_file.exists() else []
        reply = (kept / REPLY).read_text(encoding="utf-8") if (kept / REPLY).exists() else None
        for check in every[name].checks:
            anchor = f"{check.anchor[0]}: {check.anchor[1]}"
            if reply is None:
                verdict, evidence = "not run", f"no {REPLY}"
            else:
                passed, evidence = judge(check, calls, reply, kept / OUT)
                verdict = "pass" if passed else "fail"
                if not _anchored(check):
                    verdict, evidence = "stale", f"anchor gone from the skill; would be {verdict}: {evidence}"
            verdicts.append(
                Verdict(
                    scenario=name,
                    sandbox=label,
                    check=check.id,
                    what=check.what,
                    anchor=anchor,
                    verdict=verdict,
                    evidence=evidence,
                )
            )
    (results / "results.json").write_text(
        json.dumps([v.model_dump() for v in verdicts], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _report(run, verdicts, against, runtime, model)
    (results / "report.md").write_text(report, encoding="utf-8")
    print(report)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:200]


def _report(run: dict[str, Any], verdicts: list[Verdict], against: Path | None, runtime: str, model: str) -> str:
    passed = sum(v.verdict == "pass" for v in verdicts)
    lines = [
        "# Skill evals",
        "",
        f"- runtime: {runtime or 'not given'}; model and effort: {model or 'not given'}",
        f"- skills at {run['commit']}, set up {run['created']}",
        f"- {passed} of {len(verdicts)} checks pass",
        "",
        "| scenario | check | verdict | rule | evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {v.sandbox} | {v.check} | {v.verdict} | {_cell(v.anchor)} | {_cell(v.evidence)} |" for v in verdicts
    ]
    if against is not None:
        before = {
            (v["sandbox"], v["check"]): v["verdict"]
            for v in json.loads((against / "results.json").read_text(encoding="utf-8"))
        }
        changed = [v for v in verdicts if before.get((v.sandbox, v.check), v.verdict) != v.verdict]
        lines += ["", f"## Changed against {against.name}", ""]
        lines += [f"- {v.sandbox} / {v.check}: {before[(v.sandbox, v.check)]} -> {v.verdict}" for v in changed] or ["- nothing"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(prog="evals.py")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("setup", help="build one sandbox per scenario and print where each brief is")
    make.add_argument("--only", default="", help="scenario names, comma-separated; every one when left out")
    make.add_argument("--repeat", type=int, default=1, help="sandboxes per scenario")
    mark = sub.add_parser("grade", help="grade a run's sandboxes and write report.md beside run.json")
    mark.add_argument("run", type=Path, help="the run's results directory, as setup printed it")
    mark.add_argument("--against", type=Path, help="an earlier run's results directory to compare with")
    mark.add_argument("--runtime", default="", help="the runtime that ran the agents, with its version")
    mark.add_argument("--model", default="", help="the model and effort the agents ran on")
    arguments = parser.parse_args()
    if arguments.command == "setup":
        setup([name for name in arguments.only.split(",") if name], arguments.repeat)
    else:
        grade(arguments.run, arguments.against, arguments.runtime, arguments.model)


if __name__ == "__main__":
    main()
