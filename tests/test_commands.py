"""The headless commands: the wiring between the flags and the loops, with the emulator out.

`_controller` is the seam. Everything under it is an emulator adapter and
`AdbController` against a live emulator, so it is patched to hand back a mock
and what is checked is what each command asks of that mock and of the runner
it builds. The runners themselves are tested in `test_runners.py`.
"""

from __future__ import annotations

import io
import os
import json
import math
import time
from typing import TYPE_CHECKING, Literal, get_args
from pathlib import Path
from datetime import UTC, date, datetime, timedelta
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
import pytest
import adbutils
from pydantic import ValidationError

from ai_coc import plans, commands, logging_setup
from ai_coc.ui import world as world_ui
from ai_coc.models import (
    SESSION_TAKEN_SENTENCE,
    Watch,
    Caller,
    MapEdge,
    FrameLog,
    HoldStop,
    LoanStop,
    ProbeRay,
    AppConfig,
    AttackRun,
    GameLevel,
    LootOffer,
    NightPlan,
    WatchStop,
    AttackPlan,
    CartReport,
    ClockTower,
    GameDialog,
    GameEntity,
    PlateCount,
    SearchNeed,
    SheetOffer,
    StopReport,
    WaitReport,
    AdbEndpoint,
    CartOutcome,
    NextUpgrade,
    NoteOutcome,
    RunnerState,
    ShieldState,
    StockReport,
    ViewOutcome,
    AttackReport,
    AttackSeries,
    BoostOutcome,
    DonateReport,
    EmulatorStop,
    ExportReport,
    FarmedReport,
    GameEntities,
    LaunchReport,
    PressOutcome,
    RestartScope,
    SeriesEnding,
    StatusReport,
    TendedReport,
    UpgradeTimer,
    VillageStock,
    WorldOutcome,
    AttackOptions,
    AttackOutcome,
    CollectReport,
    DisplayTarget,
    DonateOptions,
    DonateOutcome,
    ExportOutcome,
    GrowthOutcome,
    LaunchOutcome,
    SearchOptions,
    SearchOutcome,
    SurveyOutcome,
    VillageExport,
    CollectOutcome,
    ConfirmOutcome,
    HelperCooldown,
    LootThresholds,
    GivebackOutcome,
    StorageCapacity,
    VillageDocument,
    StandDownOutcome,
    PlateCountOutcome,
    TurnaroundOutcome,
)
from ai_coc.adapters import gamedata
from ai_coc.constants import COC_PACKAGE
from ai_coc.ui.runner import ScreenRunner
from ai_coc.adapters.adb import PNG_MAGIC, AdbController, AdbControlError
from ai_coc.parsers.home import PANEL_CLOSE, CLOCK_TOWER_BELOW
from ai_coc.parsers.village import SECTIONS, merge
from ai_coc.parsers.gamedata import READING, GameDataError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ai_coc.models import World, SearchReport, SearchSection

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


def _tables() -> GameEntities:
    """Synthetic game tables naming the town hall, a hero and a helper."""
    return GameEntities(
        game_version="1.2.3",
        reading=1,
        halls={"town_hall": 1000001},
        entities=[
            GameEntity(
                data_id=1000001,
                zh="大本營",
                en="Town Hall",
                world="day",
                table="buildings",
                max_lvl=17,
                levels=[GameLevel(lvl=16), GameLevel(lvl=17, cost={"gold": 2}, time=60)],
            ),
            GameEntity(
                data_id=28000000,
                zh="野蠻人之王",
                en="Barbarian King",
                world="day",
                table="heroes",
                max_lvl=99,
                levels=[],
            ),
            GameEntity(
                data_id=93000000,
                zh="建築工人學徒",
                en="Builder's Apprentice",
                world="day",
                table="villager_apprentices",
                max_lvl=8,
                levels=[],
            ),
        ],
    )


def _frames(case: unittest.TestCase, *, recording: bool = False) -> FrameLog:
    """A run's frames in a directory of the test's own, removed when it ends."""
    folder = Path(case.enterContext(tempfile.TemporaryDirectory()))
    return FrameLog(directory=folder / "frames", recording=recording)


def _pair(
    label: str, serial: str, index: int = 0, *, running: bool = False
) -> tuple[MagicMock, MagicMock]:
    """One listed instance and the emulator that runs it, as `commands.emulators` yields them."""
    emulator = MagicMock(label=label)
    emulator.serial_of.return_value = serial
    emulator.serial_for.return_value = serial
    instance = MagicMock(index=index, coc_running=running, endpoint=AdbEndpoint.parse(serial))
    return emulator, instance


class EmulatorChoiceTests(unittest.TestCase):
    """Which instance every command drives: the one `adb_serial` names, picked once if it names none."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = commands.ConfigStore(path=Path(folder.name) / "config.json")
        patcher = patch.object(commands, "ConfigStore", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_with_nothing_configured_mumu_wins_and_its_serial_is_written_down(self) -> None:
        mumu = _pair("MuMu", "127.0.0.1:16480", 3, running=True)
        ld = _pair("LDPlayer", "127.0.0.1:5555", running=True)
        assert commands.chosen([mumu, ld]) == mumu
        assert self.store.load().adb_serial == "127.0.0.1:16480"

    def test_with_nothing_configured_the_instance_running_the_game_wins(self) -> None:
        mumu = _pair("MuMu", "127.0.0.1:16480", 3)
        ld = _pair("LDPlayer", "127.0.0.1:5555", running=True)
        assert commands.chosen([mumu, ld]) == ld
        assert self.store.load().adb_serial == "127.0.0.1:5555"

    def test_with_nothing_running_the_first_listed_is_taken(self) -> None:
        mumu, ld = _pair("MuMu", "127.0.0.1:16480", 3), _pair("LDPlayer", "127.0.0.1:5555")
        assert commands.chosen([mumu, ld]) == mumu

    def test_a_configured_serial_is_matched_in_either_of_adbs_spellings(self) -> None:
        mumu, ld = _pair("MuMu", "127.0.0.1:16480", 3), _pair("LDPlayer", "127.0.0.1:5555")
        self.store.save(AppConfig(adb_serial="emulator-5554"))
        assert commands.chosen([mumu, ld]) == ld

    def test_an_instance_that_is_down_is_found_by_where_it_will_listen(self) -> None:
        """MuMu reports port 0 until it is up, and `ensure_coc` is what brings it up."""
        emulator, instance = _pair("MuMu", "127.0.0.1:16480", 3)
        instance.endpoint = AdbEndpoint()
        self.store.save(AppConfig(adb_serial="127.0.0.1:16480"))
        assert commands.chosen([(emulator, instance)]) == (emulator, instance)

    def test_a_serial_nothing_answers_to_is_an_error_rather_than_a_fallback(self) -> None:
        self.store.save(AppConfig(adb_serial="127.0.0.1:7555"))
        with pytest.raises(RuntimeError, match="7555"):
            commands.chosen([_pair("MuMu", "127.0.0.1:16480", 3)])

    def test_no_instance_at_all_is_an_error(self) -> None:
        with pytest.raises(RuntimeError, match="找不到任何模擬器"):
            commands.chosen([])

    def test_a_match_among_mumus_never_asks_ldplayer(self) -> None:
        """Every command starts here, and an emulator it is not driving must not slow it down."""
        mumu = MagicMock(label="MuMu")
        mumu.enumerate_instances.return_value = [
            MagicMock(index=3, coc_running=True, endpoint=AdbEndpoint(port=16480))
        ]
        mumu.serial_for.return_value = "127.0.0.1:16480"
        self.store.save(AppConfig(adb_serial="127.0.0.1:16480"))
        with (
            patch.object(commands, "MuMuAdapter", return_value=mumu),
            patch.object(commands, "LDPlayerAdapter") as ld,
        ):
            commands.chosen(commands.emulators())
        ld.assert_not_called()

    def test_an_emulator_missing_or_not_answering_is_left_out(self) -> None:
        broken = MagicMock(label="LDPlayer")
        broken.enumerate_instances.side_effect = commands.EmulatorError("ldconsole 逾時")
        with (
            patch.object(commands, "MuMuAdapter", side_effect=commands.EmulatorError("absent")),
            patch.object(commands, "LDPlayerAdapter", return_value=broken),
        ):
            assert list(commands.emulators()) == []

    def test_no_test_reaches_an_emulator_or_the_adb_server(self) -> None:
        """`conftest` makes both raise, so a test that forgets to fake one fails instead of driving the game."""
        with pytest.raises(RuntimeError, match="reached for the emulator"):
            next(commands.emulators())
        with pytest.raises(RuntimeError, match="reached for the emulator"):
            adbutils.adb.device("emulator-0").shell("true")


class SessionTests(unittest.TestCase):
    def test_a_session_is_the_controller_recording_into_the_run_and_its_display(self) -> None:
        adb = _adb()
        frames = _frames(self)
        with patch.object(commands, "_controller", return_value=adb) as controller:
            got, display = commands._session(frames)
        controller.assert_called_once_with(frames)
        assert got is adb
        assert display == DISPLAY
        adb.display_for.assert_called_once_with(COC_PACKAGE)

    def test_every_controller_a_command_gets_records_into_its_run(self) -> None:
        """The one place a capture taken anywhere in the code finds the run's frames."""
        emulator, instance = _pair("MuMu", "127.0.0.1:16384")
        emulator.controller.side_effect = lambda _serial: _controller()
        frames = _frames(self)
        with patch.object(commands, "chosen", return_value=(emulator, instance)):
            assert commands._controller(frames).frames is frames
            assert commands._attached(frames).frames is frames


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
            patch.object(commands, "_controller", return_value=adb) as driving,
            patch.object(commands, "_attached", return_value=adb) as looking,
            patch.object(commands, "current_world", return_value=seen),
            patch.object(commands, "cross", return_value=crossed) as sailed,
        ):
            report = commands.world(go, _frames(self))
        # Reading only looks, and crossing drives: see `_attached`.
        assert (driving.called, looking.called) == ((True, False) if go else (False, True))
        return adb, sailed, report

    def test_reading_is_one_capture_and_nothing_else(self) -> None:
        """No swipe, no tap and no pinch, which is what lets a session ask at any moment."""
        adb, sailed, report = self._world("day")
        assert (report.found, report.world, report.crossed) == ("day", "day", False)
        assert report.outcome == "here"
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
        assert commands.world_line(report) == "從日世界切到夜世界"
        sailed.assert_called_once()
        assert sailed.call_args.args[2] == "night"

    def test_a_crossing_that_did_not_land_says_where_the_game_stayed(self) -> None:
        _, _, report = self._world("day", go="night", crossed="day")
        assert (report.world, report.crossed) == ("day", False)
        assert report.outcome == "crossing_failed"
        _, _, lost = self._world("day", go="night", crossed=None)
        assert lost.world is None
        assert "不明的畫面" in commands.world_line(lost)

    def test_a_game_with_no_window_yet_confirms_neither_village(self) -> None:
        _, sailed, report = self._world("day", go="night", display=False)
        assert (report.found, report.world) == (None, None)
        assert report.outcome == "no_display"
        sailed.assert_not_called()

    def test_a_frame_with_no_village_on_it_is_kept(self) -> None:
        """Two `no_village` readings on 2026-09-26 left nothing to look at afterwards."""
        adb = _adb()
        adb.screenshot.return_value = b"frame"
        frames = _frames(self)
        keep = frames.directory / "no_village.png"
        with patch.object(commands, "_attached", return_value=adb):
            with patch.object(commands, "current_world", return_value="day"):
                commands.world(None, frames)
            assert not keep.exists()
            with patch.object(commands, "current_world", return_value=None):
                commands.world(None, frames)
        assert keep.read_bytes() == b"frame"


class StockCommandTests(unittest.TestCase):
    """The read-only status check, which is about the village on screen and does not cross."""

    def _stock(
        self,
        seen: str | None,
        held: VillageStock | None = None,
        ceilings: StorageCapacity | None = None,
        under: str | None = None,
        gems: int | None = None,
    ) -> tuple[MagicMock, MagicMock, object]:
        """A real controller, because `ScreenRunner` validates the one it is given.

        `under` is what `uncovered` finds once it has pressed a panel away, which
        is only reached when the first look found no village at all.
        """
        reads = [(seen, held, gems), (under, held, gems)]
        with (
            patch.object(commands, "_controller", return_value=_controller()),
            patch.object(commands, "_settle_game", return_value=DISPLAY) as self.settle_game,
            patch.object(ScreenRunner, "read_storages", side_effect=reads),
            patch.object(ScreenRunner, "read_ceilings", return_value=ceilings) as ceiling_reader,
            patch.object(commands, "uncovered", return_value=under) as pressed,
        ):
            report = commands.stock(_frames(self))
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

    def test_the_gems_off_the_same_frame_are_reported(self) -> None:
        _, _, report = self._stock("night", VillageStock(gold=1, elixir=1, dark=0), gems=5708)
        assert report.gems == 5708

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
        """Sailing is `world --day` / `--night`, and a status check that moves the game is not one."""
        with patch.object(commands, "cross") as sailed:
            self._stock("night", VillageStock(gold=1, elixir=1, dark=0), StorageCapacity(gold=2))
        sailed.assert_not_called()

    def test_it_takes_the_far_zoom_without_parking(self) -> None:
        """The bars and their tooltips are fixed HUD places, so the park's swipes buy nothing."""
        self._stock("day", VillageStock(gold=1, elixir=1, dark=1), StorageCapacity(gold=2))
        assert self.settle_game.call_args.kwargs == {"park": False}


class ShieldLineTests(unittest.TestCase):
    """The shield reading as a line for a person, built where the log is written."""

    def test_the_shield_plate_has_three_answers_and_they_are_not_interchangeable(self) -> None:
        """No plate, a plate saying 無, and a plate with a countdown on it.

        **The two Nones sit one level apart**: no `ShieldState` is a reading
        this frame could not take, and a `ShieldState` with no `remaining` is
        the plate read and saying there is no shield. A fourth line used to sit
        between them for a shield up with an unreadable countdown, which
        `shield_state` has never been able to produce.

        Nothing covered this before, which is why swapping the two arms of the
        `shield is None` answer failed no test — and those two are the ones a
        reader most needs kept apart, since one is structural and the other is
        work to redo.
        """
        assert commands._shield_line(None, "night") == "這個世界沒有護盾"
        assert "讀不到" in commands._shield_line(None, "day")
        assert commands._shield_line(ShieldState(), "day") == "**沒有護盾**,村莊現在可以被打"
        assert commands._shield_line(ShieldState(remaining=600), "day") == "護盾還有 10 分鐘"


class StatusCommandTests(unittest.TestCase):
    """The plates, the storages and the shield off the village on screen, the timers off the export.

    The export walk itself is patched out on every test here, so nothing
    reaches the account directory, the game's tables or the clipboard.
    """

    def _status(
        self,
        counts: list[tuple[PlateCount, PlateCount]],
        under: str | None = None,
        settled: DisplayTarget | None = DISPLAY,
        exported: ExportReport | None = None,
    ) -> tuple[MagicMock, MagicMock, MagicMock, StatusReport]:
        walk = MagicMock(return_value=exported or ExportReport(outcome="settings_shut"))
        with (
            # A real controller, because `ScreenRunner` validates the one it is given.
            patch.object(commands, "_controller", return_value=_controller()),
            patch.object(commands, "_settle_game", return_value=settled) as self.settle_game,
            patch.object(AdbController, "display_for", return_value=DISPLAY),
            patch.object(commands, "_exported", walk),
            patch.object(commands.PlateRunner, "counts", side_effect=counts),
            patch.object(commands.PlateRunner, "shield", return_value=None),
            patch.object(commands, "stock_of", return_value=commands.StockReport(world="night")),
            patch.object(commands, "uncovered", return_value=under) as pressed,
            patch.object(commands, "_planner") as planner,
        ):
            report = commands.status(_frames(self))
        return pressed, walk, planner, report

    def _counts(
        self, world: str | None, outcome: str = "counted"
    ) -> tuple[PlateCount, PlateCount]:
        free = {"free": 0, "total": 5} if outcome == "counted" else {}
        return (
            PlateCount(role="builder", world=world, outcome=outcome, **free),
            PlateCount(role="lab", world=world, outcome=outcome, **free),
        )

    def test_the_readings_come_back_on_one_report_and_no_model_is_asked(self) -> None:
        pressed, walk, planner, report = self._status([self._counts("night")])
        assert (report.world, report.builder.free, report.lab.total) == ("night", 0, 5)
        assert report.stock.world == "night"
        assert report.shield is None
        assert report.export == "settings_shut"
        pressed.assert_not_called()
        walk.assert_called_once()
        planner.assert_not_called()

    def test_a_panel_over_the_village_is_cleared_once_and_the_plates_read_again(self) -> None:
        """The first reading is the check, so a covered village costs a retry and no capture."""
        pressed, walk, _, report = self._status(
            [self._counts(None, "not_a_village"), self._counts("day")], under="day"
        )
        assert (report.world, report.builder.outcome) == ("day", "counted")
        pressed.assert_called_once()
        walk.assert_called_once()

    def test_it_takes_the_far_zoom_without_parking(self) -> None:
        """The plates, the shield, the walk and the bars are all fixed HUD places."""
        self._status([self._counts("day")])
        assert self.settle_game.call_args.kwargs == {"park": False}

    def test_no_village_is_never_walked_through_the_settings(self) -> None:
        """Every tap of the walk is a fixed place, and one on a battle can deploy a card."""
        _, walk, _, report = self._status([self._counts(None, "not_a_village")] * 2)
        walk.assert_not_called()
        assert report.export == "no_village"
        assert not report.timers
        # `stock_of` read the builder base even though neither plate did.
        assert report.world == "night"
        _, walk, _, report = self._status([self._counts("day")], settled=None)
        walk.assert_not_called()
        assert report.export == "no_village"

    def test_the_timers_are_worked_out_of_a_successful_export(self) -> None:
        document = VillageDocument.model_validate({
            "tag": "#TEST",
            "timestamp": 1791626101,
            "heroes": [{"data": 28000000, "lvl": 60, "timer": 28614}],
            "buildings2": [{"data": 1000039, "lvl": 9}],
            "helpers": [{"data": 93000000, "lvl": 8, "helper_cooldown": 79823}],
            "boosts": {"builder_boost": 4606, "clocktower_cooldown": 79754},
        })
        exported = ExportReport(outcome="exported", export=merge(document, _tables(), 1791626101))
        _, _, _, report = self._status([self._counts("day")], exported=exported)
        assert report.export == "exported"
        assert report.boosts.builder_boost == 4606
        assert [(timer.zh, timer.en, timer.seconds) for timer in report.timers] == [
            ("野蠻人之王", "Barbarian King", 2862)
        ]
        assert report.clock_tower is not None
        assert report.clock_tower.ready_in == 79754
        assert [(helper.zh, helper.en, helper.ready_in) for helper in report.helpers] == [
            ("建築工人學徒", "Builder's Apprentice", 79823)
        ]

    def test_tables_that_cannot_be_had_are_none_rather_than_a_failure(self) -> None:
        """The day's export is then saved unnamed rather than lost."""
        with (
            patch.object(commands, "game_data", side_effect=GameDataError("no tables")),
            self.assertLogs("ai_coc.commands", "WARNING"),
        ):
            assert commands._tables(_adb()) is None

    def test_the_last_saved_export_is_read_without_the_emulator(self) -> None:
        """What a report after a stop is written from, when a new run would log the player out."""
        document = VillageDocument.model_validate({
            "tag": "#TEST",
            "heroes": [{"data": 28000000, "lvl": 60, "timer": 600}],
        })
        saved = ExportReport(outcome="read_back", export=merge(document, _tables(), 1791626101))
        with (
            patch.object(commands, "_last_export", return_value=saved),
            patch.object(commands, "_controller") as opened,
        ):
            report = commands.status(_frames(self), last=True)
        opened.assert_not_called()
        assert (report.export, report.builder, report.stock) == ("read_back", None, None)
        assert [timer.zh for timer in report.timers] == ["野蠻人之王"]


# The moment every `status_text` case reads from, on this machine's clock like
# the report itself, so the wall-clock times below hold in any time zone.
NOW = datetime(2026, 11, 11, 12, 0).astimezone()


def _timer(
    data_id: int, at: str, level: int | None = 4, world: World = "day", **row: object
) -> UpgradeTimer:
    """A running upgrade finishing at `at` on this machine's clock, on 2026-11-11 unless dated."""
    when = datetime.fromisoformat(at if "T" in at else f"2026-11-11T{at}").astimezone()
    fields: dict[str, object] = {
        "world": world,
        "role": "builder",
        "section": "buildings",
        "data_id": data_id,
        "zh": f"建築{data_id}",
        "level": level,
        "max_lvl": 10,
        "limited_lvl": 10,
        "next": NextUpgrade(lvl=(level or 0) + 2, cost={"gold": 1}),
        "timer": 1,
        "seconds": int((when - NOW).total_seconds()),
        "done_at": when.isoformat(),
    }
    return UpgradeTimer.model_validate(fields | row)


