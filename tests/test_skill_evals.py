"""The skill evals' own machinery: the scenarios stage, the stand-in answers from the recording, the grader reads it.

The stand-in runs as its own process, as an eval's agent runs it, with its home in this
test's directory, so nothing here reaches the emulator or the real `~/.ai_coc`. The
`uv sync` that gives a sandbox its own `ai_coc` launcher is left out: the launcher only
hands its arguments to the same script these tests run directly.
"""

from __future__ import annotations

import os
import sys
import json
from typing import TYPE_CHECKING, Any
from pathlib import Path
import argparse
from datetime import datetime
import subprocess
import importlib.util

import pytest

from ai_coc import commands
from ai_coc.models import StatusReport

if TYPE_CHECKING:
    from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "skill-evals" / "scripts"


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"skill_evals_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first, since pydantic resolves the models' annotations through it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evals = _module("evals")
standin = _module("standin")


def _staged(tmp_path: Path, name: str) -> Path:
    sandbox = tmp_path / name
    sandbox.mkdir()
    evals.stage(sandbox, name, evals.scenarios()[name], datetime.now().astimezone())
    return sandbox


def _run(sandbox: Path, *argv: str) -> tuple[int, str, dict[str, Any]]:
    """One `ai_coc` call through the stand-in: its exit code, its stdout, and the line it recorded."""
    named = ("--agent", "test", "--session", f"eval-{sandbox.name}", "--mission", "test")
    done = subprocess.run(  # noqa: S603 - this interpreter, the script under test
        [sys.executable, str(SCRIPTS / "standin.py"), str(sandbox), *argv, *named],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    calls = (sandbox / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    return done.returncode, done.stdout, json.loads(calls[-1])


@pytest.mark.parametrize("name", sorted(evals.scenarios()))
def test_every_scenario_stages_and_its_checks_point_at_the_skills(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its fixtures validate, its frames exist, its patterns compile, and every rule it pins is still in the text."""
    scenario = evals.scenarios()[name]
    sandbox = _staged(tmp_path, name)
    assert "{last_frame}" not in (sandbox / "brief.md").read_text(encoding="utf-8")
    for screen in scenario.screens:
        assert (evals.FRAMES / screen.frame).exists(), screen.frame
        assert all(tap.to < len(scenario.screens) for tap in screen.taps), screen.frame
    for check in scenario.checks:
        assert evals._anchored(check), (check.id, check.anchor)
    monkeypatch.setattr(
        commands, "ACCOUNT_JSON_DIR", sandbox / "home" / ".ai_coc" / "account_json"
    )
    sim = standin.Simulator(sandbox)
    for key, entries in scenario.replies.items():
        command, _, world = key.partition(" --")
        for _ in entries:
            arguments = argparse.Namespace(command=command, world=world or None, last=False)
            try:
                report = standin._scripted(sim, arguments)
            except tuple(commands.STAND_DOWNS):
                continue
            assert report is not None, key
            if isinstance(report, StatusReport):
                assert report.timers, "the status reply carries the staged export's timers"


def test_a_press_runs_the_real_confirm_on_the_recorded_screens(tmp_path: Path) -> None:
    """The goblin prompt reads 71 off its left button, the hire presses it, and a screen past the recording says so."""
    sandbox = _staged(tmp_path, "goblin-two-buttons")
    # The scenario stages the hold its last tap left; once given back, a hand presses nothing.
    _run(sandbox, "giveback")
    code, out, call = _run(sandbox, "confirm", "--price", "10800")
    assert (code, json.loads(out)["outcome"], call["inputs"]) == (1, "no_hold", [])
    _run(sandbox, "hold")
    code, _, call = _run(sandbox, "confirm", "--price", "10800")
    assert (code, call["outcome"], call["claims"]) == (0, "goblin_offered", True)
    _, out, call = _run(sandbox, "confirm", "--goblin", "--price", "71")
    assert call["outcome"] == "confirmed"
    assert call["inputs"] == [["tap", "965", "542"]]
    assert json.loads(out)["goblin"]["gems"] == 71
    _run(sandbox, "tap", "1572", "30")
    _, out, call = _run(sandbox, "tap", "800", "450")
    assert out.strip() == standin.RECORDING_ENDS
    assert call["unrecorded"] == "screen"


def test_a_command_that_walks_the_game_with_no_script_reaches_nothing(tmp_path: Path) -> None:
    sandbox = _staged(tmp_path, "goblin-two-buttons")
    code, out, call = _run(sandbox, "collect")
    assert (code, out.strip(), call["unrecorded"], call["claims"]) == (
        0,
        standin.RECORDING_ENDS,
        "emulator",
        True,
    )


def test_a_claim_that_waits_on_nothing_ends_the_recording_and_is_still_recorded(
    tmp_path: Path,
) -> None:
    """A yielding claim behind a holder that never releases waits before any answer exists."""
    sandbox = _staged(tmp_path, "takeover-stop-order")
    _run(sandbox, "stop")
    code, out, call = _run(sandbox, "stock", "--yield")
    assert (code, out.strip()) == (0, standin.RECORDING_ENDS)
    assert (call["unrecorded"], call["claims"], call["args"]["yields"]) == ("wait", True, True)


def test_a_misread_the_scenario_scripts_reaches_the_report(tmp_path: Path) -> None:
    sandbox = _staged(tmp_path, "goblin-reading-disagrees")
    _, out, _ = _run(sandbox, "confirm", "--price", "10800")
    assert json.loads(out)["goblin"]["gems"] == 7


def test_a_borrowed_command_that_loses_the_login_stands_down_and_a_stop_cancels_the_loan(
    tmp_path: Path,
) -> None:
    """The scripted holder lends at once, the read stands down as the real CLI does, and `stop` finds the loan."""
    sandbox = _staged(tmp_path, "borrow-session-taken")
    code, out, call = _run(sandbox, "status")
    assert (code, json.loads(out)["outcome"], call["outcome"]) == (
        1,
        "session_taken",
        "session_taken",
    )
    _, out, _ = _run(sandbox, "stop")
    assert json.loads(out)["loan"] == "cancelled"


def test_a_watch_returns_at_once_and_holds_the_section_for_the_next_command(
    tmp_path: Path,
) -> None:
    sandbox = _staged(tmp_path, "takeover-start")
    code, out, call = _run(
        sandbox, "watch", "--upgrades", "--lead", "5", "--tower", "--minutes", "180", "--farms"
    )
    assert code == 0
    assert out.startswith("（演練）watch")
    assert call["args"]["upgrades"] == []
    state = json.loads((sandbox / "home" / ".ai_coc" / "state.json").read_text(encoding="utf-8"))
    assert state["watch"]["status"] == "running"
    # As instants: on a UTC machine the file spells the offset `Z`.
    assert datetime.fromisoformat(state["watch"]["next_at"]) == datetime.fromisoformat(
        call["wake"]
    )
    _, out, _ = _run(sandbox, "stop", "watch")
    assert json.loads(out)["watch"] == "asked"


def test_the_status_script_is_printed_by_the_real_text_code(tmp_path: Path) -> None:
    sandbox = _staged(tmp_path, "takeover-start")
    _, out, call = _run(sandbox, "status")
    assert call["scripted"]
    assert "| 時間 | 目標 | 項目 |" in out
    assert "實驗室 (哥布林) | 亡靈 (13/14)" in out


def _call(command: str, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - any argument value
    return {
        "argv": [command],
        "args": {"command": command, **args},
        "defaults": {"upgrades": None, "tower": False, "next": [], "farms": False},
        "claims": command != "stop",
    }


def _check(**fields: Any) -> Any:  # noqa: ANN401 - a Check
    return evals.Check.model_validate({
        "id": "c",
        "what": "w",
        "anchor": ["AGENTS.md", "x"],
        **fields,
    })


def test_calls_are_matched_on_values_unset_arguments_boxes_and_outcomes(tmp_path: Path) -> None:
    calls = [
        _call("watch", minutes=5, upgrades=None, tower=False, farms=True),
        {**_call("tap", x=1430, y=193), "outcome": "pressed"},
    ]
    judge = evals.judge
    watch = {"command": "watch", "args": {"minutes": 5}, "unset": ["upgrades", "tower"]}
    assert judge(_check(kind="called", calls=watch), calls, "", tmp_path)[0]
    assert not judge(
        _check(kind="called", calls={**watch, "unset": ["farms"]}), calls, "", tmp_path
    )[0]
    bare = [_call("watch", minutes=5, upgrades=[], tower=False, farms=True)]
    assert not judge(_check(kind="called", calls=watch), bare, "", tmp_path)[0], (
        "a bare --upgrades"
    )
    assert judge(
        _check(kind="called", calls={"command": "tap", "box": [1395, 160, 1468, 228]}),
        calls,
        "",
        tmp_path,
    )[0]
    assert not judge(
        _check(kind="called", calls={"command": "tap", "box": [0, 0, 100, 100]}),
        calls,
        "",
        tmp_path,
    )[0]
    assert judge(
        _check(kind="not_called", calls={"command": "tap", "outcome": "^green_button$"}),
        calls,
        "",
        tmp_path,
    )[0]


def test_nothing_claims_counts_from_the_call_it_is_after(tmp_path: Path) -> None:
    calls = [{**_call("status"), "outcome": "session_taken"}, _call("stop")]
    after = {"outcome": "session_taken"}
    assert evals.judge(_check(kind="nothing_claims", after=after), calls, "", tmp_path)[0]
    assert not evals.judge(_check(kind="nothing_claims"), calls, "", tmp_path)[0]
    # `hold` claims and opens nothing, so it logs nobody out.
    calls.append(_call("hold"))
    assert evals.judge(_check(kind="nothing_claims", after=after), calls, "", tmp_path)[0]
    calls.append(_call("stock"))
    assert not evals.judge(_check(kind="nothing_claims", after=after), calls, "", tmp_path)[0]


def test_a_table_cell_is_the_last_cell_of_the_row_the_pattern_finds(tmp_path: Path) -> None:
    reply = "| 時間 | 目標 | 項目 | 後續 |\n| --- | --- | --- | --- |\n| 明天 15:14 | 工人 | 多人箭塔 (4/4) | 改升箭塔 (lv 9 → lv 10) |\n"
    found = _check(
        kind="table_cell", row=r"\| 工人 \|.*多人箭塔", pattern=r"(?<!多人)箭塔\D*9\D+10"
    )
    assert evals.judge(found, [], reply, tmp_path)[0]
    assert not evals.judge(found.model_copy(update={"pattern": "法師塔"}), [], reply, tmp_path)[0]
    assert not evals.judge(found.model_copy(update={"row": "地獄之塔"}), [], reply, tmp_path)[0]


def test_a_brief_file_is_found_by_its_pattern_and_judged_line_by_line(tmp_path: Path) -> None:
    (tmp_path / "farm-brief.md").write_text(
        "1. uv run --no-sync ai_coc world --night --yield\n"
        "2. uv run --no-sync ai_coc attack --repeat 0 --plan flat --yield\n"
        "3. uv run --no-sync ai_coc capture\n"
        "4. uv run --no-sync ai_coc world --day --yield\n",
        encoding="utf-8",
    )
    lines = {"file": "*brief.md", "select": r"ai_coc (world|attack)\b", "must": "--yield"}
    assert evals.judge(_check(kind="lines", **lines), [], "", tmp_path)[0]
    assert not evals.judge(_check(kind="lines", **{**lines, "must": "--plan"}), [], "", tmp_path)[
        0
    ]
    assert evals.judge(
        _check(kind="first_line", file="*brief.md", select="ai_coc world", must="--night"),
        [],
        "",
        tmp_path,
    )[0]
    assert evals.judge(
        _check(kind="last_line", file="*brief.md", select="ai_coc ", must="world --day"),
        [],
        "",
        tmp_path,
    )[0]
    assert evals.judge(_check(kind="exists", file="push.md", absent=True), [], "", tmp_path)[0]
