"""The entry point: every sub-command parses, and each one reaches the command it names.

Nothing here touches the emulator. `commands` is patched at the seam `_answer`
calls it through, so what is checked is that the flags on the line arrive as
the options model each loop takes, and that leaving a flag out is not the same
thing as passing it as zero.
"""

from __future__ import annotations

import io
import sys
import json
from typing import Any
import logging
from pathlib import Path
import argparse
import tempfile
import unittest
from unittest.mock import patch

import pytest

from ai_coc import cli, plans, models, commands, logging_setup
from ai_coc.cli import NO_SCREEN, _answer, _parser, _run_command
from ai_coc.models import (
    Caller,
    RunLog,
    RunEnded,
    AppConfig,
    ViewReport,
    NamedEntity,
    WorldReport,
    AttackReport,
    AttackSeries,
    VillageStock,
    AttackOptions,
    CollectReport,
    DonateOptions,
    LootOverrides,
    VillageExport,
)

FRAMES = Path(__file__).parent / "frames"

# What each sub-command needs on the line to parse at all. The parser test
# below holds this table equal to the parser's own list, so a sub-command
# added without a line here fails rather than going untested.
MINIMAL: dict[str, list[str]] = {
    "attack": [],
    "stop": [],
    "giveback": [],
    "watch": ["--next", "2026-10-12T23:10"],
    "collect": [],
    "stock": [],
    "status": [],
    "world": [],
    "view": [],
    "tap": ["100", "200"],
    "swipe": ["1385", "740", "1385", "240"],
    "confirm": ["--price", "10800"],
    "boost": [],
    "launch": [],
    "donate": [],
    "probe": [],
    "bounds": [],
    "capture": [],
    "read": ["shot.png"],
    "export": [],
}


def _args(*line: str) -> argparse.Namespace:
    return _parser().parse_args(list(line))


class ParserTests(unittest.TestCase):
    def test_every_sub_command_parses_and_none_is_missing_from_this_list(self) -> None:
        parser = _parser()
        [group] = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        assert set(group.choices) == set(MINIMAL)
        for name, extra in MINIMAL.items():
            assert parser.parse_args([name, *extra]).command == name

    def test_a_sub_command_is_required(self) -> None:
        with pytest.raises(SystemExit) as stopped:
            _args()
        assert stopped.value.code != 0

    def test_every_sub_command_takes_who_is_asking_and_none_needs_it(self) -> None:
        for name, extra in MINIMAL.items():
            asked = _args(name, *extra, "--agent", "codex", "--session", "s1", "--mission", "m")
            assert (asked.agent, asked.session, asked.mission) == ("codex", "s1", "m"), name
            bare = _args(name, *extra)
            assert (bare.agent, bare.session, bare.mission) == ("", "", ""), name

    def test_every_sub_command_that_captures_takes_debug_and_a_heartbeat(self) -> None:
        for name, extra in MINIMAL.items():
            if name in NO_SCREEN:
                for flag in (["--debug"], ["--shot-every", "4"]):
                    with pytest.raises(SystemExit):
                        _args(name, *extra, *flag)
            else:
                assert _args(name, *extra, "--debug").debug, name
                # On its own: a heartbeat does not need `--debug`.
                assert _args(name, *extra, "--shot-every", "4").shot_every == 4.0, name

    def test_stop_reaches_both_sections_unless_told_which(self) -> None:
        assert _args("stop").target == "all"
        assert _args("stop", "watch").target == "watch"
        assert _args("stop", "emulator").target == "emulator"
        with pytest.raises(SystemExit):
            _args("stop", "attack")

    def test_a_watch_says_when_it_wakes(self) -> None:
        """It is the timer now, so one with no wake time has nothing to sleep until."""
        with pytest.raises(SystemExit):
            _args("watch")

    def test_confirm_pays_a_wall_only_in_gold_or_elixir(self) -> None:
        assert _args("confirm", "--price", "800000").pay is None
        assert _args("confirm", "--price", "800000", "--pay", "elixir").pay == "elixir"
        with pytest.raises(SystemExit):
            _args("confirm", "--price", "800000", "--pay", "dark")

    def test_confirm_hires_a_goblin_only_when_asked_and_never_with_a_wall(self) -> None:
        assert not _args("confirm", "--price", "15").goblin
        assert _args("confirm", "--price", "15", "--goblin").goblin
        with pytest.raises(SystemExit):
            _args("confirm", "--price", "15", "--goblin", "--pay", "gold")