class StatusTextTests(unittest.TestCase):
    """What `status` prints: the report's own tables, for an agent to hand on."""

    def _report(self, *timers: UpgradeTimer, **fields: object) -> StatusReport:
        held = VillageStock(gold=43_699_900, elixir=9_800_000, dark=6_000)
        defaults: dict[str, object] = {
            "world": "day",
            "builder": PlateCount(role="builder", world="day", free=0, total=7, outcome="counted"),
            "lab": PlateCount(role="lab", world="day", outcome="unread"),
            "stock": StockReport(world="day", held=held, filled={"gold": "150%", "elixir": "33%"}),
            "export": "exported",
            "exported_at": datetime(2026, 11, 11, 11, 58).astimezone().isoformat(),
            "timers": list(timers),
        }
        return StatusReport.model_validate(defaults | fields)

    def test_the_village_on_screen_gets_its_storages_and_counters_and_the_other_says_why_not(
        self,
    ) -> None:
        shown = commands.status_text(self._report(), NOW).splitlines()
        assert shown[0] == "村莊匯出: 今天 11:58"
        assert (
            "日世界: 金幣 4370 萬 (150%)、聖水 980 萬 (33%)、黑水 0.6 萬; 工人 0/7、實驗室 讀不到;"
            " 護盾讀不到" in shown
        )
        assert "夜世界: 遊戲停在日世界, 倉庫跟計數這次沒讀" in shown
        assert shown.count("沒有在升的") == 2

    def test_the_village_line_carries_the_gems_and_the_home_villages_shield(self) -> None:
        held = VillageStock(gold=10_000, elixir=0, dark=0)
        for shield, said in (
            (ShieldState(), "沒有護盾"),
            (ShieldState(remaining=5400), "護盾 1 小時 30 分鐘"),
        ):
            stock = StockReport(world="day", held=held, gems=5708)
            line = commands.status_text(self._report(stock=stock, shield=shield), NOW)
            assert "黑水 0 萬、寶石 5,708; 工人 0/7、實驗室 讀不到; " + said in line, said
        night = self._report(
            world="night", stock=StockReport(world="night", held=held, gems=5708), shield=None
        )
        shown = commands.status_text(night, NOW).splitlines()
        assert "夜世界: 金幣 1 萬、聖水 0 萬、寶石 5,708; 工人 0/7、實驗室 讀不到" in shown

    def test_each_row_is_the_slot_and_the_levels_with_no_verdict(self) -> None:
        """What follows a row is the agent's call, so the table prints only what it rests on."""
        text = commands.status_text(
            self._report(
                _timer(1, "13:00"),
                _timer(2, "14:00", level=9),
                _timer(3, "15:00", limited_lvl=5),
                _timer(4, "16:00", role="lab", extra=True),
                _timer(5, "17:00", max_lvl=None),
                _timer(6, "18:00", role=None),
                _timer(7, "19:00", level=None),
            ),
            NOW,
        )
        assert "| 時間 | 目標 | 項目 |\n| --- | --- | --- |" in text
        for row in (
            "| 今天 13:00 | 工人 | 建築1 (5/10) |",
            "| 今天 14:00 | 工人 | 建築2 (10/10) |",
            "| 今天 15:00 | 工人 | 建築3 (5/10, 上限 5) |",
            "| 今天 16:00 | 實驗室 (哥布林) | 建築4 (5/10) |",
            "| 今天 17:00 | 工人 | 建築5 (5/—) |",
            "| 今天 18:00 (沒算加速) | — | 建築6 (5/10) |",
            "| 今天 19:00 | 工人 | 建築7 |",
        ):
            assert row in text, row

    def test_alike_rows_finishing_within_a_wake_are_one_row(self) -> None:
        text = commands.status_text(
            self._report(
                _timer(1, "13:00"),
                _timer(1, "13:04"),
                _timer(1, "13:06"),
                _timer(1, "13:05", extra=True),
            ),
            NOW,
        )
        assert f"| 今天 13:00 | 工人 | 建築1 {commands.TIMES}2 (5/10) |" in text
        assert "| 今天 13:06 | 工人 | 建築1 (5/10) |" in text
        # A goblin's slot is a row of its own, since it is paid for apart.
        assert "| 今天 13:05 | 工人 (哥布林) | 建築1 (5/10) |" in text

    def test_a_helper_on_a_row_is_named_in_its_slot_until_its_work_there_ends(self) -> None:
        until = datetime(2026, 11, 11, 12, 36).astimezone().isoformat()
        report = self._report(
            _timer(1, "13:00", helper=93000000, helper_until=until),
            _timer(1, "13:02"),
            _timer(2, "14:00", helper_until=until),
            helpers=[
                HelperCooldown(data_id=93000000, zh="建築工人學徒", ready_in=0, ready_at=until)
            ],
        )
        text = commands.status_text(report, NOW)
        # Not one row with the job beside it that the helper is not on.
        assert "| 今天 13:00 | 工人 (建築工人學徒幫到今天 12:36) | 建築1 (5/10) |" in text
        assert "| 今天 13:02 | 工人 | 建築1 (5/10) |" in text
        # One an export saved without the helper's multiplier could not count.
        assert "| 今天 14:00 (沒算助手) | 工人 | 建築2 (5/10) |" in text

    def test_the_clock_tower_is_a_builder_base_row_in_its_turn(self) -> None:
        later = self._report(
            _timer(1, "2026-11-12T05:00", world="night"),
            _timer(2, "2026-11-14T01:00", world="night"),
            clock_tower=ClockTower(
                ready_in=86400, ready_at=datetime(2026, 11, 12, 12, 0).astimezone().isoformat()
            ),
        )
        night = commands.status_text(later, NOW).split("夜世界")[1].splitlines()
        assert night[-3:] == [
            "| 明天 05:00 | 工人 | 建築1 (5/10) |",
            "| 明天 12:00 | — | 鐘塔冷卻結束 |",
            "| 11-14 01:00 | 工人 | 建築2 (5/10) |",
        ]
        ready = self._report(clock_tower=ClockTower(ready_in=0, ready_at=NOW.isoformat()))
        assert "| 現在 | — | 鐘塔冷卻結束 |" in commands.status_text(ready, NOW)

    def test_each_helper_that_speeds_upgrades_says_when_it_can_be_sent(self) -> None:
        def helper(data_id: int, ready_in: int) -> HelperCooldown:
            ready_at = (NOW + timedelta(seconds=ready_in)).isoformat()
            return HelperCooldown(
                data_id=data_id, zh=f"助手{data_id}", ready_in=ready_in, ready_at=ready_at
            )

        report = self._report(
            helpers=[helper(93000000, 0), helper(93000001, 600), helper(93000002, 0)]
        )
        assert "助手: 助手93000000 現在能派、助手93000001 今天 12:10 能派" in (
            commands.status_text(report, NOW)
        )
        report = self._report(helpers=[helper(93000002, 0)])
        assert "助手" not in commands.status_text(report, NOW)

    def test_rows_past_the_window_fold_into_one_line(self) -> None:
        """How the report a stop hands over lists what nobody will continue."""
        report = self._report(
            _timer(1, "13:00"), _timer(2, "2026-11-13T09:00"), _timer(3, "2026-11-14T10:19")
        )
        text = commands.status_text(report, NOW, within=24)
        assert "| 今天 13:00 | 工人 | 建築1 (5/10) |" in text
        assert "之後還有 建築2 (5/10)、建築3 (5/10), 最晚 11-14 10:19" in text
        assert "| 11-13" not in text
        alone = commands.status_text(self._report(_timer(2, "2026-11-13T09:00")), NOW, within=24)
        day = alone.split("夜世界")[0]
        assert "| 時間" not in day
        assert "沒有在升的" not in day
        assert "之後還有 建築2 (5/10), 最晚 11-13 09:00" in day

    def test_a_reading_from_earlier_says_what_has_finished_since(self) -> None:
        """`--last` prints an export from a while ago, read against the clock now."""
        report = self._report(
            _timer(1, "11:00"),
            clock_tower=ClockTower(
                ready_in=600, ready_at=datetime(2026, 11, 11, 11, 30).astimezone().isoformat()
            ),
        )
        text = commands.status_text(report, NOW)
        assert "| 今天 11:00 (已過) | 工人 | 建築1 (5/10) |" in text
        assert "| 現在 | — | 鐘塔冷卻結束 |" in text

    def test_an_export_that_failed_says_so_and_prints_no_table(self) -> None:
        text = commands.status_text(self._report(export="settings_shut", exported_at=None), NOW)
        assert text.splitlines()[0] == f"村莊匯出沒讀到: {commands.EXPORT_LINES['settings_shut']}"
        assert "| 時間" not in text
        assert "沒有在升的" not in text

    def test_a_reading_off_the_saved_export_says_nothing_was_read_on_screen(self) -> None:
        report = self._report(
            _timer(1, "13:00"), world=None, builder=None, lab=None, stock=None, export="read_back"
        )
        shown = commands.status_text(report, NOW).splitlines()
        assert shown[0] == "村莊匯出: 今天 11:58 (存下的那份, 倉庫跟計數這次沒讀)"
        assert shown[2] == "日世界"
        assert "夜世界" in shown

    def test_no_village_on_screen_is_said_for_both(self) -> None:
        report = self._report(
            world=None, stock=StockReport(), export="no_village", exported_at=None
        )
        assert commands.status_text(report, NOW).count("畫面不是村莊, 倉庫跟計數沒讀到") == 2


class CollectCommandTests(unittest.TestCase):
    def _cart(self, report: CartReport) -> tuple[MagicMock, MagicMock, MagicMock, object]:
        adb = _adb()
        with (
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "current_world", return_value="night"),
            patch.object(commands, "collect_cart", return_value=report) as cart,
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            return adb, cart, runner, commands.collect(_frames(self))

    def test_the_village_is_read_once_it_has_painted_not_off_the_first_frame(self) -> None:
        """Every branch but the cart's is the home village's.

        Measured live: `collect` started 13 s after a builder base attack stood
        down read a frame that was no village yet, took the home branch, and
        sailed off the cart it had been run for. Only the world is waited for,
        since both branches park the camera on their own.
        """
        adb = _adb()
        with (
            patch.object(commands.time, "sleep") as slept,
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "_settle_game") as settled,
            patch.object(commands, "current_world", side_effect=[None, None, "night"]),
            patch.object(
                commands, "collect_cart", return_value=CartReport(outcome="empty")
            ) as cart,
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            commands.collect(_frames(self))
        cart.assert_called_once_with(adb, DISPLAY)
        runner.assert_not_called()
        assert slept.call_count == 2
        settled.assert_not_called()

    def test_a_village_slower_than_the_wait_still_gets_its_cart(self) -> None:
        """The home branch stops at the builder base rather than sailing, so the cart is taken."""
        adb = _adb()
        with (
            patch.object(commands.time, "sleep"),
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "current_world", return_value=None),
            patch.object(
                commands, "collect_cart", return_value=CartReport(outcome="collected", elixir=5)
            ) as cart,
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            runner.return_value.collect.return_value = CollectReport(outcome="on_night")
            report = commands.collect(_frames(self))
        cart.assert_called_once_with(adb, DISPLAY)
        assert (report.outcome, report.elixir) == ("cart", 5)

    def test_the_builder_base_empties_its_cart_instead_of_sweeping(self) -> None:
        adb, cart, runner, report = self._cart(CartReport(outcome="collected", elixir=300_000))
        # No collector was tapped, because that village has none: the cart comes
        # back whole rather than as a marker count standing in for one.
        assert (report.markers, report.elixir) == (0, 300_000)
        assert (report.outcome, report.cart.outcome) == ("cart", "collected")
        assert "300000" in commands.collect_line(report)
        cart.assert_called_once_with(adb, DISPLAY)
        runner.assert_not_called()

    def test_an_empty_cart_is_reported_as_nothing_rather_than_as_a_marker(self) -> None:
        *_, report = self._cart(CartReport(outcome="empty"))
        assert (report.markers, report.elixir) == (0, 0)
        assert "推車應該是空的" in commands.collect_line(report)

    def test_a_locked_cart_is_not_reported_as_an_empty_one(self) -> None:
        """The opposite instruction: the cart is standing full and cannot be opened.

        Measured live on a builder base whose storages were both exactly at
        capacity, with 135 843 elixir in the cart. Reported as empty, it reads
        as a village with nothing left to fetch.
        """
        *_, report = self._cart(CartReport(outcome="locked"))
        assert (report.markers, report.elixir) == (0, 0)
        assert "收集鈕是灰的" in commands.collect_line(report)

    def test_every_way_the_cart_can_end_has_a_line_of_its_own(self) -> None:
        """One sentence per outcome, and each one says the thing only it means.

        Counting distinct strings is not enough: two of them could be swapped
        and still be distinct, which is how `not_found` came to claim three
        candidate spots had been tapped on a path that tapped none.
        """
        # A fragment that belongs to that outcome and to no other.
        own = {
            "collected": "收到聖水",
            "empty": "儲量沒有變",
            "locked": "車上的數字讀不到",
            "locked_holding": "倉庫有空間再收",
            "locked_empty": "推車是空的",
            "not_found": "三個候選點",
            "not_parked": "鏡頭沒辦法停回定位",
            "not_night": "不在夜世界",
            "unreadable": "有一邊的儲量條讀不到",
        }
        assert set(own) == set(get_args(CartOutcome))
        for outcome, fragment in own.items():
            line = commands.CART_LINES[outcome].format(elixir=1, held=1, capacity=1)
            assert fragment in line, outcome
            others = [
                commands.CART_LINES[other].format(elixir=1, held=1, capacity=1)
                for other in own
                if other != outcome
            ]
            assert not any(fragment in line for line in others), outcome

    def test_the_home_village_hands_over_to_the_upkeep_runner(self) -> None:
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "current_world", return_value="day"),
            patch.object(commands, "UpkeepRunner") as runner,
        ):
            runner.return_value.collect.return_value = CollectReport(
                markers=3, outcome="collected"
            )
            report = commands.collect(_frames(self))
        assert report.markers == 3


class LoopCommandTests(unittest.TestCase):
    """Each loop command builds its runner from its options, and only from them."""

    def test_donate_passes_the_dry_run_and_the_round_limit(self) -> None:
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "ClanRunner") as runner,
        ):
            runner.return_value.donate.return_value = DonateReport(outcome="donated")
            report = commands.donate(DonateOptions(dry_run=True, rounds=2), _frames(self))
        kwargs = runner.call_args.kwargs
        assert (kwargs["dry_run"], kwargs["rounds"]) == (True, 2)
        assert report.outcome == "donated"


class UpkeepLineTests(unittest.TestCase):
    """One log line per outcome for the seven reports that gained one.

    Counting distinct strings lets two of them swap and still pass, so each
    fragment has to belong to its own outcome and to no other. A
    table missing a value is worse again — a `KeyError` on the one path nobody
    exercised, raised from inside the command that was reporting a failure.
    """

    def _each(self, table: dict[str, str], own: dict[str, str], outcomes: tuple[str, ...]) -> None:
        assert set(own) == set(outcomes)
        for outcome, fragment in own.items():
            assert fragment in table[outcome], outcome
            others = [table[other] for other in own if other != outcome]
            assert not any(fragment in other for other in others), outcome

    def test_every_turnaround_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.TURNAROUND_LINES,
            {"counted": "配對到", "none": "沒有能配對"},
            get_args(TurnaroundOutcome),
        )

    def test_every_growth_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.GROWTH_LINES,
            {"compared": "種有升級", "too_few": "不到兩份"},
            get_args(GrowthOutcome),
        )

    def test_every_note_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.NOTE_LINES,
            {
                "listed": "列出交代",
                "added": "記下這一條",
                "done": "刪掉做完的",
                "incomplete": "要給世界跟",
                "no_such_note": "沒有這一條",
            },
            get_args(NoteOutcome),
        )

    def test_every_collect_outcome_has_a_line_of_its_own(self) -> None:
        """Minus `cart`, which is the builder base's trip and answers through `CartReport`."""
        self._each(
            commands.COLLECT_LINES,
            {
                "collected": "收了",
                "nothing_to_collect": "沒有採集器等著收",
                "no_village": "收集沒有開始",
                "on_night": "日世界的採集器沒有收",
                "stock_unread": "讀不到儲量",
            },
            tuple(one for one in get_args(CollectOutcome) if one != "cart"),
        )

    def test_every_plate_count_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.COUNT_LINES,
            {"counted": "{free}/{total}", "unread": "數字讀不到", "not_a_village": "不在村莊"},
            get_args(PlateCountOutcome),
        )

    def test_every_donate_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.DONATE_LINES,
            {
                "donated": "捐了",
                "dry_run": "試跑",
                "nothing_given": "但沒有捐出去",
                "nobody_asking": "沒有人在請求增援",
                "panel_shut": "捐贈畫面沒有打開",
                "no_village": "捐兵沒有開始",
                "on_night": "捐兵只在日世界",
            },
            get_args(DonateOutcome),
        )

    def test_every_world_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.WORLD_LINES,
            {
                "here": "目前在",
                "crossed": "切到",
                "crossing_failed": "畫面還停在",
                "no_village": "畫面不是村莊",
                "no_display": "遊戲還沒有畫面",
            },
            get_args(WorldOutcome),
        )

    def test_every_press_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.PRESS_LINES,
            {
                "pressed": "已經按下去",
                "sheet_up": "升級確認視窗開著",
                "dialog_up": "對話框開著",
                "on_hud": "遊戲自己的按鈕",
                "menu_gems": "建築選單那一排",
                "green_button": "綠色的按鈕",
                "in_battle": "戰鬥中",
            },
            get_args(PressOutcome),
        )

    def test_every_confirm_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.CONFIRM_LINES,
            {
                "confirmed": "視窗關了",
                "no_sheet": "沒有升級確認視窗",
                "not_a_resource": "寶石或道具",
                "price_mismatch": "價格跟給的不一樣",
                "cannot_afford": "價格是紅字",
                "refused_by_game": "已經按取消",
                "sheet_stayed": "視窗還開著",
                "unexpected": "認不得的畫面",
                "no_wall_menu": "沒有開著的城牆選單",
                "sheet_disagrees": "跟按鈕對不上",
                "no_confirmation": "沒有跳出確認視窗或對話框",
                "no_goblin": "沒有聘請哥布林工人的視窗",
                "goblin_offered": "跳出聘請哥布林工人的視窗",
            },
            get_args(ConfirmOutcome),
        )

    def test_every_boost_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.BOOST_LINES,
            {
                "boosted": "鐘塔開始加速",
                "not_night": "遊戲不在夜世界",
                "not_ready": "沒有鐘塔能開的時鐘標記",
                "no_free_boost": "找不到沒標價的免費加速鈕",
                "not_taken": "加速沒有生效",
                "unexpected": "不是它的確認視窗",
            },
            get_args(BoostOutcome),
        )

    def test_every_view_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.VIEW_LINES,
            {
                "parked": "並把鏡頭停回定位",
                "not_a_village": "畫面不是村莊",
                "zoomed_in": "拉近的鏡頭停不住",
                "never_settled": "鏡頭一直沒有停下來",
                "selected": "還選著",
            },
            get_args(ViewOutcome),
        )

    def test_every_export_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.EXPORT_LINES,
            {
                "exported": "剛從遊戲裡匯出",
                "read_back": "讀自上一次匯出",
                "never_exported": "還沒有匯出過",
                "wrong_format": "存檔讀不成村莊匯出",
                "no_village": "沒有回到村莊畫面",
                "settings_shut": "設定頁沒有打開",
                "more_settings_shut": "那一頁沒有打開",
                "row_not_found": "捲到底了",
                "nothing_copied": "剪貼簿沒有東西",
                "not_an_export": "不是村莊資料",
            },
            get_args(ExportOutcome),
        )

    def test_every_search_outcome_has_a_line_of_its_own(self) -> None:
        self._each(
            commands.SEARCH_LINES,
            {
                "found": "找到",
                "no_match": "沒有符合的",
                "no_tables": "ai_coc gamedata",
                "no_export": "還沒有存下村莊匯出",
            },
            get_args(SearchOutcome),
        )

    def test_a_launch_says_which_of_the_four_things_it_did(self) -> None:
        """`none` reads off `was_running` instead, which is all that scope can report."""

        def line(
            restart: RestartScope, was_running: bool = True, outcome: LaunchOutcome = "at_village"
        ) -> str:
            return commands.launch_line(
                LaunchReport(
                    index=0,
                    serial="127.0.0.1:16384",
                    was_running=was_running,
                    restart=restart,
                    outcome=outcome,
                )
            )

        assert "已經在跑" in line("none")
        assert "開起來" in line("none", was_running=False)
        assert line("game").startswith("已重開部落衝突")
        assert line("emulator").startswith("已重開模擬器與部落衝突")
        # Orthogonal to all four, the way the attack's counts are to its outcome.
        assert set(commands.LAUNCH_OUTCOME_LINES) == set(get_args(LaunchOutcome))
        assert "村莊沒有出現" in line("none", outcome="no_village")
        assert "收到停止要求" in line("none", outcome="stopped")
        assert "村莊沒有出現" not in line("none", outcome="stopped")


class AnswerLineTests(unittest.TestCase):
    """Each named outcome's line says something no other line in its table does."""

    def test_every_table_covers_its_outcomes_with_lines_of_their_own(self) -> None:
        for table, kind in (
            (commands.ENDING_LINES, SeriesEnding),
            (commands.STOP_LINES, EmulatorStop),
            (commands.LOAN_LINES, LoanStop),
            (commands.HOLD_LINES, HoldStop),
            (commands.WATCH_LINES, WatchStop),
            (commands.GIVEBACK_LINES, GivebackOutcome),
            (commands.STAND_DOWN_LINES, StandDownOutcome),
        ):
            assert set(table) == set(get_args(kind)), kind
            for outcome, line in table.items():
                assert not any(line in other for key, other in table.items() if key != outcome)
        assert set(commands.STAND_DOWNS.values()) == set(get_args(StandDownOutcome))

    def test_a_stop_line_names_each_section_it_reached(self) -> None:
        line = commands.stop_line(
            StopReport(
                target="all",
                emulator="asked",
                command="attack",
                pid=42,
                loan="kept",
                lender=Caller(agent="codex"),
                hold="cancelled",
                holder=Caller(agent="antigravity"),
                watch="nothing_watching",
            )
        )
        assert line.split(";") == [
            commands.STOP_LINES["asked"].format(command="attack", pid=42),
            commands.LOAN_LINES["kept"].format(lender="codex"),
            commands.HOLD_LINES["cancelled"].format(holder="antigravity"),
            commands.WATCH_LINES["nothing_watching"],
        ]
        watch_only = commands.stop_line(StopReport(target="watch", watch="nothing_watching"))
        assert watch_only == commands.WATCH_LINES["nothing_watching"]


