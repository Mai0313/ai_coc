"""The headless commands: the wiring between the flags and the loops, with the emulator out.

`_controller` is the seam. Everything under it is `MuMuAdapter` and
`AdbController` against a live emulator, so it is patched to hand back a mock
and what is checked is what each command asks of that mock and of the runner
it builds. The runners themselves are tested in `test_runners.py`.
"""

from __future__ import annotations

import json
import math
import time
from typing import get_args
from pathlib import Path
from datetime import UTC, datetime
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

from ai_coc import plans, commands
from ai_coc.ui import world as world_ui
from ai_coc.models import (
    MapEdge,
    PlateJob,
    ProbeRay,
    AppConfig,
    NightPlan,
    AttackPlan,
    HeroReport,
    PlayedPlan,
    AdbEndpoint,
    BuildReport,
    HeroOptions,
    PlateReport,
    WallOptions,
    WallOutcome,
    AttackReport,
    DonateReport,
    VillageStock,
    AttackOptions,
    BuilderReport,
    CollectReport,
    DisplayTarget,
    DonateOptions,
    EntityMapping,
    VillageExport,
    LootThresholds,
    UpgradeOptions,
    StorageCapacity,
)
from ai_coc.constants import COC_PACKAGE
from ai_coc.ui.runner import ScreenRunner
from ai_coc.adapters.adb import AdbController, AdbControlError

FRAMES = Path(__file__).parent / "frames"
DISPLAY = DisplayTarget(logical_id="2", physical_id="9")


def _adb() -> MagicMock:
    """A controller that answers a display and nothing else, for the commands' own wiring."""
    adb = MagicMock(name="adb")
    adb.display_for.return_value = DISPLAY
    return adb


def _controller() -> AdbController:
    """A real controller with no emulator behind it, for the runners that validate their `adb`."""
    return AdbController(endpoint=AdbEndpoint(port=16384))


class SessionTests(unittest.TestCase):
    def test_a_session_is_the_controller_its_display_and_a_frame_directory_that_exists(
        self,
    ) -> None:
        adb = _adb()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(commands, "_controller", return_value=adb),
        ):
            frames = Path(folder) / "frames"
            got, display = commands._session(frames)
            assert frames.is_dir()
        assert got is adb
        assert display == DISPLAY
        adb.display_for.assert_called_once_with(COC_PACKAGE)

    def test_no_frame_directory_makes_none(self) -> None:
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(Path, "mkdir") as made,
        ):
            commands._session(None)
        made.assert_not_called()


class WorldCommandTests(unittest.TestCase):
    """Reading which village is up, and sailing to the other on request."""

    def _world(
        self,
        seen: str | None,
        go: str | None = None,
        crossed: str | None = None,
        *,
        display: bool = True,
    ) -> tuple[MagicMock, MagicMock, object]:
        adb = _adb()
        if not display:
            adb.display_for.side_effect = AdbControlError("no window yet")
        with (
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "current_world", return_value=seen),
            patch.object(commands, "cross", return_value=crossed) as sailed,
        ):
            report = commands.world(go)
        return adb, sailed, report

    def test_reading_is_one_capture_and_nothing_else(self) -> None:
        """No swipe, no tap and no pinch, which is what lets a session ask at any moment."""
        adb, sailed, report = self._world("day")
        assert (report.found, report.world, report.crossed) == ("day", "day", False)
        assert "日世界" in report.message
        assert adb.screenshot.call_count == 1
        adb.tap.assert_not_called()
        adb.swipe.assert_not_called()
        adb.zoom.assert_not_called()
        sailed.assert_not_called()

    def test_being_asked_for_the_village_already_up_sails_nowhere(self) -> None:
        _, sailed, report = self._world("night", go="night")
        assert report.world == "night"
        assert not report.crossed
        sailed.assert_not_called()

    def test_a_crossing_that_lands_says_where_it_came_from(self) -> None:
        _, sailed, report = self._world("day", go="night", crossed="night")
        assert (report.found, report.world, report.crossed) == ("day", "night", True)
        assert report.message == "從日世界切到夜世界"
        sailed.assert_called_once()
        assert sailed.call_args.args[2] == "night"

    def test_a_crossing_that_did_not_land_says_where_the_game_stayed(self) -> None:
        _, _, report = self._world("day", go="night", crossed="day")
        assert (report.world, report.crossed) == ("day", False)
        assert report.message == "想切到夜世界,但畫面還停在日世界"
        _, _, lost = self._world("day", go="night", crossed=None)
        assert lost.world is None
        assert "不明的畫面" in lost.message

    def test_a_game_with_no_window_yet_confirms_neither_village(self) -> None:
        _, sailed, report = self._world("day", go="night", display=False)
        assert (report.found, report.world) == (None, None)
        assert "還沒有畫面" in report.message
        sailed.assert_not_called()