class DispatchTests(unittest.TestCase):
    """Each entry of `_answer` hands `commands` what the flags said, and no more."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = RunLog(directory=Path(folder.name))
        self.recorded = RunLog(directory=Path(folder.name) / "kept", recording=True)
        self.recorded.directory.mkdir()

    def test_every_attack_flag_reaches_its_option(self) -> None:
        with patch.object(commands, "attack", return_value=AttackSeries()) as attacked:
            _answer(
                _args(
                    "attack",
                    "--repeat",
                    "0",
                    "--min-gold",
                    "0",
                    "--stop-at",
                    "0",
                    "--until-idle",
                    "--debug",
                    "--plan",
                    "a.json",
                    "--plan-out",
                    "b.json",
                ),
                self.recorded,
            )
        assert attacked.call_args.args[1] is self.recorded.frames
        assert attacked.call_args.args[0] == AttackOptions(
            plan=Path("a.json"),
            plan_out=Path("b.json"),
            minimums=LootOverrides(min_gold=0),
            rounds=0,
            stop_at=0,
            until_idle=["builder", "lab"],
        )

    def test_until_idle_watches_what_it_names_and_both_when_it_names_none(self) -> None:
        for flags, watched in (
            ((), []),
            (("--until-idle",), ["builder", "lab"]),
            (("--until-idle", "builder"), ["builder"]),
            (("--until-idle", "lab"), ["lab"]),
        ):
            with patch.object(commands, "attack", return_value=AttackSeries()) as attacked:
                _answer(_args("attack", *flags), self.run)
            assert attacked.call_args.args[0].until_idle == watched, flags

    def test_an_omitted_flag_is_none_rather_than_zero(self) -> None:
        """None keeps the file's value and zero takes a threshold out; the parser must not merge them."""
        with patch.object(commands, "attack", return_value=AttackSeries()) as attacked:
            _answer(_args("attack"), self.run)
        options = attacked.call_args.args[0]
        assert options == AttackOptions()
        assert options.minimums == LootOverrides()
        assert options.stop_at is None
        assert not attacked.call_args.args[1].recording

    def test_donate_carries_its_own_options(self) -> None:
        with patch.object(commands, "donate") as gave:
            _answer(_args("donate", "--dry-run", "--rounds", "2"), self.run)
        assert gave.call_args.args == (DonateOptions(dry_run=True, rounds=2), self.run.frames)

    def test_the_scalar_commands_pass_their_flags_through(self) -> None:
        with (
            patch.object(commands, "view") as viewed,
            patch.object(commands, "world") as asked,
            patch.object(commands, "launch") as launched,
        ):
            _answer(_args("view", "--zoom", "in", "--times", "2"), self.run)
            _answer(_args("world", "--go", "day"), self.run)
            _answer(_args("world"), self.run)
            _answer(_args("launch", "--restart", "game"), self.run)
            _answer(_args("launch"), self.run)
        viewed.assert_called_once_with(self.run.frames, "in", 2)
        assert [call.args for call in asked.call_args_list] == [
            ("day", self.run.frames),
            (None, self.run.frames),
        ]
        # And the state file's stop, so `ai_coc stop` ends the wait for the village.
        assert [call.args for call in launched.call_args_list] == [
            ("game", commands.stop_requested, self.run.frames),
            ("none", commands.stop_requested, self.run.frames),
        ]

    def test_every_command_that_captures_gets_this_runs_one_writer(self) -> None:
        """One spelling for all of them, recording or not."""
        for name, extra in MINIMAL.items():
            if name in NO_SCREEN:
                continue
            with patch.object(commands, name) as ran:
                _answer(_args(name, *extra), self.run)
            assert any(arg is self.run.frames for arg in ran.call_args.args), name

    def test_a_model_is_written_out_as_indented_json(self) -> None:
        with (
            patch.object(commands, "view", return_value=ViewReport(outcome="parked")),
            # A claim logs, and pytest's live logging puts its own stdout back
            # over this one on every line it shows.
            patch.object(commands, "claim"),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            assert _run_command(_args("view"), self.run) == 0
        expected = ViewReport(outcome="parked").model_dump_json(indent=2)
        assert (self.run.directory / "result.json").read_text(encoding="utf-8") == expected
        assert out.getvalue() == f"{expected}\n"

    def test_text_answers_are_written_as_they_are(self) -> None:
        """`stop` and `capture` answer plain text rather than a model."""
        with (
            patch.object(commands, "stop", return_value="已要求停止"),
            patch.object(
                commands, "capture", return_value=[Path("a.png"), Path("b.png")]
            ) as captured,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(_args("stop"), self.run)
            stopped = (self.run.directory / "result.json").read_text(encoding="utf-8")
            _run_command(_args("capture", "--count", "2", "--gap", "0.5"), self.recorded)
            saved = (self.recorded.directory / "result.json").read_text(encoding="utf-8")
        assert stopped == "已要求停止"
        assert saved == "a.png\nb.png"
        # This run's own frame directory rather than a path off the command
        # line, which is what let a caller put frames anywhere it liked.
        captured.assert_called_once_with(self.recorded.frames, 2, 0.5)

    def test_a_table_is_only_for_the_terminal_and_the_answer_is_still_json(self) -> None:
        """`--table` changes what a person sees, never what the run wrote down."""
        export = VillageExport(
            tag="#TEST",
            exported_at="2026-09-06T00:00:00+00:00",
            entities=[
                NamedEntity(section="buildings", data_id=1000001, level=16, name="Town Hall")
            ],
            outcome="exported",
        )
        with (
            patch.object(commands, "export", return_value=export),
            patch.object(commands, "claim"),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            _run_command(_args("export", "--table"), self.run)
        written = (self.run.directory / "result.json").read_text(encoding="utf-8")
        assert written == export.model_dump_json(indent=2)
        # The table, not the JSON: rich draws the row rather than the field names.
        assert "Town Hall" in out.getvalue()
        assert '"data_id"' not in out.getvalue()

    def test_a_table_with_no_rows_still_says_why(self) -> None:
        """`--last --table` on a machine that has never run this is the line both
        skills open with, and its outcome is the failure's only explanation.
        """
        blank = VillageExport(tag="", exported_at="", outcome="never_exported")
        with (
            patch.object(commands, "export", return_value=blank),
            patch.object(commands, "claim"),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            _run_command(_args("export", "--table", "--last"), self.run)
        assert "還沒有匯出過任何村莊資訊" in out.getvalue()

    def test_reading_the_last_export_takes_no_claim_on_the_emulator(self) -> None:
        """`--last` reads one file. A claim there would overwrite a farming run's
        own record and release it as `idle` while a battle was still going on.
        """
        blank = VillageExport(tag="", exported_at="", outcome="never_exported")
        with (
            patch.object(commands, "export", return_value=blank) as exported,
            patch.object(commands, "claim") as claimed,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(_args("export", "--last"), self.run)
            claimed.assert_not_called()
            _run_command(_args("export"), self.run)
            claimed.assert_called_once()
        assert [call.args[1] for call in exported.call_args_list] == [True, False]

    def test_a_command_that_only_looks_takes_no_claim(self) -> None:
        """The file is a lock, so a claim stands the loop holding it down. A
        screenshot must not cost a farming run its night (#290); a crossing
        drives the game and claims like anything else.
        """
        with (
            patch.object(
                commands,
                "world",
                return_value=WorldReport(found="day", world="day", outcome="here"),
            ),
            patch.object(commands, "capture", return_value=[]),
            patch.object(commands, "giveback", return_value=""),
            patch.object(commands, "watch", return_value=""),
            patch.object(commands, "claim") as claimed,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(_args("world"), self.run)
            _run_command(_args("capture"), self.run)
            _run_command(_args("giveback"), self.run)
            _run_command(_args("watch", "--next", "2026-10-12T23:10"), self.run)
            claimed.assert_not_called()
            _run_command(_args("world", "--go", "night"), self.run)
            claimed.assert_called_once()

    def test_a_queue_a_stop_ended_answers_rather_than_crashes(self) -> None:
        """`farm` reads a run with no `result.json` as a killed process, so the
        line is written; and a command chained after it with `&&` must not start,
        so the exit is not zero.
        """
        with (
            patch.object(commands, "claim", side_effect=commands.LoanCancelledError("取消了")),
            patch("sys.stdout", new_callable=io.StringIO) as shown,
        ):
            assert _run_command(_args("attack"), self.run) != 0
        assert "取消了" in shown.getvalue()
        assert "取消了" in (self.run.directory / "result.json").read_text(encoding="utf-8")

    def test_a_capture_keeps_its_frames_in_its_own_run_without_debug(self) -> None:
        """It writes its frames under names of its own, so there is nothing for a
        caller to forget. Reached through `main`, because that is where the run
        is opened and where a `--label` also has to arrive.
        """
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(models, "LOG_DIR", Path(folder)),
            patch.object(sys, "argv", ["ai_coc", "capture", "--label", "baseline"]),
            patch.object(commands, "capture", return_value=[]) as captured,
            patch.object(cli, "configure_logging"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            assert cli.main() == 0
        frames = captured.call_args.args[0]
        assert not frames.recording
        assert frames.directory.name == "frames"
        assert frames.directory.parent.name.endswith("-capture-baseline")

    def test_debug_keeps_the_frames_and_logs_at_debug(self) -> None:
        root = logging.getLogger()
        level = root.level
        try:
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.object(models, "LOG_DIR", Path(folder)),
                patch.object(sys, "argv", ["ai_coc", "stock", "--debug"]),
                patch.object(commands, "stock", return_value="") as stocked,
                patch.object(commands, "claim"),
                # Setting up the log puts the level back to INFO, as the real
                # one does, so `--debug` only holds if it is applied afterwards.
                patch.object(
                    cli, "configure_logging", side_effect=lambda _: root.setLevel("INFO")
                ),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                assert cli.main() == 0
                assert root.level == logging.DEBUG
            assert stocked.call_args.args[0].recording
        finally:
            root.setLevel(level)

    def test_the_heartbeat_runs_around_the_whole_command_inside_its_claim(self) -> None:
        """Started where every command is run, so each one that captures has it,
        and inside the claim, so a run still queued for the emulator takes no tick.
        """
        order: list[str] = []
        with (
            patch.object(commands, "claim") as claimed,
            patch.object(commands, "FrameTicker") as ticker,
            patch.object(commands, "stock", side_effect=lambda _: order.append("stock") or ""),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            claimed.return_value.__enter__.side_effect = lambda: order.append("claimed")
            claimed.return_value.__exit__.side_effect = lambda *_: order.append("released")
            ticker.return_value.__enter__.side_effect = lambda: order.append("ticking")
            ticker.return_value.__exit__.side_effect = lambda *_: order.append("stopped")
            _run_command(_args("stock", "--shot-every", "4"), self.run)
        assert ticker.call_args.kwargs == {"frames": self.run.frames, "seconds": 4.0}
        assert order == ["claimed", "ticking", "stock", "stopped", "released"]

    def test_who_asked_reaches_the_claim(self) -> None:
        with (
            patch.object(
                commands, "collect", return_value=CollectReport(markers=0, outcome="collected")
            ),
            patch.object(commands, "claim") as claimed,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(
                _args(
                    "collect", "--agent", "claude-code", "--session", "abc", "--mission", "收集"
                ),
                self.run,
            )
        assert claimed.call_args.args[2] == Caller(
            agent="claude-code", session="abc", mission="收集"
        )

    def test_a_run_logs_who_called_it_and_warns_when_nobody_said(self) -> None:
        """Through `main`, where the log is opened, so the line lands in every
        run's own `run.log`; `read` needs no emulator for it.
        """
        asked = ["--agent", "codex", "--session", "s1", "--mission", "m"]
        for extra, warned in (([], True), (asked, False)):
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.object(models, "LOG_DIR", Path(folder)),
                patch.object(
                    sys, "argv", ["ai_coc", "read", str(FRAMES / "world_day.png"), *extra]
                ),
                patch.object(cli, "configure_logging"),
                patch.object(cli.logger, "info") as said,
                patch.object(cli.logger, "warning") as warning,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                assert cli.main() == 0
            assert any(call.args[0].startswith("Called by") for call in said.call_args_list)
            assert warning.called is warned

    def test_the_first_line_names_the_directory_and_a_cull_is_said_after(self) -> None:
        """Deleting frames is the one destructive thing in the project, and it
        runs inside `RunLog.open`, before any handler exists, so `main` says it
        once the run's own lines are out. A run that deleted nothing says nothing.
        """
        for culled in (3, 0):
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.object(models, "LOG_DIR", Path(folder)),
                patch.object(models, "_cull_old_frames", return_value=culled),
                patch.object(sys, "argv", ["ai_coc", "read", str(FRAMES / "world_day.png")]),
                patch.object(cli, "configure_logging"),
                patch.object(cli.logger, "info") as said,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                assert cli.main() == 0
            lines = [call.args[0] for call in said.call_args_list]
            assert lines[0].startswith("This run is being kept in")
            deleted = [n for n, line in enumerate(lines) if line.startswith("Deleted the frames")]
            assert deleted == ([3] if culled else []), culled

    def test_no_library_writes_ahead_of_the_first_line_at_debug(self) -> None:
        """The journal's file lock logs at DEBUG, and once did so before it."""
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        try:
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.object(models, "LOG_DIR", Path(folder)),
                patch.dict("os.environ", {"COC_LOG_LEVEL": "DEBUG"}),
                patch.object(sys, "argv", ["ai_coc", "read", str(FRAMES / "world_day.png")]),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                assert cli.main() == 0
                for handler in root.handlers:
                    if handler not in handlers:
                        handler.close()
                log = next(Path(folder).glob("*/run.log")).read_text(encoding="utf-8")
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
        assert "This run is being kept in" in log.splitlines()[0]

    def test_read_puts_the_named_file_through_every_parser(self) -> None:
        """The one command that runs its real code here: it needs no emulator."""
        with patch("sys.stdout", new_callable=io.StringIO):
            _run_command(_args("read", str(FRAMES / "hud_day.png")), self.run)
        written = (self.run.directory / "result.json").read_text(encoding="utf-8")
        assert '"world": "day"' in written

    def test_collect_answers_through_the_same_path(self) -> None:
        with (
            patch.object(
                commands, "collect", return_value=CollectReport(markers=2, outcome="collected")
            ),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(_args("collect"), self.run)
        assert '"markers": 2' in (self.run.directory / "result.json").read_text(encoding="utf-8")


def _journal() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in logging_setup.JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
    ]


class JournalTests(unittest.TestCase):
    """`runs.jsonl` answers on its own once a run's directory is gone, and never fails the run."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.run = RunLog(directory=self.folder / "2026-11-03-000000-attack")
        self.run.directory.mkdir()

    def test_a_run_leaves_a_start_and_an_end_that_lead_back_to_its_directory(self) -> None:
        """Through `main`, so the start carries the line as typed."""
        line = ["read", str(FRAMES / "world_day.png"), "--agent", "codex", "--session", "s1"]
        with (
            patch.object(models, "LOG_DIR", self.folder),
            patch.object(sys, "argv", ["ai_coc", *line]),
            patch.object(cli, "configure_logging"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            assert cli.main() == 0
        start, end = _journal()
        directory = Path(start["log"])
        assert (start["event"], start["run"], start["args"]) == ("start", directory.name, line)
        assert start["caller"]["agent"] == "codex"
        assert directory.parent == self.folder
        assert (end["event"], end["run"], end["exit"]) == ("end", directory.name, 0)
        assert end["result"] is not None

    def test_an_attack_ends_on_its_last_round_the_others_having_lines_of_their_own(self) -> None:
        series = AttackSeries([
            AttackReport(outcome="took_loot"),
            AttackReport(outcome="stock_full"),
        ])
        with (
            patch.object(commands, "attack", return_value=series),
            patch.object(commands, "claim"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(_args("attack"), self.run)
        (end,) = _journal()
        assert (end["outcome"], end["result"]["outcome"]) == ("stock_full", "stock_full")

    def test_every_round_of_a_series_gets_a_line_with_the_storages_before_it(self) -> None:
        """What `farm` works a run's income out of once its directory is gone."""
        holding = VillageStock(gold=1, elixir=2, dark=3)
        root = logging.getLogger()
        journal, level = logging_setup.Journal(self.run), root.level
        root.addHandler(journal)
        root.setLevel(logging.INFO)
        try:
            with (
                patch.object(commands, "_controller"),
                patch.object(commands, "current_world", return_value="day"),
                patch.object(commands, "_planner", return_value=None),
                patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
                patch.object(commands, "_rest", return_value=False),
                patch.object(commands, "AttackRunner") as runner,
            ):
                runner.return_value.run.side_effect = [
                    AttackReport(outcome="took_loot", phases=1),
                    AttackReport(outcome="stock_full"),
                ]
                runner.return_value.configure_mock(
                    played=None, holding=holding, capacity=None, cart=None
                )
                commands.attack(AttackOptions(rounds=0), self.run.frames)
        finally:
            root.removeHandler(journal)
            root.setLevel(level)
        rounds = [
            (event["round"], event["report"]["outcome"], event["holding"]["dark"])
            for event in _journal()
            if event["event"] == "round"
        ]
        assert rounds == [(1, "took_loot", 3), (2, "stock_full", 3)]

    def test_a_round_line_carries_the_plan_it_played_in_a_shape_plan_takes_back(self) -> None:
        """The one record of which tactic each round played, on either village."""
        played = [plans.flat(), plans.night_flat()]
        root = logging.getLogger()
        journal, level = logging_setup.Journal(self.run), root.level
        root.addHandler(journal)
        root.setLevel(logging.INFO)
        try:
            with (
                patch.object(commands, "_controller"),
                patch.object(commands, "current_world", return_value="day"),
                patch.object(commands, "_planner", return_value=None),
                patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
                patch.object(commands, "AttackRunner") as runner,
            ):
                runner.return_value.run.return_value = AttackReport(outcome="took_loot", phases=1)
                for plan in played:
                    runner.return_value.configure_mock(
                        played=plan, holding=None, capacity=None, cart=None
                    )
                    commands.attack(AttackOptions(), self.run.frames)
        finally:
            root.removeHandler(journal)
            root.setLevel(level)
        replay = self.folder / "replay.json"
        back = []
        for event in _journal():
            if event["event"] == "round":
                replay.write_text(json.dumps(event["plan"]), encoding="utf-8")
                back.append(plans.load_any(replay))
        assert back == played

    def test_the_emulator_changing_hands_is_kept_with_who_was_on_the_other_side(self) -> None:
        """`state.json` keeps only the latest of these, so this is the loan's history."""
        farming = Caller(agent="codex", session="s1", mission="打日世界資源")
        borrowing = Caller(agent="claude-code", session="s2", mission="測一個修正")
        root = logging.getLogger()
        journal, level = logging_setup.Journal(self.run), root.level
        root.addHandler(journal)
        root.setLevel(logging.INFO)
        try:
            with commands.claim("attack", caller=farming, yields=True):
                mine = commands.read_state()
                assert mine is not None
                commands.ask_to_stand_down(mine, borrowing)
                # The ask came from another process, which is where it is recorded.
                commands._borrowed = None
        finally:
            root.removeHandler(journal)
            root.setLevel(level)
        seen = [(event["what"], (event["other"] or {}).get("agent")) for event in _journal()]
        assert seen == [("claimed", None), ("borrowed", "codex"), ("lent", "claude-code")]

    def test_a_warning_is_copied_with_its_trace_and_plain_progress_is_not(self) -> None:
        named = logging.getLogger("ai_coc.test_journal")
        journal = logging_setup.Journal(self.run)
        named.addHandler(journal)
        named.setLevel(logging.INFO)
        try:
            named.info("Round 1 of no limit")
            try:
                raise ValueError("boom")
            except ValueError:
                named.exception("Crashed")
        finally:
            named.removeHandler(journal)
        (warned,) = _journal()
        assert (warned["event"], warned["level"], warned["text"]) == ("warn", "ERROR", "Crashed")
        assert "ValueError: boom" in warned["trace"]

    def test_a_line_that_cannot_be_written_does_not_fail_the_run(self) -> None:
        with (
            # A directory where the file should be, so the append itself fails.
            patch.object(logging_setup, "JOURNAL_PATH", self.folder),
            patch("sys.stderr", new_callable=io.StringIO) as said,
        ):
            logging_setup.Journal(self.run).write(RunEnded(exit=0, seconds=1.0))
        assert "沒寫進去" in said.getvalue()


if __name__ == "__main__":
    sys.exit(unittest.main())