class AttackSeriesEndingTests(unittest.TestCase):
    """A series answers how it ended, which its last round cannot say."""

    FOUGHT = LootOffer(gold=1, elixir=1, dark=1)

    def _ending(
        self,
        played: list[object] | Callable[[], AttackReport],
        rounds: int = 0,
        should_stop: Callable[[], bool] | None = None,
        rest: SeriesEnding | None = None,
    ) -> AttackRun:
        runner = MagicMock(name="AttackRunner")
        runner.configure_mock(played=None, holding=None, capacity=None, cart=None, since_cart=None)
        runner.run.side_effect = played
        runner.lost = 0
        runner.world = "day"
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner),
            patch.object(commands, "_rest", return_value=rest),
        ):
            return commands.attack(
                AttackOptions(rounds=rounds),
                _frames(self),
                should_stop or MagicMock(return_value=False),
            )

    def _stopped_after_one(self, rounds: int) -> AttackRun:
        """A stop that lands during the first round, which itself ends as an ordinary battle."""
        ran: list[AttackReport] = []

        def fight() -> AttackReport:
            ran.append(AttackReport(outcome="took_loot", attacked=self.FOUGHT))
            return ran[-1]

        return self._ending(fight, rounds=rounds, should_stop=lambda: bool(ran))

    def test_the_rounds_asked_for_played_out(self) -> None:
        fought = [AttackReport(outcome="took_loot", attacked=self.FOUGHT)] * 2
        run = self._ending(fought, rounds=2)
        assert (run.ending, len(run.rounds.root)) == ("repeat_done", 2)

    def test_a_stop_before_a_round_between_rounds_or_during_the_last(self) -> None:
        """Each ends on an ordinary round or none, whose own outcome says nothing of the stop."""
        before = self._ending([], should_stop=MagicMock(return_value=True))
        assert (before.ending, before.rounds.root) == ("stopped", [])
        for rounds in (0, 1):
            run = self._stopped_after_one(rounds)
            assert (run.ending, len(run.rounds.root)) == ("stopped", 1), rounds
            assert run.rounds.root[-1].outcome == "took_loot"

    def test_a_ctrl_c_in_a_round_or_in_the_rest(self) -> None:
        assert self._ending([KeyboardInterrupt()]).ending == "interrupted"
        run = self._ending([AttackReport(outcome="no_opponent")], rest="interrupted")
        assert (run.ending, run.rounds.root[-1].outcome) == ("interrupted", "no_opponent")

    def test_rounds_failing_back_to_back(self) -> None:
        silent = self._ending([AdbControlError("adb read timeout")] * commands.ADAPTER_FAILURES)
        assert (silent.ending, len(silent.rounds.root)) == (
            "emulator_silent",
            commands.ADAPTER_FAILURES,
        )
        stuck = self._ending([AttackReport(outcome="no_attack_menu")] * commands.MENU_FAILURES)
        assert (stuck.ending, len(stuck.rounds.root)) == ("no_attack_menu", commands.MENU_FAILURES)

    def test_a_round_that_ends_the_series_names_it(self) -> None:
        for outcome, ending in commands.ROUND_ENDINGS.items():
            run = self._ending([AttackReport(outcome=outcome)])
            assert (run.ending, run.rounds.root[-1].outcome) == (ending, outcome)


class WaitTests(unittest.TestCase):
    """Where one run stands, off the journal (conftest's tmp copy) and its process."""

    RUN = "2026-11-12-100000-attack-d-a1"

    def setUp(self) -> None:
        self.lines: list[dict[str, object]] = []
        self.logs = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _start(self, run: str = RUN, command: str = "attack", pid: int = 424242) -> Path:
        log = self.logs / run
        log.mkdir()
        (log / "run.log").write_text("line\n", encoding="utf-8")
        self.lines.append({
            "run": run,
            "at": "2026-11-12T10:00:00+08:00",
            "event": "start",
            "command": command,
            "log": str(log),
            "pid": pid,
            "pid_created": 100.0,
        })
        return log

    def _line(self, event: str, run: str = RUN, **fields: object) -> None:
        self.lines.append({
            "run": run,
            "at": "2026-11-12T10:01:00+08:00",
            "event": event,
            **fields,
        })

    def _wait(self, run: str, *, alive: bool = True, seconds: float = 0.0) -> WaitReport:
        logging_setup.JOURNAL_PATH.write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in self.lines),
            encoding="utf-8",
        )
        with patch.object(commands, "_alive", return_value=alive):
            return commands.wait(Path(run), seconds)

    def test_a_finished_run_answers_its_exit_and_outcome(self) -> None:
        log = self._start()
        self._line("end", exit=0, outcome="stock_full")
        report = self._wait(str(log))
        assert (report.outcome, report.exit, report.run_outcome, report.command) == (
            "finished",
            0,
            "stock_full",
            "attack",
        )

    def test_a_live_run_drives_or_queues_and_says_behind_whom(self) -> None:
        self._start()
        self._line("lock", what="queued")
        holder = RunnerState(status="running", pid=1, log=Path("theirs"))
        with patch.object(commands, "holder", return_value=holder):
            queued = self._wait("d-a1")
        assert (queued.outcome, queued.holder) == ("queued", Path("theirs"))
        with patch.object(commands, "holder", return_value=None):
            behind_a_loan = self._wait("d-a1")
        assert (behind_a_loan.outcome, behind_a_loan.holder) == ("queued", None)
        self._line("lock", what="claimed")
        running = self._wait("d-a1")
        assert (running.outcome, running.pid) == ("running", 424242)
        assert running.quiet is not None

    def test_a_dead_run_is_gone_unless_it_answered(self) -> None:
        """Its `end` line is the one write that can be dropped."""
        log = self._start()
        assert self._wait("d-a1", alive=False).outcome == "gone"
        (log / "result.json").write_text("{}", encoding="utf-8")
        assert self._wait("d-a1", alive=False).outcome == "finished"

    def test_a_label_takes_the_newest_run_carrying_it_and_never_its_own(self) -> None:
        older = "2026-11-12-090000-attack-d-a1"
        self._start(older)
        self._line("end", run=older, exit=0, outcome="repeat_done")
        newer = self._start()
        self._start("2026-11-12-100100-wait-d-a1", command="wait", pid=os.getpid())
        assert self._wait("d-a1").log == newer
        assert self._wait("a1-x").outcome == "no_such_run"

    def test_a_directory_named_past_a_labels_forty_characters_is_found_by_its_name(self) -> None:
        """Only a label is cut down on its way into a directory name."""
        long = "2026-11-12-100000-attack-day3-c2-attack-1"
        log = self._start(long)
        assert self._wait(str(log)).log == log
        assert self._wait(long).log == log
        assert self._wait("day3-c2-attack-1").log == log

    def test_a_wait_returns_once_the_run_finishes(self) -> None:
        self._start()
        ended = {"run": self.RUN, "at": "2026-11-12T10:02:00+08:00", "event": "end", "exit": 0}

        def finish(_seconds: float) -> None:
            with logging_setup.JOURNAL_PATH.open("a", encoding="utf-8") as journal:
                journal.write(json.dumps(ended) + "\n")

        with patch.object(commands.time, "sleep", side_effect=finish) as slept:
            report = self._wait("d-a1", seconds=600)
        assert report.outcome == "finished"
        slept.assert_called_once()


class RoundLineTests(unittest.TestCase):
    """One log line per attack outcome, plus the counts that are orthogonal to it.

    `run.log` is where a farming session looks first, and the two outcomes that
    used to read alike — an army that never left its cards against one that went
    down and came home empty — sent readers to opposite halves of the code.
    """

    def test_every_way_a_round_can_end_has_a_line_of_its_own(self) -> None:
        own = {
            "took_loot": "已進攻並回營",
            "deployed": "沒有戰利品面板可以判斷",
            "loot_unread": "讀不到戰利品面板",
            "no_loot": "部隊有出去",
            "nothing_deployed": "沒有任何一張卡片",
            "army_short": "兵力不足",
            "stock_full": "倉庫都滿過",
            "builder_free": "有工人閒著",
            "lab_free": "實驗室有空檔",
            "no_opponent": "等不到對手",
            "all_skipped": "都未達門檻",
            "stopped": "收到停止要求",
            "no_attack_menu": "沒有開啟攻擊選單",
            "server_loading": "卡在載入畫面",
            "server_flapping": "又回到載入畫面",
            "emulator_silent": "模擬器沒有回應",
            "other_village": "遊戲停在另一個村莊",
            "session_taken": "另一部裝置登入",
        }
        assert set(own) == set(get_args(AttackOutcome))
        for outcome, fragment in own.items():
            line = commands.ROUND_LINES[outcome]
            assert fragment in line, outcome
            others = [commands.ROUND_LINES[other] for other in own if other != outcome]
            assert not any(fragment in other for other in others), outcome

    def test_the_counts_are_appended_rather_than_written_into_each_sentence(self) -> None:
        """Because they are orthogonal: a forced battle can end any of four ways."""
        plain = commands.round_line(AttackReport(outcome="took_loot"))
        assert plain == commands.ROUND_LINES["took_loot"]
        forced = commands.round_line(AttackReport(outcome="took_loot", forced=True))
        assert forced.startswith("倒數結束被強制開戰")
        assert commands.ROUND_LINES["took_loot"] in forced
        skipped = commands.round_line(AttackReport(outcome="all_skipped", skipped=7))
        assert "跳過 7 個對手" in skipped
        staged = commands.round_line(AttackReport(outcome="took_loot", world="night", phases=2))
        assert "共出兵 2 次" in staged
        # One phase is the ordinary battle, so it is not worth a clause.
        assert "共出兵" not in commands.round_line(
            AttackReport(outcome="took_loot", world="night", phases=1)
        )


class AttackSeriesStopTests(unittest.TestCase):
    """The series stops on the condition it is handed, whose default is `ai_coc stop`."""

    def _series(self, should_stop: MagicMock) -> tuple[MagicMock, list]:
        runner = MagicMock(name="AttackRunner")
        runner.configure_mock(played=None, holding=None, capacity=None, cart=None)
        runner.run.return_value = AttackReport(outcome="no_opponent")
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner) as built,
        ):
            series = commands.attack(AttackOptions(rounds=1), _frames(self), should_stop)
        return built, series.rounds.root

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

    def test_a_plan_by_name_asks_no_model_and_leaves_the_village_to_the_round(self) -> None:
        runner = MagicMock(name="AttackRunner")
        runner.configure_mock(played=None, holding=None, capacity=None, cart=None)
        runner.run.return_value = AttackReport(outcome="no_opponent")
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner") as planner,
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner) as built,
        ):
            commands.attack(AttackOptions(plan_name="flat"), _frames(self), lambda: False)
        planner.assert_not_called()
        made = built.call_args.kwargs
        assert (made["ai"], made["plan"], made["plan_name"], made["world"]) == (
            None,
            None,
            "flat",
            None,
        )


class AttackSeriesAdapterFailureTests(unittest.TestCase):
    """An emulator error ends the series rather than the process.

    Nothing above `main()` catches an `AdbControlError`, so one raised mid-round
    used to kill the run outright: `result.json` was never written and the rounds
    already played were countable only out of `run.log`. Seven runs died that way
    in three days, one of them on a round it had just won outright.
    """

    def _series(self, played: list, rounds: int = 0) -> list:
        runner = MagicMock(name="AttackRunner")
        runner.configure_mock(played=None, holding=None, capacity=None, cart=None)
        runner.run.side_effect = played
        # The count lives on the runner, because one runner plays one series.
        runner.lost = 0
        runner.world = "day"
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner),
            patch.object(commands, "_rest", return_value=None),
        ):
            series = commands.attack(
                AttackOptions(rounds=rounds), _frames(self), MagicMock(return_value=False)
            )
        return series.rounds.root

    def test_a_round_lost_to_the_emulator_is_recorded_and_the_series_carries_on(self) -> None:
        """One timeout is a blip, and an overnight run should survive it."""
        rounds = self._series([
            AdbControlError("ADB 指令失敗：adb read timeout"),
            AttackReport(outcome="stock_full"),
        ])
        assert len(rounds) == 2
        assert rounds[0].outcome == "emulator_silent"
        assert rounds[-1].stock_full

    def test_three_in_a_row_ends_the_series_holding_what_it_played(self) -> None:
        """An emulator that answers nothing is not worth aiming more rounds at.

        The rounds already played still come back, which is the whole point:
        before this they went down with the process.
        """
        rounds = self._series([
            AttackReport(outcome="took_loot"),
            *[AdbControlError("adb read timeout")] * 4,
        ])
        assert len(rounds) == 4
        assert rounds[0].outcome == "took_loot"
        assert all(report.outcome == "emulator_silent" for report in rounds[1:])

    def test_a_round_that_came_back_clears_the_count(self) -> None:
        """Consecutive, not total: three blips spread over a run are not a dead emulator.

        Three of them, because two do not separate the two readings — the count
        reaches 2 either way and never trips. Counting them as a total instead
        ends this series on the third error, at five rounds.
        """
        rounds = self._series([
            AdbControlError("adb read timeout"),
            AttackReport(outcome="took_loot"),
            AdbControlError("adb read timeout"),
            AttackReport(outcome="took_loot"),
            AdbControlError("adb read timeout"),
            AttackReport(outcome="stock_full"),
        ])
        assert len(rounds) == 6
        assert rounds[-1].stock_full


def _played(
    world: World, *rounds: tuple[str, tuple[int, int, int] | None], cart: CartReport | None = None
) -> AttackSeries:
    """A series of (outcome, storages before the round) on one village, each round seeing `cart`."""
    capacity = StorageCapacity(
        gold=10_000_000, elixir=10_000_000, dark=200_000 if world == "day" else None
    )
    fought = ("took_loot", "deployed", "no_loot", "loot_unread", "nothing_deployed")
    return AttackSeries([
        AttackReport(
            world=world,
            outcome=outcome,
            attacked=LootOffer(gold=1, elixir=1, dark=1)
            if outcome in fought and world == "day"
            else None,
            phases=1 if outcome in fought and world == "night" else 0,
            holding=None
            if held is None
            else VillageStock(gold=held[0], elixir=held[1], dark=held[2]),
            capacity=capacity,
            cart=cart,
        )
        for outcome, held in rounds
    ])


class AttackSummaryTests(unittest.TestCase):
    """What a series brought home, said by the series itself rather than pieced together out of `run.log`."""

    def test_each_round_carries_what_the_runner_read_around_it(self) -> None:
        runner = MagicMock(name="AttackRunner")
        held = VillageStock(gold=1, elixir=2, dark=3)
        cart = CartReport(outcome="locked_holding", held=5, capacity=10)
        runner.configure_mock(
            played=None,
            holding=held,
            capacity=StorageCapacity(gold=9),
            cart=cart,
            since_cart=2,
            lost=0,
        )
        runner.run.side_effect = [AttackReport(outcome="took_loot"), AdbControlError("timeout")]
        runner.world = "day"
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner),
            patch.object(commands, "_rest", return_value=None),
        ):
            series = commands.attack(
                AttackOptions(rounds=2), _frames(self), MagicMock(return_value=False)
            )
        for report in series.rounds.root:
            assert (report.holding, report.capacity, report.cart) == (
                held,
                StorageCapacity(gold=9),
                cart,
            )

    def test_a_round_another_device_cut_short_carries_them_too(self) -> None:
        runner = MagicMock(name="AttackRunner")
        held = VillageStock(gold=1, elixir=2, dark=3)
        runner.configure_mock(played=None, holding=held, capacity=None, cart=None, since_cart=None)
        runner.run.side_effect = [commands.SessionTakenError("taken")]
        runner.world = "day"
        runner.lost = 0
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner),
        ):
            series = commands.attack(
                AttackOptions(rounds=0), _frames(self), MagicMock(return_value=False)
            )
        [report] = series.rounds.root
        assert (report.outcome, report.holding) == ("session_taken", held)

    def test_the_loot_is_the_last_reading_less_the_first(self) -> None:
        village = commands.farmed(
            _played(
                "day",
                ("took_loot", (1_000_000, 1_000_000, 10_000)),
                ("took_loot", (2_000_000, 1_500_000, 13_000)),
                ("stock_full", (3_000_000, 2_000_000, 16_000)),
            ),
            "stock_full",
        )
        assert village is not None
        assert (village.battles, village.uncounted, village.ending) == (2, 0, "stock_full")
        assert village.gained == VillageStock(gold=2_000_000, elixir=1_000_000, dark=6_000)
        assert village.held == VillageStock(gold=3_000_000, elixir=2_000_000, dark=16_000)

    def test_battles_no_two_readings_bracket_are_counted_apart(self) -> None:
        village = commands.farmed(
            _played("day", ("took_loot", None), ("took_loot", (1, 1, 1)), ("took_loot", None)),
            "repeat_done",
        )
        assert village is not None
        assert (village.battles, village.uncounted) == (3, 3)
        assert village.gained == VillageStock(gold=0, elixir=0, dark=0)
        village = commands.farmed(
            _played("day", ("took_loot", None), ("no_attack_menu", None)), "no_attack_menu"
        )
        assert village is not None
        assert (village.battles, village.uncounted, village.gained) == (1, 1, None)
        assert village.ending == "no_attack_menu"

    def test_the_cart_is_read_from_its_first_trip_to_its_last(self) -> None:
        emptied, banking = (
            CartReport(outcome="collected", elixir=400),
            CartReport(outcome="locked_holding", held=1_550_000, capacity=1_600_000),
        )
        series = _played("night", ("deployed", (1, 1, 0)), cart=emptied)
        series.root += _played("night", ("deployed", (2, 2, 0)), cart=banking).root
        series.root[-1].since_cart = 3
        village = commands.farmed(series, "stock_full")
        assert village is not None
        assert (village.cart_gained, village.cart_now, village.cart_capacity) == (
            1_550_000,
            1_550_000,
            1_600_000,
        )
        assert village.cart_unread == 3
        lost = commands.farmed(
            _played("night", ("deployed", (1, 1, 0)), cart=CartReport(outcome="not_found")),
            "repeat_done",
        )
        assert lost is not None
        assert (lost.cart_gained, lost.cart_now) == (None, None)

    def test_a_round_on_neither_village_counts_towards_none(self) -> None:
        series = AttackSeries([
            AttackReport(world=None, outcome="no_attack_menu"),
            *_played("night", ("deployed", (1, 1, 0))).root,
        ])
        village = commands.farmed(series, "repeat_done")
        assert village is not None
        assert (village.world, village.battles) == ("night", 1)
        nowhere = AttackSeries([AttackReport(world=None, outcome="stopped")])
        assert commands.farmed(nowhere, "stopped") is None

    def test_the_text_is_the_farming_reports_table(self) -> None:
        day = _played(
            "day",
            ("took_loot", (1_000_000, 2_000_000, 6_000)),
            ("stock_full", (10_000_000, 2_760_000, 12_000)),
        )
        village = commands.farmed(day, "stock_full")
        assert village is not None
        assert commands.farmed_text(village).splitlines() == [
            f"日世界: 1 場, {commands.ENDING_LINES['stock_full']}",
            "",
            "| 資源 | 進帳 | 現在 |",
            "| --- | --- | --- |",
            "| 金幣 | +900 萬 | 1000 萬 (100%) |",
            "| 聖水 | +76 萬 | 276 萬 (27%) |",
            "| 黑水 | +0.6 萬 | 1 萬 (6%) |",
        ]
        night = _played(
            "night",
            ("deployed", (100_000, 100_000, 0)),
            ("deployed", (5_780_000, 5_070_000, 0)),
            ("deployed", None),
            cart=CartReport(outcome="locked_holding", held=1_600_000, capacity=1_600_000),
        )
        night.root[-1].since_cart = 2
        village = commands.farmed(night, "stopped")
        assert village is not None
        text = commands.farmed_text(village)
        for row in (
            f"夜世界: 3 場, {commands.ENDING_LINES['stopped']}",
            "| 金幣 | +568 萬 | 578 萬 (57%) |",
            "| 聖水車 | +0 萬 | 160 萬 / 160 萬 |",
            "有 2 場打完之後沒讀到倉庫, 沒算進進帳",
            "有 2 場的聖水在最後一次去聖水車之後才進車, 沒算進進帳",
        ):
            assert row in text, row
        assert "| 黑水" not in text
        # A run still going, or one written before series named their endings.
        unknown = village.model_copy(update={"ending": None})
        assert commands.farmed_text(unknown).splitlines()[0] == "夜世界: 3 場"

    def test_an_attack_that_read_no_village_still_says_how_it_ended(self) -> None:
        """Stopped before its first round, it used to print only that nothing was fought."""
        text = commands.attack_text(AttackRun(ending="stopped"))
        assert text == f"一輪都沒有讀到是哪個村莊, 一場也沒打; {commands.ENDING_LINES['stopped']}"