class StockCommandTests(unittest.TestCase):
    """The read-only status check, which is about the village on screen and does not cross."""

    def _stock(
        self,
        seen: str | None,
        held: VillageStock | None = None,
        ceilings: StorageCapacity | None = None,
        under: str | None = None,
    ) -> tuple[MagicMock, MagicMock, object]:
        """A real controller, because `ScreenRunner` validates the one it is given.

        `under` is what `uncovered` finds once it has pressed a panel away, which
        is only reached when the first look found no village at all.
        """
        reads = [(seen, held), (under, held)]
        with (
            patch.object(commands, "_controller", return_value=_controller()),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(ScreenRunner, "read_storages", side_effect=reads),
            patch.object(ScreenRunner, "read_ceilings", return_value=ceilings) as ceiling_reader,
            patch.object(commands, "uncovered", return_value=under) as pressed,
        ):
            report = commands.stock()
        return pressed, ceiling_reader, report

    def test_the_home_village_answers_a_share_of_each_ceiling(self) -> None:
        """The form every decision here is made in: `stop_at` is a percentage."""
        _, _, report = self._stock(
            "day",
            VillageStock(gold=12_750_000, elixir=6_125_000, dark=400_000),
            StorageCapacity(gold=25_500_000, elixir=24_500_000, dark=400_000),
        )
        assert report.world == "day"
        assert report.filled == {"gold": "50%", "elixir": "25%", "dark": "100%"}

    def test_a_resource_with_no_ceiling_is_left_out_rather_than_guessed(self) -> None:
        """The builder base has no dark elixir bar, so it has no share to report."""
        _, _, report = self._stock(
            "night",
            VillageStock(gold=2_050_000, elixir=0, dark=0),
            StorageCapacity(gold=4_100_000, elixir=3_450_000),
        )
        assert report.world == "night"
        assert report.filled == {"gold": "50%", "elixir": "0%"}
        # Absent rather than 0%: nothing is known about how full it is, and a
        # zero would read as empty. The elixir above really is at zero.
        assert "dark" not in report.filled

    def test_a_village_whose_ceilings_will_not_read_still_reports_what_it_holds(self) -> None:
        """A partial ceiling read comes back None, and the water level is the useful half."""
        _, _, report = self._stock("day", VillageStock(gold=1, elixir=2, dark=3), None)
        assert report.held == VillageStock(gold=1, elixir=2, dark=3)
        assert report.filled == {}

    def test_a_screen_that_is_not_a_village_says_so_and_taps_nothing(self) -> None:
        pressed, ceiling_reader, report = self._stock(None)
        assert (report.world, report.held) == (None, None)
        # The ceilings are what cost six taps and three captures, so what this
        # is really about is that a frame with no village on it never spends
        # them. Asserting on `_tap` could not fail: it is only reachable through
        # the two readers this harness patches out.
        ceiling_reader.assert_not_called()
        pressed.assert_called_once()

    def test_a_panel_over_the_village_is_pressed_away_rather_than_given_up_on(self) -> None:
        """The ordinary way to arrive here, now that the skills ask for this before every run.

        Nothing above this clears a panel: `ScreenRunner` has no `_home` and
        `_settle_game` only waits, so without this the command would spend its
        whole wait reading None off a screen the last command left behind.
        """
        _, _, report = self._stock(
            None, VillageStock(gold=1, elixir=1, dark=1), StorageCapacity(gold=4), under="day"
        )
        assert report.world == "day"
        assert report.filled == {"gold": "25%"}

    def test_it_never_crosses_to_the_other_village(self) -> None:
        """Sailing is `world --go`, and a status check that moves the game is not one."""
        with patch.object(commands, "cross") as sailed:
            self._stock("night", VillageStock(gold=1, elixir=1, dark=0), StorageCapacity(gold=2))
        sailed.assert_not_called()


class PlateLineTests(unittest.TestCase):
    """The plate readings as a line for a person, built where the log is written.

    The report itself carries fields and an `outcome` and no prose, which is
    `StockReport`'s bargain applied to the two commands that were still keeping
    a sentence of their own. The sentence is still worth having — it is what a
    person reads in `run.log` — it just is not a field.
    """

    def _line(self, role: str = "builder", **fields: object) -> str:
        return commands._plate_line(PlateReport(role=role, **fields))

    def test_a_plate_with_work_on_it_names_the_soonest(self) -> None:
        assert (
            self._line(
                world="night",
                outcome="read",
                free=1,
                total=3,
                jobs=[PlateJob(name="X連弩", remaining=600), PlateJob(remaining=7200)],
            )
            == "建築工人 1/3,2 個在跑,最快的是X連弩,還要 10 分鐘"
        )

    def test_a_row_with_no_name_still_reports_its_countdown(self) -> None:
        line = self._line(
            world="day", outcome="read", free=0, total=6, jobs=[PlateJob(remaining=90_000)]
        )
        assert "1 天 1 小時" in line
        assert "最快的還要" in line

    def test_a_count_that_will_not_read_says_so_once(self) -> None:
        """The running total used to be printed twice on this path."""
        line = self._line(world="day", outcome="read", jobs=[PlateJob(remaining=600)])
        assert "數字讀不到" in line
        assert line.count("個在跑") == 1

    def test_every_countdown_unreadable_is_not_an_empty_plate(self) -> None:
        line = self._line(world="day", outcome="read", free=0, total=6, jobs=[PlateJob()])
        assert "每一個的倒數都讀不到" in line

    def test_the_three_failures_read_as_three_different_things(self) -> None:
        """Which is the whole reason `outcome` is a field: the rest are identical.

        An idle plate, a panel that would not open and a badge that was not on
        the frame all leave a village, a count that may or may not have read,
        and no rows.
        """
        assert (
            self._line(world="day", outcome="idle", free=3, total=3)
            == "建築工人 3/3,沒有在跑的項目"
        )
        assert "打不開" in self._line(world="day", outcome="panel_shut", free=0, total=3)
        assert "看不到護盾的牌子" in self._line("shield", world="day", outcome="no_badge")
        assert "不在村莊" in self._line(outcome="not_a_village")


class StatusCommandTests(unittest.TestCase):
    """The four readings taken off one village in one pass, and never the other one.

    Untested until the sentence came off the model: what `status` contributes of
    its own is the composition and the one retry, and both used to be checked
    only by whatever the joined message happened to read.
    """

    def _status(
        self, plates: list[PlateReport], under: str | None = None
    ) -> tuple[MagicMock, object]:
        with (
            # A real controller, because `ScreenRunner` validates the one it is given.
            patch.object(commands, "_controller", return_value=_controller()),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "_namer", return_value=None),
            patch.object(commands.PlateRunner, "read", side_effect=plates),
            patch.object(commands.PlateRunner, "shield", return_value=None),
            patch.object(commands, "stock_of", return_value=commands.StockReport(world="night")),
            patch.object(commands, "uncovered", return_value=under) as pressed,
        ):
            return pressed, commands.status()

    def _plate(self, role: str, world: str | None, outcome: str) -> PlateReport:
        return PlateReport(role=role, world=world, outcome=outcome)

    def test_the_four_readings_come_back_on_one_report(self) -> None:
        pressed, report = self._status([
            self._plate("builder", "night", "idle"),
            self._plate("lab", "night", "read"),
        ])
        assert (report.world, report.builder.outcome, report.lab.outcome) == (
            "night",
            "idle",
            "read",
        )
        assert report.stock.world == "night"
        assert report.shield is None
        pressed.assert_not_called()

    def test_a_panel_over_the_village_is_cleared_once_and_the_plate_read_again(self) -> None:
        """The first reading is the check, so a covered village costs a retry and no capture."""
        pressed, report = self._status(
            [
                self._plate("builder", None, "not_a_village"),
                self._plate("builder", "day", "read"),
                self._plate("lab", "day", "read"),
            ],
            under="day",
        )
        assert (report.world, report.builder.outcome) == ("day", "read")
        pressed.assert_called_once()

    def test_the_world_falls_back_through_whichever_reading_found_one(self) -> None:
        """No plate placing itself is not the same as no village: the bars may still say."""
        _, report = self._status([
            self._plate("builder", None, "not_a_village"),
            self._plate("lab", None, "not_a_village"),
        ])
        # `stock_of` read the builder base even though neither plate did.
        assert report.world == "night"


