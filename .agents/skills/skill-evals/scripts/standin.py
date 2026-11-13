"""The `ai_coc` an eval's agent runs: the real command line, with the emulator replaced by a recording.

Run as `python standin.py <scenario dir> <ai_coc arguments>`, which the sandbox's own
`ai_coc` launcher does. Everything that reads or writes files is the code under test:
argparse, `-h`, the claim and the loan on the sandbox's `state.json`, `note`, `stop`,
`giveback`, `search`, `status --last`, the run directories and the journal. Three
things are swapped:

- The home directory is the scenario's `home`, set before `ai_coc` is imported, so
  every path under `~/.ai_coc` lands in the sandbox; it refuses to run otherwise.
- Nothing reaches an emulator. A command that walks the game (`status`, `world --day`,
  `stock`, ...) answers the scenario's scripted report, rendered by the real text
  code; one with no script answers that the recording ends there. The commands that
  only attach (`tap`, `swipe`, `confirm`, `capture`, a plain `world`) run for real on
  the scenario's `tests/frames/` screens: a tap inside a box the current screen lists
  moves to the screen it names, and any other input ends the recording, since a
  screen recorded for one path is the wrong one for any other.
- `ai_coc watch` writes its section (and with `--hold` moves the session's hold on, as
  the real one does) and returns at once, since the dry run ends there, and pids the
  scenario lists read as live processes, which stand down at once when a claim asks
  them to.

Every call is appended to the scenario's `calls.jsonl`, which `evals.py grade` reads.
"""

from __future__ import annotations

import os
import sys
import json
from typing import TYPE_CHECKING, Any
from pathlib import Path
from datetime import datetime
import contextlib

if TYPE_CHECKING:
    import argparse

    from ai_coc.models import RunLog

# What a command prints when the scenario recorded nothing past this point.
RECORDING_ENDS = "（演練）這之後的畫面沒有錄下來，模擬器停在這裡；照你這時會怎麼做，回覆使用者。"
WATCH_SET = "（演練）watch 掛上了，會睡到 {wake:%m-%d %H:%M}；模擬器不會真的睡，這次演練到這裡結束。"
# How many sleeps a command may ask for before it is taken for a wait nothing will end.
SLEEP_LIMIT = 5000


class Unrecorded(RuntimeError):
    """The command reached something the scenario did not record."""


class Simulator:
    """The scenario's world, kept in files beside it because every command is a new process."""

    def __init__(self, scenario: Path) -> None:
        self.dir = scenario
        self.scene = json.loads((scenario / "scene.json").read_text(encoding="utf-8"))
        self.state = self._load("sim.json", {"screen": 0, "replies": {}, "live": self.scene["live"]})
        self.call: dict[str, Any] = {"argv": sys.argv[2:], "inputs": [], "scripted": False}
        self.sleeps = 0

    def _load(self, name: str, default: dict[str, Any]) -> dict[str, Any]:
        path = self.dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def save(self) -> None:
        (self.dir / "sim.json").write_text(json.dumps(self.state), encoding="utf-8")

    def live(self) -> set[int]:
        return set(self.state["live"])

    def set_live(self, pid: int, alive: bool) -> None:
        pids = self.live()
        pids.add(pid) if alive else pids.discard(pid)
        self.state["live"] = sorted(pids)

    def screen(self) -> bytes:
        screens = self.scene["screens"]
        if self.state["screen"] >= len(screens):
            raise Unrecorded("screen")
        return Path(screens[self.state["screen"]]["frame"]).read_bytes()

    def press(self, kind: str, *where: int) -> None:
        """Record one input and move to the screen a tap there leads to, or past the last."""
        self.call["inputs"].append([kind, *(str(part) for part in where)])
        screens = self.scene["screens"]
        taps = screens[self.state["screen"]]["taps"] if self.state["screen"] < len(screens) else []
        hit = [
            tap["to"]
            for tap in taps
            if kind == "tap" and tap["box"][0] <= where[0] <= tap["box"][2] and tap["box"][1] <= where[1] <= tap["box"][3]
        ]
        self.state["screen"] = hit[0] if hit else len(screens)

    def reply(self, key: str) -> dict[str, Any] | None:
        """The next scripted answer for `key`, the last one repeating; None when there is none."""
        queue = self.scene["replies"].get(key)
        if not queue:
            return None
        used = self.state["replies"].get(key, 0)
        self.state["replies"][key] = used + 1
        return queue[min(used, len(queue) - 1)]