class FarmedRunsTests(unittest.TestCase):
    """A session's farming summed over its attack runs, off the journal alone (conftest's tmp copy)."""

    def setUp(self) -> None:
        self.lines: list[dict[str, object]] = []

    def _run(  # noqa: PLR0913, PLR0917 - one line of the journal's fields
        self,
        run: str,
        at: str,
        rounds: list[tuple[str, tuple[int, int, int] | None]],
        world: World = "day",
        session: str = "s1-farm",
        ended: bool = True,
        cart: dict[str, object] | None = None,
        old: bool = False,
        ending: str | None = None,
    ) -> None:
        """One attack run's start, its rounds a minute apart, and its end; `old` writes the storages beside the report only."""
        start = datetime.fromisoformat(f"2026-11-11T{at}:00+08:00")
        self.lines.append({
            "run": run,
            "at": start.isoformat(),
            "event": "start",
            "command": "attack",
            "caller": {"agent": "a", "session": session, "mission": "m"},
        })
        for number, report in enumerate(_played(world, *rounds).root, 1):
            dumped = report.model_dump(mode="json")
            beside = ("holding", "capacity", "cart")
            self.lines.append({
                "run": run,
                "at": (start + timedelta(minutes=number)).isoformat(),
                "event": "round",
                "round": number,
                "report": {k: v for k, v in dumped.items() if k not in beside} if old else dumped,
                "holding": dumped["holding"],
                "capacity": dumped["capacity"],
                "cart": cart,
            })
        if ended:
            self.lines.append({
                "run": run,
                "at": (start + timedelta(minutes=len(rounds) + 1)).isoformat(),
                "event": "end",
                "outcome": ending,
                # A run from before series named their endings kept its last round alone.
                "result": None if ending is None else {"ending": ending, "rounds": []},
            })

    def _farmed(self, **asked: object) -> FarmedReport:
        logging_setup.JOURNAL_PATH.write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in self.lines),
            encoding="utf-8",
        )
        return commands.farmed_runs(**asked)

    def test_every_run_of_the_session_and_its_farmer_is_summed_per_run(self) -> None:
        self._run(
            "r1",
            "10:00",
            [("took_loot", (1_000_000, 1_000_000, 0)), ("took_loot", (2_000_000, 1_500_000, 0))],
        )
        # Lent for an upgrade that spent eight million between the two runs.
        self._run(
            "r2",
            "10:10",
            [("took_loot", (500_000, 1_500_000, 0)), ("stock_full", (1_500_000, 2_000_000, 0))],
        )
        self._run("other", "10:20", [("took_loot", (1, 1, 1))], session="someone-else")
        report = self._farmed(by="s1")
        [day] = report.villages
        assert (day.battles, day.uncounted, day.ending) == (3, 1, "stock_full")
        assert day.gained == VillageStock(gold=2_000_000, elixir=1_000_000, dark=0)
        assert day.held == VillageStock(gold=1_500_000, elixir=2_000_000, dark=0)
        assert report.started == datetime.fromisoformat("2026-11-11T10:00:00+08:00")
        assert report.ended == datetime.fromisoformat("2026-11-11T10:13:00+08:00")
        assert not report.running
        later = self._farmed(by="s1", since=datetime.fromisoformat("2026-11-11T10:05:00+08:00"))
        assert later.villages[0].battles == 1

    def test_a_round_line_written_before_the_reports_carried_storages_still_counts(self) -> None:
        self._run("r1", "10:00", [("took_loot", (1, 1, 1)), ("stock_full", (3, 3, 3))], old=True)
        [day] = self._farmed(by="s1").villages
        assert day.gained == VillageStock(gold=2, elixir=2, dark=2)

    def test_each_village_ends_as_its_last_run_did(self) -> None:
        """Off the `end` line; an old one only where its last round ends a series, none still going."""
        self._run(
            "r1", "10:00", [("took_loot", (1, 1, 1)), ("took_loot", (2, 2, 2))], ending="stopped"
        )
        self._run("n1", "10:10", [("deployed", (1, 1, 0))], world="night")
        [day, night] = self._farmed(by="s1").villages
        assert (day.ending, night.ending) == ("stopped", None)
        self._run("r2", "10:20", [("took_loot", (3, 3, 3)), ("stock_full", (4, 4, 4))])
        self._run("n2", "10:30", [("deployed", (2, 2, 0))], world="night", ended=False)
        [day, night] = self._farmed(by="s1").villages
        assert (day.ending, night.ending) == ("stock_full", None)

    def test_a_round_line_carrying_the_carts_old_outcome_still_counts(self) -> None:
        """`not_night` was written `wrong_world`, and a line that no longer parses is left out of the sum."""
        self._run(
            "r1",
            "10:00",
            [("took_loot", (1, 1, 1)), ("stock_full", (3, 3, 3))],
            cart={"outcome": "wrong_world"},
        )
        [day] = self._farmed(by="s1").villages
        assert day.gained == VillageStock(gold=2, elixir=2, dark=2)

    def test_a_run_still_holding_the_emulator_is_said_to_be_going(self) -> None:
        self._run("r1", "10:00", [("took_loot", (1, 1, 1))], ended=False)
        with patch.object(
            commands,
            "read_state",
            return_value=RunnerState(status="running", log=Path("C:/logs/r1")),
        ):
            report = self._farmed(by="s1")
        assert report.running
        assert report.ended == datetime.fromisoformat("2026-11-11T10:01:00+08:00")
        assert commands.farmed_report_text(report).splitlines()[0].endswith(", 還在打")
        # Started again after a loan, and still before its first round.
        self._run("r2", "10:10", [], ended=False)
        with patch.object(
            commands,
            "read_state",
            return_value=RunnerState(status="running", log=Path("C:/logs/r2")),
        ):
            assert self._farmed(by="s1").running

    def test_the_cart_is_summed_per_run_and_unread_where_a_run_that_fought_could_not_say(
        self,
    ) -> None:
        holding = {"outcome": "locked_holding", "held": 800_000, "capacity": 1_600_000}
        self._run(
            "r1", "10:00", [("deployed", (1, 1, 0))], world="night", cart={"outcome": "collected"}
        )
        self._run("r2", "10:10", [("deployed", (1, 1, 0))], world="night", cart=holding)
        [night] = self._farmed(by="s1").villages
        assert (night.cart_gained, night.cart_now) == (0, 800_000)
        self._run(
            "r3", "10:20", [("deployed", (1, 1, 0))], world="night", cart={"outcome": "not_found"}
        )
        [night] = self._farmed(by="s1").villages
        assert night.cart_gained is None

    def test_nothing_farmed_says_so(self) -> None:
        assert commands.farmed_report_text(self._farmed(by="s1")) == "這段時間沒有打資源的紀錄"


class TendedTests(unittest.TestCase):
    """A session's upgrades, boosts and lost logins, off the journal alone (conftest's tmp copy)."""

    def setUp(self) -> None:
        self.lines: list[dict[str, object]] = []

    def _run(  # noqa: PLR0913, PLR0917 - one run's journal fields
        self,
        command: str,
        at: str,
        outcome: str | None = None,
        result: object = None,
        session: str = "s1",
        mission: str = "m",
        claimed: bool = True,
        exit_code: int = 0,
    ) -> None:
        """One run's start, its claim, and its end ten seconds later, named for the minute it started."""
        start = datetime.fromisoformat(f"2026-11-11T{at}:00+08:00")
        run = f"{at}-{command}"
        self.lines.append({
            "run": run,
            "at": start.isoformat(),
            "event": "start",
            "command": command,
            "caller": {"agent": "a", "session": session, "mission": mission},
        })
        if claimed:
            self.lines.append({
                "run": run,
                "at": start.isoformat(),
                "event": "lock",
                "what": "claimed",
            })
        self.lines.append({
            "run": run,
            "at": (start + timedelta(seconds=10)).isoformat(),
            "event": "end",
            "exit": exit_code,
            "outcome": outcome,
            "result": result,
        })

    def _confirm(self, at: str, outcome: str, **paid: object) -> None:
        mission = str(paid.pop("mission", "m"))
        report = {"asked": 1, "outcome": outcome, "frame": "after.png", **paid}
        self._run("confirm", at, outcome, report, mission=mission)

    def _tended(self, **asked: object) -> TendedReport:
        logging_setup.JOURNAL_PATH.write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in self.lines),
            encoding="utf-8",
        )
        return commands.tended(**asked)

    def test_a_goblin_hire_is_one_upgrade_with_the_press_before_it(self) -> None:
        elixir = {"price": 8_000_000, "resource": "elixir", "point": [1121, 788]}
        hire = {"point": [965, 542]}
        self._confirm("17:20", "unexpected", offer=elixir, item="雷電飛龍 8 級")
        # A hire that read other gems pressed nothing and leaves the press to the next.
        self._confirm("17:21", "price_mismatch", goblin={"gems": 150, **hire})
        self._confirm("17:22", "confirmed", goblin={"gems": 160, **hire})
        dark = {"price": 116_000, "resource": "dark", "point": [1121, 788]}
        self._confirm("18:26", "confirmed", offer=dark, mission="全天候託管: 亡靈王子續升81級")
        # A press the game turned away bought nothing, and a hire past the gap
        # after it pays its gems alone.
        self._confirm("18:40", "unexpected", offer=dark)
        self._confirm("18:44", "confirmed", goblin={"gems": 99, **hire}, item="X")
        # Nor does a press that read the wrong price leave anything for a hire.
        self._confirm("18:57", "unexpected", offer=dark)
        self._confirm("18:58", "price_mismatch", offer=dark)
        self._confirm("19:00", "confirmed", goblin={"gems": 50, **hire}, item="Y")
        first, second, third, fourth = self._tended(by="s1").upgrades
        assert (first.item, first.price, first.resource, first.gems) == (
            "雷電飛龍 8 級",
            8_000_000,
            "elixir",
            160,
        )
        assert (second.item, second.gems) == ("全天候託管: 亡靈王子續升81級", None)
        assert (third.item, third.price, third.gems) == ("X", None, 99)
        assert (fourth.item, fourth.price, fourth.gems) == ("Y", None, 50)
        text = commands.tended_text(self._tended(by="s1"))
        assert text.splitlines()[0] == "- 升級: 4 件"
        assert "雷電飛龍 8 級 (8,000,000 聖水, 哥布林 160 寶石)" in text

    def test_a_press_that_named_the_hire_prompt_is_the_hires_upgrade(self) -> None:
        """The press carries the prompt's gems too, and is still no hire of its own."""
        elixir = {"price": 8_000_000, "resource": "elixir", "point": [1121, 788]}
        hire = {"gems": 160, "point": [965, 542]}
        self._confirm("17:20", "goblin_offered", offer=elixir, goblin=hire, item="雷電飛龍 8 級")
        self._confirm("17:22", "confirmed", goblin=hire)
        (only,) = self._tended(by="s1").upgrades
        assert (only.item, only.price, only.resource, only.gems) == (
            "雷電飛龍 8 級",
            8_000_000,
            "elixir",
            160,
        )

    def test_only_the_session_and_its_farmer_since_the_time_asked(self) -> None:
        self._run("boost", "09:00", "boosted")
        self._run("boost", "10:00", "boosted", session="s1-farm")
        self._run("boost", "10:30", "not_ready")
        self._run("boost", "11:00", "boosted", session="someone-else")
        report = self._tended(by="s1", since=datetime.fromisoformat("2026-11-11T09:30:00+08:00"))
        assert report.boosts == [datetime.fromisoformat("2026-11-11T10:00:10+08:00")]

    def test_a_lost_login_is_counted_once_and_back_at_the_next_run_that_got_in(self) -> None:
        self._run("attack", "12:22", "session_taken", session="s1-farm")
        # The session's own status stood down on the same loss, and its watch
        # and its stop claimed nothing.
        self._run("status", "12:23", None, SESSION_TAKEN_SENTENCE, exit_code=1)
        # Its `hold` claimed and opened nothing, so it logged nobody back in.
        self._run("hold", "12:24")
        self._run("watch", "12:24", "due", claimed=False)
        self._run("launch", "12:28")
        self._run("attack", "13:00", "session_taken", session="s1-farm")
        [first, second] = self._tended(by="s1").lost
        assert (first.taken, first.back) == (
            datetime.fromisoformat("2026-11-11T12:22:10+08:00"),
            datetime.fromisoformat("2026-11-11T12:28:10+08:00"),
        )
        assert second.back is None
        assert (
            commands
            .tended_text(self._tended(by="s1"))
            .splitlines()[-1]
            .endswith(f"{second.taken.astimezone():%H:%M} 被搶, 還沒接回)")
        )

    def test_a_stand_down_reads_by_its_outcome_and_the_old_bare_sentence_still_counts(
        self,
    ) -> None:
        """Runs from before the stand-downs became reports answered with a sentence and no outcome."""
        self._run("status", "12:23", None, SESSION_TAKEN_SENTENCE, exit_code=1)
        self._run("launch", "12:28")
        self._run("status", "13:00", "session_taken", {"outcome": "session_taken"}, exit_code=1)
        first, second = self._tended(by="s1").lost
        assert first.back == datetime.fromisoformat("2026-11-11T12:28:10+08:00")
        assert (second.taken, second.back) == (
            datetime.fromisoformat("2026-11-11T13:00:10+08:00"),
            None,
        )

    def test_nothing_done_says_so_line_by_line(self) -> None:
        assert commands.tended_text(self._tended(by="s1")) == (
            "- 升級: 0 件\n- 鐘塔: 沒開\n- 被搶登: 沒有"
        )


class AttackSeriesMenuTests(unittest.TestCase):
    """A series that keeps failing to open the attack menu ends instead of resting on it.

    Measured 2026-10-08: an event dialog `back` does not close covered the home
    village, and a `--repeat 0` series came back `no_attack_menu` eleven times in
    a row until the agent driving it noticed and stopped it.
    """

    def _series(
        self,
        played: list[AttackReport] | Callable[[], AttackReport],
        should_stop: MagicMock | None = None,
    ) -> list:
        runner = MagicMock(name="AttackRunner")
        runner.configure_mock(played=None, holding=None, capacity=None, cart=None)
        runner.run.side_effect = played
        runner.lost = 0
        runner.world = "day"
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner),
            patch.object(commands, "_rest", return_value=None),
        ):
            series = commands.attack(
                AttackOptions(rounds=0),
                _frames(self),
                should_stop or MagicMock(return_value=False),
            )
        return series.rounds.root

    def test_a_run_of_them_ends_the_series(self) -> None:
        stuck = AttackReport(outcome="no_attack_menu")
        with self.assertLogs("ai_coc.commands", "ERROR") as logged:
            rounds = self._series([
                AttackReport(outcome="took_loot"),
                *[stuck] * (commands.MENU_FAILURES + 2),
            ])
        assert [report.outcome for report in rounds] == [
            "took_loot",
            *["no_attack_menu"] * commands.MENU_FAILURES,
        ]
        assert f"{commands.MENU_FAILURES} rounds in a row" in logged.output[-1]

    def test_the_run_outlasts_a_battle_left_on_screen(self) -> None:
        """A battle a killed run left behind ends by itself, and the rests between
        the rounds that met it have to add up to more than one.
        """
        assert (commands.MENU_FAILURES - 1) * commands.IDLE_REST >= commands.BATTLE_TIMEOUT

    def test_a_round_that_opened_the_menu_starts_the_count_again(self) -> None:
        """Consecutive, not total: stuck rounds either side of a battle are not a stuck game."""
        stuck = AttackReport(outcome="no_attack_menu")
        rounds = self._series([
            *[stuck] * (commands.MENU_FAILURES - 1),
            AttackReport(outcome="took_loot"),
            *[stuck] * (commands.MENU_FAILURES - 1),
            AttackReport(outcome="stock_full"),
        ])
        assert len(rounds) == 2 * commands.MENU_FAILURES
        assert rounds[-1].stock_full

    def test_a_stop_on_the_last_of_them_ends_as_a_stop(self) -> None:
        """A loan or a stop arriving then reads as one, not as a series that gave up."""
        stuck = AttackReport(outcome="no_attack_menu")
        played: list[AttackReport] = []

        def run() -> AttackReport:
            played.append(stuck)
            return stuck

        stopping = MagicMock(side_effect=lambda: len(played) >= commands.MENU_FAILURES)
        with self.assertNoLogs("ai_coc.commands", "ERROR"):
            rounds = self._series(run, should_stop=stopping)
        assert len(rounds) == commands.MENU_FAILURES


class AttackSeriesArmyShortTests(unittest.TestCase):
    """An army under the threshold ends the series instead of resting on it.

    Training is instant in the current game, so a camp that cannot reach
    `MIN_ARMY_RATIO` never will. Measured 2026-09-22: a limited-time event ended
    and took its troops out of the saved army, leaving 20 of 340 against a
    threshold of 306, and every round after that backed out before the search
    fee — a `--repeat 0` run walked the same menus once a minute and attacked
    nothing.
    """

    def _series(self, played: list) -> tuple[list, MagicMock]:
        runner = MagicMock(name="AttackRunner")
        runner.configure_mock(played=None, holding=None, capacity=None, cart=None)
        runner.run.side_effect = played
        runner.lost = 0
        runner.world = "day"
        with (
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "AttackRunner", return_value=runner),
            patch.object(commands, "_rest", return_value=None) as rest,
        ):
            series = commands.attack(
                AttackOptions(rounds=0), _frames(self), MagicMock(return_value=False)
            )
        return series.rounds.root, rest

    def test_an_army_short_round_ends_the_series_holding_what_it_played(self) -> None:
        fought = LootOffer(gold=1, elixir=1, dark=1)
        rounds, rest = self._series([
            AttackReport(outcome="took_loot", attacked=fought),
            AttackReport(outcome="army_short"),
            AttackReport(outcome="took_loot", attacked=fought),
        ])
        assert [report.outcome for report in rounds] == ["took_loot", "army_short"]
        # And it leaves without spending the wait between rounds on a camp that
        # will read the same a minute later.
        rest.assert_not_called()

    def test_the_other_village_ends_the_series_too(self) -> None:
        """Nothing sails, so every round after would find the game on it as well."""
        rounds, rest = self._series([
            AttackReport(outcome="other_village"),
            AttackReport(outcome="took_loot", attacked=LootOffer(gold=1, elixir=1, dark=1)),
        ])
        assert [report.outcome for report in rounds] == ["other_village"]
        rest.assert_not_called()

    def test_a_slot_freeing_up_ends_the_series_too(self) -> None:
        """It is what a run started with `--until-idle` was waiting for."""
        for freed in ("builder_free", "lab_free"):
            rounds, rest = self._series([
                AttackReport(outcome=freed),
                AttackReport(outcome="took_loot", attacked=LootOffer(gold=1, elixir=1, dark=1)),
            ])
            assert [report.outcome for report in rounds] == [freed]
            rest.assert_not_called()

    def test_another_device_logging_in_ends_the_series_from_wherever_it_was_read(self) -> None:
        """The player is on, and the next round's restart would log them out."""
        fought = LootOffer(gold=1, elixir=1, dark=1)
        rounds, rest = self._series([
            AttackReport(outcome="took_loot", attacked=fought),
            commands.SessionTakenError("taken"),
            AttackReport(outcome="took_loot", attacked=fought),
        ])
        assert [report.outcome for report in rounds] == ["took_loot", "session_taken"]
        assert rounds[-1].world == "day"
        rest.assert_not_called()

    def test_a_round_that_found_nobody_still_comes_round_again(self) -> None:
        """Only those end it, besides a run of rounds that never opened the menu
        (`MENU_FAILURES`): every other empty round rests and retries.

        No opponent above the thresholds is a matchmaker that will offer a
        different set next time, which is exactly what the wait is still for.
        """
        rounds, rest = self._series([
            AttackReport(outcome="no_opponent"),
            AttackReport(outcome="stock_full"),
        ])
        assert [report.outcome for report in rounds] == ["no_opponent", "stock_full"]
        rest.assert_called_once()


class SettleGameTests(unittest.TestCase):
    """Waiting for a village that can be tapped, then pinching the camera out."""

    def test_a_reward_chest_is_tapped_open(self) -> None:
        """`back` does nothing to it, so the wait would otherwise run out beneath it."""
        adb = MagicMock()
        with (
            patch.object(commands, "welcome_back", return_value=False),
            patch.object(commands, "star_bonus", return_value=False),
            patch.object(commands, "event_ended", return_value=False),
            patch.object(commands, "build_panel_open", return_value=False),
            patch.object(commands, "reward_chest", return_value=4),
            patch.object(commands, "open_chest") as opened,
        ):
            said = commands._press_away(adb, DISPLAY, b"")
        assert said == "a reward chest was covering the village"
        opened.assert_called_once_with(adb, DISPLAY)

    def _settle(
        self,
        worlds: list[str | None],
        displays: list[object] | None = None,
        polls: int = 3,
        stop: bool = False,
        park: bool = True,
        **screens: list[bool],
    ) -> tuple[MagicMock, DisplayTarget | None]:
        """Poll against canned readings; `dropped`, `taken`, `welcome`, `panel` and `loading` are what each frame reads as."""
        adb = _adb()
        if displays is not None:
            adb.display_for.side_effect = displays
        with (
            patch.object(commands.time, "sleep"),
            patch.object(commands, "current_world", side_effect=worlds),
            patch.object(
                commands, "idle_disconnected", side_effect=screens.get("dropped") or [False] * 9
            ),
            patch.object(commands, "refuse_taken", side_effect=screens.get("taken")),
            patch.object(
                commands, "welcome_back", side_effect=screens.get("welcome") or [False] * 9
            ),
            patch.object(commands, "star_bonus", side_effect=screens.get("star") or [False] * 9),
            patch.object(commands, "event_ended", side_effect=screens.get("event") or [False] * 9),
            patch.object(
                commands, "build_panel_open", side_effect=screens.get("panel") or [False] * 9
            ),
            patch.object(commands, "reward_chest", return_value=0),
            patch.object(
                commands, "loading_screen", side_effect=screens.get("loading") or [False] * 9
            ),
            patch.object(commands, "relogin", return_value=DISPLAY) as restarted,
            # `_settle_game` parks, and the park reads its own frames now.
            # None of these tests is about the park itself.
            patch.object(commands, "park_camera", return_value=True) as parked,
        ):
            adb.restarted = restarted
            adb.parked = parked
            return adb, commands._settle_game(adb, polls, lambda: stop, park=park)

    def test_either_village_ends_the_wait_and_the_camera_goes_out(self) -> None:
        """The park carries the pinch now, so one call settles both scale and position."""
        adb, display = self._settle([None, "night"])
        assert display == DISPLAY
        assert adb.screenshot.call_count == 2
        adb.parked.assert_called_once_with(adb, DISPLAY, "night")

    def test_the_attack_takes_the_far_zoom_without_the_park(self) -> None:
        """It taps no map coordinate before its battle, which measures its own camera.

        On the builder base the park it used to make came straight before the
        cart visit's own, so the camera settled twice in a row.
        """
        adb, display = self._settle(["night"], park=False)
        assert display == DISPLAY
        adb.parked.assert_not_called()
        assert adb.zoom.call_args.args[0] == "out"

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

    def test_the_raid_report_is_pressed_away_rather_than_waited_under(self) -> None:
        """首領，歡迎回來 held a launch for its whole patience; `back` closes it."""
        adb, display = self._settle([None, "night"], welcome=[True])
        assert display == DISPLAY
        adb.back.assert_called_once_with(DISPLAY)

    def test_the_star_bonus_sheet_is_pressed_away_rather_than_waited_under(self) -> None:
        """已收到勝利之星獎勵 held `status` for its whole patience; `back` closes it."""
        adb, display = self._settle([None, "day"], star=[True])
        assert display == DISPLAY
        adb.back.assert_called_once_with(DISPLAY)

    def test_an_events_end_notice_is_pressed_away_rather_than_waited_under(self) -> None:
        """活動已結束 comes once per event, over whichever village the game is on (#493)."""
        adb, display = self._settle([None, "day"], event=[True])
        assert display == DISPLAY
        adb.back.assert_called_once_with(DISPLAY)

    def test_the_build_panel_is_shut_with_its_own_x_rather_than_waited_under(self) -> None:
        """It shifts the corner column, so the village under it never reads, and `back` leaves it up."""
        adb, display = self._settle([None, "day"], panel=[True])
        assert display == DISPLAY
        adb.tap.assert_called_once_with(*PANEL_CLOSE, DISPLAY)
        adb.back.assert_not_called()

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

    def test_another_devices_session_is_left_to_it(self) -> None:
        """The restart that answers the other dropped sessions would log the player out."""
        with pytest.raises(commands.SessionTakenError):
            self._settle([None], dropped=[True], taken=commands.SessionTakenError("taken"))


def _device() -> MagicMock:
    """A device whose every capture is a PNG ending in the physical display it was asked for."""
    device = MagicMock(name="device")
    device.shell.side_effect = lambda command, **_: PNG_MAGIC + command[-1].encode()
    return device