class CollectCommandTests(unittest.TestCase):
    def test_the_builder_base_empties_its_cart_instead_of_sweeping(self) -> None:
        adb = _adb()
        with (
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "current_world", return_value="night"),
            patch.object(commands, "collect_cart", return_value=300_000) as cart,
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            report = commands.collect()
        assert report == CollectReport(markers=1, elixir=300_000, message=report.message)
        assert "300000" in report.message
        cart.assert_called_once_with(adb, DISPLAY)
        runner.assert_not_called()

    def test_an_empty_cart_is_reported_as_nothing_rather_than_as_a_marker(self) -> None:
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "current_world", return_value="night"),
            patch.object(commands, "collect_cart", return_value=0),
        ):
            report = commands.collect()
        assert (report.markers, report.elixir) == (0, 0)
        assert report.message == "推車裡沒有東西可以收"

    def test_the_home_village_hands_over_to_the_upkeep_runner(self) -> None:
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "current_world", return_value="day"),
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            runner.return_value.collect.return_value = CollectReport(markers=3, message="收了")
            frames = Path(folder) / "frames"
            report = commands.collect(frames)
            assert runner.call_args.kwargs["frame_dir"] == frames
        assert report.markers == 3


class LoopCommandTests(unittest.TestCase):
    """Each loop command builds its runner from its options, and only from them."""

    def _planners(self) -> tuple[MagicMock, MagicMock]:
        main, lite = MagicMock(name="main tier"), MagicMock(name="lite tier")
        patcher = patch.object(
            commands,
            "_planner",
            side_effect=lambda _config, tier="main": lite if tier == "lite" else main,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        config = patch.object(commands.ConfigStore, "load", return_value=AppConfig())
        config.start()
        self.addCleanup(config.stop)
        return main, lite

    def test_upgrade_asks_the_main_tier_where_to_look_and_the_lite_tier_what_it_found(
        self,
    ) -> None:
        main, lite = self._planners()
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            runner.return_value.upgrade.return_value = BuildReport(message="開始了")
            report = commands.upgrade(UpgradeOptions(keep_gold=5, keep_elixir=7, only="金礦"))
        kwargs = runner.call_args.kwargs
        assert (kwargs["keep_gold"], kwargs["keep_elixir"], kwargs["only"]) == (5, 7, "金礦")
        assert kwargs["ai"] is main
        assert kwargs["namer"] is lite
        assert report.message == "開始了"

    def test_a_named_building_skips_the_finder_but_keeps_the_namer(self) -> None:
        """The whole point of asking is to find them; a caller who named them already looked."""
        _, lite = self._planners()
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            runner.return_value.upgrade.return_value = BuildReport()
            commands.upgrade(UpgradeOptions(at=[(900, 430)]))
        kwargs = runner.call_args.kwargs
        assert kwargs["ai"] is None
        assert kwargs["namer"] is lite
        assert kwargs["at"] == [(900, 430)]

    def test_hero_passes_the_hero_and_the_spot_it_was_given(self) -> None:
        main, _ = self._planners()
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "HeroRunner") as runner,
        ):
            runner.return_value.run.return_value = HeroReport(message="讀到")
            report = commands.hero(HeroOptions(upgrade="duke", at=(990, 430)))
        kwargs = runner.call_args.kwargs
        assert (kwargs["hero"], kwargs["at"]) == ("duke", (990, 430))
        assert kwargs["ai"] is main
        assert report.message == "讀到"

    def test_donate_passes_the_dry_run_and_the_round_limit(self) -> None:
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "ClanRunner") as runner,
        ):
            runner.return_value.donate.return_value = DonateReport(message="捐了")
            report = commands.donate(DonateOptions(dry_run=True, rounds=2))
        kwargs = runner.call_args.kwargs
        assert (kwargs["dry_run"], kwargs["rounds"]) == (True, 2)
        assert report.message == "捐了"

    def test_walls_skips_the_finder_when_told_where_the_walls_are(self) -> None:
        main, _ = self._planners()
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "WallRunner") as runner,
        ):
            runner.return_value.run.return_value = MagicMock(
                outcome="nothing_bought", upgrades=[], walls=0
            )
            runner.return_value.run.return_value.paid.return_value = 0
            commands.walls(WallOptions(at=[(1, 2)]))
            named = runner.call_args.kwargs
            commands.walls(WallOptions())
            found = runner.call_args.kwargs
        assert named["ai"] is None
        assert found["ai"] is main

    def test_builders_is_a_single_read(self) -> None:
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            runner.return_value.builders.return_value = BuilderReport(free=1, total=5)
            report = commands.builders()
        assert (report.free, report.total) == (1, 5)

    def test_a_caller_with_its_own_stop_reaches_the_wall_runner_and_the_report(self) -> None:
        """What lets the window run this function: it has a button, not a terminal.

        The runner reads it during the opening scan, which is the longest
        unguarded stretch a wall run has — and it is the runner that names the
        stop now, which is why this asserts the flag reached it rather than
        checking a sentence this command used to prefix.
        """
        self._planners()
        button = MagicMock(return_value=True)
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "WallRunner") as runner,
        ):
            runner.return_value.run.return_value = MagicMock(outcome="nothing_bought", walls=0)
            runner.return_value.run.return_value.upgrades = []
            report = commands.walls(WallOptions(), button)
        assert runner.call_args.kwargs["should_stop"] is button
        # The flag was never written, so this is the caller's condition alone.
        assert not commands.stop_requested()
        # Named by the runner rather than rewritten here; the mock stands in for
        # a loop that never reached its own stop check.
        assert report.outcome == "nothing_bought"