class FakeController:
    """What `commands._attached` hands back: the scenario's screens, and every input recorded."""

    def __init__(self, sim: Simulator) -> None:
        self.sim = sim
        self.frames = None

    def display_for(self, package: str) -> object:
        from ai_coc.models import DisplayTarget

        return DisplayTarget(logical_id="0", physical_id="0")

    def screenshot(self, display: object, label: str | None) -> bytes:
        png = self.sim.screen()
        if self.frames is not None:
            self.frames.looked(self, display)
            if label is not None:
                self.frames.record(label, png)
        return png

    def tap(self, x: int, y: int, display: object) -> None:
        self.sim.press("tap", x, y)

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: int, display: object) -> None:
        self.sim.press("swipe", *start, *end)

    def back(self, display: object) -> None:
        self.sim.press("back")

    def __getattr__(self, name: str) -> object:
        raise Unrecorded(name)


def _key(arguments: argparse.Namespace) -> str | None:
    """Which scripted answer a call reads: the command, and the village it crosses to."""
    if getattr(arguments, "last", False):
        return None
    if arguments.command == "world":
        return None if arguments.world is None else f"world --{arguments.world}"
    return arguments.command


def _scripted(sim: Simulator, arguments: argparse.Namespace) -> Any:  # noqa: ANN401 - any report model
    """The report the scenario recorded for this call, or None when it recorded none."""
    from ai_coc import commands
    from ai_coc.models import (
        AttackRun,
        BoostReport,
        ViewReport,
        StockReport,
        WorldReport,
        LaunchReport,
        StatusReport,
        CollectReport,
    )

    key = _key(arguments)
    entry = None if key is None else sim.reply(key)
    if entry is None:
        return None
    sim.call["scripted"] = True
    if "stand_down" in entry:
        raise {outcome: error for error, outcome in commands.STAND_DOWNS.items()}[entry["stand_down"]]
    models = {
        "world": WorldReport,
        "stock": StockReport,
        "launch": LaunchReport,
        "view": ViewReport,
        "boost": BoostReport,
        "collect": CollectReport,
        "attack": AttackRun,
    }
    if arguments.command != "status":
        return models[arguments.command].model_validate(entry)
    base = json.loads((sim.dir / "status.json").read_text(encoding="utf-8"))
    report = StatusReport.model_validate(_merged(base, entry))
    report.notes = commands._kept().notes  # noqa: SLF001 - the sandbox's state file, as the real walk reads it
    return commands._timed(report, commands._last_export().export)  # noqa: SLF001 - the walk's own last step


def _defaults(parser: argparse.ArgumentParser, command: str) -> dict[str, Any]:
    """What each of a sub-command's arguments holds when it is not given, which the grader's `unset` compares with."""
    import argparse

    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))  # noqa: SLF001
    return {action.dest: action.default for action in sub.choices[command]._actions}  # noqa: SLF001