class FrameTickerTests(unittest.TestCase):
    """The heartbeat shoots where the command last looked, numbered among its frames."""

    def _command(self, frames: FrameLog) -> AdbController:
        """The controller a command captures through, attached to the run as `_recording` does."""
        adb = _controller()
        adb.frames = frames
        return adb

    def _tick(self, frames: FrameLog, *between: Callable[[], object]) -> None:
        """Run the heartbeat on this thread: each step, then one tick, with no real wait."""
        ticker = commands.FrameTicker(frames=frames, seconds=5)
        steps = iter(between)

        def wait(_: float) -> bool:
            step = next(steps, None)
            if step is None:
                return True
            step()
            return False

        with patch.object(ticker._stop, "wait", side_effect=wait):
            ticker._tick()

    def _names(self, frames: FrameLog) -> list[str]:
        return sorted(path.name for path in frames.directory.glob("*.png"))

    def test_no_interval_starts_no_thread(self) -> None:
        frames = _frames(self)
        with commands.FrameTicker(frames=frames, seconds=0) as ticker:
            assert ticker._thread is None
        assert not frames.directory.exists()

    def test_leaving_stops_the_heartbeat_without_waiting_out_its_interval(self) -> None:
        started = time.monotonic()
        with commands.FrameTicker(frames=_frames(self), seconds=60) as ticker:
            pass
        assert time.monotonic() - started < 5
        assert ticker._thread is not None
        assert not ticker._thread.is_alive()

    def test_nothing_is_shot_before_the_command_first_captures(self) -> None:
        frames = _frames(self)
        with patch.object(AdbController, "screenshot") as shot:
            self._tick(frames, lambda: None, lambda: None)
        shot.assert_not_called()
        assert not frames.directory.exists()

    def test_ticks_are_numbered_among_the_commands_own_frames(self) -> None:
        """So sorting `frames/` by name is time order across both."""
        frames = _frames(self, recording=True)
        adb = self._command(frames)
        with (
            patch.object(AdbController, "connect", return_value=_device()),
            self.assertLogs("ai_coc.models") as logged,
        ):
            self._tick(
                frames,
                lambda: adb.screenshot(DISPLAY, "scout"),
                lambda: adb.screenshot(DISPLAY, "pass"),
            )
        assert self._names(frames) == [
            "0001_scout.png",
            "0002_tick.png",
            "0003_pass.png",
            "0004_tick.png",
        ]
        assert "INFO:ai_coc.models:Saved frame 0002_tick.png" in logged.output

    def test_a_heartbeat_without_debug_keeps_its_ticks_and_not_the_commands_reads(self) -> None:
        frames = _frames(self)
        adb = self._command(frames)
        with patch.object(AdbController, "connect", return_value=_device()):
            self._tick(frames, lambda: adb.screenshot(DISPLAY, "scout"))
        assert self._names(frames) == ["0001_tick.png"]

    def test_the_tick_follows_the_command_onto_the_display_a_restart_moved_it_to(self) -> None:
        """No re-sync: the command's own capture is what moves the heartbeat."""
        frames = _frames(self)
        adb = self._command(frames)
        moved = DisplayTarget(logical_id="3", physical_id="11")
        with (
            patch.object(AdbController, "connect", return_value=_device()),
            patch.object(FrameLog, "looked", autospec=True, side_effect=FrameLog.looked) as looked,
        ):
            self._tick(
                frames,
                lambda: adb.screenshot(DISPLAY, "scout"),
                lambda: adb.screenshot(moved, "scout"),
            )
        first, second = (frames.directory / name for name in self._names(frames))
        assert first.read_bytes().endswith(b"9")
        assert second.read_bytes().endswith(b"11")
        # Only the command's two captures say where it looked; a tick's own,
        # landing after the command moved on, would move the heartbeat back.
        assert looked.call_count == 2

    def test_a_run_of_failing_ticks_warns_once_and_says_when_they_land_again(self) -> None:
        """Every warning lands in `runs.jsonl` with its trace, so an outage warns once."""
        frames = _frames(self)
        frames.looked(_controller(), DISPLAY)
        gone = AdbControlError("gone")
        with (
            patch.object(
                AdbController, "screenshot", side_effect=[gone, gone, gone, b"png", gone]
            ),
            self.assertLogs("ai_coc.commands") as logged,
        ):
            self._tick(frames, *[lambda: None] * 5)
        assert [line.split(":")[0] for line in logged.output] == ["WARNING", "INFO", "WARNING"]
        assert self._names(frames) == ["0001_tick.png"]


class RunPlumbingTests(unittest.TestCase):
    """The small pieces `attack` is built from, each with its own reason to exist."""

    def test_the_plan_that_ran_is_written_where_it_was_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deep" / "plan.json"
            commands._write_plan(path, plans.flat())
            assert plans.load(path) == plans.flat()
            commands._write_plan(None, plans.flat())
            commands._write_plan(Path(folder) / "never.json", None)
            assert not (Path(folder) / "never.json").exists()

    def test_a_handed_in_plan_names_its_own_village(self) -> None:
        """A written tactic is for one village, and the series plays that one."""
        day = plans.PLAN_DIR / "flat.json"
        night = plans.PLAN_DIR / "night_flat.json"
        plan, world = commands._handed_plan(AttackOptions(plan=day))
        assert (isinstance(plan, AttackPlan), world) == (True, "day")
        plan, world = commands._handed_plan(AttackOptions(plan=night))
        assert (isinstance(plan, NightPlan), world) == (True, "night")
        assert commands._handed_plan(AttackOptions()) == (None, None)

    def test_a_misedited_plan_reports_only_its_own_villages_errors(self) -> None:
        """Listing the other plan's missing fields reads as if this one needed them too."""
        broken = json.loads((plans.PLAN_DIR / "flat.json").read_text(encoding="utf-8"))
        broken["steps"][0]["act"] = "troop"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with pytest.raises(ValidationError) as raised:
                plans.load_any(path)
        assert "act" in str(raised.value)
        assert "deploy_start" not in str(raised.value)

    def test_the_barracks_wait_runs_its_course_with_nobody_stopping_it(self) -> None:
        with patch.object(commands, "STOP_POLL", 0.001):
            assert not commands._rest(0.005)

    def test_stop_names_what_it_asked_to_stand_down(self) -> None:
        """Which command and which pid, because the answer to "did that land"
        used to be a file that looked the same however it got there.
        """
        with commands.claim("attack"):
            stopped = commands.stop()
            assert (stopped.emulator, stopped.command, stopped.pid) == (
                "asked",
                "attack",
                os.getpid(),
            )
            assert commands.stop_requested()

    def test_a_stop_ends_with_the_run_it_was_asked_of(self) -> None:
        """The next claim does not stand down on a stop the last run already served."""
        with commands.claim("attack"):
            commands.stop()
            assert commands.stop_requested()
        with commands.claim("collect"):
            assert not commands.stop_requested()


class TurnaroundTests(unittest.TestCase):
    """Seconds from a timer `status` read running out to the confirm that bought the next level.

    The journal lives in this test's tmp directory (conftest), and so do the run logs here.
    """

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.logs = Path(folder.name)
        self.lines: list[dict] = []

    def _status(
        self, at: str, *timers: tuple[str, str, str, str], export: str = "exported"
    ) -> None:
        run = f"status-{len(self.lines)}"
        self.lines.append({"run": run, "at": at, "event": "start", "command": "status"})
        rows = [
            {
                "world": "day",
                "role": role,
                "section": section,
                "data_id": 1,
                "name": name,
                "timer": 1,
                "seconds": 1,
                "done_at": done,
            }
            for section, role, name, done in timers
        ]
        result = {"export": export, "timers": rows}
        self.lines.append({"run": run, "at": at, "event": "end", "result": result})

    def _confirm(self, at: str, tapped: str, **paid: object) -> None:
        run = f"confirm-{len(self.lines)}"
        log = self.logs / run
        log.mkdir()
        (log / "run.log").write_text(
            f"{tapped} INFO    ai_coc.adapters.adb: Tap 127.0.0.1:5555 display 0 at (1, 2)\n",
            encoding="utf-8",
        )
        self.lines.append({
            "run": run,
            "at": at,
            "event": "start",
            "command": "confirm",
            "log": str(log),
        })
        self.lines.append({
            "run": run,
            "at": at,
            "event": "end",
            "outcome": "confirmed",
            "result": paid,
        })

    def _count(self, since: datetime | None = None) -> list[tuple[str, str | None, float]]:
        journal = logging_setup.JOURNAL_PATH
        journal.write_text("".join(json.dumps(line) + "\n" for line in self.lines), "utf-8")
        report = commands.turnaround(since)
        return [(sample.group, sample.name, sample.seconds) for sample in report.samples]

    def test_each_confirm_is_timed_from_the_timer_it_bought(self) -> None:
        self._status(
            "2026-11-07T10:15:00+08:00",
            ("heroes", "builder", "Archer Queen", "2026-11-07T10:20:00+08:00"),
            ("units", "lab", "Druid", "2026-11-07T10:30:00+08:00"),
            ("buildings", "builder", "Mortar", "2026-11-07T10:40:00+08:00"),
        )
        self._confirm("2026-11-07T10:20:10+08:00", "10:20:25")
        self._confirm("2026-11-07T10:30:10+08:00", "10:30:16")
        self._confirm("2026-11-07T10:40:10+08:00", "10:40:24")
        assert self._count() == [
            ("hero", "Archer Queen", 25),
            ("lab", "Druid", 16),
            ("building", "Mortar", 24),
        ]

    def test_goblins_walls_and_a_second_confirm_on_one_timer_are_not_counted(self) -> None:
        self._status(
            "2026-11-07T10:15:00+08:00",
            ("units", "lab", "Bowler", "2026-11-07T10:20:00+08:00"),
            ("buildings", "builder", "Wall", "2026-11-07T10:30:00+08:00"),
        )
        self._confirm("2026-11-07T10:20:40+08:00", "10:20:59", goblin={"price": 31})
        self._confirm("2026-11-07T10:21:30+08:00", "10:21:40")
        self._confirm("2026-11-07T10:30:10+08:00", "10:30:20", wall={"price": 1})
        assert self._count() == []

    def test_since_keeps_only_the_presses_from_then_on(self) -> None:
        self._status(
            "2026-11-06T10:15:00+08:00",
            ("heroes", "builder", "Barbarian King", "2026-11-06T10:20:00+08:00"),
        )
        self._confirm("2026-11-06T10:20:10+08:00", "10:20:20")
        self._status(
            "2026-11-07T10:15:00+08:00",
            ("heroes", "builder", "Archer Queen", "2026-11-07T10:20:00+08:00"),
        )
        self._confirm("2026-11-07T10:20:10+08:00", "10:20:30")
        self._status(
            "2026-11-07T11:15:00+08:00",
            ("heroes", "builder", "Grand Warden", "2026-11-07T11:20:00+08:00"),
        )
        self._confirm("2026-11-07T11:20:10+08:00", "11:20:40")
        assert len(self._count(datetime.fromisoformat("2026-11-07T00:00:00+08:00"))) == 2
        assert self._count(datetime.fromisoformat("2026-11-07T11:00:00+08:00")) == [
            ("hero", "Grand Warden", 40)
        ]
        assert len(self._count()) == 3

    def test_a_status_whose_export_failed_does_not_hide_the_one_before(self) -> None:
        self._status(
            "2026-11-07T10:15:00+08:00",
            ("heroes", "builder", "Archer Queen", "2026-11-07T10:20:00+08:00"),
        )
        self._status("2026-11-07T10:17:00+08:00", export="no_village")
        self._confirm("2026-11-07T10:20:10+08:00", "10:20:25")
        assert self._count() == [("hero", "Archer Queen", 25)]

    def test_the_latest_due_timer_is_paired_and_one_past_the_window_is_not(self) -> None:
        self._status(
            "2026-11-07T10:15:00+08:00",
            ("units", "lab", "Druid", "2026-11-07T10:20:00+08:00"),
            ("buildings", "builder", "Mortar", "2026-11-07T10:25:00+08:00"),
            ("heroes", "builder", "Archer Queen", "2026-11-07T11:00:00+08:00"),
        )
        self._confirm("2026-11-07T10:25:10+08:00", "10:25:20")
        self._confirm("2026-11-07T11:15:50+08:00", "11:16:00")
        assert self._count() == [("building", "Mortar", 20)]

    def test_a_plain_confirm_just_before_a_goblin_hire_is_not_counted(self) -> None:
        """It went on the goblin's slot: the hire is the game asking for the slot it just used."""
        self._status(
            "2026-11-07T10:15:00+08:00",
            ("heroes", "builder", "Barbarian King", "2026-11-07T10:20:00+08:00"),
            ("units", "lab", "Bowler", "2026-11-07T10:22:00+08:00"),
        )
        self._confirm("2026-11-07T10:20:20+08:00", "10:20:30")
        self._confirm("2026-11-07T10:22:30+08:00", "10:22:40", goblin={"price": 31})
        assert self._count() == []


class NoteTests(unittest.TestCase):
    """The user's notes, kept in the state file beside the emulator and the watch.

    The state file and its `backups/` live in this test's tmp directory (conftest).
    """

    def setUp(self) -> None:
        # A note goes only into a file that exists, which any claim writes.
        commands._write_state(RunnerState())

    def test_a_note_is_added_listed_and_dropped_by_its_number(self) -> None:
        commands.note("add", "day", "冰人這一級升完改研究巨石投手")
        commands.note("add", "day", "巨石投手滿級以後升空中戰車")
        commands.note("add", "night", "  聖水瓶這一級升完改升建築大師訓練營  ")
        assert commands.note("list").notes.night == ["聖水瓶這一級升完改升建築大師訓練營"]
        report = commands.note("done", "day", "1")
        assert report.outcome == "done"
        assert commands.note("list").notes.day == ["巨石投手滿級以後升空中戰車"]

    def test_the_list_prints_the_number_done_takes(self) -> None:
        commands.note("add", "day", "a")
        commands.note("add", "day", "b")
        assert (
            commands.note_text(commands.note("list")) == "日世界\n1. a\n2. b\n\n夜世界\n沒有交代"
        )
        dropped = commands.note_text(commands.note("done", "day", "2"))
        assert dropped.splitlines()[0] == commands.NOTE_LINES["done"]
        assert "1. a" in dropped
        assert "2. b" not in dropped

    def test_a_note_that_cannot_be_named_changes_nothing(self) -> None:
        commands.note("add", "day", "a")
        for args, outcome in (
            (("done", "day", "2"), "no_such_note"),
            (("done", "day", "x"), "no_such_note"),
            (("done", "night", "1"), "no_such_note"),
            (("add", "day", " "), "incomplete"),
            (("done", None, "1"), "incomplete"),
        ):
            assert commands.note(*args).outcome == outcome, args
        assert commands.note("list").notes.day == ["a"]

    def test_the_emulator_and_watch_writes_carry_the_notes_over(self) -> None:
        commands.note("add", "day", "a")
        with commands.claim("attack"):
            commands._write_watch(Watch(status="idle"))
        assert commands.note("list").notes.day == ["a"]

    def test_a_file_that_will_not_read_carries_on_from_its_newest_backup(self) -> None:
        """Writing an empty file over it would lose the notes, which nothing writes back."""
        commands.note("add", "day", "a")
        commands.STATE_PATH.write_text('{"emulator": {"status": "runn', encoding="utf-8")
        with patch.object(commands, "TORN_GAP", 0):
            commands._write_state(RunnerState(command="attack"))
        assert commands.note("list").notes.day == ["a"]

    def test_a_file_deleted_by_hand_starts_afresh_but_keeps_its_notes(self) -> None:
        commands.note("add", "night", "a")
        with commands.claim("attack"):
            pass
        commands.STATE_PATH.unlink()
        commands._write_state(RunnerState(command="collect"))
        assert commands.note("list").notes.night == ["a"]
        state = commands.read_state()
        assert state is not None
        assert state.command == "collect"

    def test_a_note_is_not_written_into_a_file_deleted_by_hand(self) -> None:
        """Putting the file back would hide the stop its deletion is."""
        commands.note("add", "day", "a")
        with commands.claim("attack"):
            commands.STATE_PATH.unlink()
            with pytest.raises(commands.NoStateFileError):
                commands.note("add", "day", "b")
            assert commands.stop_requested()
        assert commands.note("list").notes.day == ["a"]

    def test_only_the_newest_copies_are_kept(self) -> None:
        with patch.object(commands, "STATE_BACKUPS", 3):
            for text in "abcde":
                commands.note("add", "day", text)
        copies = sorted(commands.STATE_PATH.with_name("backups").glob("state-*.json"))
        assert len(copies) == 3
        assert json.loads(copies[-1].read_text(encoding="utf-8"))["notes"]["day"] == list("abcde")


class WatchTests(unittest.TestCase):
    """The upgrade watch, which is the sleeping process its own record names.

    The state file lives in this test's tmp directory (conftest), so nothing
    here touches the real one. A watch and a stop run in one process here,
    and `_alive` never counts this process as somebody else's, so the tests
    that need a live watcher patch it.
    """

    MINE = Caller(agent="claude-code", session="a", mission="盯升級")
    THEIRS = Caller(agent="antigravity", session="b", mission="打資源")
    OTHER_PID = 424242

    def setUp(self) -> None:
        commands._write_state(RunnerState())
        self.enterContext(patch.object(commands, "STOP_POLL", 0.01))

    def _soon(self, seconds: float = 0.05) -> datetime:
        return datetime.now().astimezone() + timedelta(seconds=seconds)

    def _theirs(self) -> Watch:
        return Watch(
            status="running",
            pid=self.OTHER_PID,
            pid_created=1.0,
            started=self._soon(-60),
            caller=self.THEIRS,
            next_at=self._soon(3600),
            about="鐘塔",
        )

    def test_it_wakes_at_the_earliest_of_everything_asked_for(self) -> None:
        now = datetime.now().astimezone()

        def at(minutes: float) -> str:
            return (now + timedelta(minutes=minutes)).isoformat()

        timers = [_timer(1, at(120)), _timer(2, at(60), role="lab"), _timer(4, at(3), role="pet")]
        tower = ClockTower(ready_in=1, ready_at=at(50))

        def minutes(*args: object, **kwargs: object) -> float:
            wake = commands.wake_at(*args, **kwargs)
            assert wake is not None
            return round((wake - now).total_seconds() / 60)

        with (
            patch.object(commands, "_last_export") as saved,
            patch.object(commands, "upgrade_timers", return_value=timers),
            patch.object(commands, "clock_tower", return_value=tower),
        ):
            # The pet's lead has run out, so it wakes at its own time.
            assert minutes([], [], lead=5) == 3
            assert minutes([], ["lab", "builder"], lead=5) == 55
            assert minutes([], ["lab", "builder"], lead=5, tower=True) == 50
            assert minutes([], ["builder"], lead=5) == 115
            assert minutes([], ["builder"], lead=5, minutes=30) == 30
            assert minutes([now + timedelta(minutes=10)], ["builder"]) == 10
            assert commands.wake_at([], ["pet", "lab"], lead=0) is not None
            saved.reset_mock()
            assert minutes([], minutes=180) == 180
            saved.assert_not_called()
        done = [_timer(3, at(-1))]
        with (
            patch.object(commands, "_last_export"),
            patch.object(commands, "upgrade_timers", return_value=done),
            patch.object(
                commands, "clock_tower", return_value=ClockTower(ready_in=0, ready_at=at(-5))
            ),
        ):
            # One already done wakes the session now; a tower ready now is left out.
            assert minutes([], ["builder"]) == 0
            assert commands.wake_at([], tower=True) is None
            assert commands.wake_at([], ["lab"], tower=True) is None

    def test_nothing_to_wait_for_answers_at_once_and_leaves_a_live_watch_alone(self) -> None:
        commands._write_watch(self._theirs())
        with patch.object(commands, "_alive", return_value=True):
            report = commands.watch(self.MINE, None, "工人")
        assert (report.outcome, report.next_at) == ("nothing_due", None)
        after = commands.read_watch()
        assert after is not None
        assert (after.status, after.pid, after.caller) == ("running", self.OTHER_PID, self.THEIRS)

    def test_a_watch_that_farms_says_so_on_its_record(self) -> None:
        seen: list[Watch | None] = []
        with patch.object(
            commands.time, "sleep", side_effect=lambda _s: seen.append(commands.read_watch())
        ):
            commands.watch(self.MINE, self._soon(0.2), "工人", farms=True)
        assert seen[0] is not None
        assert seen[0].farms
        commands.watch(self.MINE, self._soon(-1), "工人")
        after = commands.read_watch()
        assert after is not None
        assert not after.farms

    def test_it_sleeps_until_its_time_and_says_it_came_due(self) -> None:
        wake = self._soon()
        report = commands.watch(self.MINE, wake, "日世界 Archer Queen")
        assert (report.outcome, report.stop_by) == ("due", None)
        assert report.ended >= wake
        after = commands.read_watch()
        assert after is not None
        assert (after.status, after.caller, after.next_at) == ("idle", self.MINE, wake)

    def test_while_it_sleeps_the_record_is_its_own_process(self) -> None:
        seen: list[Watch | None] = []
        with patch.object(
            commands.time, "sleep", side_effect=lambda _s: seen.append(commands.read_watch())
        ):
            commands.watch(self.MINE, self._soon(0.2), "工人")
        held = seen[0]
        assert held is not None
        assert (held.status, held.pid, held.caller, held.about) == (
            "running",
            os.getpid(),
            self.MINE,
            "工人",
        )
        assert held.pid_created is not None

    def test_a_stop_wakes_it_and_it_says_who(self) -> None:
        with (
            patch.object(commands, "_alive", return_value=True),
            patch.object(
                commands.time, "sleep", side_effect=lambda _s: commands.stop(self.THEIRS)
            ),
        ):
            report = commands.watch(self.MINE, self._soon(3600), "工人")
        assert (report.outcome, report.stop_by) == ("stopped", self.THEIRS)
        after = commands.read_watch()
        assert after is not None
        assert (after.status, after.stop_by) == ("idle", self.THEIRS)

    def test_a_new_watch_stops_a_live_one_and_takes_its_place(self) -> None:
        """Like an emulator claim: the old one is asked, named, and waited out."""
        commands._write_watch(self._theirs())
        asked: list[Watch | None] = []

        def old_one_lets_go(_seconds: float) -> None:
            asked.append(commands.read_watch())
            commands._write_watch(self._theirs().model_copy(update={"status": "idle"}))

        with (
            patch.object(commands, "_alive", side_effect=lambda w: w.pid == self.OTHER_PID),
            patch.object(commands.time, "sleep", side_effect=old_one_lets_go),
        ):
            report = commands.watch(self.MINE, self._soon(-1), "工人")
        assert asked[0] is not None
        assert (asked[0].status, asked[0].stop_by) == ("stopping", self.MINE)
        assert report.outcome == "due"
        after = commands.read_watch()
        assert after is not None
        assert after.caller == self.MINE

    def test_a_record_whose_process_is_gone_is_taken_over_at_once(self) -> None:
        commands._write_watch(self._theirs())
        with patch.object(commands.time, "sleep", side_effect=AssertionError("waited")):
            commands.watch(self.MINE, self._soon(-1), "工人")
        after = commands.read_watch()
        assert after is not None
        assert after.caller == self.MINE

    def test_a_deleted_file_is_a_stop_and_is_not_written_back(self) -> None:
        """The deletion is a stop to the emulator's holder too, which a file back
        in place before it looked would hide.
        """
        with patch.object(
            commands.time, "sleep", side_effect=lambda _s: commands.STATE_PATH.unlink()
        ):
            report = commands.watch(self.MINE, self._soon(3600), "工人")
        assert (report.outcome, report.stop_by) == ("stopped", None)
        assert not commands.STATE_PATH.exists()

    def test_the_emulator_s_claims_and_releases_leave_the_watch_alone(self) -> None:
        theirs = self._theirs()
        commands._write_watch(theirs)
        with commands.claim("stock", caller=self.MINE):
            held = commands.read_watch()
        assert held == commands.read_watch() == theirs

    def test_one_stop_reaches_both_and_a_target_reaches_one(self) -> None:
        running = RunnerState(status="running", pid=self.OTHER_PID, command="attack")
        for target, emulator, watch in (
            ("emulator", "stopping", "running"),
            ("watch", "running", "stopping"),
            ("all", "stopping", "stopping"),
        ):
            commands._write_state(running)
            commands._write_watch(self._theirs())
            with patch.object(commands, "_alive", return_value=True):
                commands.stop(self.MINE, target)
            state, watching = commands.read_state(), commands.read_watch()
            assert state is not None
            assert watching is not None
            assert (state.status, watching.status) == (emulator, watch), target

    def test_a_stop_with_nothing_running_says_so_for_both(self) -> None:
        stopped = commands.stop()
        assert (stopped.emulator, stopped.loan, stopped.watch) == (
            "nothing_running",
            None,
            "nothing_watching",
        )

    def test_a_file_written_before_the_split_reads_as_the_emulator_s_record(self) -> None:
        """So a run of the old code still holding the emulator is not read as idle.
        Its watch, a record with no process behind it, is dropped, whether it
        was one or `null`, which is how most were left.
        """
        for watch in ({"caller": {"agent": "antigravity"}, "since": None, "about": "鐘塔"}, None):
            commands.STATE_PATH.write_text(
                json.dumps({
                    "status": "running",
                    "pid": self.OTHER_PID,
                    "command": "attack",
                    "watch": watch,
                }),
                encoding="utf-8",
            )
            state, watching = commands.read_state(), commands.read_watch()
            assert state is not None
            assert watching is not None
            assert (state.status, state.pid, watching.status) == (
                "running",
                self.OTHER_PID,
                "idle",
            )

    def test_a_watch_will_not_write_back_a_deleted_file(self) -> None:
        """Deleting it is a stop the emulator's holder has not read yet."""
        commands.STATE_PATH.unlink()
        with pytest.raises(commands.NoStateFileError):
            commands.watch(self.MINE, self._soon(), "工人")
        assert not commands.STATE_PATH.exists()

    def test_a_write_tries_again_while_another_process_has_the_file_open(self) -> None:
        replace = os.replace
        refusals = iter([PermissionError("in use")])

        def busy_once(source: Path, target: Path) -> None:
            if (refusal := next(refusals, None)) is not None:
                raise refusal
            replace(source, target)

        with patch.object(commands.os, "replace", side_effect=busy_once):
            commands._write_watch(self._theirs())
        watching = commands.read_watch()
        assert watching is not None
        assert watching.caller == self.THEIRS


