"""The headless commands: the wiring between the flags and the loops, with the emulator out.

`_controller` is the seam. Everything under it is `MuMuAdapter` and
`AdbController` against a live emulator, so it is patched to hand back a mock
and what is checked is what each command asks of that mock and of the runner
it builds. The runners themselves are tested in `test_runners.py`.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

from ai_coc import plans, commands
from ai_coc.models import (
    MapEdge,
    ProbeRay,
    AppConfig,
    NightPlan,
    AttackPlan,
    HeroReport,
    PlayedPlan,
    AdbEndpoint,
    BuildReport,
    HeroOptions,
    WallOptions,
    DonateReport,
    VillageStock,
    AttackOptions,
    BuilderReport,
    CollectReport,
    DisplayTarget,
    DonateOptions,
    LootThresholds,
    UpgradeOptions,
    StorageCapacity,
)
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import ZOOM_PINCHES, AdbController, AdbControlError

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
            runner.return_value.run.return_value = MagicMock(message="", upgrades=[])
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
        ):
            adb.restarted = restarted
            return adb, commands._settle_game(adb, polls, lambda: stop)

    def test_either_village_ends_the_wait_and_the_camera_goes_out(self) -> None:
        adb, display = self._settle([None, "night"])
        assert display == DISPLAY
        assert adb.screenshot.call_count == 2
        adb.zoom.assert_called_once_with("out", ZOOM_PINCHES, COC_PACKAGE, DISPLAY)

    def test_a_game_with_no_window_yet_is_waited_on(self) -> None:
        adb, display = self._settle(["day"], displays=[AdbControlError("not yet"), DISPLAY])
        assert display == DISPLAY
        assert adb.display_for.call_count == 2

    def test_a_village_that_never_paints_gives_up_without_a_pinch(self) -> None:
        adb, display = self._settle([None, None, None], polls=3)
        assert display is None
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
            restart.assert_called_once_with(runner, ticker)

    def test_a_restart_that_failed_is_an_error_unless_somebody_asked_to_stop(self) -> None:
        with (
            patch.object(commands, "_restart_emulator", return_value=False),
            patch.object(commands.logger, "error") as alarm,
        ):
            assert commands._restarted(MagicMock(), MagicMock(), 2, 2) is None
            assert alarm.call_count == 1
            commands.stop()
            assert commands._restarted(MagicMock(), MagicMock(), 2, 2) is None
            assert alarm.call_count == 1

    def test_the_barracks_wait_runs_its_course_with_nobody_stopping_it(self) -> None:
        with patch.object(commands, "STOP_POLL", 0.001):
            assert not commands._rest(0.005)

    def test_stop_names_the_flag_it_wrote(self) -> None:
        message = commands.stop()
        assert str(commands.STOP_FLAG) in message
        assert commands.stop_requested()


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

    def test_view_sends_the_pinch_at_the_games_own_display(self) -> None:
        adb = _adb()
        with patch.object(commands, "_controller", return_value=adb):
            report = commands.view("in", 2)
            far = commands.view()
        adb.zoom.assert_any_call("in", 2, COC_PACKAGE, DISPLAY)
        assert "拉近" in report.message
        assert "拉遠" in far.message


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


class StorageCeilingTests(unittest.TestCase):
    """`StorageCapacity` is what the attack loop measures a village against; kept beside the loop."""

    def test_a_partial_ceiling_watches_only_what_it_has(self) -> None:
        assert StorageCapacity(gold=100).full(VillageStock(gold=95, elixir=0, dark=0), 90) == [
            "金幣"
        ]


if __name__ == "__main__":
    unittest.main()