def _merged(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _install(sim: Simulator) -> None:  # noqa: C901 - one patch per seam, kept together
    """Swap the emulator out of `cli` and `commands`, and record the call."""
    import time

    from ai_coc import cli, commands
    from ai_coc.models import Caller, RunnerState

    real_answer, real_run, real_watch = cli._answer, cli._run_command, commands.watch  # noqa: SLF001
    real_ask, real_goblin = commands.ask_to_stand_down, commands.goblin_offer

    def refused(*_: object) -> object:
        raise Unrecorded("emulator")

    def sleep(_: float) -> None:
        sim.sleeps += 1
        if sim.sleeps > SLEEP_LIMIT:
            raise Unrecorded("wait")

    def alive(state: object) -> bool:
        pid = getattr(state, "pid", 0)
        return pid != os.getpid() and pid in sim.live()

    def attached(frames: object) -> FakeController:
        adb = FakeController(sim)
        adb.frames = frames
        return adb

    def ask(held: RunnerState, caller: Caller) -> None:
        """Ask as the real code does; a scripted holder then stands down at once, as one at its seam would."""
        real_ask(held, caller)
        with commands._turn():  # noqa: SLF001
            current = commands.read_state()
            if current is not None and current.pid == held.pid and current.status == "stopping":
                commands._write_state(  # noqa: SLF001
                    RunnerState(
                        status="idle",
                        pid=held.pid,
                        command=held.command,
                        started=held.started,
                        ended=datetime.now().astimezone(),
                        log=held.log,
                        caller=held.caller,
                        stop_by=current.stop_by,
                        loan=current.loan,
                    )
                )
        sim.set_live(held.pid, alive=False)

    def watch(
        caller: Caller | None, next_at: datetime | None, about: str, log: Path | None = None, *, farms: bool = False, hold: bool = False
    ) -> object:
        if next_at is None:
            return real_watch(caller, next_at, about, log, farms=farms, hold=hold)
        wake = next_at if next_at.tzinfo else next_at.astimezone()
        if hold:
            commands._keep_hold(caller or Caller(), wake + commands.HOLD_TIME)  # noqa: SLF001
        current = commands.read_watch()
        if current is not None:
            sim.set_live(current.pid, alive=False)
        commands._take_watch(caller or Caller(), wake, about, log, farms)  # noqa: SLF001
        sim.set_live(os.getpid(), alive=True)
        sim.call["wake"] = wake.isoformat()
        return WATCH_SET.format(wake=wake)

    def goblin(png: bytes) -> object:
        read = real_goblin(png)
        gems = sim.scene.get("goblin_gems")
        return read if read is None or gems is None else read.model_copy(update={"gems": gems})

    def answer(arguments: argparse.Namespace, run: object) -> object:
        try:
            reply = _scripted(sim, arguments)
            reply = real_answer(arguments, run) if reply is None else reply
        except Unrecorded as end:
            sim.call["unrecorded"] = str(end)
            return RECORDING_ENDS
        except tuple(commands.STAND_DOWNS) as error:
            sim.call["outcome"] = commands.STAND_DOWNS[type(error)]
            raise
        sim.call["outcome"] = getattr(reply, "ending", None) or getattr(reply, "outcome", None)
        return reply

    def run_command(arguments: argparse.Namespace, run: RunLog) -> int:
        """Record the call before its claim, which can queue or stand down before any answer exists."""
        sim.call["args"] = vars(arguments)
        sim.call["defaults"] = _defaults(cli._parser(), arguments.command)  # noqa: SLF001
        sim.call["claims"] = not (
            arguments.command in cli.WITHOUT_CLAIM
            or getattr(arguments, "last", False)
            or (arguments.command == "world" and arguments.world is None)
        )
        try:
            code = real_run(arguments, run)
        except Unrecorded as end:
            # A wait nothing in the recording ends, such as a yielding claim behind a loan.
            sim.call["unrecorded"] = str(end)
            run.answer(RECORDING_ENDS)
            sys.stdout.write(f"{RECORDING_ENDS}\n")
            return 0
        if "outcome" not in sim.call and (result := run.directory / "result.json").exists():
            with contextlib.suppress(ValueError):
                sim.call["outcome"] = json.loads(result.read_text(encoding="utf-8")).get("outcome")
        return code

    cli._answer = answer  # noqa: SLF001
    cli._run_command = run_command  # noqa: SLF001
    commands._controller = refused  # noqa: SLF001
    commands.emulators = refused
    commands._attached = attached  # noqa: SLF001
    commands._alive = alive  # noqa: SLF001
    commands.ask_to_stand_down = ask
    commands.watch = watch
    commands.goblin_offer = goblin
    time.sleep = sleep


def main() -> int:
    scenario = Path(sys.argv[1]).resolve()
    home = scenario / "home"
    os.environ["USERPROFILE"] = str(home)
    os.environ["HOME"] = str(home)
    from ai_coc import cli, constants

    if not constants.data_root().resolve().is_relative_to(home):
        sys.stderr.write(f"refusing: ~/.ai_coc resolved to {constants.data_root()}, outside {home}\n")
        return 3
    sim = Simulator(scenario)
    _install(sim)
    sys.argv = ["ai_coc", *sys.argv[2:]]
    code: int = 1
    try:
        code = cli.main()
    except SystemExit as leaving:
        code = leaving.code if isinstance(leaving.code, int) else 0 if leaving.code is None else 2
    finally:
        sim.call |= {"at": datetime.now().astimezone().isoformat(), "exit": code}
        with (scenario / "calls.jsonl").open("a", encoding="utf-8") as calls:
            calls.write(json.dumps(sim.call, ensure_ascii=False, default=str) + "\n")
        sim.save()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