class ReadCommandTests(unittest.TestCase):
    def test_a_home_village_frame_reads_as_one(self) -> None:
        reading = commands.read((FRAMES / "hud_day.png").read_bytes())
        assert reading.world == "day"
        assert reading.stock is not None
        assert reading.scout is None
        assert not reading.attack_menu

    def test_a_builder_base_frame_carries_its_own_two_rows(self) -> None:
        """`stock` reads the gems bar as dark elixir there; `builder_stock` is the one to believe."""
        reading = commands.read((FRAMES / "hud_night.png").read_bytes())
        assert reading.world == "night"
        assert reading.builder_stock == VillageStock(gold=6_000_000, elixir=5_600_000, dark=0)

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
            patch.object(commands, "_attached", return_value=adb),
            patch.object(commands.time, "sleep") as slept,
        ):
            saved = commands.capture(_frames(self), count=2, gap=0.5)
        assert [path.name for path in saved] == ["frame_000.png", "frame_001.png"]
        assert all(path.read_bytes() == b"x" for path in saved)
        # Between the two, and not after the last one.
        slept.assert_called_once_with(0.5)

    def _view(
        self, seen: str | None, zoom: str = "out", times: int = 2, selected: bool = False
    ) -> tuple[MagicMock, object]:
        adb = _adb()
        with (
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands, "current_world", return_value=seen),
            patch.object(commands, "selection_row", return_value=selected),
            patch.object(world_ui.time, "sleep"),
            # The park reads its own frames now; a still one is what says it
            # arrived, and `PARK_STILL` of them is what it swipes for.
            patch.object(world_ui, "view_shift", return_value=(0, 0)),
            # These fakes hand back a MagicMock rather than a frame, and the
            # boat's marker reads pixels; `ParkCameraTests` is where it is tested.
            patch.object(world_ui, "boat_marker", return_value=(442, 542)),
        ):
            return adb, commands.view(_frames(self), zoom, times)

    def test_view_sends_the_pinch_at_the_games_own_display(self) -> None:
        adb, report = self._view("day", "in")
        _, far = self._view("day", "out", 3)
        adb.zoom.assert_any_call("in", 2, COC_PACKAGE, DISPLAY)
        assert (report.zoom, report.times) == ("in", 2)
        assert (far.zoom, far.times) == ("out", 3)

    def test_zooming_in_does_not_claim_a_park(self) -> None:
        """The walk is only bounded at the far zoom, so there is none to make here.

        Measured two pinches in, the camera pans off the village into the map's
        dark border and was still moving after fourteen swipes. Parking anyway
        would undo the zoom that was just asked for and report a position it
        never reached, which is the one thing this command is trusted for.
        """
        adb, report = self._view("day", "in")
        adb.swipe.assert_not_called()
        # Only the capture that let the pinch through: there is no park to aim,
        # so a second one would be 0.7 s spent on nothing.
        adb.screenshot.assert_called_once()
        assert report.outcome == "zoomed_in"

    def test_view_parks_the_camera_as_well_as_zooming_it(self) -> None:
        """Scale was only ever half of what a caller asking for the view wants.

        The far zoom was documented as centring the village too, and measured it
        does not move the camera at all — so a command that only pinched left
        every map coordinate valid until the next thing that moved the camera.
        """
        adb, report = self._view("day")
        crossing = world_ui.CROSSINGS["night"]
        landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
        assert [call.args[:2] for call in adb.swipe.call_args_list] == [
            (crossing.start, landing)
        ] * world_ui.PARK_STILL + [world_ui.CENTRE_DRAGS["free"]]
        assert (report.outcome, report.world) == ("parked", "day")

    def test_a_village_with_something_selected_is_pinched_and_not_dragged(self) -> None:
        """A drag starting on the selection's buttons leaves the camera where it was (#429)."""
        adb, report = self._view("night", selected=True)
        adb.zoom.assert_called_once()
        adb.swipe.assert_not_called()
        assert (report.outcome, report.world) == ("selected", "night")

    def test_the_builder_base_is_parked_into_its_own_corner(self) -> None:
        """The two maps clamp in opposite corners, so one push cannot serve both.

        Always pushing the home village's way would leave this village short of
        any clamp, at a position that is neither measured nor reproducible —
        which is the one thing the command now promises.
        """
        adb, _ = self._view("night")
        crossing = world_ui.CROSSINGS["day"]
        landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
        # Into the corner first, then the one drag that brings the village to the middle.
        assert [call.args[:2] for call in adb.swipe.call_args_list] == [
            (crossing.start, landing)
        ] * world_ui.PARK_STILL + [world_ui.CENTRE_DRAGS["night"]]

    def test_a_screen_that_is_not_a_village_gets_no_gesture_at_all(self) -> None:
        """A killed run leaves the game mid-battle, and that is when this gets reached for.

        A pinch's fingers land on whatever is drawn under them, and a swipe with
        a card selected deploys troops, so neither goes before a capture reads a
        village, whichever way the zoom was asked.
        """
        for zoom in ("out", "in"):
            adb, report = self._view(None, zoom)
            adb.zoom.assert_not_called()
            adb.swipe.assert_not_called()
            assert (report.outcome, report.world, report.times) == ("not_a_village", None, 0)

    def test_the_open_build_panel_is_not_pinched(self) -> None:
        """One finger of the pinch starts on the lab tab's rows (2026-11-10).

        It opened 將亡靈升至13級？ on the live game. The frame goes through the
        real village reading here, since that reading is the whole guard.
        """
        adb = _adb()
        adb.screenshot.return_value = (FRAMES / "hud_day_lab_panel.png").read_bytes()
        with patch.object(commands, "_controller", return_value=adb):
            report = commands.view(_frames(self))
        adb.zoom.assert_not_called()
        adb.swipe.assert_not_called()
        adb.tap.assert_not_called()
        assert (report.outcome, report.times) == ("not_a_village", 0)
        assert commands.view_line(report) == "鏡頭沒有拉遠,畫面不是村莊,沒有把鏡頭停回定位"