class WallLineTests(unittest.TestCase):
    """One log line per wall outcome, each saying the thing only it means.

    The three a project skill used to tell apart by wording are the reason:
    counting distinct strings would let two of them swap and still pass.
    """

    def test_every_way_a_wall_run_can_stop_has_a_line_of_its_own(self) -> None:
        own = {
            "bought": "升級了",
            "nothing_bought": "說不出原因",
            "cannot_afford": "買不起",
            "builders_busy": "工人都在忙",
            "no_walls_found": "找不到任何城牆",
            "no_village": "沒辦法回到村莊",
            "no_stock": "看不到村莊的儲量",
            "stopped": "收到停止要求",
        }
        assert set(own) == set(get_args(WallOutcome))
        for outcome, fragment in own.items():
            line = commands.WALL_LINES[outcome].format(walls=3)
            assert fragment in line, outcome
            others = [
                commands.WALL_LINES[other].format(walls=3) for other in own if other != outcome
            ]
            assert not any(fragment in other for other in others), outcome


class AttackSeriesStopTests(unittest.TestCase):
    """The series answers whoever started it, which is what lets the window run it.

    `ai_coc stop` is still the default and still the only thing that reaches a
    run started from another process. What the parameter adds is the window's
    own button, which has nothing else to write to — and going through here is
    what finally gives that button's flag somewhere to be cleared.
    """

    def _series(self, should_stop: MagicMock) -> tuple[MagicMock, list]:
        runner = MagicMock(name="AttackRunner")
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "_pick_world", return_value="day"),
            patch.object(commands, "FrameTicker"),
            patch.object(commands, "AttackRunner", return_value=runner) as built,
        ):
            series = commands.attack(AttackOptions(rounds=1), should_stop)
        return built, series.root

    def test_a_caller_that_is_already_stopping_plays_nothing(self) -> None:
        built, rounds = self._series(MagicMock(return_value=True))
        assert rounds == []
        built.return_value.run.assert_not_called()
        # Nobody asked the state file to stop; the round loop ended on the
        # caller's own condition.
        assert not commands.stop_requested()

    def test_the_condition_is_handed_to_the_runner_that_plays_the_battle(self) -> None:
        """Between rounds is not enough on its own: a stop asked for mid-search
        has to reach the runner, which is the only place that can stand down
        between opponents rather than mid-deploy.
        """
        button = MagicMock(return_value=False)
        built, rounds = self._series(button)
        assert built.call_args.kwargs["should_stop"] is button
        assert len(rounds) == 1


class AttackSeriesAdapterFailureTests(unittest.TestCase):
    """An emulator error ends the series rather than the process.

    Nothing above `main()` catches an `AdbControlError`, so one raised mid-round
    used to kill the run outright: `result.json` was never written and the rounds
    already played were countable only out of `run.log`. Seven runs died that way
    in three days, one of them on a round it had just won outright.
    """

    def _series(self, played: list, rounds: int = 0) -> list:
        runner = MagicMock(name="AttackRunner")
        runner.run.side_effect = played
        # The count lives on the runner, because one runner plays one series.
        runner.lost = 0
        runner.world = "day"
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "_pick_world", return_value="day"),
            patch.object(commands, "FrameTicker"),
            patch.object(commands, "AttackRunner", return_value=runner),
            patch.object(commands, "_rest", return_value=False),
        ):
            series = commands.attack(AttackOptions(rounds=rounds), MagicMock(return_value=False))
        return series.root

    def test_a_round_lost_to_the_emulator_is_recorded_and_the_series_carries_on(self) -> None:
        """One timeout is a blip, and an overnight run should survive it."""
        rounds = self._series([
            AdbControlError("ADB 指令失敗：adb read timeout"),
            AttackReport(stock_full=True, message="倉庫滿了"),
        ])
        assert len(rounds) == 2
        assert "模擬器沒有回應" in rounds[0].message
        assert rounds[-1].stock_full

    def test_three_in_a_row_ends_the_series_holding_what_it_played(self) -> None:
        """An emulator that answers nothing is not worth aiming more rounds at.

        The rounds already played still come back, which is the whole point:
        before this they went down with the process.
        """
        rounds = self._series([
            AttackReport(message="已進攻並回營"),
            *[AdbControlError("adb read timeout")] * 4,
        ])
        assert len(rounds) == 4
        assert rounds[0].message == "已進攻並回營"
        assert all("模擬器沒有回應" in report.message for report in rounds[1:])

    def test_a_round_that_came_back_clears_the_count(self) -> None:
        """Consecutive, not total: three blips spread over a run are not a dead emulator.

        Three of them, because two do not separate the two readings — the count
        reaches 2 either way and never trips. Counting them as a total instead
        ends this series on the third error, at five rounds.
        """
        rounds = self._series([
            AdbControlError("adb read timeout"),
            AttackReport(message="已進攻並回營"),
            AdbControlError("adb read timeout"),
            AttackReport(message="已進攻並回營"),
            AdbControlError("adb read timeout"),
            AttackReport(stock_full=True, message="倉庫滿了"),
        ])
        assert len(rounds) == 6
        assert rounds[-1].stock_full


