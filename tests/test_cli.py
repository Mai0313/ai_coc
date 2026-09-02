"""The entry point: every sub-command parses, and each one reaches the command it names.

Nothing here touches the emulator. `commands` is patched at the seam `_answer`
calls it through, so what is checked is that the flags on the line arrive as
the options model each loop takes, and that leaving a flag out is not the same
thing as passing it as zero.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
import argparse
import tempfile
import unittest
from unittest.mock import patch

import pytest

from ai_coc import commands
from ai_coc.cli import RECORDABLE, _spot, _answer, _parser, _run_command
from ai_coc.models import (
    RunLog,
    ViewReport,
    HeroOptions,
    WallOptions,
    AttackSeries,
    AttackOptions,
    CollectReport,
    DonateOptions,
    LootOverrides,
    UpgradeOptions,
)

FRAMES = Path(__file__).parent / "frames"

# What each sub-command needs on the line to parse at all. The parser test
# below holds this table equal to the parser's own list, so a sub-command
# added without a line here fails rather than going untested.
MINIMAL: dict[str, list[str]] = {
    "attack": [],
    "stop": [],
    "walls": [],
    "collect": [],
    "builders": [],
    "upgrade": [],
    "hero": [],
    "world": [],
    "view": [],
    "launch": [],
    "donate": [],
    "probe": [],
    "bounds": [],
    "capture": ["shots"],
    "read": ["shot.png"],
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

    def test_no_sub_command_means_the_window(self) -> None:
        arguments = _args()
        assert arguments.command is None
        assert not arguments.live_test
        assert arguments.agent_command == ""

    def test_record_is_only_offered_where_a_loop_reads_frames(self) -> None:
        for name in RECORDABLE:
            assert _args(name, "--record").record, name
        # `world` reads one frame and keeps nothing, so it has no such flag.
        with pytest.raises(SystemExit):
            _args("world", "--record")

    def test_a_spot_is_two_integers(self) -> None:
        assert _spot("260,140") == (260, 140)
        with pytest.raises(ValueError, match="invalid literal"):
            _spot("260")

    def test_hero_only_names_a_real_hero(self) -> None:
        """`unknown` is a `HeroKind` for the planner's benefit, not a hero to raise."""
        assert _args("hero", "--upgrade", "duke").upgrade == "duke"
        with pytest.raises(SystemExit):
            _args("hero", "--upgrade", "unknown")


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
                    "--world",
                    "night",
                    "--repeat",
                    "0",
                    "--min-gold",
                    "0",
                    "--restart-every",
                    "0",
                    "--stop-at",
                    "0",
                    "--record",
                    "--shot-every",
                    "4",
                    "--plan-in",
                    "a.json",
                    "--plan-out",
                    "b.json",
                ),
                self.recorded,
            )
        assert attacked.call_args.args[0] == AttackOptions(
            frame_dir=self.recorded.frames,
            plan_in=Path("a.json"),
            plan_out=Path("b.json"),
            plan_log=self.recorded.plan_log,
            minimums=LootOverrides(min_gold=0),
            world="night",
            rounds=0,
            shot_every=4.0,
            restart_every=0,
            stop_at=0,
        )

    def test_an_omitted_flag_is_none_rather_than_zero(self) -> None:
        """None keeps the file's value and zero takes a threshold out; the parser must not merge them."""
        with patch.object(commands, "attack", return_value=AttackSeries()) as attacked:
            _answer(_args("attack"), self.run)
        options = attacked.call_args.args[0]
        assert options == AttackOptions(plan_log=self.run.plan_log)
        assert options.minimums == LootOverrides()
        assert (options.restart_every, options.stop_at, options.world) == (None, None, None)
        assert options.frame_dir is None

    def test_walls_collects_every_named_spot(self) -> None:
        with patch.object(commands, "walls") as walled:
            _answer(
                _args(
                    "walls",
                    "--at",
                    "260,140",
                    "--at",
                    "420,260",
                    "--keep-gold",
                    "5",
                    "--rounds",
                    "2",
                ),
                self.run,
            )
        assert walled.call_args.args[0] == WallOptions(
            keep_gold=5, rounds=2, at=[(260, 140), (420, 260)]
        )

    def test_upgrade_hero_and_donate_carry_their_own_options(self) -> None:
        with (
            patch.object(commands, "upgrade") as upgraded,
            patch.object(commands, "hero") as raised,
            patch.object(commands, "donate") as gave,
        ):
            _answer(
                _args("upgrade", "--only", "金礦", "--at", "900,430", "--keep-elixir", "7"),
                self.run,
            )
            _answer(_args("hero", "--upgrade", "duke", "--at", "990,430"), self.run)
            _answer(_args("hero"), self.run)
            _answer(_args("donate", "--dry-run", "--rounds", "2"), self.run)
        assert upgraded.call_args.args[0] == UpgradeOptions(
            keep_elixir=7, at=[(900, 430)], only="金礦"
        )
        assert [call.args[0] for call in raised.call_args_list] == [
            HeroOptions(upgrade="duke", at=(990, 430)),
            HeroOptions(),
        ]
        assert gave.call_args.args[0] == DonateOptions(dry_run=True, rounds=2)

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
        viewed.assert_called_once_with("in", 2)
        assert [call.args for call in asked.call_args_list] == [("day",), (None,)]
        assert [call.args for call in launched.call_args_list] == [("game",), ("none",)]

    def test_the_frame_only_commands_get_this_runs_frame_directory(self) -> None:
        """None when nothing is recorded, which every loop reads as "do not save"."""
        for name in ("collect", "builders", "probe", "bounds"):
            with patch.object(commands, name) as ran:
                _answer(_args(name), self.run)
                _answer(_args(name, "--record"), self.recorded)
            assert [call.args for call in ran.call_args_list] == [
                (None,),
                (self.recorded.frames,),
            ], name

    def test_a_model_is_written_out_as_indented_json(self) -> None:
        with (
            patch.object(commands, "view", return_value=ViewReport(message="拉遠了")),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            assert _run_command(_args("view"), self.run) == 0
        expected = ViewReport(message="拉遠了").model_dump_json(indent=2)
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
            _run_command(_args("capture", "shots", "--count", "2", "--gap", "0.5"), self.run)
            saved = (self.run.directory / "result.json").read_text(encoding="utf-8")
        assert stopped == "已要求停止"
        assert saved == "a.png\nb.png"
        captured.assert_called_once_with(Path("shots"), 2, 0.5)

    def test_read_puts_the_named_file_through_every_parser(self) -> None:
        """The one command that runs its real code here: it needs no emulator."""
        with patch("sys.stdout", new_callable=io.StringIO):
            _run_command(_args("read", str(FRAMES / "world_day.png")), self.run)
        written = (self.run.directory / "result.json").read_text(encoding="utf-8")
        assert '"world": "day"' in written

    def test_collect_answers_through_the_same_path(self) -> None:
        with (
            patch.object(commands, "collect", return_value=CollectReport(markers=2)),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _run_command(_args("collect"), self.run)
        assert '"markers": 2' in (self.run.directory / "result.json").read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(unittest.main())