class PressAndConfirmTests(unittest.TestCase):
    """The hands the agent drives the build panel with, and the one press that spends."""

    def _run(
        self, frames: list[str], act: Callable[[FrameLog], object]
    ) -> tuple[MagicMock, object]:
        adb = _adb()
        pngs = [(FRAMES / name).read_bytes() for name in frames]
        # The last frame stands for the screen staying put however often it is
        # looked at again.
        adb.screenshot.side_effect = lambda *_: pngs.pop(0) if len(pngs) > 1 else pngs[0]
        with (
            patch.object(commands, "_attached", return_value=adb),
            patch.object(commands.time, "sleep"),
        ):
            report = act(_frames(self))
        assert report.frame.is_file()  # type: ignore[attr-defined]
        return adb, report

    def test_a_tap_on_the_map_lands(self) -> None:
        adb, report = self._run(
            ["hud_day.png", "hud_day_build_panel.png"], lambda d: commands.tap((800, 450), d)
        )
        adb.tap.assert_called_once_with(800, 450, DISPLAY)
        assert report.outcome == "pressed"

    def test_the_huds_own_buttons_are_refused_on_a_village(self) -> None:
        """The shop and the + buttons open the gem store."""
        adb, report = self._run(["hud_day.png"], lambda d: commands.tap((1515, 450), d))
        adb.tap.assert_not_called()
        assert report.outcome == "on_hud"

    def test_a_menus_gem_plate_is_refused_across_its_whole_width(self) -> None:
        """加速所有同類項目 on its blue plate, x 722-881 on this frame, paid in a magic item.

        Its left edge included, where the nearest grid point is the gap beside it.
        """
        for x in (730, 800, 870):
            adb, report = self._run(
                ["menu_with_gem_plate.png"], lambda d, at=x: commands.tap((at, 700), d)
            )
            adb.tap.assert_not_called()
            assert report.outcome == "menu_gems", x

    def test_a_menu_row_with_a_finish_button_takes_no_tap_anywhere_along_it(self) -> None:
        """A builder base running row selects the building: 資訊, 取消 and 立即完成 for gems."""
        for point, outcome in (
            ((757, 710), "green_button"),
            ((585, 710), "menu_gems"),
            ((410, 710), "menu_gems"),
        ):
            adb, report = self._run(
                ["night_menu_finishing.png"], lambda d, p=point: commands.tap(p, d)
            )
            adb.tap.assert_not_called()
            assert report.outcome == outcome, point

    def test_a_ready_clock_tower_is_selected_and_its_free_boost_pressed_once(self) -> None:
        """The tower, its free boost, then the 加速 on the sheet that press raised."""
        adb, report = self._run(
            [
                "night_clock_ready.png",
                "night_clock_selected.png",
                "night_clock_boost_sheet.png",
                "hud_night.png",
            ],
            commands.boost,
        )
        assert [call.args for call in adb.tap.call_args_list] == [
            (816, 426 + CLOCK_TOWER_BELOW, DISPLAY),
            (976, 700, DISPLAY),
            (804, 614, DISPLAY),
        ]
        assert report.outcome == "boosted"  # type: ignore[attr-defined]
        assert report.frame.name == "after.png"  # type: ignore[attr-defined]

    def test_a_boost_is_not_tried_where_its_marker_is_not(self) -> None:
        """Off the builder base, or on it while the boost cools down and costs gems."""
        for frame, outcome in (("hud_day.png", "not_night"), ("hud_night.png", "not_ready")):
            adb, report = self._run([frame], commands.boost)
            adb.tap.assert_not_called()
            assert report.outcome == outcome, frame  # type: ignore[attr-defined]
            # The rule `tap` follows: `after` only when something was pressed.
            assert report.frame.name == "refused.png", frame  # type: ignore[attr-defined]

    def test_a_tower_tap_that_opened_no_free_boost_presses_nothing_more(self) -> None:
        adb, report = self._run(["night_clock_ready.png", "hud_night.png"], commands.boost)
        assert adb.tap.call_count == 1
        assert report.outcome == "no_free_boost"  # type: ignore[attr-defined]

    def test_a_boost_press_is_never_repeated(self) -> None:
        """Whatever stands after it, the button is pressed once and no other sheet is answered."""
        for after, outcome in (
            ("night_clock_selected.png", "not_taken"),
            ("night_sheet_hero.png", "unexpected"),
        ):
            adb, report = self._run(
                ["night_clock_ready.png", "night_clock_selected.png", after], commands.boost
            )
            assert adb.tap.call_count == 2
            assert report.outcome == outcome, after  # type: ignore[attr-defined]

    def test_a_boost_sheet_that_stays_is_not_pressed_again(self) -> None:
        adb, report = self._run(
            ["night_clock_ready.png", "night_clock_selected.png", "night_clock_boost_sheet.png"],
            commands.boost,
        )
        assert adb.tap.call_count == 3
        assert report.outcome == "not_taken"  # type: ignore[attr-defined]

    def test_the_boost_sheets_lime_button_takes_no_tap_anywhere_on_it(self) -> None:
        """Its top half and right edge took a tap before, which only `boost` should press (#457)."""
        for point in ((804, 580), (804, 609), (881, 640), (700, 660)):
            adb, report = self._run(
                ["night_clock_boost_sheet.png"], lambda d, p=point: commands.tap(p, d)
            )
            adb.tap.assert_not_called()
            assert report.outcome == "green_button", point  # type: ignore[attr-defined]

    def test_home_village_grass_in_that_lime_does_not_block_a_map_tap(self) -> None:
        """Its patches run past 100 000 px; only their fill keeps them from reading as a button."""
        for frame in ("day_shield_over_icon.png", "home_builders_busy.png"):
            adb, report = self._run([frame, frame], lambda d: commands.tap((800, 450), d))
            adb.tap.assert_called_once_with(800, 450, DISPLAY)
            assert report.outcome == "pressed", frame  # type: ignore[attr-defined]

    def test_a_running_row_opens_and_its_sheet_keeps_the_book_out_of_reach(self) -> None:
        """Opened ahead of time, a hero's sheet turns into the next level's as its countdown ends."""
        adb, report = self._run(
            ["hud_day_build_panel.png", "sheet_running_book.png"],
            lambda d: commands.tap((1300, 90), d),
        )
        adb.tap.assert_called_once_with(1300, 90, DISPLAY)
        assert report.outcome == "pressed"
        adb, report = self._run(["sheet_running_book.png"], lambda d: commands.tap((867, 785), d))
        adb.tap.assert_not_called()
        assert report.outcome == "sheet_up"

    def test_a_running_researchs_sheet_takes_only_its_x(self) -> None:
        """The lab's row opens the same kind of sheet: 立即完成 for gems, and 戰鬥之書."""
        for point in ((867, 785), (1121, 785)):
            adb, report = self._run(
                ["sheet_running_research.png"], lambda d, p=point: commands.tap(p, d)
            )
            adb.tap.assert_not_called()
            assert report.outcome == "sheet_up", point

    def test_a_sheet_takes_its_x_and_nothing_else(self) -> None:
        """Its green button is 立即完成 for gems on this one: buying is `confirm`'s."""
        adb, report = self._run(["sheet_running_gems.png"], lambda d: commands.tap((1121, 783), d))
        adb.tap.assert_not_called()
        assert report.outcome == "sheet_up"
        adb, report = self._run(
            ["sheet_running_gems.png", "hud_day_build_panel.png"],
            lambda d: commands.tap((1413, 70), d),
        )
        adb.tap.assert_called_once_with(1413, 70, DISPLAY)
        assert report.outcome == "pressed"

    def test_the_builder_base_sheet_takes_its_own_x(self) -> None:
        """Pale enough to pass for a dialog, so it is read as a sheet first."""
        adb, report = self._run(["night_sheet_red.png"], lambda d: commands.tap((805, 789), d))
        adb.tap.assert_not_called()
        assert report.outcome == "sheet_up"
        adb, report = self._run(
            ["night_sheet_red.png", "night_menu_finishing.png"],
            lambda d: commands.tap((1340, 57), d),
        )
        assert report.outcome == "pressed"

    def test_the_labs_running_research_window_takes_its_x_and_nothing_else(self) -> None:
        """Beside its X stand a gem 立即完成 and a research potion, a magic item."""
        for point in ((1215, 315), (1215, 215), (700, 700)):
            adb, report = self._run(
                ["night_lab_researching.png"], lambda d, p=point: commands.tap(p, d)
            )
            adb.tap.assert_not_called()
            assert report.outcome == "sheet_up", point
        adb, report = self._run(
            ["night_lab_researching.png", "night_lab_panel.png"],
            lambda d: commands.tap((1318, 62), d),
        )
        adb.tap.assert_called_once_with(1318, 62, DISPLAY)
        assert report.outcome == "pressed"

    def test_a_research_that_opens_its_running_window_is_confirmed(self) -> None:
        """The builder base lab answers 確認 with 升級部隊, not with its panel (#492)."""
        adb, report = self._run(
            ["night_sheet_lab.png", "night_lab_researching.png"],
            lambda d: commands.confirm(3440000, d),
        )
        adb.tap.assert_called_once()
        assert report.outcome == "confirmed"

    def test_a_home_research_whose_sheet_turns_into_the_running_one_is_confirmed(self) -> None:
        """The home lab keeps its sheet up as 正在將X升至N級, with a gem 立即完成 (#527)."""
        adb, report = self._run(
            ["sheet_pet_dark.png", "sheet_running_after_confirm.png"],
            lambda d: commands.confirm(32000, d),
        )
        adb.tap.assert_called_once()
        assert report.outcome == "confirmed"

    def test_confirm_presses_the_builder_base_button_in_the_middle(self) -> None:
        adb, report = self._run(
            ["night_sheet_elixir.png", "night_menu_finishing.png"],
            lambda d: commands.confirm(4000, d),
        )
        adb.tap.assert_called_once_with(805, 789, DISPLAY)
        assert report.outcome == "confirmed"

    def test_confirm_presses_nothing_on_a_builder_base_price_in_red(self) -> None:
        adb, report = self._run(["night_sheet_red.png"], lambda d: commands.confirm(2560000, d))
        adb.tap.assert_not_called()
        assert report.outcome == "cannot_afford"

    def test_the_busy_builders_gem_offer_takes_its_x_and_nothing_else(self) -> None:
        """Raised over the panel when 確認 meets every builder busy: one green button, in gems."""
        adb, report = self._run(["night_builders_busy.png"], lambda d: commands.tap((797, 570), d))
        adb.tap.assert_not_called()
        assert report.outcome == "green_button"
        adb, report = self._run(
            ["night_builders_busy.png", "night_menu_finishing.png"],
            lambda d: commands.tap((1125, 272), d),
        )
        assert report.outcome == "pressed"

    def test_the_goblin_builders_prompt_takes_its_x_and_nothing_else(self) -> None:
        """Its gem button is a green `green_buttons` misses; only `confirm --goblin` pays it."""
        for frame, point in (
            ("goblin_builder_offer.png", (1105, 545)),
            ("goblin_builder_offer.png", (1010, 520)),
            ("goblin_builder_offer.png", (1200, 580)),
            ("goblin_builder_two_buttons.png", (965, 542)),
            ("goblin_builder_two_buttons.png", (1235, 542)),
        ):
            adb, report = self._run([frame], lambda d, p=point: commands.tap(p, d))
            adb.tap.assert_not_called()
            assert report.outcome == "sheet_up", (frame, point)
        adb, report = self._run(
            ["goblin_builder_offer.png"], lambda d: commands.swipe((1430, 193), (1105, 545), d)
        )
        adb.swipe.assert_not_called()
        assert report.outcome == "sheet_up"
        adb, report = self._run(
            ["goblin_builder_offer.png", "hud_day_build_panel.png"],
            lambda d: commands.tap((1430, 193), d),
        )
        adb.tap.assert_called_once_with(1430, 193, DISPLAY)
        assert report.outcome == "pressed"

    def test_a_green_button_right_of_the_panels_edge_takes_no_tap_with_the_panel_shut(
        self,
    ) -> None:
        """The hero hall's card buttons reach x 1447; only an open panel's own rows are spared."""
        adb, report = self._run(["hero_hall.png"], lambda d: commands.tap((1328, 640), d))
        adb.tap.assert_not_called()
        assert report.outcome == "green_button"

    def test_the_bars_the_open_panel_pushes_left_still_take_no_tap(self) -> None:
        """The + by the gems opens the shop, panel or no panel."""
        adb, report = self._run(["hud_day_build_panel.png"], lambda d: commands.tap((914, 171), d))
        adb.tap.assert_not_called()
        assert report.outcome == "on_hud"

    def test_a_dialog_takes_its_cancel_and_nothing_else(self) -> None:
        adb, report = self._run(["wall_spend_dialog.png"], lambda d: commands.tap((973, 562), d))
        adb.tap.assert_not_called()
        assert report.outcome == "dialog_up"
        adb, report = self._run(
            ["wall_spend_dialog.png", "hud_day.png"], lambda d: commands.tap((623, 572), d)
        )
        assert report.outcome == "pressed"

    def test_nothing_is_pressed_in_a_battle(self) -> None:
        for act in (
            lambda d: commands.tap((800, 450), d),
            lambda d: commands.swipe((800, 450), (900, 450), d),
        ):
            adb, report = self._run(["battle_in_progress.png"], act)
            adb.tap.assert_not_called()
            adb.swipe.assert_not_called()
            assert report.outcome == "in_battle"

    def test_a_swipe_scrolls_the_open_panel_slowly(self) -> None:
        adb, report = self._run(
            ["hud_day_build_panel.png"], lambda d: commands.swipe((1385, 740), (1385, 240), d)
        )
        adb.swipe.assert_called_once_with((1385, 740), (1385, 240), commands.SWIPE_TIME, DISPLAY)
        assert report.outcome == "pressed"

    def test_no_swipe_crosses_a_sheet_even_from_its_x(self) -> None:
        for frame, x in (
            ("sheet_hero_dark.png", (1413, 70)),
            ("night_lab_researching.png", (1318, 62)),
        ):
            adb, report = self._run([frame], lambda d, at=x: commands.swipe(at, (1215, 315), d))
            adb.swipe.assert_not_called()
            assert report.outcome == "sheet_up", frame

    def test_confirm_presses_the_resource_button_once_when_the_price_agrees(self) -> None:
        adb, report = self._run(
            ["sheet_hero_dark.png", "hud_day_build_panel.png"],
            lambda d: commands.confirm(10800, d),
        )
        adb.tap.assert_called_once()
        assert adb.tap.call_args.args[0] > 1000
        assert report.outcome == "confirmed"
        assert report.offer.resource == "dark"
        assert report.frame.name == "after.png"

    def test_confirm_presses_nothing_on_a_price_it_was_not_given(self) -> None:
        adb, report = self._run(["sheet_hero_dark.png"], lambda d: commands.confirm(13500, d))
        adb.tap.assert_not_called()
        assert report.outcome == "price_mismatch"

    def test_confirm_presses_nothing_on_a_gem_button(self) -> None:
        adb, report = self._run(["sheet_running_gems.png"], lambda d: commands.confirm(500, d))
        adb.tap.assert_not_called()
        assert report.outcome == "not_a_resource"

    def test_confirm_presses_nothing_without_a_sheet(self) -> None:
        adb, report = self._run(["hud_day.png"], lambda d: commands.confirm(10800, d))
        adb.tap.assert_not_called()
        assert report.outcome == "no_sheet"
        assert report.frame.name == "refused.png"

    def test_confirm_presses_nothing_on_a_price_written_in_red(self) -> None:
        """Red is the game saying the village cannot pay; the press would open the gem shop."""
        image = Image.open(FRAMES / "sheet_hero_dark.png").convert("RGB")
        pixels = image.load()
        for x in range(1000, 1190):
            for y in range(765, 803):
                red, green, blue = pixels[x, y]
                if red > 225 and green > 200 and blue < 150:
                    pixels[x, y] = (255, 110, 100)
        out = io.BytesIO()
        image.save(out, format="PNG")
        adb = _adb()
        adb.screenshot.return_value = out.getvalue()
        with (
            patch.object(commands, "_attached", return_value=adb),
            patch.object(commands.time, "sleep"),
        ):
            report = commands.confirm(10800, _frames(self))
        adb.tap.assert_not_called()
        assert report.outcome == "cannot_afford"
        assert report.offer is not None
        assert report.offer.price == 10800

    def test_a_dialog_after_the_press_gets_cancel_and_nothing_more(self) -> None:
        """Not enough resources or every builder busy, both answered with a gem offer."""
        adb, report = self._run(
            ["sheet_hero_dark.png", "wall_spend_dialog.png", "hud_day_build_panel.png"],
            lambda d: commands.confirm(10800, d),
        )
        assert adb.tap.call_count == 2
        assert adb.tap.call_args.args == (623, 572, DISPLAY)
        assert report.outcome == "refused_by_game"

    def test_a_dialog_that_slides_in_after_one_clear_look_still_gets_cancel(self) -> None:
        adb, report = self._run(
            [
                "sheet_hero_dark.png",
                "hud_day_build_panel.png",
                "wall_spend_dialog.png",
                "hud_day_build_panel.png",
            ],
            lambda d: commands.confirm(10800, d),
        )
        assert adb.tap.call_args.args == (623, 572, DISPLAY)
        assert report.outcome == "refused_by_game"

    def test_the_goblin_builder_is_hired_only_at_the_gems_asked(self) -> None:
        """Its one button is gems; `--goblin` presses it at exactly `--price` of them."""
        adb, report = self._run(
            ["goblin_builder_offer.png", "hud_day_build_panel.png"],
            lambda d: commands.confirm(15, d, goblin=True),
        )
        adb.tap.assert_called_once_with(1105, 545, DISPLAY)
        assert report.outcome == "confirmed"
        assert report.goblin is not None
        assert report.goblin.gems == 15
        adb, report = self._run(
            ["goblin_builder_offer.png"], lambda d: commands.confirm(14, d, goblin=True)
        )
        adb.tap.assert_not_called()
        assert report.outcome == "price_mismatch"

    def test_beside_a_second_button_only_the_goblins_is_pressed(self) -> None:
        """空出一個建築工人, on the right, finishes a running job for gems; it is never read or pressed."""
        adb, report = self._run(
            ["goblin_builder_two_buttons.png", "hud_day_build_panel.png"],
            lambda d: commands.confirm(71, d, goblin=True),
        )
        (x, y, _), _ = adb.tap.call_args
        assert 848 < x < 1085
        assert 490 < y < 595
        assert report.outcome == "confirmed"
        adb, report = self._run(
            ["goblin_builder_two_buttons.png"], lambda d: commands.confirm(29, d, goblin=True)
        )
        adb.tap.assert_not_called()
        assert report.outcome == "price_mismatch"

    def test_no_other_gem_button_is_taken_for_the_goblin_builder(self) -> None:
        """所有建築工人都在忙碌中 finishes another job for gems; nothing else hires a goblin."""
        # The home village's asked 103, under the cap, when the goblin was busy too.
        for frame, gems in (
            ("night_builders_busy.png", 284),
            ("home_builders_busy_offer.png", 103),
            ("sheet_running_gems.png", 284),
            ("hud_day.png", 284),
        ):
            adb, report = self._run([frame], lambda d, n=gems: commands.confirm(n, d, goblin=True))
            adb.tap.assert_not_called()
            assert report.outcome == "no_goblin", frame

    def test_a_press_that_raises_the_goblin_prompt_names_it_with_its_gems(self) -> None:
        """Every builder busy but an event's goblin: the hire is the agent's call, so nothing more is pressed."""
        for frame, gems in (
            ("goblin_builder_offer.png", 15),
            ("goblin_builder_two_buttons.png", 71),
        ):
            adb, report = self._run(
                ["sheet_hero_dark.png", frame], lambda d: commands.confirm(10800, d)
            )
            adb.tap.assert_called_once()
            assert report.outcome == "goblin_offered", frame
            assert report.goblin is not None
            assert report.goblin.gems == gems
            assert report.offer.price == 10800
            line = commands.confirm_line(report)
            assert line.startswith("確認鈕 10,800 黑水,預期 10,800:"), line
            assert line.endswith(f"聘請鈕 {gems} 寶石"), line

    def test_the_busy_builders_gem_offer_after_a_press_stays_unexpected(self) -> None:
        """It finishes another job for gems, and nothing reads it apart from any other screen."""
        adb, report = self._run(
            ["sheet_hero_dark.png", "home_builders_busy_offer.png"],
            lambda d: commands.confirm(10800, d),
        )
        adb.tap.assert_called_once()
        assert report.outcome == "unexpected"
        assert report.goblin is None

    def test_a_sheet_still_up_after_the_press_is_not_pressed_again(self) -> None:
        adb, report = self._run(["sheet_hero_dark.png"], lambda d: commands.confirm(10800, d))
        adb.tap.assert_called_once()
        assert report.outcome == "sheet_stayed"

    def test_a_screen_nothing_recognises_after_the_press_is_left_alone(self) -> None:
        adb, report = self._run(
            ["sheet_hero_dark.png", "army_screen.png"], lambda d: commands.confirm(10800, d)
        )
        adb.tap.assert_called_once()
        assert report.outcome == "unexpected"

    def test_a_walls_batch_is_paid_through_its_button_and_then_its_dialog(self) -> None:
        adb, report = self._run(
            ["night_wall_batch.png", "night_wall_dialog.png", "night_wall_menu.png"],
            lambda d: commands.confirm(1600000, d, "elixir"),
        )
        assert [call.args for call in adb.tap.call_args_list] == [
            (671, 790, DISPLAY),
            (973, 560, DISPLAY),
        ]
        assert report.outcome == "confirmed"
        assert report.wall is not None
        assert (report.wall.price, report.wall.resource) == (1600000, "elixir")
        assert report.offer is None

    def test_a_single_wall_is_paid_through_its_button_and_then_its_sheet(self) -> None:
        adb, report = self._run(
            ["night_wall_menu.png", "night_sheet_wall.png", "night_wall_menu.png"],
            lambda d: commands.confirm(800000, d, "elixir"),
        )
        assert [call.args for call in adb.tap.call_args_list] == [
            (671, 790, DISPLAY),
            (805, 789, DISPLAY),
        ]
        assert report.outcome == "confirmed"
        assert report.offer is not None
        assert report.offer.price == 800000

    def test_a_wall_button_with_another_price_is_not_pressed(self) -> None:
        adb, report = self._run(
            ["night_wall_menu.png"], lambda d: commands.confirm(1600000, d, "elixir")
        )
        adb.tap.assert_not_called()
        assert report.outcome == "price_mismatch"
        assert report.wall is not None
        assert report.frame.name == "refused.png"

    def test_a_wall_button_written_in_red_is_not_pressed(self) -> None:
        adb, report = self._run(
            ["night_wall_batch.png"], lambda d: commands.confirm(1600000, d, "gold")
        )
        adb.tap.assert_not_called()
        assert report.outcome == "cannot_afford"

    def test_no_wall_menu_or_one_under_a_standing_dialog_presses_nothing(self) -> None:
        """The row-move mode, a dialog already standing, a panel with no wall selected."""
        for frame in ("night_wall_row.png", "night_wall_dialog.png", "hud_night_build_panel.png"):
            adb, report = self._run([frame], lambda d: commands.confirm(1600000, d, "elixir"))
            adb.tap.assert_not_called()
            assert report.outcome == "no_wall_menu", frame

    def test_a_dialog_or_a_sheet_already_up_takes_the_wall_menu_away(self) -> None:
        """The dialog `_raised` presses has to be one this raised, whatever shows under it."""
        for name, standing in (
            ("game_dialog", GameDialog(confirm=(973, 560), cancel=(623, 572))),
            ("sheet_open", "night"),
        ):
            with patch.object(commands, name, return_value=standing):
                adb, report = self._run(
                    ["night_wall_menu.png"], lambda d: commands.confirm(800000, d, "elixir")
                )
            adb.tap.assert_not_called()
            assert report.outcome == "no_wall_menu", name

    def test_a_sheet_in_the_other_resource_is_left_unpressed(self) -> None:
        adb, report = self._run(
            ["night_wall_menu.png", "night_sheet_wall.png"],
            lambda d: commands.confirm(800000, d, "gold"),
        )
        adb.tap.assert_called_once_with(497, 790, DISPLAY)
        assert report.outcome == "sheet_disagrees"

    def test_a_sheet_written_in_red_after_the_wall_button_is_left_unpressed(self) -> None:
        red = SheetOffer(price=800000, resource="elixir", point=(805, 789), affordable=False)
        with patch.object(commands, "sheet_offer", return_value=red):
            adb, report = self._run(
                ["night_wall_menu.png", "night_sheet_wall.png"],
                lambda d: commands.confirm(800000, d, "elixir"),
            )
        adb.tap.assert_called_once_with(671, 790, DISPLAY)
        assert report.outcome == "sheet_disagrees"

    def test_a_sheet_that_disagrees_with_the_wall_button_is_left_unpressed(self) -> None:
        adb, report = self._run(
            ["night_wall_menu.png", "night_sheet_elixir.png"],
            lambda d: commands.confirm(800000, d, "elixir"),
        )
        adb.tap.assert_called_once_with(671, 790, DISPLAY)
        assert report.outcome == "sheet_disagrees"
        assert report.offer is not None
        assert report.offer.price == 4000

    def test_a_wall_button_that_raises_nothing_says_so(self) -> None:
        adb, report = self._run(
            ["night_wall_menu.png"], lambda d: commands.confirm(800000, d, "elixir")
        )
        adb.tap.assert_called_once_with(671, 790, DISPLAY)
        assert report.outcome == "no_confirmation"

    def test_busy_builders_after_a_walls_dialog_are_left_alone(self) -> None:
        """The game answers 確定 with its gem offer when every builder is busy."""
        adb, report = self._run(
            ["night_wall_batch.png", "night_wall_dialog.png", "night_builders_busy.png"],
            lambda d: commands.confirm(1600000, d, "elixir"),
        )
        assert adb.tap.call_count == 2
        assert report.outcome == "unexpected"


class SurveyRunnerTests(unittest.TestCase):
    """The two battles spent measuring rather than fighting."""

    def _boundary(self) -> commands._BoundarySurvey:
        return commands._BoundarySurvey(
            adb=_controller(), display=DISPLAY, thresholds=LootThresholds()
        )

    def _map(self) -> commands._MapSurvey:
        return commands._MapSurvey(adb=_controller(), display=DISPLAY, thresholds=LootThresholds())

    def test_a_survey_another_device_took_over_stands_down(self) -> None:
        """Otherwise it hands back rays measured under the dialog and exits 0."""
        taken = AttackReport(outcome="session_taken")
        for command, survey in (
            (commands.probe, commands._BoundarySurvey),
            (commands.bounds, commands._MapSurvey),
        ):
            with (
                patch.object(commands, "_session", return_value=(_controller(), DISPLAY)),
                patch.object(commands, "_village_now", return_value="day"),
                patch.object(survey, "run", return_value=taken),
                pytest.raises(commands.SessionTakenError),
            ):
                command(_frames(self))

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
            patch.object(commands, "_village_now", return_value="day"),
            patch.object(commands._BoundarySurvey, "run"),
            patch.object(commands._MapSurvey, "run"),
        ):
            assert commands.probe(_frames(self)).outcome == "surveyed"
            assert commands.bounds(_frames(self)).outcome == "surveyed"

    def test_the_surveys_stop_on_the_builder_base_before_spending_anything(self) -> None:
        """Both measure the home village's battlefield, and nothing here sails to it."""
        with (
            patch.object(commands.time, "sleep"),
            patch.object(commands, "_controller", return_value=_adb()),
            patch.object(commands, "current_world", return_value="night"),
            patch.object(commands, "_BoundarySurvey") as probe,
            patch.object(commands, "_MapSurvey") as bounds,
        ):
            assert commands.probe(_frames(self)).outcome == "on_night"
            assert commands.bounds(_frames(self)).outcome == "on_night"
        probe.assert_not_called()
        bounds.assert_not_called()

    def test_every_survey_outcome_has_a_line_of_its_own(self) -> None:
        own = {"surveyed": "量完了", "on_night": "遊戲停在夜世界"}
        assert set(own) == set(get_args(SurveyOutcome))
        for outcome, fragment in own.items():
            assert fragment in commands.SURVEY_LINES[outcome], outcome
            others = [commands.SURVEY_LINES[other] for other in own if other != outcome]
            assert not any(fragment in other for other in others), outcome


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
        stack = [
            patch.object(commands, "_controller", return_value=self.adb),
            patch.object(commands, "_settle_game", return_value=DISPLAY),
            patch.object(commands, "_tables", return_value=_tables()),
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
            patch.object(commands, "window_open", return_value=screens.get("window", True)),
            patch.object(commands, "settings_open", return_value=screens.get("settings", True)),
            patch.object(commands, "more_settings_open", return_value=screens.get("more", True)),
            patch.object(commands, "export_row", return_value=screens.get("row", (1131, 560))),
            patch.object(commands, "read_clipboard", side_effect=reads),
            patch.object(commands, "write_clipboard") as self.wrote,
            patch.object(commands, "clear_clipboard") as cleared,
        ):
            return commands.export(_frames(self)), cleared

    def test_the_village_is_merged_with_the_tables_and_written_down(self) -> None:
        report, cleared = self._export()
        assert report.outcome == "exported"
        village = report.export
        assert village is not None
        assert village.tag == "#TEST"
        # The game's own export time, not this machine's clock, on the local clock.
        moment = datetime.fromtimestamp(1788624472, UTC).astimezone()
        assert village.exported_at == moment.isoformat()
        assert village.timestamp == 1788624472
        assert village.boosts.clocktower_cooldown == 77842
        names = {row.data: row.zh for _, _, row in village.rows()}
        # Named where the game's tables have it, None where they do not —
        # never the number written into the name.
        assert names == {1000001: "大本營", 90000000: None}
        assert village.halls == {"town_hall": 16}
        cleared.assert_called_once()
        # One file a day, named for the day the game took it on, here and now.
        written = (self.saved / f"#TEST_{moment:%Y-%m-%d}.json").read_text(encoding="utf-8")
        assert VillageExport.model_validate_json(written) == village

    def test_with_no_tables_to_be_had_the_day_is_still_saved(self) -> None:
        with patch.object(commands, "_tables", return_value=None):
            report, _ = self._export()
        assert report.outcome == "exported"
        [path] = self.saved.glob("*.json")
        written = VillageExport.model_validate_json(path.read_text(encoding="utf-8"))
        assert (written.game_version, written.halls) == (None, {})
        assert [row.zh for _, _, row in written.rows()] == [None, None]

    def _save_old(self) -> Path:
        """A file in the shape saved before the merge, as the real ones are."""
        old = {
            "tag": "#TEST",
            "exported_at": "2026-11-01T12:00:00+00:00",
            "timestamp": 1793534400,
            "entities": [
                {
                    "data_id": 1000001,
                    "level": 16,
                    "count": 1,
                    "section": "buildings",
                    "name": "TH",
                },
                {"data_id": 52000379, "level": None, "count": 1, "section": "skins", "name": None},
            ],
            "boosts": {},
            "outcome": "exported",
        }
        path = self.saved / "#TEST_2026-11-01.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        return path

    def test_an_old_file_is_converted_where_it_lies_when_the_village_is_exported(self) -> None:
        old = self._save_old()
        self._export()
        converted = VillageExport.model_validate_json(old.read_text(encoding="utf-8"))
        assert converted.timestamp == 1793534400
        assert [(section, row.data, row.zh) for _, section, row in converted.rows()] == [
            ("buildings", 1000001, "大本營")
        ]

    def test_reading_back_converts_an_old_file_with_the_tables_kept_on_disk(self) -> None:
        old = self._save_old()
        kept = gamedata.GAME_DATA_DIR / "1.2.3"
        kept.mkdir(parents=True)
        (kept / "entities.json").write_text(_tables().model_dump_json(), encoding="utf-8")
        report = commands.export(_frames(self), last=True)
        assert report.outcome == "read_back"
        assert report.export is not None
        assert report.export.game_version == "1.2.3"
        assert VillageExport.model_validate_json(old.read_text(encoding="utf-8")) == report.export

    def test_an_old_file_with_no_tables_kept_is_left_as_it_is(self) -> None:
        """Converting drops the name the old file carried, so it waits for tables."""
        old = self._save_old()
        before = old.read_text(encoding="utf-8")
        with self.assertLogs("ai_coc.commands", "WARNING"):
            report = commands.export(_frames(self), last=True)
        assert report.outcome == "wrong_format"
        assert old.read_text(encoding="utf-8") == before

    def test_growth_compares_the_saved_exports_inside_the_dates_asked_for(self) -> None:
        """Both days count: the baseline is the last export before the start day, the end the last on the end day.

        Stamped at noon UTC, so every timezone from UTC-11 to UTC+11 reads the same days.
        """

        def save(name: str, day: int, level: int) -> None:
            at = int(datetime(2026, 11, day, 12, tzinfo=UTC).timestamp())
            document = VillageDocument.model_validate({
                "tag": "#TEST",
                "heroes": [{"data": 28000000, "lvl": level}],
            })
            export = merge(document, _tables(), at)
            (self.saved / name).write_text(export.model_dump_json(), encoding="utf-8")

        save("#TEST_2026-11-01.json", 1, 50)
        save("#TEST_2026-11-03.json", 3, 51)
        save("#TEST_2026-11-05.json", 5, 53)
        report = commands.growth()
        assert (report.outcome, report.changes[0].before, report.changes[0].after) == (
            "compared",
            {50: 1},
            {53: 1},
        )
        # The 3rd's upgrade happened on the 3rd, so starting there still counts it.
        third = commands.growth(start=date(2026, 11, 3), end=date(2026, 11, 3))
        assert (third.changes[0].before, third.changes[0].after) == ({50: 1}, {51: 1})
        assert commands.growth(start=date(2026, 11, 6)).outcome == "too_few"
        assert commands.growth(end=date(2026, 11, 3)).changes[0].after == {51: 1}
        text = commands.growth_text(report)
        assert "日世界\n  建築 (含英雄)\n    野蠻人之王  50 → 53" in text
        assert "夜世界\n  (沒有升級)" in text

    def test_it_takes_the_far_zoom_without_parking(self) -> None:
        """Every step of the walk is a fixed coordinate on a full-screen page."""
        with patch.object(commands, "_settle_game", return_value=DISPLAY) as settle_game:
            self._export()
        assert settle_game.call_args.kwargs == {"park": False}

    def test_a_game_that_never_came_back_is_not_tapped_at(self) -> None:
        """`_settle_game` answers None for a dropped session it could not restart,
        a game still loading, and an emulator with no window yet. The avatar is a
        fixed coordinate, so none of those may be tapped on.
        """
        with patch.object(commands, "_settle_game", return_value=None):
            report = commands.export(_frames(self))
        assert report.outcome == "no_village"
        self.adb.tap.assert_not_called()
        self.adb.swipe.assert_not_called()

    def test_a_menu_that_did_not_open_stops_before_the_next_tap(self) -> None:
        """The whole point of reading: the second tap is a fixed coordinate."""
        report, _ = self._export(settings=False)
        assert report.outcome == "settings_shut"
        assert report.export is None
        # The avatar, the tab, and then the close — never 更多設定 at a screen
        # nobody confirmed.
        assert [call.args[:2] for call in self.adb.tap.call_args_list] == [
            commands.PROFILE_AVATAR,
            commands.SETTINGS_TAB,
            commands.CLOSE_SETTINGS,
        ]

    def test_a_profile_window_that_never_opened_is_not_closed(self) -> None:
        """With no window up, the close sits over the gold bar."""
        report, _ = self._export(window=False)
        assert report.outcome == "settings_shut"
        assert [call.args[:2] for call in self.adb.tap.call_args_list] == [commands.PROFILE_AVATAR]

    def test_a_list_that_never_reaches_the_row_is_never_tapped(self) -> None:
        """A missed scroll leaves the copy coordinate over a settings toggle."""
        report, cleared = self._export(row=None)
        assert report.outcome == "row_not_found"
        cleared.assert_not_called()
        assert self.adb.swipe.call_count == commands.SCROLL_TRIES
        assert (1131, 560) not in [call.args[:2] for call in self.adb.tap.call_args_list]

    def test_an_empty_clipboard_is_reported_rather_than_read_as_a_village(self) -> None:
        """Cleared first, so nothing arriving really is nothing copied."""
        with patch.object(commands, "CLIPBOARD_POLLS", 2):
            report, cleared = self._export(clip="")
        assert report.outcome == "nothing_copied"
        cleared.assert_called_once()
        assert not list(self.saved.glob("*.json"))

    def test_whatever_was_on_the_clipboard_goes_back_on_it(self) -> None:
        """It belongs to whoever is at the keyboard, and this runs while they are working."""
        held = "something the user was copying"
        report, _ = self._export(clip=[held, PAYLOAD])
        assert report.outcome == "exported"
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
        assert report.outcome == "not_an_export"
        assert not list(self.saved.glob("*.json"))

    def test_the_settings_window_is_closed_even_when_the_export_failed(self) -> None:
        """Every other command starts by assuming nothing is covering the village."""
        self._export(more=False)
        assert self.adb.tap.call_args_list[-1].args[:2] == commands.CLOSE_SETTINGS

    def test_a_tag_off_the_clipboard_cannot_escape_the_export_directory(self) -> None:
        """The tag is outside data: anything that parses as JSON can carry a slash."""
        when = datetime(2026, 11, 7, 4, tzinfo=UTC)
        day = when.astimezone().strftime("%Y-%m-%d")
        assert commands._export_path("#GOOD", when).parent == self.saved
        assert commands._export_path("../../etc/passwd", when).name == f"etcpasswd_{day}.json"
        assert commands._export_path("", when).name == f"UNKNOWN_{day}.json"

    def test_reading_the_last_export_says_so_when_there_is_nothing_to_read(self) -> None:
        assert commands.export(_frames(self), last=True).outcome == "never_exported"

    def test_a_file_from_before_this_command_is_named_rather_than_read_as_empty(self) -> None:
        """The old shape held the game's raw payload, which parses as a village with nothing in it."""
        (self.saved / "#OLD.json").write_text(PAYLOAD, encoding="utf-8")
        report = commands.export(_frames(self), last=True)
        assert report.outcome == "wrong_format"
        assert report.export is None