class SettleGameTests(unittest.TestCase):
    """Waiting for a village that can be tapped, then pinching the camera out."""

    def _settle(
        self,
        worlds: list[str | None],
        displays: list[object] | None = None,
        polls: int = 3,
        stop: bool = False,
        **screens: list[bool],
    ) -> tuple[MagicMock, DisplayTarget | None]:
        """Poll against canned readings; `dropped` and `loading` are what each frame reads as."""
        adb = _adb()
        if displays is not None:
            adb.display_for.side_effect = displays
        with (
            patch.object(commands.time, "sleep"),
            patch.object(commands, "current_world", side_effect=worlds),
            patch.object(
                commands, "idle_disconnected", side_effect=screens.get("dropped") or [False] * 9
            ),
            patch.object(
                commands, "loading_screen", side_effect=screens.get("loading") or [False] * 9
            ),
            patch.object(commands, "restart_game", return_value=DISPLAY) as restarted,
            # `_settle_game` parks, and the park reads its own frames now.
            # None of these tests is about the park itself.
            patch.object(commands, "park_camera", return_value=True) as parked,
        ):
            adb.restarted = restarted
            adb.parked = parked
            return adb, commands._settle_game(adb, polls, lambda: stop)

    def test_either_village_ends_the_wait_and_the_camera_goes_out(self) -> None:
        """The park carries the pinch now, so one call settles both scale and position."""
        adb, display = self._settle([None, "night"])
        assert display == DISPLAY
        assert adb.screenshot.call_count == 2
        adb.parked.assert_called_once_with(adb, DISPLAY, "night")

    def test_a_game_with_no_window_yet_is_waited_on(self) -> None:
        adb, display = self._settle(["day"], displays=[AdbControlError("not yet"), DISPLAY])
        assert display == DISPLAY
        assert adb.display_for.call_count == 2

    def test_a_village_that_never_paints_gives_up_without_touching_the_camera(self) -> None:
        adb, display = self._settle([None, None, None], polls=3)
        assert display is None
        adb.parked.assert_not_called()
        adb.zoom.assert_not_called()

    def test_a_stop_ends_the_wait_before_the_first_capture(self) -> None:
        adb, display = self._settle([], stop=True)
        assert display is None
        adb.screenshot.assert_not_called()

    def test_a_dropped_session_is_restarted_once_and_no_more(self) -> None:
        """A restart that lands back on the dialog is the server still down, not a game to restart again."""
        adb, display = self._settle(
            [None, None, "day"], polls=3, dropped=[True, True, False], loading=[False]
        )
        assert display == DISPLAY
        assert adb.restarted.call_count == 1

    def test_giving_up_names_the_loading_screen(self) -> None:
        with self.assertLogs("ai_coc.commands", "WARNING") as logged:
            adb, display = self._settle([None] * 3, polls=3, loading=[True] * 3)
        assert display is None
        adb.restarted.assert_not_called()
        assert "loading screen" in logged.output[-1]

    def test_a_dialog_that_survives_the_restart_is_named_rather_than_the_village(self) -> None:
        with self.assertLogs("ai_coc.commands", "WARNING") as logged:
            adb, display = self._settle([None] * 3, polls=3, dropped=[True] * 3)
        assert display is None
        assert adb.restarted.call_count == 1
        assert "session is dropped" in logged.output[-1]


class FrameTickerTests(unittest.TestCase):
    def _ticker(self, folder: Path, seconds: float) -> commands.FrameTicker:
        return commands.FrameTicker(
            adb=_controller(), display=DISPLAY, out_dir=folder, seconds=seconds
        )

    def test_no_interval_starts_no_thread(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self._ticker(Path(folder), 0) as ticker:
                assert ticker._thread is None
            assert not list(Path(folder).iterdir())

    def test_frames_are_named_by_how_far_into_the_run_they_were_taken(self) -> None:
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(AdbController, "screenshot", return_value=b"png"),
        ):
            with self._ticker(Path(folder), 0.01):
                time.sleep(0.1)
            ticks = sorted(Path(folder).glob("tick_*.png"))
            assert ticks
            assert ticks[0].name == "tick_00000.0s.png"
            assert all(tick.read_bytes() == b"png" for tick in ticks)

    def test_a_failed_capture_is_a_gap_in_the_recording_rather_than_the_end_of_the_run(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(AdbController, "screenshot", side_effect=AdbControlError("gone")),
        ):
            with self._ticker(Path(folder), 0.01):
                time.sleep(0.05)
            assert not list(Path(folder).iterdir())