def _tower(data_id: int, zh: str, en: str, world: World, max_lvl: int) -> GameEntity:
    return GameEntity(
        data_id=data_id,
        zh=zh,
        en=en,
        world=world,
        table="buildings",
        max_lvl=max_lvl,
        levels=[
            GameLevel(lvl=lvl, needs={"town_hall": lvl}, cost={"gold": lvl * 100}, time=lvl * 60)
            for lvl in range(1, max_lvl + 1)
        ],
    )


def _search(
    query: str = "",
    section: SearchSection | Literal["all"] | None = None,
    world: World | None = None,
    skip_max_lvl: bool = False,
    every_row: bool = False,
) -> SearchReport:
    """`commands.search` asked for `query` under whichever filters are named."""
    return commands.search(
        SearchOptions(
            query=query,
            section=section,
            world=world,
            skip_max_lvl=skip_max_lvl,
            every_row=every_row,
        )
    )


class SearchTests(unittest.TestCase):
    """`search` over tables and an export kept in the test's own directories, which conftest points the code at."""

    TABLES = GameEntities(
        game_version="1.2.3",
        reading=READING,
        halls={"town_hall": 1000001, "builder_barracks": 1000040, "siege_workshop": 1000059},
        entities=[
            _tower(1000009, "箭塔", "Archer Tower", "day", 5),
            _tower(1000048, "箭塔", "Archer Tower", "night", 5),
            _tower(1000084, "多人箭塔", "Multi-Archer Tower", "day", 4),
            _tower(1000040, "建築大師訓練營", "Builder Barracks", "night", 12),
            _tower(1000059, "攻城機器工坊", "Workshop", "day", 9),
            GameEntity(
                data_id=4000070,
                zh="飛豬騎士",
                en="Hog Glider",
                world="night",
                table="characters",
                max_lvl=16,
                levels=[
                    GameLevel(lvl=15, needs={"builder_barracks": 11}),
                    GameLevel(lvl=16, needs={"star_laboratory": 9}, cost={"elixir": 5}, time=60),
                ],
            ),
            GameEntity(
                data_id=4000051,
                zh="攻城戰車",
                en="Wall Wrecker",
                world="day",
                table="characters",
                max_lvl=1,
                levels=[GameLevel(lvl=1, needs={"siege_workshop": 1})],
            ),
            # A super troop, which these tables give nothing to unlock it by.
            GameEntity(
                data_id=4000063,
                zh="地獄飛龍",
                en="Inferno Dragon",
                world="day",
                table="characters",
                max_lvl=1,
                levels=[GameLevel(lvl=1)],
            ),
            GameEntity(
                data_id=4000059,
                zh="雷電飛龍",
                en="Electro Dragon",
                world="day",
                table="characters",
                max_lvl=9,
                levels=[GameLevel(lvl=1), GameLevel(lvl=2, cost={"elixir": 6}, time=345600)],
            ),
            GameEntity(
                data_id=93000000,
                zh="建築工人學徒",
                en="Builder's Apprentice",
                world="day",
                table="villager_apprentices",
                max_lvl=8,
                levels=[],
            ),
        ],
    )
    DOCUMENT = VillageDocument.model_validate({
        "tag": "#TEST",
        "buildings": [
            {"data": 1000084, "lvl": 2, "cnt": 2},
            {"data": 1000084, "lvl": 3, "timer": 600, "extra": True},
            {"data": 1000084, "lvl": 2, "timer": 60},
            # A second level 2 being upgraded, whose next upgrade is the same as the first's.
            {"data": 1000084, "lvl": 2, "timer": 300},
            # The town hall, which these tables name as a hall and do not list.
            {"data": 1000001, "lvl": 3},
        ],
        "units": [{"data": 4000059, "lvl": 1}],
        "helpers": [{"data": 93000000, "lvl": 8, "helper_cooldown": 120}],
        "buildings2": [{"data": 1000048, "lvl": 2}, {"data": 1000040, "lvl": 10}],
    })
    AT = 1793534400

    def _keep(
        self, tables: bool = True, export: bool = True, document: VillageDocument = DOCUMENT
    ) -> None:
        if tables:
            folder = gamedata.GAME_DATA_DIR / "1.2.3"
            folder.mkdir(parents=True)
            (folder / "entities.json").write_text(self.TABLES.model_dump_json(), encoding="utf-8")
        if export:
            village = merge(document, self.TABLES, self.AT)
            path = commands.ACCOUNT_JSON_DIR / "#TEST_2026-11-01.json"
            path.write_text(village.model_dump_json(), encoding="utf-8")

    def test_a_number_is_one_data_id_and_anything_else_a_piece_of_either_name(self) -> None:
        self._keep()
        for query in ("4000059", "雷電飛龍", "電飛", "electro DRAGON"):
            assert [hit.data_id for hit in _search(query).hits] == [4000059], query
        # Every hit, the player's own first: the night tower and the Multi-Archer
        # Tower are owned, the home village's tower is not.
        found = _search("箭塔")
        assert (found.outcome, [hit.data_id for hit in found.hits]) == (
            "found",
            [1000048, 1000084, 1000009],
        )
        assert (found.game_version, found.tag) == ("1.2.3", "#TEST")
        # A number is never a piece of a name or of another number.
        assert _search("40000").outcome == "no_match"

    def test_the_players_rows_of_one_data_id_are_summed_up(self) -> None:
        self._keep()
        [owned] = _search("多人箭塔").hits
        assert (owned.count, owned.owned, owned.limited_lvl, owned.rows) == (
            5,
            {2: 4, 3: 1},
            3,
            None,
        )
        # Soonest first, each with the level it is leaving and whose slot it is on.
        assert [(one.lvl, one.extra) for one in owned.upgrading] == [
            (2, False),
            (2, False),
            (3, True),
        ]
        # The two idle level 2s can buy 3 now. The two upgrading level 2s lead
        # to 4, listed once, which a town hall 3 does not allow; the level 3 is
        # already buying the last level.
        assert [(one.lvl, one.cost, one.ready, one.waiting) for one in owned.next] == [
            (3, {"gold": 300}, 2, 0),
            (4, {"gold": 400}, 0, 0),
        ]
        assert len(owned.levels) == 4
        unowned = _search("1000009").hits[0]
        assert (unowned.count, unowned.owned, unowned.upgrading, unowned.next) == (0, {}, [], [])
        assert unowned.levels == self.TABLES.entities[0].levels

    def test_rows_gives_every_row_with_its_own_level_timer_and_slot(self) -> None:
        self._keep()
        [owned] = _search("Multi-Archer", every_row=True).hits
        assert owned.rows is not None
        assert [(row.lvl, row.cnt, row.timer, row.extra) for row in owned.rows] == [
            (2, 2, None, None),
            (3, None, 600, True),
            (2, None, 60, None),
            (2, None, 300, None),
        ]
        assert all(row.done_at for row in owned.rows if row.timer)

    def test_a_section_lists_what_the_village_holds_of_it_on_both_villages(self) -> None:
        self._keep()
        [helper] = _search("", section="helpers").hits
        assert (helper.data_id, helper.section, helper.ready_at is not None) == (
            93000000,
            "helpers",
            True,
        )
        # The town hall, which the tables lack, is placed by where its row sits.
        built = _search("", section="builder")
        assert [(hit.data_id, hit.world) for hit in built.hits] == [
            (1000048, "night"),
            (1000084, "day"),
            (1000001, "day"),
            (1000040, "night"),
        ]
        assert [hit.data_id for hit in _search("", section="lab").hits] == [4000059]
        everything = _search("", section="all").hits
        assert len(everything) == 6
        assert {hit.section for hit in everything} == {"builder", "lab", "helpers"}

    def test_every_filter_narrows_a_listing_and_a_query_the_same_way(self) -> None:
        self._keep()
        # A query reaches the tower the village lacks, inside the section asked for.
        towers = _search("箭塔", section="builder")
        assert [hit.data_id for hit in towers.hits] == [1000048, 1000084, 1000009]
        night = _search("箭塔", section="builder", world="night")
        assert [hit.data_id for hit in night.hits] == [1000048]
        assert _search("箭塔", section="lab").outcome == "no_match"
        listed = _search("", section="all", world="night")
        assert [hit.data_id for hit in listed.hits] == [1000048, 1000040]
        # One the village lacks is placed by its game table, a siege machine by
        # the workshop unlocking it.
        [wrecker] = _search("4000051").hits
        assert (wrecker.world, wrecker.kind, wrecker.section) == ("day", "siege_machines", "lab")
        [glider] = _search("4000070").hits
        assert (glider.kind, glider.section) == ("units", "lab")
        asked = _search("", section="pets", world="day", skip_max_lvl=True)
        assert (asked.outcome, asked.section, asked.world, asked.skip_max_lvl) == (
            "no_match",
            "pets",
            "day",
            True,
        )

    def test_skip_max_lvl_drops_a_kind_only_when_every_copy_is_at_its_last_level(self) -> None:
        document = VillageDocument.model_validate({
            "tag": "#TEST",
            "buildings": [
                {"data": 1000009, "lvl": 5},
                {"data": 1000009, "lvl": 4},
                {"data": 1000059, "lvl": 9},
                # The tables lack it, so nothing says where it tops out.
                {"data": 1000001, "lvl": 3},
            ],
            "helpers": [{"data": 93000000, "lvl": 8}],
        })
        self._keep(document=document)
        kept = _search("", section="all", skip_max_lvl=True)
        assert [hit.data_id for hit in kept.hits] == [1000001, 1000009]
        # Summed up the kind stays whole; row by row it loses the one at its last level.
        assert kept.hits[1].owned == {4: 1, 5: 1}
        [tower] = _search("1000009", skip_max_lvl=True, every_row=True).hits
        assert tower.rows is not None
        assert [row.lvl for row in tower.rows] == [4]
        # One the village lacks has no level to stand at.
        assert _search("多人箭塔", skip_max_lvl=True).hits[0].count == 0

    def test_each_section_lists_the_quickest_upgrade_a_copy_can_start_first(self) -> None:
        document = VillageDocument.model_validate({
            "tag": "#TEST",
            "buildings": [
                {"data": 1000009, "lvl": 2},
                {"data": 1000009, "lvl": 1, "timer": 60},
                {"data": 1000084, "lvl": 1},
                {"data": 1000084, "lvl": 2, "timer": 60},
                # Idle, but its next level needs a town hall 4.
                {"data": 1000059, "lvl": 3},
                {"data": 1000001, "lvl": 3},
            ],
        })
        self._keep(document=document)
        report = _search("", section="builder")
        # 2 minutes, then 3; then what no copy can start now, by data_id.
        assert [hit.data_id for hit in report.hits] == [1000084, 1000009, 1000001, 1000059]
        tower, workshop = report.hits[1], report.hits[-1]
        # One idle level 2 and one level 1 buying 2 both lead to 3.
        assert [(one.lvl, one.ready, one.waiting) for one in tower.next] == [(3, 1, 1)]
        assert [(one.lvl, one.ready, one.waiting) for one in workshop.next] == [(4, 0, 0)]
        text = commands.search_text(report)
        assert "  下一次 3 級 300 金幣, 3 分鐘, 現在能升 1 個, 1 個要等手上的升級做完\n" in text

    def test_one_held_under_its_last_level_by_a_hall_is_not_skipped(self) -> None:
        self._keep()
        # Its level 3 cannot buy 4 under a town hall 3, and 4 is still its last level.
        [towers] = _search("多人箭塔", skip_max_lvl=True, every_row=True).hits
        assert towers.rows is not None
        assert (towers.limited_lvl, towers.max_lvl, [row.lvl for row in towers.rows]) == (
            3,
            4,
            [2, 3, 2, 2],
        )

    def test_a_section_this_code_has_not_heard_of_is_listed_under_its_own_name(self) -> None:
        document = VillageDocument.model_validate({
            "tag": "#TEST",
            "gizmos": [{"data": 99000001, "lvl": 1}],
        })
        self._keep(document=document)
        report = _search("", section="all")
        assert [hit.section for hit in report.hits] == ["gizmos"]
        assert "-- gizmos --" in commands.search_text(report)
        assert _search("", section="builder").outcome == "no_match"

    def test_a_data_id_the_tables_lack_is_still_found_with_what_its_row_says(self) -> None:
        self._keep()
        [hit] = _search("1000001").hits
        assert (hit.zh, hit.world, hit.section, hit.table, hit.count, hit.owned) == (
            None,
            "day",
            "builder",
            None,
            1,
            {3: 1},
        )

    def test_a_section_needs_an_export_and_no_tables(self) -> None:
        self._keep(tables=False)
        report = _search("", section="lab")
        assert (report.outcome, report.game_version) == ("found", None)
        # The names were merged into the export when it was saved.
        assert report.hits[0].zh == "雷電飛龍"
        # So a name can be looked for inside one, though not across the tables.
        assert _search("雷電", section="lab").outcome == "found"
        assert _search("雷電").outcome == "no_tables"

    def test_each_way_a_search_finds_nothing_is_named(self) -> None:
        assert _search("箭塔").outcome == "no_tables"
        assert _search("箭塔", section="builder").outcome == "no_tables"
        assert _search("", section="helpers").outcome == "no_export"
        self._keep()
        nothing = _search("不存在的東西")
        assert (nothing.outcome, nothing.hits) == ("no_match", [])

    def test_without_an_export_the_tables_are_still_searched(self) -> None:
        self._keep(export=False)
        report = _search("箭塔")
        assert (report.outcome, report.exported_at, len(report.hits)) == ("found", None, 3)
        assert len(_search("箭塔", section="builder", world="day").hits) == 2
        text = commands.search_text(report)
        assert "沒有存下村莊匯出" in text
        # No export was read, so nothing is said about what the village has.
        assert "村莊裡沒有" not in text

    def test_the_text_says_what_is_owned_and_that_prices_are_list_prices(self) -> None:
        self._keep()
        text = commands.search_text(_search("箭塔"))
        assert "多人箭塔 / Multi-Archer Tower  #1000084  建築  滿級 4" in text
        assert "  有 5 個: 2 級 4 個, 3 級 1 個" in text
        # Each upgrading copy by the level it is buying.
        assert text.count("  在升到 3 級, ") == 2
        assert "  在升到 4 級, " in text
        assert "(哥布林那格)" in text
        # Level 4 needs a town hall 4 and this village's is 3.
        assert "  下一次 4 級 400 金幣, 4 分鐘 (上限 3 級)\n" in text
        assert "  下一次 3 級 300 金幣, 3 分鐘, 現在能升 2 個\n" in text
        # A building's first level is not said to unlock it.
        assert "箭塔 / Archer Tower  #1000009  建築  滿級 5\n  村莊裡沒有\n" in text
        assert text.endswith(commands.LIST_PRICE)
        every = commands.search_text(_search("多人箭塔", every_row=True))
        assert "\n  3 級 1 個  在升到 4 級, " in every
        assert "\n  2 級 2 個  下一次 3 級 300 金幣, 3 分鐘" in every
        assert " 能再派" in commands.search_text(_search("", section="helpers"))

    def test_every_section_the_code_groups_has_a_kind_of_its_own(self) -> None:
        assert set(commands.KIND_NAMES) == set(SECTIONS)
        assert len(set(commands.KIND_NAMES.values())) == len(commands.KIND_NAMES)

    def test_the_text_gives_each_village_a_block_of_its_own_split_by_section(self) -> None:
        self._keep()
        text = commands.search_text(_search("", section="all"))
        day, night = text.index("== 日世界 =="), text.index("== 夜世界 ==")
        builder, lab, helpers = (
            text.index(f"-- {name} --")
            for name in ("工人 (builder)", "實驗室 (lab)", "助手 (helpers)")
        )
        assert day < builder < lab < helpers < night < text.index("-- 工人 (builder) --", night)
        # The Builder Barracks is the night village's, the Multi-Archer Tower the day's.
        assert day < text.index("多人箭塔") < night < text.index("建築大師訓練營 /")
        # Both villages unless one is asked for, and an empty one says so.
        helped = commands.search_text(_search("", section="helpers"))
        assert helped.index("== 夜世界 ==") < helped.index("\n沒有符合的\n")
        only = commands.search_text(_search("", section="helpers", world="day"))
        assert ("== 日世界 ==" in only, "夜世界" in only) == (True, False)
        head = commands.search_text(
            _search("箭塔", section="builder", world="night", skip_max_lvl=True)
        ).splitlines()[0]
        assert head == "找 「箭塔」 --section builder --night --skip-max-lvl: 找到 1 種"

    def test_one_the_village_lacks_says_what_unlocks_it_and_where_the_village_stands(self) -> None:
        self._keep()
        # An export saved before the tables named the barracks has no level of
        # it in its own halls; the rows still have it.
        path = commands.ACCOUNT_JSON_DIR / "#TEST_2026-11-01.json"
        saved = VillageExport.model_validate_json(path.read_text(encoding="utf-8"))
        path.write_text(saved.model_copy(update={"halls": {}}).model_dump_json(), encoding="utf-8")
        [glider] = _search("飛豬騎士").hits
        assert glider.unlock == [
            SearchNeed(hall="builder_barracks", zh="建築大師訓練營", lvl=11, village=10)
        ]
        assert _search("多人箭塔").hits[0].unlock == []
        text = commands.search_text(_search("騎士"))
        assert (
            "飛豬騎士 / Hog Glider  #4000070  兵種  滿級 16\n"
            "  村莊裡沒有, 解鎖要 建築大師訓練營 11 級 (村莊裡 10 級)\n"
        ) in text
        text = commands.search_text(_search("4000051"))
        assert (
            "攻城戰車 / Wall Wrecker  #4000051  攻城機器  滿級 1\n"
            "  村莊裡沒有, 解鎖要 攻城機器工坊 1 級 (村莊裡還沒有)\n"
        ) in text
        # Nothing in the tables unlocks it, so nothing is said.
        text = commands.search_text(_search("地獄飛龍"))
        assert "  村莊裡沒有\n" in text
        # Tables an older reading built name the laboratory there, so they unlock nothing.
        older = self.TABLES.model_copy(update={"reading": READING - 1})
        kept = gamedata.GAME_DATA_DIR / "1.2.3" / "entities.json"
        kept.write_text(older.model_dump_json(), encoding="utf-8")
        assert _search("飛豬騎士").hits[0].unlock == []


class StorageCeilingTests(unittest.TestCase):
    """`StorageCapacity` is what the attack loop measures a village against; kept beside the loop."""

    def test_a_partial_ceiling_watches_only_what_it_has(self) -> None:
        assert StorageCapacity(gold=100).full(VillageStock(gold=95, elixir=0, dark=0), 90) == [
            "金幣"
        ]


if __name__ == "__main__":
    unittest.main()