class RunPlumbingTests(unittest.TestCase):
    """The small pieces `attack` is built from, each with its own reason to exist."""

    def test_a_heartbeat_with_nowhere_to_write_is_refused(self) -> None:
        with pytest.raises(ValueError, match="--record"):
            commands._prepare_frames(AttackOptions(shot_every=1))
        with tempfile.TemporaryDirectory() as folder:
            frames = Path(folder) / "frames"
            commands._prepare_frames(AttackOptions(frame_dir=frames, shot_every=1))
            assert frames.is_dir()

    def test_the_plan_that_ran_is_written_where_it_was_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deep" / "plan.json"
            commands._write_plan(path, plans.flat())
            assert plans.load(path) == plans.flat()
            commands._write_plan(None, plans.flat())
            commands._write_plan(Path(folder) / "never.json", None)
            assert not (Path(folder) / "never.json").exists()

    def test_the_plan_log_holds_one_line_per_round_of_either_village(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "plans.jsonl"
            commands._log_plan(log, 1, plans.flat())
            commands._log_plan(log, 2, plans.night_flat())
            commands._log_plan(log, 3, None)
            lines = [
                PlayedPlan.model_validate_json(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
        assert [line.round for line in lines] == [1, 2]
        assert isinstance(lines[0].plan, AttackPlan)
        assert isinstance(lines[1].plan, NightPlan)

    def test_the_cart_is_emptied_every_few_builder_base_battles_and_never_on_the_home_village(
        self,
    ) -> None:
        adb = _adb()
        with patch.object(commands, "collect_cart") as cart:
            for battles in range(1, 2 * commands.CART_EVERY + 1):
                commands._empty_cart("night", battles, adb, DISPLAY)
            emptied = cart.call_count
            commands._empty_cart("day", commands.CART_EVERY, adb, DISPLAY)
            commands._empty_cart("night", 0, adb, DISPLAY)
        assert emptied == 2
        assert cart.call_count == 2

    def test_the_restart_counter_carries_on_until_it_is_due(self) -> None:
        runner, ticker = MagicMock(), MagicMock()
        with patch.object(commands, "_restart_emulator", return_value=True) as restart:
            assert commands._restarted(runner, ticker, 3, 0) == 3
            assert commands._restarted(runner, ticker, 1, 2) == 1
            restart.assert_not_called()
            assert commands._restarted(runner, ticker, 2, 2) == 0
            restart.assert_called_once_with(runner, ticker, commands.stop_requested)

    def test_a_restart_that_failed_is_an_error_unless_somebody_asked_to_stop(self) -> None:
        with (
            patch.object(commands, "_restart_emulator", return_value=False),
            patch.object(commands.logger, "error") as alarm,
            # `stop` only has something to ask of a run that claimed the
            # emulator, which is every real one and no bare unit test.
            commands.claim("attack"),
        ):
            assert commands._restarted(MagicMock(), MagicMock(), 2, 2) is None
            assert alarm.call_count == 1
            commands.stop()
            assert commands._restarted(MagicMock(), MagicMock(), 2, 2) is None
            assert alarm.call_count == 1

    def test_the_barracks_wait_runs_its_course_with_nobody_stopping_it(self) -> None:
        with patch.object(commands, "STOP_POLL", 0.001):
            assert not commands._rest(0.005)

    def test_stop_names_what_it_asked_to_stand_down(self) -> None:
        """Which command and which pid, because the answer to "did that land"
        used to be a file that looked the same however it got there.
        """
        with commands.claim("attack"):
            message = commands.stop()
            assert "attack" in message
            assert str(commands.STATE_PATH) in message
            assert commands.stop_requested()

    def test_the_claim_is_public_because_the_start_button_needs_it(self) -> None:
        """The window decides whether to stand down before it calls anything here.

        So a `stopping` left by `ai_coc stop` — the ordinary state after a
        farming session, since nothing takes it once the loops stop — has to be
        overwritten by `start_automation` itself. Reached through the public
        name rather than the widget, which no test can build.
        """
        with commands.claim("attack"):
            commands.stop()
            assert commands.stop_requested()
        with commands.claim("automation"):
            assert not commands.stop_requested()


class ReadCommandTests(unittest.TestCase):
    def test_a_home_village_frame_reads_as_one(self) -> None:
        reading = commands.read((FRAMES / "world_day.png").read_bytes())
        assert reading.world == "day"
        assert reading.stock is not None
        assert reading.scout is None
        assert not reading.attack_menu

    def test_a_builder_base_frame_carries_its_own_two_rows(self) -> None:
        """`stock` reads the gems bar as dark elixir there; `builder_stock` is the one to believe."""
        reading = commands.read((FRAMES / "world_night.png").read_bytes())
        assert reading.world == "night"
        assert reading.builder_stock == VillageStock(gold=574030, elixir=589419, dark=0)

    def test_read_says_when_a_frame_is_the_loading_screen(self) -> None:
        """Every other field is empty there, which used to leave a run's no-village report unexplained."""
        reading = commands.read((FRAMES / "loading_screen.png").read_bytes())
        assert reading.loading
        assert reading.world is None
        assert not commands.read((FRAMES / "world_day.png").read_bytes()).loading

    def test_the_builder_bases_own_screens_each_have_a_field(self) -> None:
        """A night round reporting 畫面不在建築大師基地 could be asked every question but that one.

        `attack_menu` answers for the home village alone — the two dialogs share
        nothing but the corner the button that opens them sits in — so without
        these a session debugging that report had no reader to ask.
        """
        menu = commands.read((FRAMES / "night_menu.png").read_bytes())
        assert menu.night_menu
        assert not menu.attack_menu
        searching = commands.read((FRAMES / "night_searching.png").read_bytes())
        assert searching.searching
        assert not searching.night_menu
        village = commands.read((FRAMES / "world_day.png").read_bytes())
        assert not village.night_menu
        assert not village.searching

    def test_the_result_screen_is_what_ends_a_battle_and_read_says_so(self) -> None:
        """`read_scout` answering None does not end one; these two were confused once."""
        assert commands.read((FRAMES / "battle_result.png").read_bytes()).battle_over
        assert commands.read((FRAMES / "battle_result_lit.png").read_bytes()).battle_over
        assert not commands.read((FRAMES / "world_day.png").read_bytes()).battle_over

    def test_the_sheet_and_the_dialog_a_spend_confirms_through_are_both_reported(self) -> None:
        assert commands.read((FRAMES / "upgrade_sheet.png").read_bytes()).upgrade_sheet is not None
        assert commands.read((FRAMES / "wall_spend_dialog.png").read_bytes()).dialog is not None
        village = commands.read((FRAMES / "world_day.png").read_bytes())
        assert village.upgrade_sheet is None
        assert village.dialog is None

    def test_a_battle_frame_measures_where_the_village_sits(self) -> None:
        """The one measurement every coordinate in a battle rests on."""
        box = commands.read((FRAMES / "battle_boundary_grass.png").read_bytes()).village_box
        assert box is not None
        assert box[2] - box[0] > 400


class CaptureAndViewTests(unittest.TestCase):
    def test_a_burst_is_numbered_and_spaced_by_the_gap(self) -> None:
        adb = _adb()
        adb.screenshot.return_value = b"x"
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands.time, "sleep") as slept,
        ):
            saved = commands.capture(Path(folder) / "shots", count=2, gap=0.5)
            assert [path.name for path in saved] == ["frame_000.png", "frame_001.png"]
            assert all(path.read_bytes() == b"x" for path in saved)
        # Between the two, and not after the last one.
        slept.assert_called_once_with(0.5)

    def _view(
        self, seen: str | None, zoom: str = "out", times: int = 2
    ) -> tuple[MagicMock, object]:
        adb = _adb()
        with (
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "current_world", return_value=seen),
            patch.object(world_ui.time, "sleep"),
            # The park reads its own frames now; a still one is what says it
            # arrived, and `PARK_STILL` of them is what it swipes for.
            patch.object(world_ui, "view_shift", return_value=(0, 0)),
        ):
            return adb, commands.view(zoom, times)

    def test_view_sends_the_pinch_at_the_games_own_display(self) -> None:
        adb, report = self._view("day", "in")
        _, far = self._view("day", "out", 3)
        adb.zoom.assert_any_call("in", 2, COC_PACKAGE, DISPLAY)
        assert "拉近" in report.message
        assert "拉遠" in far.message

    def test_zooming_in_does_not_claim_a_park(self) -> None:
        """The walk is only bounded at the far zoom, so there is none to make here.

        Measured two pinches in, the camera pans off the village into the map's
        dark border and was still moving after fourteen swipes. Parking anyway
        would undo the zoom that was just asked for and report a position it
        never reached, which is the one thing this command is trusted for.
        """
        adb, report = self._view("day", "in")
        adb.swipe.assert_not_called()
        # Refused before the capture, not after it: the village reading is only
        # there to tell the park which corner to push into, and this path has no
        # park to aim. Asking anyway is 0.7 s a run spends on nothing.
        adb.screenshot.assert_not_called()
        assert "沒有把鏡頭停回定位" in report.message

    def test_view_parks_the_camera_as_well_as_zooming_it(self) -> None:
        """Scale was only ever half of what a caller asking for the view wants.

        The far zoom was documented as centring the village too, and measured it
        does not move the camera at all — so a command that only pinched left
        every map coordinate valid until the next thing that moved the camera.
        """
        adb, report = self._view("day")
        assert adb.swipe.call_count == world_ui.PARK_STILL
        crossing = world_ui.CROSSINGS["night"]
        landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
        for call in adb.swipe.call_args_list:
            assert call.args[:2] == (crossing.start, landing)
        assert "停回定位" in report.message

    def test_the_builder_base_is_parked_into_its_own_corner(self) -> None:
        """The two maps clamp in opposite corners, so one push cannot serve both.

        Always pushing the home village's way would leave this village short of
        any clamp, at a position that is neither measured nor reproducible —
        which is the one thing the command now promises.
        """
        adb, _ = self._view("night")
        crossing = world_ui.CROSSINGS["day"]
        landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
        assert adb.swipe.call_count == world_ui.PARK_STILL
        for call in adb.swipe.call_args_list:
            assert call.args[:2] == (crossing.start, landing)

    def test_a_screen_that_is_not_a_village_keeps_the_zoom_and_nothing_else(self) -> None:
        """A killed run leaves the game mid-battle, and that is when this gets reached for.

        A swipe with a card selected deploys troops along its path rather than
        panning, so the park is gated on the same capture that says which village
        it is. The pinch is safe on any screen and still goes.
        """
        adb, report = self._view(None)
        adb.zoom.assert_called_once()
        adb.swipe.assert_not_called()
        assert "沒有把鏡頭停回定位" in report.message


class SurveyRunnerTests(unittest.TestCase):
    """The two battles spent measuring rather than fighting."""

    def _boundary(self) -> commands._BoundarySurvey:
        return commands._BoundarySurvey(
            adb=_controller(), display=DISPLAY, thresholds=LootThresholds()
        )

    def _map(self) -> commands._MapSurvey:
        return commands._MapSurvey(adb=_controller(), display=DISPLAY, thresholds=LootThresholds())

    def test_a_ray_agrees_when_the_inside_drop_is_refused_and_the_outside_one_lands(self) -> None:
        runner = self._boundary()
        with (
            patch.object(commands, "SURVEY_RAYS", (0.0, 90.0)),
            patch.object(commands, "card_groups", return_value=[[171]]),
            patch.object(commands._BoundarySurvey, "_wait_for_battle", return_value=b"battle"),
            patch.object(commands, "boundary_reach", side_effect=[(1200, 400), None]),
            # The inside drop drains nothing (refused) and the outside one drains the card.
            patch.object(commands, "card_drained", side_effect=[[], [171]]),
            patch.object(commands._BoundarySurvey, "_frame", return_value=b""),
            patch.object(AdbController, "tap_many") as tapped,
            patch.object(commands.time, "sleep"),
        ):
            runner._deploy(b"")
        assert runner.survey.rays == [
            ProbeRay(degrees=0.0, predicted=400, inside_refused=True, outside_refused=False)
        ]
        assert runner.survey.unread == [90.0]
        assert runner.survey.agreement == "1/1 rays agreed, 1 unread"
        assert tapped.call_count == 2

    def test_a_battle_that_never_opened_surveys_nothing(self) -> None:
        runner = self._boundary()
        with (
            patch.object(commands, "card_groups", return_value=[[171]]),
            patch.object(commands._BoundarySurvey, "_wait_for_battle", return_value=None),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            runner._deploy(b"")
        assert not runner.survey.rays
        tapped.assert_not_called()

    def _edge(self, drained: list[list[int]], alive: list[int] | None = None) -> MapEdge | None:
        runner = self._map()
        runner._troops = [171]
        with (
            patch.object(commands, "live_cards", return_value=[171] if alive is None else alive),
            patch.object(commands, "card_drained", side_effect=drained),
            patch.object(commands._MapSurvey, "_frame", return_value=b"shot"),
            patch.object(AdbController, "tap_many"),
            patch.object(commands.time, "sleep"),
        ):
            edge, _ = runner._edge(0.0, b"opening")
        return edge

    def test_a_ray_walks_inwards_until_a_drop_lands(self) -> None:
        """Due east the playfield ends 770 px out, and the third probe is two steps in."""
        edge = self._edge([[], [], [171]])
        assert edge == MapEdge(degrees=0.0, reached=770 - 2 * commands.MAP_STEP, predicted=770)

    def test_a_drop_taken_at_the_screen_edge_measured_the_screen_and_not_the_map(self) -> None:
        edge = self._edge([[171]])
        assert edge is not None
        assert edge.reached is None

    def test_a_ray_nothing_lands_on_and_a_row_with_no_troops_left_both_end_quietly(self) -> None:
        assert self._edge([[]] * commands.MAP_PROBES) is None
        assert self._edge([], alive=[]) is None

    def test_the_survey_fits_what_every_ray_measured(self) -> None:
        runner = self._map()
        radius = 1 / (math.cos(math.radians(45)) / 1000 + math.sin(math.radians(45)) / 450)
        readings = {0.0: 1000, 45.0: round(radius), 135.0: round(radius), 180.0: 1000}
        with (
            patch.object(commands, "card_groups", return_value=[[171]]),
            patch.object(commands._MapSurvey, "_wait_for_battle", return_value=b""),
            patch.object(
                commands._MapSurvey,
                "_edge",
                side_effect=lambda degrees, shot: (
                    (MapEdge(degrees=degrees, reached=readings[degrees], predicted=800), shot)
                    if degrees in readings
                    else (None, shot)
                ),
            ),
        ):
            runner._deploy(b"")
        assert len(runner.survey.edges) == len(readings)
        fitted = runner.survey.fitted
        assert fitted is not None
        assert abs(fitted.half_width - 1000) <= 3
        assert abs(fitted.half_height - 450) <= 3
        assert "found the map edge" in runner.survey.summary

    def test_probe_and_bounds_hand_the_survey_back(self) -> None:
        with (
            patch.object(commands, "_session", return_value=(_controller(), DISPLAY)),
            patch.object(commands._BoundarySurvey, "run"),
            patch.object(commands._MapSurvey, "run"),
        ):
            assert commands.probe().rays == []
            assert commands.bounds().edges == []


PAYLOAD = json.dumps({
    "tag": "#TEST",
    "timestamp": 1788624472,
    "buildings": [{"data": 1000001, "lvl": 16}],
    "equipment": [{"data": 90000000, "lvl": 3}],
    "boosts": {"clocktower_cooldown": 77842},
})


class ExportTests(unittest.TestCase):
    """Walking the settings menus for the game's own village export.

    Every tap is a fixed coordinate, so what is tested here is mostly refusal:
    a step that cannot confirm its screen must not go on to the next one.
    """

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.saved = Path(folder.name)
        self.adb = _adb()
        mapping = EntityMapping.model_validate({"th_buildings": {1000001: "Town Hall"}})
        stack = [
            patch.object(commands, "_controller", return_value=self.adb),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "fetch_entity_mapping", return_value=mapping),
            patch.object(commands, "ACCOUNT_JSON_DIR", self.saved),
            patch.object(commands, "PAGE_SETTLE", 0),
            patch.object(commands, "CLIPBOARD_GAP", 0),
        ]
        for one in stack:
            one.start()
            self.addCleanup(one.stop)

    def _export(self, **screens: object) -> tuple[object, MagicMock]:
        # The clipboard is read once for what was already on it and then once
        # per poll, so a `clip` given as a list is those reads in order.
        clip = screens.get("clip", PAYLOAD)
        reads = clip if isinstance(clip, list) else [clip] * (commands.CLIPBOARD_POLLS + 2)
        with (
            patch.object(commands, "settings_open", return_value=screens.get("settings", True)),
            patch.object(commands, "more_settings_open", return_value=screens.get("more", True)),
            patch.object(commands, "export_row", return_value=screens.get("row", (1131, 560))),
            patch.object(commands, "read_clipboard", side_effect=reads),
            patch.object(commands, "write_clipboard") as self.wrote,
            patch.object(commands, "clear_clipboard") as cleared,
        ):
            return commands.export(), cleared

    def test_the_village_is_named_and_written_down(self) -> None:
        report, cleared = self._export()
        assert report.tag == "#TEST"
        # The game's own export time, not this machine's clock.
        assert report.exported_at == datetime.fromtimestamp(1788624472, UTC).isoformat()
        assert report.timestamp == 1788624472
        assert report.boosts == {"clocktower_cooldown": 77842}
        names = {entity.data_id: entity.name for entity in report.entities}
        # Mapped where the community table has it, None where it does not —
        # never the number written into the name.
        assert names == {1000001: "Town Hall", 90000000: None}
        cleared.assert_called_once()
        written = (self.saved / "#TEST.json").read_text(encoding="utf-8")
        assert VillageExport.model_validate_json(written).tag == "#TEST"

    def test_a_game_that_never_came_back_is_not_tapped_at(self) -> None:
        """`_settle_game` answers None for a dropped session it could not restart,
        a game still loading, and an emulator with no window yet. The gear is a
        fixed coordinate, so none of those may be tapped on.
        """
        with patch.object(commands, "_settle_game", return_value=None):
            report = commands.export()
        assert "沒有回到村莊畫面" in report.message
        self.adb.tap.assert_not_called()
        self.adb.swipe.assert_not_called()

    def test_a_menu_that_did_not_open_stops_before_the_next_tap(self) -> None:
        """The whole point of reading: the second tap is a fixed coordinate."""
        report, _ = self._export(settings=False)
        assert "設定視窗沒有打開" in report.message
        assert report.entities == []
        # The gear, and then the close — never 更多設定 at a screen nobody confirmed.
        assert [call.args[:2] for call in self.adb.tap.call_args_list] == [
            commands.SETTINGS_GEAR,
            commands.CLOSE_SETTINGS,
        ]

    def test_a_list_that_never_reaches_the_row_is_never_tapped(self) -> None:
        """A missed scroll leaves the copy coordinate over a settings toggle."""
        report, cleared = self._export(row=None)
        assert "找不到" in report.message
        cleared.assert_not_called()
        assert self.adb.swipe.call_count == commands.SCROLL_TRIES
        assert (1131, 560) not in [call.args[:2] for call in self.adb.tap.call_args_list]

    def test_an_empty_clipboard_is_reported_rather_than_read_as_a_village(self) -> None:
        """Cleared first, so nothing arriving really is nothing copied."""
        with patch.object(commands, "CLIPBOARD_POLLS", 2):
            report, cleared = self._export(clip="")
        assert "剪貼簿沒有東西" in report.message
        cleared.assert_called_once()
        assert not list(self.saved.glob("*.json"))

    def test_whatever_was_on_the_clipboard_goes_back_on_it(self) -> None:
        """It belongs to whoever is at the keyboard, and this runs while they are working."""
        held = "something the user was copying"
        report, _ = self._export(clip=[held, PAYLOAD])
        assert report.tag == "#TEST"
        self.wrote.assert_called_once_with(held)

    def test_the_clipboard_is_put_back_even_when_nothing_was_copied(self) -> None:
        """The failing path is the one where taking it and not giving it back would hurt most."""
        with patch.object(commands, "CLIPBOARD_POLLS", 2):
            self._export(clip="")
        self.wrote.assert_called_once_with("")

    def test_a_clipboard_holding_something_else_is_reported_rather_than_raised(self) -> None:
        """The clipboard is the one input from outside: anything copied during
        the poll is read as the payload, and a parse error reaching the top
        would leave `result.json` unwritten.
        """
        report, _ = self._export(clip="just some text somebody copied")
        assert "不是村莊資料" in report.message
        assert not list(self.saved.glob("*.json"))

    def test_the_settings_window_is_closed_even_when_the_export_failed(self) -> None:
        """Every other command starts by assuming nothing is covering the village."""
        self._export(more=False)
        assert self.adb.tap.call_args_list[-1].args[:2] == commands.CLOSE_SETTINGS

    def test_a_tag_off_the_clipboard_cannot_escape_the_export_directory(self) -> None:
        """The tag is outside data: anything that parses as JSON can carry a slash."""
        assert commands._export_path("#GOOD").parent == self.saved
        assert commands._export_path("../../etc/passwd").name == "etcpasswd.json"
        assert commands._export_path("").name == "UNKNOWN.json"

    def test_reading_the_last_export_says_so_when_there_is_nothing_to_read(self) -> None:
        assert "還沒有匯出過" in commands.export(last=True).message

    def test_a_file_from_before_this_command_is_named_rather_than_read_as_empty(self) -> None:
        """The old shape held the game's raw payload, which parses as a village with nothing in it."""
        (self.saved / "#OLD.json").write_text(PAYLOAD, encoding="utf-8")
        report = commands.export(last=True)
        assert "不是這個指令存的格式" in report.message
        assert report.entities == []


class StorageCeilingTests(unittest.TestCase):
    """`StorageCapacity` is what the attack loop measures a village against; kept beside the loop."""

    def test_a_partial_ceiling_watches_only_what_it_has(self) -> None:
        assert StorageCapacity(gold=100).full(VillageStock(gold=95, elixir=0, dark=0), 90) == [
            "金幣"
        ]


if __name__ == "__main__":
    unittest.main()
