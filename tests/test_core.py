import io
import os
import json
import math
import time
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, MagicMock, call, patch
from collections.abc import Callable

from PIL import Image, ImageDraw
import pytest
from pydantic import ValidationError

from ai_coc import plans, models, commands, logging_setup
from ai_coc.ui import hero, walls, attack, upkeep
from ai_coc.ui import world as world_ui
from ai_coc.ui import runner as shared
from ai_coc.models import (
    RunLog,
    HeroCard,
    ProbeRay,
    WallMenu,
    AppConfig,
    BattleRow,
    LootOffer,
    ScoutView,
    WallBatch,
    AttackPlan,
    AttackStep,
    HeroReport,
    PlayedPlan,
    AdbEndpoint,
    RunnerState,
    ScreenPoint,
    ScreenSpots,
    WallOptions,
    WallUpgrade,
    AttackSeries,
    BuildingName,
    VillageStock,
    AttackOptions,
    DisplayTarget,
    GeminiSetting,
    LootOverrides,
    UpgradeButton,
    WallCandidate,
    BoundarySurvey,
    BuildCandidate,
    GeminiSettings,
    LootThresholds,
    StorageCapacity,
)
from ai_coc.prompts import PROMPTS, PROMPT_DIR, render
from ai_coc.ui.hero import HeroRunner
from ai_coc.ui.walls import WallRunner
from ai_coc.constants import DEFAULT_LITE_MODEL
from ai_coc.ui.attack import (
    PLAYFIELD,
    RAGE_SPAN,
    CARD_ROW_Y,
    DEPLOY_END,
    END_BATTLE,
    DROP_STRIDE,
    LINE_POINTS,
    NUDGE_REACH,
    DEPLOY_LINES,
    DEPLOY_START,
    PLAN_TIMEOUT,
    ABANDON_BUTTON,
    DROPS_PER_PASS,
    DEPLOY_ATTEMPTS,
    RESULT_ATTEMPTS,
    UNREADABLE_SKIPS,
    AttackRunner,
    merged,
    spaced,
    push_out,
    push_line,
    deploy_line,
    drop_points,
    planned_line,
    single_spots,
    deploy_candidates,
)
from ai_coc.ui.runner import SWEEP_X, SWEEP_Y, SWEEP_LIMIT, SWEEP_STAGGER
from ai_coc.ui.upkeep import UpkeepRunner
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import (
    EV_ABS,
    EV_KEY,
    EV_SYN,
    BTN_TOUCH,
    SYN_REPORT,
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_TRACKING_ID,
    AdbController,
    AdbControlError,
    pinch_events,
    focused_display,
    physical_display,
)
from ai_coc.parsers.clan import panel_top, donatable_cards, reinforce_button
from ai_coc.parsers.hero import SCROLL_LEFT, SCROLL_RIGHT, can_scroll, hero_cards, hall_buttons
from ai_coc.parsers.home import builder_jobs, free_builders, collect_bubbles
from ai_coc.logging_setup import _attach_run, configure_logging
from ai_coc.parsers.field import view_shift
from ai_coc.parsers.scout import (
    PANEL_LEFT,
    ROW_BOUNDS,
    PANEL_RIGHT,
    card_count,
    live_cards,
    read_scout,
    read_stock,
    battle_over,
    card_groups,
    field_units,
    card_drained,
    freeze_cards,
    skip_offered,
    army_strength,
    counted_cards,
    loading_screen,
    selected_cards,
    attack_menu_open,
    storage_capacity,
    idle_disconnected,
    night_attack_menu,
    read_builder_stock,
    searching_opponent,
)
from ai_coc.parsers.world import info_badges, current_world
from ai_coc.parsers.glyphs import digits_from
from ai_coc.ui.main_window import LIVE_INTERVAL, MainWindow
from ai_coc.adapters.config import ConfigStore
from ai_coc.parsers.village import parse_village
from ai_coc.adapters.secrets import dotenv_value
from ai_coc.parsers.boundary import DEPLOY_BOUND, fitted_line, village_box, boundary_reach
from ai_coc.parsers.building import (
    PRICE_TOLERANCE,
    wall_menu,
    name_strip,
    game_dialog,
    upgrade_sheet,
    upgrade_buttons,
)
from ai_coc.adapters.database import Database

FRAMES = Path(__file__).parent / "frames"


# A plan has to carry both ends of its line and each of the three lists the
# planner is asked to fill, so tests that do not care about any of them still
# have to supply them. They are required on purpose: a field with a default is
# optional in the JSON schema, and that is how a live run came back naming
# neither a hero nor a freeze point against a screen holding four hero cards.
def _step(
    act: str, *points: tuple[float, float], who: str = "unknown", seconds: int = 0
) -> AttackStep:
    """One step of a tactic, with the fields it does not use filled in anyway.

    All four are required on purpose: a field with a default is optional in the
    JSON schema, and that is how a live run came back naming neither a hero nor
    a freeze point against a screen holding four hero cards. So a step that
    needs no points still says so.
    """
    return AttackStep(
        act=act, who=who, at=[ScreenPoint(x_pct=x, y_pct=y) for x, y in points], seconds=seconds
    )


# The drop line lives on the plan's first `troops` step, so a plan that has to
# carry a line at all carries one of these.
_LINE_ENDS = ((37.5, 12.2), (14.4, 42.2))
_ANSWERED = {"steps": [_step("troops", *_LINE_ENDS)]}
_LINE = _ANSWERED

# Trimmed from a live MuMu instance: the launcher holds display 0 and the game
# sits on its own, with the logical and physical ids numbered apart.
WINDOW_DISPLAYS = """WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=3 (organized)
    mCurrentFocus=null
    mFocusedApp=null
  Display: mDisplayId=0 (organized)
    mCurrentFocus=null
    mFocusedApp=ActivityRecord{202723142 u0 app.lawnchair/.LawnchairLauncher t2}
  Display: mDisplayId=2 (organized)
    mCurrentFocus=Window{964a007 u0 com.supercell.clashofclans/com.supercell.titan.GameApp}
    mFocusedApp=ActivityRecord{14144584 u0 com.supercell.clashofclans/com.supercell.titan.GameApp}
"""

# The same instance after another app took the focus on a display of its own:
# only the topmost display keeps mCurrentFocus, so the game is left with mFocusedApp.
UNFOCUSED_DISPLAYS = """WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=2 (organized)
    mCurrentFocus=null
    mFocusedApp=ActivityRecord{14144584 u0 com.supercell.clashofclans/com.supercell.titan.GameApp}
  Display: mDisplayId=4 (organized)
    mCurrentFocus=Window{f551b42 u0 com.android.documentsui/com.android.documentsui.files.FilesActivity}
    mFocusedApp=ActivityRecord{185215537 u0 com.android.documentsui/.files.FilesActivity}
"""

DISPLAY_DEVICES = """Display Devices: size=3
  DisplayDeviceInfo{"mumuscreen000": uniqueId="local:4619827820427265280", 1080 x 1920}
  DisplayDeviceInfo{"mumuscreen001": uniqueId="local:4619827767814508545", 1080 x 1920}
  DisplayDeviceInfo{"mumuscreen002": uniqueId="local:4619826888814064386", 1080 x 1920}
Logical Displays: size=3
  mDisplayId=0
    mBaseDisplayInfo=DisplayInfo{"mumuscreen000", displayId 0, displayGroupId 0, FLAG_SECURE}
  mDisplayId=2
    mBaseDisplayInfo=DisplayInfo{"mumuscreen001", displayId 2, displayGroupId 0, FLAG_SECURE}
  mDisplayId=3
    mBaseDisplayInfo=DisplayInfo{"mumuscreen002", displayId 3, displayGroupId 0, FLAG_SECURE}
"""


class CoreTests(unittest.TestCase):
    def test_tolerant_village_and_join(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "village.json"
            source.write_text(
                json.dumps({
                    "tag": "#TEST",
                    "buildings2": [{"data": 1000055, "lvl": 8, "cnt": 2, "future": 1}],
                    "future_section": {"x": 1},
                }),
                encoding="utf-8",
            )
            snapshot = parse_village(source)
            assert snapshot.tag == "#TEST"
            assert snapshot.entities[0].count == 2
            db = Database(path=root / "test.sqlite3")
            db.save_account(snapshot)
            rows = db.account_rows("#TEST")
            assert rows[0].name == "Crusher"

    def test_village_entry_keeps_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "village.json"
            source.write_text(
                json.dumps({
                    "player_tag": "#ALIAS",
                    "units": [
                        {"id": 4000041, "level": "3", "count": None, "unreleased": True},
                        {"name": "no data id"},
                    ],
                }),
                encoding="utf-8",
            )
            snapshot = parse_village(source)
            assert snapshot.tag == "#ALIAS"
            # The row without a data_id is dropped; the other keeps its unknown field.
            assert len(snapshot.entities) == 1
            entity = snapshot.entities[0]
            assert (entity.data_id, entity.level, entity.count) == (4000041, 3, 1)
            assert (entity.model_extra or {})["unreleased"] is True

    def test_saved_snapshot_can_be_reimported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "village.json"
            source.write_text(
                json.dumps({"tag": "#TEST", "buildings2": [{"data": 1000055, "lvl": 8}]}),
                encoding="utf-8",
            )
            saved = Path(td) / "saved.json"
            saved.write_text(parse_village(source).model_dump_json(), encoding="utf-8")
            assert parse_village(saved).entities[0].data_id == 1000055

    def test_display_lookup_picks_the_game_over_the_launcher(self) -> None:
        logical = focused_display(WINDOW_DISPLAYS, "com.supercell.clashofclans")
        assert logical == "2"
        assert physical_display(DISPLAY_DEVICES, logical) == "4619827767814508545"

    def test_display_lookup_survives_another_display_taking_the_focus(self) -> None:
        assert focused_display(UNFOCUSED_DISPLAYS, "com.supercell.clashofclans") == "2"

    def test_display_lookup_is_empty_when_the_package_has_no_window(self) -> None:
        assert focused_display(WINDOW_DISPLAYS, "com.example.absent") == ""
        assert physical_display(DISPLAY_DEVICES, "9") == ""


class WorldTests(unittest.TestCase):
    """Live frames of both villages, masked to the plate row and the storage column."""

    def test_the_home_village_carries_three_plates(self) -> None:
        assert current_world((FRAMES / "world_day.png").read_bytes()) == "day"

    def test_the_builder_base_storages_are_read_two_rows_deep(self) -> None:
        """Three would read the gems bar, which sits at exactly the dark row's y.

        Measured, a builder base holding 10 152 gems reports `dark=410152`
        through `read_stock`, the green `+` beside the number reading as a
        leading 4. Here `dark` is 0, and that village's `StorageCapacity` has no
        dark ceiling either, so nothing ever compares the two.
        """
        assert read_builder_stock((FRAMES / "world_night.png").read_bytes()) == VillageStock(
            gold=574030, elixir=589419, dark=0
        )

    def test_the_builder_base_carries_two(self) -> None:
        """It has no 護盾 plate, and a real-time mode structurally cannot grow one."""
        assert current_world((FRAMES / "world_night.png").read_bytes()) == "night"

    def test_the_plates_are_found_where_the_camera_left_them(self) -> None:
        """The row is laid out from the middle, so both villages move it as they please.

        These two are the same villages with the camera dragged to a map corner
        to reach the boat, which is where every crossing reads the world from.
        """
        assert current_world((FRAMES / "world_day_corner.png").read_bytes()) == "day"
        assert current_world((FRAMES / "world_night_corner.png").read_bytes()) == "night"

    def test_a_collector_marker_behind_the_dark_row_does_not_hide_the_village(self) -> None:
        """The dark row has its own left edge because its number is the shortest.

        Measured live: a collector's own full marker floated behind that row and
        laid ink from x 1300 to 1338, which failed the row — and `read_stock`
        failing is how `_home` decides it is not on the home village, so
        `walls`, `collect` and the attack loop all stood down together on a
        village plainly on screen.

        Gold is what stops the other two rows moving with it. This frame holds
        14 000 000, whose leading digit reaches back past the marker: given the
        dark row's edge it reads back 4 000 000, which is the truncation this
        project treats as the dangerous kind of wrong.
        """
        stock = read_stock((FRAMES / "home_marker_over_bars.png").read_bytes())
        assert stock is not None
        assert (stock.gold, stock.elixir, stock.dark) == (14000000, 17254813, 460500)

    def test_a_village_reads_where_its_storage_bars_do_not(self) -> None:
        """Which is the whole reason this replaced `read_stock` as the village test.

        The camera at a map corner leaves the home village's dark elixir row
        unreadable, so `read_stock` calls a perfectly ordinary village no village
        at all. The plate row is untouched by where the camera is.
        """
        frame = (FRAMES / "world_day_corner.png").read_bytes()
        assert read_stock(frame) is None
        assert current_world(frame) == "day"

    def test_a_battle_is_neither_village(self) -> None:
        """None is a third answer: nothing about a battle says which village is under it."""
        assert current_world((FRAMES / "world_night_battle.png").read_bytes()) is None

    def test_a_dialog_over_the_village_is_neither(self) -> None:
        """The builder base's own attack dialog covers the plate row it would be read from."""
        assert current_world((FRAMES / "world_night_menu.png").read_bytes()) is None

    def test_each_badge_is_rejoined_across_its_own_glyph(self) -> None:
        """The white `i` splits every badge into two runs of about 8 px.

        Left unjoined, each half is under the width floor and every village reads
        as no village; joined too eagerly, two badges 175 px apart would become
        one and the home village would read as the builder base.
        """
        badges = info_badges((FRAMES / "world_day.png").read_bytes())
        assert len(badges) == 3
        assert all(20 <= right - left + 1 <= 40 for left, right in badges)

    def test_the_screen_size_is_checked(self) -> None:
        small = io.BytesIO()
        Image.new("RGB", (800, 450)).save(small, format="PNG")
        with pytest.raises(ValueError, match="1600x900"):
            current_world(small.getvalue())


class WorldChoiceTests(unittest.TestCase):
    """Which village a series decides to play, and when that decision is fatal."""

    def _series(
        self, world: str | None, seen: str | None, crossed: str | None = None
    ) -> tuple[AttackSeries, MagicMock]:
        with (
            patch.object(commands, "_controller"),
            patch.object(commands, "_settle_game", return_value=None),
            patch.object(commands, "current_world", return_value=seen),
            patch.object(commands, "cross", return_value=crossed),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "FrameTicker"),
            patch.object(commands, "_rest", return_value=False),
            patch.object(commands, "AttackRunner") as runner,
            patch.object(commands, "_restart_emulator", return_value=True),
        ):
            runner.return_value.run.return_value = MagicMock(
                stock_full=True, attacked=None, phases=0, message=""
            )
            runner.return_value.played = None
            series = commands.attack(AttackOptions(world=world, rounds=1))
            return series, runner

    def test_an_unreadable_frame_falls_through_to_the_home_village(self) -> None:
        """A loading screen, a dialog and a dropped session all read as no village.

        Bailing on those ended the whole series before round one, where
        `_open_attack_menu` recovers from every one of them — it waits, restarts
        the game, leaves a result screen, and sails home from the wrong village.
        """
        _, runner = self._series(None, None)
        assert runner.call_args.kwargs["world"] == "day"

    def test_a_named_village_the_crossing_could_not_reach_stops_the_series(self) -> None:
        """Here the caller said which one, so playing the other is not a fallback."""
        series, runner = self._series("night", None, crossed="day")
        assert runner.return_value.run.call_count == 0
        assert "沒辦法切到夜世界" in series.root[0].message

    def test_a_named_village_the_game_is_already_on_costs_no_crossing(self) -> None:
        _, runner = self._series("night", "night", crossed="night")
        assert runner.call_args.kwargs["world"] == "night"

    def test_a_frame_that_reads_as_nothing_is_left_to_the_runner(self) -> None:
        """Unreadable is not "the other village", and the runner waits one out.

        Measured live: a run asked for `--world night` while a battle was still
        on screen — an ordinary state, a round abandoned by a stop — ended
        immediately with 沒辦法切到夜世界, having done nothing and waited for
        nothing. What it gets back is the rounds rather than that round: a
        battle is the one state neither this nor the runner can shorten, so the
        first round still comes back empty and `IDLE_REST` is what outlasts it.
        """
        _, runner = self._series("night", None, crossed=None)
        assert runner.call_args.kwargs["world"] == "night"
        assert runner.return_value.run.call_count == 1


class NightAttackTests(unittest.TestCase):
    """The builder base half of the attack loop, and the two screens only it has."""

    def _runner(self) -> AttackRunner:
        return AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            world="night",
            thresholds=LootThresholds(),
        )

    def test_the_attack_dialog_needs_its_panel_as_well_as_its_button(self) -> None:
        """A village is mostly grass, and the button it offers is green.

        Measured through the button's own box, a home village battlefield reads
        up to 0.69 of button green against the dialog's own 0.63 — so the green
        alone is not a screen. What no battlefield has behind it is the dialog's
        cream panel.
        """
        assert night_attack_menu((FRAMES / "night_menu.png").read_bytes())
        for other in ("night_searching", "night_cards", "world_day", "attack_menu"):
            assert not night_attack_menu((FRAMES / f"{other}.png").read_bytes()), other

    def test_the_matchmaker_is_read_off_its_cancel_button(self) -> None:
        """It has no timer and no other feature; the red button is the whole screen."""
        assert searching_opponent((FRAMES / "night_searching.png").read_bytes())
        for other in ("night_menu", "night_cards", "world_day", "battle_result"):
            assert not searching_opponent((FRAMES / f"{other}.png").read_bytes()), other

    def test_the_card_row_puts_the_machine_first_where_the_home_village_puts_it_last(self) -> None:
        """Which is why the split is read off the `xN` corner rather than off a group index.

        The home village orders its row troops-then-heroes and the builder base
        leads with the machine, so a positional split is wrong in one of the
        two. Only troops carry a count in either.
        """
        png = (FRAMES / "night_cards.png").read_bytes()
        groups = card_groups(png)
        assert groups == [[164], [307, 433, 560, 686, 813]]
        slots = [slot for group in groups for slot in group]
        assert counted_cards(png, slots) == [307, 433, 560, 686, 813]

    def test_the_builder_base_count_fails_rather_than_reading_wrongly(self) -> None:
        """It writes `4x` where the home village writes `x4`, half as big again.

        Its digits miss every template far enough that the 4 comes back as a 9,
        which `COUNT_DIGIT_TOLERANCE` would accept. None costs the fallback tap
        count; a 9 costs the burst that follows it.
        """
        png = (FRAMES / "night_cards.png").read_bytes()
        assert all(card_count(png, slot) is None for slot in (307, 433, 560, 686, 813))

    def test_the_machine_goes_in_ahead_of_the_troops(self) -> None:
        """The other way round from the home village, where the heroes follow the army.

        Here the machine is the army's cover, so it lands first and the troops
        follow once it has walked into the fire. How long that is comes off the
        plan, which is why `troops_after` is required on it.
        """
        runner = self._runner()
        order: list[str] = []
        with (
            patch.object(AttackRunner, "_settle_camera", return_value=b""),
            patch.object(AttackRunner, "_settle_zoom", return_value=b""),
            patch.object(attack, "card_groups", return_value=[[164], [307]]),
            patch.object(attack, "counted_cards", return_value=[307]),
            patch.object(attack, "live_cards", return_value=[164]),
            patch.object(AttackRunner, "_night_plan", return_value=plans.night_flat()),
            patch.object(AttackRunner, "_flank", return_value=DEPLOY_LINES["top_left"]),
            patch.object(
                AttackRunner,
                "_drop_singles",
                side_effect=lambda *a, **k: (order.append("machine"), ([164], [164]))[1],
            ),
            patch.object(AttackRunner, "_hold", side_effect=lambda *a: order.append("hold")),
            patch.object(
                AttackRunner, "_spread_night", side_effect=lambda *a: order.append("troops") or []
            ),
        ):
            assert runner._deploy_night(b"") == ([164], [307])
        assert order == ["machine", "hold", "troops"]

    def test_the_flat_plan_loads_and_carries_no_spells(self) -> None:
        plan = plans.night_flat()
        assert plan.deploy_from == "top_left"
        assert plan.troops_after > 0
        assert not plan.hero_points
        assert deploy_candidates(plan)[0] == (plan.deploy_start.pixels(), plan.deploy_end.pixels())

    def test_a_stage_that_ends_on_a_village_ends_the_round(self) -> None:
        """The ordinary attack: one deployment, and the game goes home afterwards."""
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(attack, "read_builder_stock", return_value=None),
            patch.object(AttackRunner, "_find_opponent", return_value=b""),
            patch.object(AttackRunner, "_deploy_night", return_value=([164], [307])) as deployed,
            patch.object(AttackRunner, "_wait_out_night"),
            patch.object(AttackRunner, "_next_stage", return_value=None),
        ):
            report = runner.run()
        assert (report.world, report.phases) == ("night", 1)
        assert deployed.call_count == 1

    def test_a_second_stage_puts_the_army_down_again(self) -> None:
        """**Nothing predicts it.** The game offers a second base after a first attack

        takes the whole of one, and the condition for that is the sort of rule
        that changes between releases — so the loop asks the question that
        cannot go stale, which is whether it is back on a village, and plays
        whatever else it is shown.
        """
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(attack, "read_builder_stock", return_value=None),
            patch.object(AttackRunner, "_find_opponent", return_value=b""),
            patch.object(AttackRunner, "_deploy_night", return_value=([164], [307])) as deployed,
            patch.object(AttackRunner, "_wait_out_night"),
            patch.object(AttackRunner, "_next_stage", side_effect=[b"", None]),
        ):
            report = runner.run()
        assert report.phases == 2
        assert deployed.call_count == 2

    def test_the_stages_are_capped_even_if_the_game_keeps_offering(self) -> None:
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(attack, "read_builder_stock", return_value=None),
            patch.object(AttackRunner, "_find_opponent", return_value=b""),
            patch.object(AttackRunner, "_deploy_night", return_value=([], [307])),
            patch.object(AttackRunner, "_wait_out_night"),
            patch.object(AttackRunner, "_next_stage", return_value=b""),
        ):
            report = runner.run()
        assert report.phases == attack.NIGHT_PHASES

    def test_a_pass_that_drained_nothing_ends_the_row_rather_than_moving_the_flank(self) -> None:
        """`live_cards` is the home village's spent-card test and does not hold here.

        A builder base card greys when the troops it put out die, not when it
        empties: measured over one recorded attack, all five read `0x` on the
        frame after the first pass while every one was still in colour, and they
        greyed one at a time over the next thirty seconds. Believed, that cost
        four more passes into empty cards — and since each drained nothing, the
        flank was pushed 280 px away from a village the army had already reached.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "card_drained", side_effect=[[307], []]),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            runner._spread_night([307, 433], ((600, 110), (230, 380)), 0)
        # Two cards over two passes: the one that drained, and the one that
        # proved the row empty. No third, and no pushed-out line.
        assert tapped.call_count == 4

    def test_a_row_that_keeps_draining_keeps_getting_passes(self) -> None:
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "card_drained", return_value=[307]),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            runner._spread_night([307], ((600, 110), (230, 380)), 0)
        assert tapped.call_count == attack.DEPLOY_PASSES

    def test_a_round_that_put_nothing_down_reports_no_phase(self) -> None:
        """Counted, such a round says 已進攻並回營 for a battle nothing was played in.

        It is load-bearing rather than cosmetic: `commands.attack` reads
        `phases` to decide whether a round really fought, so a phantom one is
        counted toward the emulator restart and the loot cart, and skips the
        wait the loop would otherwise take.
        """
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(attack, "read_builder_stock", return_value=None),
            patch.object(AttackRunner, "_find_opponent", return_value=b""),
            patch.object(AttackRunner, "_deploy_night", return_value=None),
            patch.object(AttackRunner, "_wait_out_night") as waited,
        ):
            report = runner.run()
        assert (report.phases, waited.call_count) == (0, 0)
        assert report.message == "沒有成功部署任何部隊"

    def test_an_opening_pass_that_landed_nothing_pushes_the_flank_out(self) -> None:
        """Nothing probes the line any more, so the first pass is what tests it.

        A later pass draining nothing is an empty row — one pass taps
        `DROPS_PER_PASS` against a card holding about four. The first one is a
        line the base has grown over, and pushing it out is what the three probe
        troops used to buy.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "card_drained", side_effect=[[], [307], []]),
            patch.object(AdbController, "tap_many"),
            patch.object(attack, "deploy_line", return_value=[(600, 110)]) as drawn,
        ):
            runner._spread_night([307], ((600, 110), (230, 380)), 0)
        # Once for the opening line and once for the pushed one, and no third:
        # the pass after the one that landed is an empty row, not a bad flank.
        assert [call.args[1:] for call in drawn.call_args_list] == [
            tuple(push_line(((600, 110), (230, 380)), step, attack.SCREEN_CENTRE))
            for step in (0, 1)
        ]

    def test_a_result_screen_that_will_not_close_gets_back_pressed_at_it(self) -> None:
        """The game's own popups sit over the result and answer nothing at 回營.

        They close on a red X in their own corner, so tapping where the button
        would be does nothing however often it is repeated — measured live, one
        held a run for 40 minutes.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "battle_over", return_value=True),
            patch.object(AdbController, "tap"),
            patch.object(AdbController, "back") as pressed,
        ):
            runner._leave_result()
        assert pressed.call_count == 1

    def test_a_result_screen_that_closes_is_never_pressed_at(self) -> None:
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "battle_over", side_effect=[True, False]),
            patch.object(AdbController, "tap"),
            patch.object(AdbController, "back") as pressed,
        ):
            runner._leave_result()
        assert pressed.call_count == 0

    def test_the_last_tap_landing_is_not_answered_with_back(self) -> None:
        """That tap is unchecked and is the one most likely to have worked.

        The button only comes alive once the stars have flown in, which is what
        the retries are for — so the village can be back by then, and `back`
        there is 確定退出遊戲嗎.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            # True for every check inside the loop, then False for the read that
            # guards the press: the final tap worked.
            patch.object(
                attack, "battle_over", side_effect=[True] * attack.RESULT_ATTEMPTS + [False]
            ),
            patch.object(AdbController, "tap"),
            patch.object(AdbController, "back") as pressed,
        ):
            runner._leave_result()
        assert pressed.call_count == 0

    def test_a_popup_over_the_base_is_pressed_away_rather_than_tapped_behind(self) -> None:
        """The 攻擊 tap lands on the popup, so the round would spend every attempt on it.

        Measured live on an event reward page: five attempts a round, then
        畫面不在建築大師基地, then the same again — 18 rounds of it, with nothing
        between rounds to clear the screen.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(
                runner, "_frame", return_value=(FRAMES / "event_reward.png").read_bytes()
            ),
            patch.object(attack, "uncovered", return_value=None) as cleared,
            patch.object(AdbController, "tap") as tapped,
        ):
            assert runner._open_attack_menu() is None
        assert cleared.call_count == attack.HOME_ATTEMPTS
        # And never at 攻擊, which is what was being spent behind the popup.
        assert tapped.call_count == 0

    def test_a_repainted_card_row_ends_the_stage(self) -> None:
        """A stage can end without a result screen, and waiting for one cost a whole second stage.

        The game opened it with a fresh timer and the surviving troops back in
        their cards; `battle_over` stayed False because a live battle is not a
        result, and the loop spent four minutes tapping a dead machine's card.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "battle_over", return_value=False),
            patch.object(attack, "card_drained", side_effect=[[], [307]]),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            runner._wait_out_night([164], [307])
        # Two passes: the quiet one, then the repaint that ends it. The quiet
        # one offers the ability once a second for the length of the poll.
        assert tapped.call_count == round(attack.ABILITY_POLL / attack.ABILITY_TAP)

    def test_the_head_start_offers_the_ability_every_second(self) -> None:
        """The recharge is about 14 s and a tap costs 50 ms, so only the capture is paced."""
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep") as slept,
            patch.object(AdbController, "tap_many") as tapped,
        ):
            runner._hold(4, [164])
        assert tapped.call_count == 4
        assert slept.call_count == 4

    def test_a_stage_with_no_troop_cards_still_ends_on_the_result(self) -> None:
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "battle_over", side_effect=[False, True]),
            patch.object(AdbController, "tap_many"),
        ):
            runner._wait_out_night([164], [])

    def test_the_flank_keeps_moving_until_something_lands(self) -> None:
        """Reading the pass index instead gave it exactly one push.

        The pass after the pushed one has an index of 1, so it read as an empty
        row and returned with the whole army still in its cards — against the
        four-step ladder the probing used to walk.
        """
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "card_drained", side_effect=[[], [], [307], []]),
            patch.object(AdbController, "tap_many"),
            patch.object(attack, "deploy_line", return_value=[(600, 110)]) as drawn,
        ):
            assert runner._spread_night([307], ((600, 110), (230, 380)), 0) is not None
        # Pushed twice before anything landed, and not again once it had.
        assert [call.args[1:] for call in drawn.call_args_list] == [
            tuple(push_line(((600, 110), (230, 380)), step, attack.SCREEN_CENTRE))
            for step in (0, 1, 2)
        ]

    def test_a_flank_that_never_takes_a_troop_is_not_a_deployment(self) -> None:
        """`commands.attack` reads `phases` as "did this round really fight"."""
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "card_drained", return_value=[]),
            patch.object(AdbController, "tap_many"),
            patch.object(attack, "deploy_line", return_value=[(600, 110)]),
        ):
            assert runner._spread_night([307], ((600, 110), (230, 380)), 0) is None

    def test_a_machine_that_died_in_the_stage_before_is_not_sent_out_again(self) -> None:
        """The second stage opens with whatever survived, so a dead machine is the norm.

        Its card stays on the row greyed out, and `_drop_singles` would spend its
        whole ladder of spots on it — five taps, each with a settle and a
        capture — before reporting that it took nothing. Skipping it also skips
        the head start, which is the machine's and nobody else's.
        """
        runner = self._runner()
        order: list[str] = []
        with (
            patch.object(AttackRunner, "_settle_camera", return_value=b""),
            patch.object(AttackRunner, "_settle_zoom", return_value=b""),
            patch.object(attack, "card_groups", return_value=[[164], [307]]),
            patch.object(attack, "counted_cards", return_value=[307]),
            # The machine card is grey: its unit died in the stage before.
            patch.object(attack, "live_cards", return_value=[]),
            patch.object(AttackRunner, "_night_plan", return_value=plans.night_flat()),
            patch.object(AttackRunner, "_flank", return_value=DEPLOY_LINES["top_left"]),
            patch.object(attack, "deploy_line", return_value=[(600, 110)]),
            patch.object(
                AttackRunner,
                "_drop_singles",
                side_effect=lambda *a, **k: (order.append("machine"), ([], []))[1],
            ),
            patch.object(AttackRunner, "_hold", side_effect=lambda *a: order.append("hold")),
            patch.object(
                AttackRunner, "_spread_night", side_effect=lambda *a: order.append("troops") or []
            ),
        ):
            assert runner._deploy_night(b"") == ([], [307])
        assert order == ["troops"]

    def test_the_second_stage_sends_the_preselected_machine_from_the_field(self) -> None:
        """The surviving machine's card comes back selected, so the field is tapped, not the card.

        Measured on five recorded second stages: the card is drawn at the
        selected width with a white border. Dropping it the usual way spent
        five retries on its ability button and held the troops behind a machine
        already fighting; a tap on the card would only deselect it, and one
        blind tap on the field was swallowed on a live round.
        """
        runner = self._runner()
        order: list[str] = []
        with (
            patch.object(AttackRunner, "_settle_camera", return_value=b""),
            patch.object(AttackRunner, "_settle_zoom", return_value=b""),
            patch.object(attack, "card_groups", return_value=[[164], [307]]),
            patch.object(attack, "counted_cards", return_value=[307]),
            patch.object(attack, "live_cards", return_value=[164]),
            patch.object(AttackRunner, "_night_plan", return_value=plans.night_flat()),
            patch.object(AttackRunner, "_flank", return_value=DEPLOY_LINES["top_left"]),
            patch.object(attack, "deploy_line", return_value=[(600, 110)]),
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            # The first spot is swallowed; the second sends it.
            patch.object(attack, "selected_cards", side_effect=[[164], []]),
            patch.object(AdbController, "tap", side_effect=lambda *a: order.append("field")),
            patch.object(
                AttackRunner, "_drop_singles", side_effect=lambda *a, **k: order.append("machine")
            ),
            patch.object(AttackRunner, "_hold", side_effect=lambda *a: order.append("hold")),
            patch.object(
                AttackRunner, "_spread_night", side_effect=lambda *a: order.append("troops") or []
            ),
        ):
            assert runner._deploy_night(b"", 1) == ([164], [307])
        assert order == ["field", "field", "hold", "troops"]

    def test_a_preselected_card_is_read_by_its_white_border(self) -> None:
        """The border is what says the next tap on the field deploys this card."""
        selected = (FRAMES / "night_stage2_cards.png").read_bytes()
        assert selected_cards(selected, [164, 307]) == [164]
        resting = (FRAMES / "night_cards.png").read_bytes()
        assert selected_cards(resting, [164, 307]) == []

    def test_a_selected_machine_card_is_still_a_card(self) -> None:
        """The second stage opens with the machine's card preselected, 118 px wide.

        At the resting ceiling it came apart into two pieces a 1 px sliver of
        its own border kept from rejoining, and the row read as six troops and
        no machine on every recorded second stage.
        """
        png = (FRAMES / "night_stage2_cards.png").read_bytes()
        groups = card_groups(png)
        assert groups == [[164], [307, 433, 560, 686, 813, 939]]
        slots = [slot for group in groups for slot in group]
        assert counted_cards(png, slots) == [307, 433, 560, 686, 813, 939]
        assert live_cards(png, [164]) == [164]

    def test_every_live_machine_is_offered_its_ability_whatever_the_drop_read_said(self) -> None:
        """`field_units` misses a machine whose health bar is no longer green.

        Offering the ability only to the cards read as landed left a machine on
        the field with its ability ready and untouched for the rest of the
        stage; the tap is harmless on a card still holding its unit.
        """
        runner = self._runner()
        held: list[list[int]] = []
        with (
            patch.object(AttackRunner, "_settle_camera", return_value=b""),
            patch.object(AttackRunner, "_settle_zoom", return_value=b""),
            patch.object(attack, "card_groups", return_value=[[164], [307]]),
            patch.object(attack, "counted_cards", return_value=[307]),
            patch.object(attack, "live_cards", return_value=[164]),
            patch.object(AttackRunner, "_night_plan", return_value=plans.night_flat()),
            patch.object(AttackRunner, "_flank", return_value=DEPLOY_LINES["top_left"]),
            patch.object(attack, "deploy_line", return_value=[(600, 110)]),
            # The read says nothing landed.
            patch.object(AttackRunner, "_drop_singles", return_value=([], [])),
            patch.object(
                AttackRunner, "_hold", side_effect=lambda seconds, cards: held.append(cards)
            ),
            patch.object(AttackRunner, "_spread_night", return_value=[(600, 110)]),
        ):
            assert runner._deploy_night(b"") == ([164], [307])
        assert held == [[164]]

    def test_a_full_builder_base_stands_down_before_the_search(self) -> None:
        """Its own ceilings, and asked before the search rather than after it.

        The search costs nothing in game but waits on a live player — measured,
        one ran to five and a half minutes — which is the real price of a round
        that had nowhere to put what it won.

        The ceilings are this builder base's real ones, and dark carries none at
        all: that village has no dark elixir bar to tap, so nothing compares the
        0 `read_builder_stock` reports against anything.
        """
        runner = AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            world="night",
            thresholds=LootThresholds(),
            stop_at=90,
        )
        runner._capacity = StorageCapacity(gold=2_050_000, elixir=2_450_000)
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(
                attack,
                "read_builder_stock",
                return_value=VillageStock(gold=1_900_000, elixir=2_300_000, dark=0),
            ),
            patch.object(AttackRunner, "_find_opponent") as searched,
            patch.object(AdbController, "back"),
        ):
            report = runner.run()
        assert report.stock_full
        assert searched.call_count == 0

    def test_a_builder_base_short_of_its_limits_keeps_farming(self) -> None:
        """**All** the watched resources, not any one of them, the same as the home village."""
        runner = AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            world="night",
            thresholds=LootThresholds(),
            stop_at=90,
        )
        runner._capacity = StorageCapacity(gold=2_050_000, elixir=2_450_000)
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(
                attack,
                "read_builder_stock",
                # Gold is past 90% of its 2 050 000 and elixir is not, which is
                # one round more rather than a run standing down.
                return_value=VillageStock(gold=1_900_000, elixir=2_000_000, dark=0),
            ),
            patch.object(AttackRunner, "_find_opponent", return_value=None) as searched,
        ):
            report = runner.run()
        assert not report.stock_full
        assert searched.call_count == 1

    def test_nobody_matched_is_reported_rather_than_deployed_into(self) -> None:
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(attack, "read_builder_stock", return_value=None),
            patch.object(AttackRunner, "_find_opponent", return_value=None),
            patch.object(AttackRunner, "_deploy_night") as deployed,
        ):
            report = runner.run()
        assert (report.phases, deployed.call_count) == (0, 0)


class CrossingTests(unittest.TestCase):
    """Sailing between the two villages, which is a tap the boat may or may not take."""

    def _cross(
        self,
        seen: list[str | None],
        want: str = "day",
        battle: bool = False,
        loading: bool = False,
    ) -> tuple[MagicMock, str | None]:
        adb = MagicMock()
        with (
            patch.object(world_ui.time, "sleep"),
            patch.object(world_ui, "current_world", side_effect=seen),
            patch.object(world_ui, "card_groups", return_value=[[1]] if battle else []),
            patch.object(world_ui, "battle_over", return_value=False),
            patch.object(world_ui, "loading_screen", return_value=loading),
        ):
            landed = world_ui.cross(adb, DisplayTarget(logical_id="1", physical_id="2"), want)
        return adb, landed

    def test_being_there_already_costs_one_capture_and_nothing_else(self) -> None:
        """Which is what lets a caller ask on every run instead of working out whether to."""
        adb, landed = self._cross(["day"])
        assert landed == "day"
        assert adb.swipe.call_count == 0
        assert adb.tap.call_count == 0

    def test_the_first_spot_that_sails_ends_it(self) -> None:
        adb, landed = self._cross(["night", "day"])
        assert landed == "day"
        assert adb.swipe.call_count == world_ui.SWIPES
        adb.tap.assert_called_once_with(*world_ui.CROSSINGS["day"].spots[0], ANY)

    def test_a_tap_that_missed_the_boat_tries_the_next_spot(self) -> None:
        """Nothing recognises the boat, so a miss looks exactly like a world that did not change."""
        # The village still reading between the two spots is the miss that hit
        # water rather than a building; nothing has to be cleared.
        adb, landed = self._cross(["night", *["night"] * world_ui.SAIL_POLLS, "night", "day"])
        assert landed == "day"
        assert adb.back.call_count == 0
        assert [call.args[:2] for call in adb.tap.call_args_list] == list(
            world_ui.CROSSINGS["day"].spots[:2]
        )

    def test_a_spot_that_opened_a_building_is_cleared_before_the_next_one(self) -> None:
        """A panel swallows every tap after it, so one missed spot used to cost the rest.

        Measured live: a `collect` run left an 8級聖水收集器 panel up and two
        `world --go night` runs in a row reported 畫面還停在不明的畫面 without
        moving anything.
        """
        adb, landed = self._cross([
            "night",
            *["night"] * world_ui.SAIL_POLLS,
            None,  # the first spot opened a building rather than the boat
            "night",  # and `back` got the village back
            "day",  # so the second spot could be tried at all
        ])
        assert landed == "day"
        assert adb.back.call_count == 1
        assert [call.args[:2] for call in adb.tap.call_args_list] == list(
            world_ui.CROSSINGS["day"].spots[:2]
        )

    def test_a_covered_village_is_uncovered_rather_than_given_up_on(self) -> None:
        adb, landed = self._cross([None, "night", "day"])
        assert landed == "day"
        assert adb.back.call_count == 1
        assert adb.swipe.call_count == world_ui.SWIPES

    def test_no_village_is_not_a_failed_crossing(self) -> None:
        """A loading screen has no boat on it and nothing to sail from; the caller waits.

        `back` cannot hurry a game that is loading, which is what bounds the
        pressing rather than any risk in it.
        """
        adb, landed = self._cross([None] * (world_ui.UNCOVER_TRIES + 1))
        assert landed is None
        assert adb.swipe.call_count == 0
        assert adb.back.call_count == world_ui.UNCOVER_TRIES

    def test_a_loading_screen_is_never_pressed_at(self) -> None:
        """Nothing on it answers a press, and the presses were noise in the one log that matters."""
        adb, landed = self._cross([None], loading=True)
        assert landed is None
        assert adb.back.call_count == 0
        assert adb.screenshot.call_count == 1

    def test_the_cart_is_judged_on_the_builder_bases_own_two_rows(self) -> None:
        """`read_stock` wants a third row that village does not have.

        What sits at that y is its gems bar, which comes back as `dark=410005`
        with the green `+` read as a leading 4 — and it has to resolve at all
        for `read_stock` to answer anything, so a gems row that will not read
        would lose the whole trip. The numbers are one real trip's own.
        """
        adb = MagicMock()
        with (
            patch.object(world_ui.time, "sleep"),
            patch.object(world_ui, "current_world", return_value="night"),
            patch.object(world_ui, "loot_cart_open", return_value=True),
            patch.object(
                world_ui,
                "read_builder_stock",
                side_effect=[
                    VillageStock(gold=1584654, elixir=726429, dark=0),
                    VillageStock(gold=1584654, elixir=842429, dark=0),
                ],
            ) as read,
        ):
            gained = world_ui.collect_cart(adb, DisplayTarget(logical_id="1", physical_id="2"))
        assert gained == 116_000
        assert read.call_count == 2

    def test_a_cart_that_never_opened_pays_nothing(self) -> None:
        """A tap the game swallowed leaves the cart looking exactly like an emptied one."""
        adb = MagicMock()
        with (
            patch.object(world_ui.time, "sleep"),
            patch.object(world_ui, "current_world", return_value="night"),
            patch.object(world_ui, "loot_cart_open", return_value=False),
            patch.object(world_ui, "read_builder_stock", return_value=None) as read,
        ):
            gained = world_ui.collect_cart(adb, DisplayTarget(logical_id="1", physical_id="2"))
        assert gained == 0
        # The one before the taps, and none after: it gave up before collecting.
        assert read.call_count == 1

    def test_a_battle_on_screen_is_never_pressed_at(self) -> None:
        """`back` there is aimed at 放棄, and a killed run leaves exactly this state.

        The callers hand this whatever is on screen — `world --go` and a run that
        named a village both go straight into the crossing on a frame nothing
        could read — so the filter lives here rather than in a docstring asking
        them not to.
        """
        adb, landed = self._cross([None], battle=True)
        assert landed is None
        assert adb.back.call_count == 0
        assert adb.swipe.call_count == 0

    def test_the_camera_goes_back_to_the_far_zoom_either_way(self) -> None:
        """The swiping above parks it at a map corner, and every coordinate here wants it centred."""
        # The long one is every spot missing: one read to start, then each spot's
        # polls plus the read that checks it opened nothing, then the final one.
        for seen in (["night", "day"], ["night"] * (world_ui.SAIL_POLLS * 3 + 5)):
            adb, _ = self._cross(seen)
            assert adb.zoom.call_count == 1


class ScoutTests(unittest.TestCase):
    """Frames captured from a live scout screen, masked down to the panels read here."""

    def test_loot_is_read_over_grass(self) -> None:
        view = read_scout((FRAMES / "scout_grass.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (625427, 631851, 7365)
        assert view.can_skip

    def test_loot_is_read_over_stone(self) -> None:
        """Grey paving behind the digits is what a plain brightness threshold gets wrong."""
        view = read_scout((FRAMES / "scout_stone.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (767905, 814967, 12891)

    def test_seven_figure_loot_is_not_cut_short(self) -> None:
        """The panel box has to clear x 207; a village this rich is what pays for it."""
        view = read_scout((FRAMES / "scout_seven_digits.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (1746707, 1705910, 16361)

    def test_the_dark_row_is_read_where_it_is_dimmest(self) -> None:
        """It peaks at 206, so the shared ink floor of 200 left the row unreadable."""
        view = read_scout((FRAMES / "scout_dim_dark.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (185482, 133614, 2780)

    def test_village_showing_through_the_panel_is_not_read_as_digits(self) -> None:
        """Bright paving behind the panel used to add a digit to the end of every row.

        Two of those blobs are wider than any digit, so this is also what holds
        the line on splitting one: cut, they come apart at 52/63 and 30/52 bits
        against real digits' 12 and 4, and believing either cut would put an
        invented digit on the end of a row rather than a rejected one.
        """
        view = read_scout((FRAMES / "scout_bright_backdrop.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (180728, 24752, 505)

    def test_two_digits_with_no_gap_between_them_are_cut_apart(self) -> None:
        """Nothing guarantees a gap between digits, and this opponent's 7 and 4 left none.

        The 74 of 741 829 arrives as a single 30 px span — 7 is 13 px wide, 4 is
        17, and they touch — which matches "3" at 47 bits. Dropping that glyph
        read the row as 1 829, so an opponent holding 741k was passed over for
        being poor. Failing the row instead is no better here: every one of the
        seventeen frames of that scout screen read the same way, so the round was
        spent polling a panel that was never going to resolve, and ended with
        等不到對手畫面 after the search had already been paid for.

        Only the two boxes `read_scout` reads are the live capture; the rest of
        the frame is filled flat, because the village it came with is 2.9 MB.
        """
        view = read_scout((FRAMES / "scout_touching_digits.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (741829, 713776, 6328)

    def test_three_touching_digits_fail_rather_than_read_as_two(self) -> None:
        """A cut hands back two digits, so a run of three has to fail instead.

        This one is 164, and cut anywhere it comes apart into a 6 at 24 bits and
        a 4 at 14 — both comfortably inside the tolerance, so nothing downstream
        objects to a row that has quietly become 64. That is the silent kind of
        wrong this reader exists to avoid, and it is the kind splitting can
        introduce: before splitting existed the whole span failed and the caller
        re-read the next frame.

        Bounding each half to a digit's own width is what keeps that: no cut
        through a 39 px run leaves both halves small enough to be digits, so
        none is believed. Eleven other runs built from this fixture's digits
        behave the same way.

        Built by butting three of the fixture's own digits together, since the
        recorded frames only ever caught two touching.
        """
        image = Image.open(io.BytesIO((FRAMES / "scout_seven_digits.png").read_bytes())).convert(
            "RGB"
        )
        top, bottom = ROW_BOUNDS[0]
        # The 1, 6 and 4 of 1 746 707, each measured off this frame's gold row.
        digits = [
            image.crop((PANEL_LEFT + a, top, PANEL_LEFT + b, bottom))
            for a, b in ((10, 17), (59, 74), (41, 58))
        ]
        ImageDraw.Draw(image).rectangle((PANEL_LEFT, top, PANEL_RIGHT, bottom), fill=(18, 20, 16))
        offset = 10
        for digit in digits:
            image.paste(digit, (PANEL_LEFT + offset, top))
            offset += digit.width
        frame = io.BytesIO()
        image.save(frame, "PNG")
        assert read_scout(frame.getvalue()) is None

    def test_a_digit_the_frame_cannot_read_fails_its_whole_row(self) -> None:
        """Skipping it instead divides the number by ten, which reads as a poor village.

        Live, the second 7 of an opponent's 1 047 758 swung between 14 and 42
        bits off its template from one frame to the next, so four readings in ten
        came back as 104 758 — under the 500k threshold that had just accepted
        it. The fixture reproduces that on the 0 of 1 746 707, five digits into
        seven: skipped, the row reads 174 677. A row the caller can re-read on
        the next frame is worth more than one that is quietly wrong.
        """
        assert read_scout((FRAMES / "scout_smudged_digit.png").read_bytes()) is None

    def test_speckle_beside_a_digit_does_not_fail_the_row(self) -> None:
        """Two lit pixels under the 9 of 297 906 cost a whole battle its loot reading.

        Live, this village read as nothing on all four of the polls its battle
        got, so the run reported an attack that took 875k gold as one where
        nothing had been deployed. The panel is kept and the rest of the frame
        flattened; whole, it is 3 MB.
        """
        view = read_scout((FRAMES / "scout_speckled_panel.png").read_bytes())
        assert view is not None
        assert view.loot == LootOffer(gold=297906, elixir=94145, dark=0)

    def test_the_resource_icon_bleeding_into_the_box_costs_nothing(self) -> None:
        """The dark drop's glow reached the column beside the 1 of 10 428.

        The two never touched, but they shared a column, so they came out as one
        glyph matching 3 and the row failed — and one unreadable row is an
        opponent nobody read at all. Live, that round never deployed, the
        countdown started the battle anyway, and the army went with it, along
        with the two rounds spent tapping at a battle nothing recognised.
        """
        view = read_scout((FRAMES / "scout_icon_bleed.png").read_bytes())
        assert view is not None
        assert view.loot == LootOffer(gold=742073, elixir=778243, dark=10428)
        assert view.can_skip

    def test_a_started_battle_is_no_longer_skippable(self) -> None:
        """The loot panel stays on screen once the countdown expires; 下一個 does not."""
        view = read_scout((FRAMES / "scout_in_battle.png").read_bytes())
        assert view is not None
        assert not view.can_skip

    def test_the_search_transition_reads_as_no_opponent(self) -> None:
        assert read_scout((FRAMES / "searching.png").read_bytes()) is None

    def test_the_attack_menu_is_recognised_before_the_run_commits(self) -> None:
        assert attack_menu_open((FRAMES / "attack_menu.png").read_bytes())
        assert not attack_menu_open((FRAMES / "scout_grass.png").read_bytes())

    def test_the_result_screen_is_recognised_so_the_next_run_can_start(self) -> None:
        """One 回營 tap was not enough, and four runs in a row then stood down.

        **The game draws this button two ways and only one of them was here.**
        `battle_result.png` is the plain green plate that measured 0.3283 and
        set the old 0.25 line; the live game also draws it lit, with a white
        border and a washed-out fill, and that reads 0.2488. Swept over a day of
        recorded farming, 20 of 21 result screens were the lit kind and not one
        of them cleared the line — so `battle_over` was False on the real
        thing every round while this test went on passing.
        """
        assert battle_over((FRAMES / "battle_result.png").read_bytes())
        assert battle_over((FRAMES / "battle_result_lit.png").read_bytes())
        assert not battle_over((FRAMES / "attack_menu.png").read_bytes())
        assert not battle_over((FRAMES / "scout_in_battle.png").read_bytes())

    def test_the_loading_screen_is_read_off_its_bar_and_nothing_else_is(self) -> None:
        """正在載入, which is where a game sits while the server will not answer.

        The bar is UI over a splash that changes with the season, and every
        other reader answers None on it, so without this a game waiting on the
        server read as a game on the wrong screen. The fixture is the bar alone;
        the frames it is held against are the ones a grey strip alone would
        match — a result screen reads 1.000 on the plate — which is why the
        purple fill at the bar's left end is required as well.
        """
        png = (FRAMES / "loading_screen.png").read_bytes()
        assert loading_screen(png)
        # The fixture keeps the dialog box too, because both `_home` and
        # `_open_attack_menu` ask `idle_disconnected` first: a splash that read
        # as the dialog would be restarted out of rather than waited on.
        assert not idle_disconnected(png)
        for name in ("battle_result", "event_reward", "searching", "world_day", "attack_menu"):
            assert not loading_screen((FRAMES / f"{name}.png").read_bytes()), name

    def test_the_lost_connection_dialog_reads_as_a_dropped_session(self) -> None:
        """連線已中斷 is the idle dialog's sheet with 再試一次 where the button was.

        Nothing tells the two apart and nothing needs to: a restart logs in
        again exactly as either button would. What this holds is that the one
        reader keeps covering both, and that the sheet is not the loading
        screen, which is waited on rather than restarted out of.
        """
        png = (FRAMES / "connection_lost.png").read_bytes()
        assert idle_disconnected(png)
        assert not loading_screen(png)

    def test_an_event_reward_page_is_not_a_result_screen(self) -> None:
        """A page of green tick marks, one of which lands in the 回營 box.

        Measured 0.2009 of that box against the real button's 0.3283, which is
        what put the threshold between them rather than at the old 0.15. What it
        cost first: the screen closes on a red X in its own corner and answers
        nothing where 回營 sits, so a run read it as a result screen and tapped
        an empty patch for 40 minutes — 18 rounds, each reporting
        畫面不在建築大師基地, while a battle it had already matched into ran out
        underneath it.
        """
        png = (FRAMES / "event_reward.png").read_bytes()
        assert not battle_over(png)
        # And not a village either, so nothing downstream mistakes it for one.
        assert current_world(png) is None

    def test_only_the_card_that_lost_one_shows_it(self) -> None:
        """A drop is judged on the card's own corner, which repaints when it loses one.

        The corner is read rather than the number: the giant's illustration
        swallows its count entirely, and this still separates the card that
        deployed from the nine that did not.
        """
        before = (FRAMES / "pass_before.png").read_bytes()
        after = (FRAMES / "pass_after.png").read_bytes()
        slots = [171, 293, 413, 557, 694, 804, 925, 1046, 1181, 1302]
        assert card_drained(before, after, slots) == [171]
        assert card_drained(before, before, slots) == []

    def test_a_hero_on_the_field_is_read_off_its_health_bar(self) -> None:
        """Three of the four went down; the run that recorded this reported four."""
        frame = (FRAMES / "heroes_down.png").read_bytes()
        assert field_units(frame, [557, 694, 804, 925, 1046]) == [694, 804, 925]

    def test_a_hero_still_in_its_card_carries_no_bar(self) -> None:
        """A hero keeps its card once it lands, so nothing else separates the two."""
        assert field_units((FRAMES / "cards_full.png").read_bytes(), [815, 925, 1046, 1167]) == []

    def test_the_grass_above_the_card_row_is_not_a_health_bar(self) -> None:
        """The strip sits above the cards, so the battlefield shows through it.

        A live battle over a bright village read every hero card as landed while
        all four were still in their cards, which is the whole failure this
        reader exists to catch. The two measured colours are what tells them
        apart: the bar has almost no blue in it and grass keeps a third of a
        channel, so the fill ratio alone cannot separate them.
        """

        def strip(colour: tuple[int, int, int]) -> bytes:
            frame = Image.new("RGB", (1600, 900), (20, 20, 20))
            frame.paste(Image.new("RGB", (120, 30), colour), (640, 712))
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            return buffer.getvalue()

        assert field_units(strip((131, 184, 53)), [694]) == []
        assert field_units(strip((101, 231, 9)), [694]) == [694]

    def test_army_strength_splits_on_the_glyphs_that_are_not_digits(self) -> None:
        """The troop icon and the slash are found by matching no digit well."""
        assert army_strength((FRAMES / "army_full.png").read_bytes()) == (305, 305)

    def test_army_strength_is_none_away_from_the_army_screen(self) -> None:
        assert army_strength((FRAMES / "scout_grass.png").read_bytes()) is None

    def test_the_village_storages_are_read_off_the_home_screen(self) -> None:
        """The bars carry their own fill highlight behind the digits."""
        assert read_stock((FRAMES / "home_storages.png").read_bytes()) == VillageStock(
            gold=1053405, elixir=375386, dark=143079
        )

    def test_a_bar_gloss_between_two_digits_does_not_fail_the_row(self) -> None:
        """The dark bar's gloss bridged the 1 and the 3, and took the whole row down.

        The frame is a live one with everything outside the bars flattened: whole,
        this village comes to 3 MB and no colour reduction gets it under the
        repo's file-size limit without also flattening away the gloss itself.
        """
        assert read_stock((FRAMES / "home_storage_gloss.png").read_bytes()) == VillageStock(
            gold=447824, elixir=5141375, dark=130480
        )

    def test_storages_are_none_away_from_the_home_screen(self) -> None:
        """Three readable rows is what says the home village is up; nothing else does."""
        assert read_stock((FRAMES / "scout_grass.png").read_bytes()) is None
        assert read_stock((FRAMES / "attack_menu.png").read_bytes()) is None

    def test_a_frame_of_another_resolution_is_rejected(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (800, 450)).save(buffer, "PNG")
        with pytest.raises(ValueError, match="1600x900"):
            read_scout(buffer.getvalue())


class CeilingReadTests(unittest.TestCase):
    """Turning the tooltips into this village's ceilings, once a run."""

    def _runner(self, world: str = "day", stop_at: int = 90) -> AttackRunner:
        return AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            world=world,
            thresholds=LootThresholds(),
            stop_at=stop_at,
        )

    def _read(self, runner: AttackRunner, answers: list[int | None]) -> list[tuple[int, int]]:
        """Run one `_settle_ceilings` against canned tooltip readings, keeping the taps."""
        taps: list[tuple[int, int]] = []
        with (
            patch.object(AttackRunner, "_tap", lambda _, point: taps.append(point)),
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack, "storage_capacity", side_effect=answers),
            patch.object(attack.time, "sleep"),
        ):
            runner._settle_ceilings()
        return taps

    def test_a_village_that_reads_every_row_is_asked_once_for_the_whole_run(self) -> None:
        runner = self._runner()
        self._read(runner, [24_000_000, 24_000_000, 370_000])
        assert runner._ceiling == StorageCapacity(gold=24_000_000, elixir=24_000_000, dark=370_000)
        # Nothing left to ask, so a second round spends no taps at all.
        assert self._read(runner, []) == []

    def test_a_partial_read_is_thrown_away_rather_than_kept(self) -> None:
        """Kept, it would stand the run down on dark alone with the big storages empty.

        And an empty one is the other half of the same bug: it watches nothing,
        so an overnight run farms straight past full storages. Either way the
        next round simply asks again.
        """
        runner = self._runner()
        # Gold and elixir fail both tries; dark answers.
        self._read(runner, [None, None, None, None, 370_000])
        assert runner._capacity is None
        assert runner._ceiling.full(VillageStock(gold=0, elixir=0, dark=370_000), 90) is None

    def test_a_run_that_can_never_stand_down_does_not_tap_the_bars(self) -> None:
        """`probe` and `bounds` take the default 0, and so does 不監控 in the window."""
        assert self._read(self._runner(stop_at=0), []) == []

    def test_the_builder_base_leaves_its_gems_row_alone(self) -> None:
        """Tapping it opens the shop, and there is no dark elixir there to read."""
        runner = self._runner(world="night")
        taps = self._read(runner, [2_050_000, 2_550_000])
        assert runner._ceiling == StorageCapacity(gold=2_050_000, elixir=2_550_000)
        assert {y for _, y in taps} == {attack.STOCK_BAR_Y[0], attack.STOCK_BAR_Y[1]}

    def test_a_row_that_reads_is_tapped_shut_behind_itself(self) -> None:
        """An open tooltip covers the rows under it, so it does not outlive the read."""
        taps = self._read(self._runner(), [24_000_000, 24_000_000, 370_000])
        assert taps == [(attack.STOCK_BAR_X, y) for y in attack.STOCK_BAR_Y for _ in range(2)]


class StorageTipTests(unittest.TestCase):
    """最大儲存量, off the tooltip a tapped storage bar drops open.

    This is the one place the game writes down how much a storage holds, and
    reading it is what lets both villages share one 90% rather than six typed-in
    amounts that go stale as the storages grow.
    """

    def test_every_row_of_both_villages_reads_its_own_ceiling(self) -> None:
        """Each row of each village, because the panel is not the same in any two.

        The tooltip hangs under the bar that opened it, so the line moves by the
        84 px row pitch; the home village writes three lines to the builder
        base's two; and the dark row's panel sits further right, its label
        reaching inside the box the other rows only show the colon in. What
        makes one box serve all five is that 最大儲存量 is not a number: it
        misses every digit template by 52 bits and up, so the label is cut away
        wherever it falls and the capacity is what is left.
        """
        assert [
            storage_capacity((FRAMES / f"stock_tip_{name}.png").read_bytes(), row)
            for name, row in (
                ("day_gold", 0),
                ("day_elixir", 1),
                ("day_dark", 2),
                ("night_gold", 0),
                ("night_elixir", 1),
            )
        ] == [24_000_000, 24_000_000, 370_000, 2_050_000, 2_450_000]

    def test_the_builder_base_elixir_is_not_read_ten_times_over(self) -> None:
        """2 450 000, not 24 500 000, which is what a loose tolerance reads here.

        The colon comes back as a `2` at 40 bits, and that ceiling is ten times
        the real one — a village that could never fill it and a run that would
        never stand down. This is `CAPACITY_TOLERANCE`'s own test: the digits it
        has to accept sit at 3 bits and the character it has to reject at 40.
        """
        capacity = storage_capacity((FRAMES / "stock_tip_night_elixir.png").read_bytes(), 1)
        assert capacity == 2_450_000

    def test_a_frame_with_no_tooltip_open_reads_nothing(self) -> None:
        """Which is what leaves that resource out rather than guessing at one."""
        for world in ("world_day", "world_night"):
            png = (FRAMES / f"{world}.png").read_bytes()
            assert [storage_capacity(png, row) for row in range(3)] == [None, None, None]


class SurveyTests(unittest.TestCase):
    """The survey exists to catch the boundary reader disagreeing with the game."""

    def _ray(self, inside: bool, outside: bool) -> ProbeRay:
        return ProbeRay(degrees=30, predicted=400, inside_refused=inside, outside_refused=outside)

    def test_a_ray_agrees_only_when_the_line_sits_between_the_two_drops(self) -> None:
        assert self._ray(inside=True, outside=False).agrees
        # Accepted inside the predicted line: the reader put it too far out.
        assert not self._ray(inside=False, outside=False).agrees
        # Refused outside it: too far in, which is the one that loses troops.
        assert not self._ray(inside=True, outside=True).agrees

    def test_the_summary_counts_the_rays_that_could_not_be_read(self) -> None:
        survey = BoundarySurvey(
            rays=[self._ray(inside=True, outside=False), self._ray(inside=True, outside=True)],
            unread=[90.0, 270.0],
        )
        assert survey.agreement == "1/2 rays agreed, 2 unread"


class PlanTests(unittest.TestCase):
    """A tactic written down, so it can be replayed, edited, or swapped for the AI's."""

    def test_the_flat_plan_loads_and_carries_a_full_tactic(self) -> None:
        """Every act the loop can play, in an order that reads as an attack."""
        plan = plans.flat()
        assert plan.deploy_start is not None
        assert plan.deploy_end is not None
        played = [step.act for step in plan.steps]
        assert set(played) == {"siege", "troops", "hero", "ability", "rage", "freeze", "wait"}
        # The machine opens the path, the troops follow it, and no spell goes
        # down before there are troops for it to cover.
        assert played.index("siege") < played.index("troops") < played.index("rage")
        assert played.index("rage") < played.index("freeze")
        # As many bottles as an unreadable rage card is assumed to hold, so the
        # fallback tactic can place everything the loop asks it to.
        assert len(plan.acts("rage")[0].at) == attack.RAGE_BOTTLES
        # A tactic with no pause in it is one where every clock is zero.
        assert [step.seconds for step in plan.acts("wait")] == [4, 15, 1]

    def test_the_flat_plan_draws_the_line_the_loop_used_to_hold_in_constants(self) -> None:
        """It has to reproduce the old fallback, or the default quietly changed."""
        assert planned_line(plans.flat()) == DEPLOY_LINES["top_left"]

    def test_a_plan_survives_being_written_out_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plan.json"
            path.write_text(plans.flat().model_dump_json(indent=2), encoding="utf-8")
            assert plans.load(path) == plans.flat()

    def test_every_timing_on_an_attack_is_asked_for(self) -> None:
        """The planner answers all of them, and there is nowhere else to look.

        They used to be a table of constants in the settings file, chosen without
        a village on screen — which is the same guess the planner makes, minus
        the village. A field with a default is optional in the JSON schema, so
        anything a tactic cannot be played without carries none.
        """
        assert set(AttackPlan.model_json_schema()["required"]) == {"steps"}
        assert set(AttackStep.model_json_schema()["required"]) == {"act", "who", "at", "seconds"}

    def test_the_planner_is_asked_with_a_deadline_and_falls_back_without_one(self) -> None:
        """One call took 180.7 s and the three-minute battle it planned was over.

        The flat plan is right there and costs nothing, so a planner that will
        not answer inside the scout countdown is simply not waited for.
        """
        runner = AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            thresholds=LootThresholds(),
            ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()),
        )
        with patch.object(
            GeminiClient, "generate_structured", side_effect=TimeoutError("Request timed out")
        ) as asked:
            assert runner._plan(b"", 5, 1) == plans.flat()
        assert asked.call_args.args[3] == PLAN_TIMEOUT


class ConfigTests(unittest.TestCase):
    """One settings file, because a run has to play the same way from either side."""

    def test_a_missing_file_reads_as_what_the_window_has_always_shown(self) -> None:
        """Not the field defaults underneath: those are attack anything, stop at nothing."""
        with tempfile.TemporaryDirectory() as td:
            config = ConfigStore(path=Path(td) / "config.json").load()
        assert config.thresholds != LootThresholds()
        # 0 is "never stand down", which is the wrong thing to farm with — the
        # same trap as thresholds of zero, one field further along.
        assert config.stop_at

    def test_settings_survive_being_written_out_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(path=Path(td) / "config.json")
            saved = AppConfig(
                thresholds=LootThresholds(min_gold=1, min_elixir=2, min_dark=3),
                gemini=GeminiSettings(main=GeminiSetting(model="gemini-not-the-default")),
            )
            store.save(saved)
            assert store.load() == saved

    def test_a_setting_nothing_reads_any_more_is_dropped_from_the_file(self) -> None:
        """A key left behind reads as one still being honoured, and is not.

        `timings` stayed in every existing file for a release after every clock
        moved onto the plan, so someone editing 大守護者's thirty seconds there
        would have been editing nothing at all. `keepalive_seconds` is the next
        one out, now that holding the session open has been dropped. Pydantic
        ignoring the key is what keeps the upgrade from failing; rewriting the
        file is what stops it lying about what the run will do.
        """
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(
                json.dumps({
                    "restart_every": 7,
                    "timings": {"queen": 1, "warden": 30},
                    "keepalive_seconds": 120.0,
                }),
                encoding="utf-8",
            )
            config = ConfigStore(path=path).load()
            written = json.loads(path.read_text(encoding="utf-8"))
        assert config.restart_every == 7
        assert "timings" not in written
        assert "keepalive_seconds" not in written
        # What it parsed is what it wrote. Rewriting `AppConfig()` instead would
        # pass every other assertion here while wiping the user's settings, and
        # this call is the first thing that runs after an upgrade.
        assert written["restart_every"] == 7
        # And a key the file never had is filled in, so it reads as what this run
        # will actually do rather than as what happened to be saved once.
        assert written["stop_at"] == AppConfig().stop_at

    def test_a_file_already_matching_the_model_is_left_alone(self) -> None:
        """Rewriting on every load would touch the file a run only ever reads.

        Spying on `save` rather than watching the mtime: Windows advances a
        file's last-write time on the ~15.6 ms system tick rather than per
        write, so a timestamp comparison is asking the clock a question about
        the code, and this suite runs on Windows alone.
        """
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(path=Path(td) / "config.json")
            store.save(AppConfig(restart_every=7))
            with patch.object(ConfigStore, "save") as saved:
                assert store.load().restart_every == 7
            saved.assert_not_called()

    def test_a_file_that_will_not_parse_raises_rather_than_farming_on_defaults(self) -> None:
        """Silently defaulting is the failure this file was added to close."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text("{ not json at all", encoding="utf-8")
            with pytest.raises(ValidationError):
                ConfigStore(path=path).load()

    def test_an_omitted_minimum_leaves_the_configured_one_alone(self) -> None:
        base = LootThresholds(min_gold=500_000, min_elixir=400_000, min_dark=5_000)
        assert LootOverrides(min_gold=10).over(base) == LootThresholds(
            min_gold=10, min_elixir=400_000, min_dark=5_000
        )

    def test_a_minimum_passed_as_zero_really_takes_that_threshold_out(self) -> None:
        """Zero is the interesting value: it is how a run being studied attacks anything."""
        base = LootThresholds(min_gold=500_000, min_elixir=400_000, min_dark=5_000)
        assert LootOverrides(min_gold=0, min_elixir=0, min_dark=0).over(base) == LootThresholds()


class PromptTests(unittest.TestCase):
    """Prompts live as Markdown so a change to what the model is told is a readable diff."""

    def test_every_prompt_the_code_asks_for_exists_and_none_is_left_behind(self) -> None:
        """Both directions: a missing file is a `KeyError` at the first call, and a
        file nothing asks for is a prompt somebody will keep rewording for nothing.
        """
        assert set(PROMPTS) == {
            "attack_plan",
            "find_targets",
            "locate_target",
            "name_building",
            "night_plan",
        }

    def test_a_prompt_fills_in_its_placeholders(self) -> None:
        filled = render("locate_target", goal="設定齒輪")
        assert "設定齒輪" in filled
        assert "{" not in filled

    def test_the_attack_prompt_still_takes_its_spell_counts(self) -> None:
        """`_plan` formats these in; losing them would ask for the wrong spell count."""
        assert "{rage_count}" in PROMPTS["attack_plan"]
        assert "{freeze_count}" in PROMPTS["attack_plan"]

    def test_a_prompt_file_is_shipped_beside_the_module(self) -> None:
        """PyInstaller lays the bundle out this way, so the loader looks here."""
        assert (PROMPT_DIR / "attack_plan.md").is_file()


class MapFrameTests(unittest.TestCase):
    """The battle map is a diamond, and a rectangle's corners are not on it."""

    def test_a_preset_flank_is_on_the_map_and_a_pushed_corner_is_not(self) -> None:
        """Both points are live evidence: one deploys troops, the other lost a hero."""
        assert DEPLOY_BOUND.contains((600, 110))
        assert not DEPLOY_BOUND.contains((30, 175))

    def test_every_drop_the_survey_measured_is_inside_the_bound(self) -> None:
        """`ai_coc bounds` had all six rays taken at the screen edge, so it clamps none."""
        for point in ((1570, 400), (30, 400), (1100, 700), (500, 700), (505, 105), (1095, 105)):
            assert DEPLOY_BOUND.contains(point), point

    def test_clamping_pulls_a_point_back_onto_the_map(self) -> None:
        pulled = DEPLOY_BOUND.clamp((30, 175))
        assert DEPLOY_BOUND.contains(pulled)
        # Back along the line from the middle, so it keeps the direction it was pushed.
        assert pulled[0] < DEPLOY_BOUND.centre[0]
        assert pulled[1] < DEPLOY_BOUND.centre[1]

    def test_a_point_already_on_the_map_is_left_alone(self) -> None:
        assert DEPLOY_BOUND.clamp((600, 110)) == (600, 110)


class BoundaryTests(unittest.TestCase):
    """The red stroke the game draws around a village it will not accept drops inside.

    The two frames are live battles masked down to the rays the tests walk, one
    per village theme, because the theme changes the ground under the stroke.
    """

    def _painted(self, marks: list[tuple[int, tuple[int, int, int]]]) -> Image.Image:
        """A blank battle frame with vertical marks at the given x, for one ray east."""
        image = Image.new("RGB", (1600, 900), (60, 120, 40))
        pixels = image.load()
        for x, colour in marks:
            for offset in range(2):
                pixels[x + offset, 400] = colour
        return image

    def test_the_stroke_is_found_where_it_was_painted(self) -> None:
        found = boundary_reach(self._painted([(1200, (170, 70, 26))]), 0)
        assert found == (1201, 400)

    def test_the_outer_crossing_is_the_one_that_bounds_the_drop(self) -> None:
        """A ray leaving the middle crosses a stair-step boundary more than once."""
        marks = [(1000, (170, 70, 26)), (1300, (170, 70, 26))]
        assert boundary_reach(self._painted(marks), 0) == (1301, 400)

    def test_a_wall_highlight_is_too_bright_to_be_the_stroke(self) -> None:
        """Measured, a wall reads (255, 71, 0) and a fire (255, 140, 24)."""
        assert boundary_reach(self._painted([(1200, (255, 71, 0))]), 0) is None
        assert boundary_reach(self._painted([(1200, (255, 140, 24))]), 0) is None

    def test_a_frame_of_another_resolution_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="1600x900"):
            boundary_reach(Image.new("RGB", (1280, 720)), 0)

    def test_a_flank_is_fitted_onto_the_village_own_boundary(self) -> None:
        """A preset flank is drawn for a village that does not exist; this moves it."""
        png = (FRAMES / "battle_boundary_grass.png").read_bytes()
        # Ends on the 150 and 210 degree rays, so their midpoint lands on 180 —
        # all three kept in this frame, and the midpoint is fitted too.
        preset = ((540, 550), (540, 250))
        fitted = fitted_line(png, *preset)
        assert fitted is not None
        assert len(fitted) == 3
        for point in fitted:
            assert math.hypot(point[0] - 800, point[1] - 400) > 200

    def test_a_bent_line_walks_through_every_anchor(self) -> None:
        """A chord across a diamond cuts back inside it, so the middle gets its own anchor."""
        bent = deploy_line(5, (100, 150), (400, 150), (400, 400))
        assert bent[0] == (100, 150)
        assert bent[-1] == (400, 400)
        # The corner is an anchor, so a point lands on it rather than cutting it off.
        assert (400, 150) in bent

    def test_a_crossing_too_far_in_reads_as_nothing(self) -> None:
        """A ray that stops at the playfield edge never met a boundary lying beyond it.

        Whatever red it grazed on the way is then its answer, and two live
        surveys measured that landing at 0.45 of the distance travelled or less
        while every crossing that matched the game sat at 0.62 or more.
        """
        near = self._painted([(860, (170, 70, 26))])
        # 60 px out of the 770 this ray can travel: a building, not the boundary.
        assert boundary_reach(near, 0) is None
        far = self._painted([(1500, (170, 70, 26))])
        assert boundary_reach(far, 0) is not None

    def test_the_ui_the_game_paints_in_red_is_not_the_boundary(self) -> None:
        """摧毀率 and 結束戰鬥 sit in the corners a lower flank's rays run into.

        The outermost crossing is the answer, so a panel painted in the game's
        own red beats the real stroke every time. Measured on a recorded battle,
        the midpoint ray of the bottom-left flank answered (95, 665) — the 放棄
        button, 753 px out against a boundary sitting around 350 — and the
        bottom-right one answered (1556, 685).
        """
        image = Image.new("RGB", (1600, 900), (60, 120, 40))
        pixels = image.load()
        for step in (500, 691):
            # 691 steps out along this ray lands at (1449, 636), inside 摧毀率.
            pixels[self._along(20, step)] = (170, 70, 26)
        assert boundary_reach(image, 20) == self._along(20, 500)

    def test_a_ray_is_judged_on_the_distance_it_could_read(self) -> None:
        """The blanked corner is not ground the ray was ever able to look at.

        This ray meets 放棄 at 613 steps and the playfield edge at 825, so a
        crossing 400 out is 0.65 of what was readable and 0.48 of what was
        walked — under `MIN_REACH_RATIO` and thrown away, which is the same
        correct reading the blanking above exists to expose.
        """
        image = Image.new("RGB", (1600, 900), (60, 120, 40))
        pixels = image.load()
        pixels[self._along(159, 400)] = (170, 70, 26)
        assert boundary_reach(image, 159) == self._along(159, 400)

    def _along(self, degrees: float, step: int) -> tuple[int, int]:
        """The point a ray of `boundary_reach`'s own walks at this step."""
        return (
            round(800 + math.cos(math.radians(degrees)) * step),
            round(400 + math.sin(math.radians(degrees)) * step),
        )

    def test_a_flank_whose_anchors_disagree_is_thrown_away(self) -> None:
        """One ray matching a wall inside the village puts an anchor where drops are refused.

        `ai_coc probe` measured this on four of twelve rays: the reader put the
        line well inside the boundary the game was actually enforcing. These
        three rays read 585, 381 and 182 from the middle, which no single flank's
        boundary does, so the fit is dropped and the caller probes as before.
        """
        png = (FRAMES / "battle_boundary_grass.png").read_bytes()
        assert fitted_line(png, (500, 400), (650, 140)) is None

    def test_a_flank_with_no_boundary_under_it_is_left_alone(self) -> None:
        """Both ends have to read, or the caller keeps its preset and probes."""
        blank = Image.new("RGB", (1600, 900), (60, 120, 40))
        buffer = io.BytesIO()
        blank.save(buffer, format="PNG")
        assert fitted_line(buffer.getvalue(), (600, 110), (230, 380)) is None

    def test_the_village_is_found_in_the_middle_of_the_screen(self) -> None:
        """Both themes, so nothing downstream has to take the camera on trust."""
        for name in ("battle_boundary_grass", "battle_boundary_ice"):
            box = village_box((FRAMES / f"{name}.png").read_bytes())
            assert box is not None, name
            middle = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            assert math.hypot(middle[0] - 800, middle[1] - 400) < 60, (name, middle)

    def test_a_screen_with_no_boundary_on_it_has_no_village_box(self) -> None:
        assert village_box((FRAMES / "attack_menu.png").read_bytes()) is None

    def test_the_boundary_is_read_over_two_village_themes(self) -> None:
        """The theme repaints the ground under the stroke, not the stroke itself."""
        rays = [angle * 30 for angle in range(12)]
        for name in ("battle_boundary_grass.png", "battle_boundary_ice.png"):
            frame = (FRAMES / name).read_bytes()
            points = [reach for angle in rays if (reach := boundary_reach(frame, angle))]
            radii = sorted(math.hypot(x - 800, y - 400) for x, y in points)
            assert len(points) >= 6, name
            # One closed curve, so the readings sit well out from the middle.
            assert radii[len(radii) // 2] > 200, (name, radii)


class FieldTests(unittest.TestCase):
    """Reading how far the camera moved by sliding one frame over the other."""

    def _village(self, drop: int) -> bytes:
        """Something with enough texture to line up, drawn `drop` px further down."""
        image = Image.new("RGB", (1600, 900), (60, 120, 40))
        painter = ImageDraw.Draw(image)
        for row in range(6):
            for column in range(9):
                left, top = 420 + column * 80, 180 + row * 70 + drop
                painter.rectangle(
                    (left, top, left + 40 + column * 2, top + 30 + row * 3),
                    fill=(40 + column * 20, 80, 200 - row * 25),
                )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_the_drag_the_game_took_is_read_off_the_two_frames(self) -> None:
        """`village_box` said 23 px of a 98 px drag the game had taken in full."""
        shift = view_shift(self._village(0), self._village(98), (0, 98))
        # The search is in steps, so the answer is to within one of them.
        assert shift[0] == 0
        assert abs(shift[1] - 98) <= 10

    def test_a_drag_the_game_never_took_reads_as_none_of_it(self) -> None:
        assert view_shift(self._village(0), self._village(0), (0, 98)) == (0, 0)


class AttackTests(unittest.TestCase):
    def test_every_threshold_has_to_be_met(self) -> None:
        offer = LootOffer(gold=700000, elixir=500000, dark=2000)
        assert LootThresholds(min_gold=500000, min_elixir=500000).accepts(offer)
        assert not LootThresholds(min_gold=500000, min_dark=5000).accepts(offer)

    def test_a_threshold_left_at_zero_ignores_that_resource(self) -> None:
        assert LootThresholds().accepts(LootOffer(gold=0, elixir=0, dark=0))

    def test_one_full_storage_is_not_a_reason_to_stop(self) -> None:
        """A battle brings home three resources, so one ceiling earns the fee out.

        This stopped on the first storage to fill until a live village showed
        what that costs: its elixir cap had grown past the configured limit, so
        every check stood the run down with five million of gold room unused,
        and no amount of farming could ever fill it.
        """
        capacity = StorageCapacity(gold=24_000_000, elixir=24_000_000)
        assert capacity.full(VillageStock(gold=24_000_000, elixir=400_000, dark=1000), 90) is None

    def test_farming_stops_once_every_watched_storage_is_full(self) -> None:
        capacity = StorageCapacity(gold=24_000_000, elixir=24_000_000)
        full = capacity.full(VillageStock(gold=22_000_000, elixir=23_400_000, dark=1000), 90)
        # Dark has no ceiling here, so it neither stops the run nor holds it open —
        # which is what the builder base's gems row relies on.
        assert full == ["金幣", "聖水"]

    def test_a_storage_short_of_the_share_holds_the_run_open(self) -> None:
        """21 599 999 of 24 000 000 is 89.99%, and the run keeps farming on it."""
        capacity = StorageCapacity(gold=24_000_000)
        assert capacity.full(VillageStock(gold=21_599_999, elixir=0, dark=0), 90) is None
        assert capacity.full(VillageStock(gold=21_600_000, elixir=0, dark=0), 90) == ["金幣"]

    def test_a_share_of_zero_watches_nothing(self) -> None:
        capacity = StorageCapacity(gold=24_000_000, elixir=24_000_000, dark=370_000)
        assert capacity.full(VillageStock(gold=99999999, elixir=99999999, dark=99999), 0) is None

    def test_a_village_whose_ceilings_never_read_never_stands_a_run_down(self) -> None:
        assert StorageCapacity().full(VillageStock(gold=99999999, elixir=1, dark=1), 90) is None

    def test_deploy_line_runs_the_whole_flank(self) -> None:
        points = deploy_line(8)
        assert len(points) == 8
        assert points[0] == DEPLOY_START
        assert points[-1] == DEPLOY_END

    def test_a_hero_step_names_the_hero_rather_than_the_slot(self) -> None:
        """An upgrading hero has no card at all, so every slot after it shifts.

        Naming the hero on its own step is what survives that: the loop matches
        `hero` steps to cards left to right, so a row one card shorter costs the
        missing hero and nothing else. The name is what makes a bad battle
        legible afterwards — `plans.jsonl` says which hero went where.
        """
        steps = [_step("hero", (20, 40), who="queen"), _step("hero", (45, 35), who="warden")]
        assert [step.who for step in steps] == ["queen", "warden"]
        assert steps[0].at[0].pixels() == (320, 360)

    def test_pushing_a_drop_out_moves_it_off_the_middle_and_stays_on_screen(self) -> None:
        point = DEPLOY_START
        assert push_out(point, 0) == point
        pushed = push_out(point, 4)
        # DEPLOY_START sits up and left of centre, so pushing goes further that way.
        assert pushed[0] < point[0]
        assert pushed[1] < point[1]
        assert PLAYFIELD[0] <= pushed[0] <= PLAYFIELD[2]
        assert PLAYFIELD[1] <= pushed[1] <= PLAYFIELD[3]

    def test_a_pushed_drop_never_lands_on_the_abandon_button(self) -> None:
        """One that did opened 結束戰鬥？, which then read as the battle being over."""
        grid = [
            (x, y)
            for x in range(PLAYFIELD[0], PLAYFIELD[2] + 1, 70)
            for y in range(PLAYFIELD[1], PLAYFIELD[3] + 1, 70)
        ]
        drops = [push_out(point, steps) for point in grid for steps in range(DEPLOY_ATTEMPTS)]
        assert not any(x < ABANDON_BUTTON[0] and y > ABANDON_BUTTON[1] for x, y in drops)

    def test_a_drawn_line_never_crosses_the_abandon_button(self) -> None:
        """Both ends can clear that corner while the span between them cuts across it."""
        points = deploy_line(LINE_POINTS, (30, 700), (600, 700))
        assert not any(x < ABANDON_BUTTON[0] and y > ABANDON_BUTTON[1] for x, y in points)

    def test_a_line_the_planner_drew_under_the_card_row_is_pulled_back_up(self) -> None:
        """Gemini answered y_pct 80, which is y 720 — the army bar, not the ground."""
        under = deploy_line(LINE_POINTS, (320, 522), (672, 720))
        assert all(PLAYFIELD[1] <= y <= PLAYFIELD[3] for _, y in under)
        assert all(PLAYFIELD[0] <= x <= PLAYFIELD[2] for x, _ in under)

    def test_the_lower_flanks_still_push_past_a_wide_village(self) -> None:
        """Trimming the whole bottom edge to dodge that button left them nowhere to go."""
        _, end = DEPLOY_LINES["bottom_left"]
        assert push_out(end, DEPLOY_ATTEMPTS - 1)[1] > end[1]

    def test_each_pass_spreads_its_drops_and_shifts(self) -> None:
        """A card holding one troop must not drop it where every other card started."""
        line = deploy_line(LINE_POINTS)
        first, second = drop_points(line, 0), drop_points(line, 1)
        # The first circuit is every point on the line and nothing else; the
        # ones after it land between those, which the next test covers.
        assert set(first[:LINE_POINTS]) == set(line)
        assert first[0] != second[0]

    def test_a_pass_outlasts_the_biggest_card_this_village_fields(self) -> None:
        """One pass has to empty the card, or the tail of it lands on its own.

        Measured on a live row of x9 dragons and x16 balloons at one circuit: the
        first pass left four balloons, and they went down 1.4 seconds later —
        behind the push, and balloons are slow.
        """
        assert DROPS_PER_PASS >= 16

    def test_a_counted_card_is_tapped_as_many_times_as_it_holds(self) -> None:
        """Plus one for slack, the way a spell card already is.

        A fixed pass spends whatever is left of its circuits on ground with
        nothing selected: measured on a live row of x9, x3 and x2 against a
        24-tap pass, that is 55 taps of nothing — about 3.4 s of a ten-second
        deployment.
        """
        line = deploy_line(LINE_POINTS)
        assert len(drop_points(line, 0, 4)) == 4
        # And an unreadable corner still gets the whole pass, which is what the
        # fixed count was always for.
        assert len(drop_points(line, 0)) == DROPS_PER_PASS

    def _burst(self, counts: dict[int, int | None]) -> list[list[tuple[int, int]]]:
        """The taps `_spread_troops` sends, given what each card's corner reads."""
        runner = self._runner()
        sent: list[list[tuple[int, int]]] = []

        def tapped(
            _self: object, points: list[tuple[int, int]], display: object, **_: object
        ) -> None:
            sent.append(points)

        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack, "card_count", side_effect=lambda _png, slot: counts[slot]),
            patch.object(attack, "card_drained", return_value=True),
            patch.object(attack, "live_cards", return_value=[]),
            patch.object(AdbController, "tap_many", autospec=True, side_effect=tapped),
            patch.object(attack.time, "sleep"),
        ):
            runner._spread_troops(list(counts), DEPLOY_LINES["top_left"], 0)
        return sent

    def test_a_card_that_reads_high_is_still_held_to_one_pass(self) -> None:
        """This reader fails high, and a committed fixture proves it.

        A four-pixel sliver of card art past the last digit matches a `1` inside
        tolerance on some frames and not others: across three fixtures the same
        x12 card reads 12, None and 121. Taken at face value the last is 122
        drops in one burst, about 7.6 s — more than double what reading the
        count saves, with every hero waiting behind it.
        """
        assert card_count((FRAMES / "cards_dark_hero.png").read_bytes(), 171) == 121
        [burst] = self._burst({171: 121})
        # One card-select tap, then no more drops than the pass would have sent.
        assert len(burst) - 1 == DROPS_PER_PASS

    def test_a_card_that_reads_gets_its_own_taps_and_one_spare(self) -> None:
        """And a corner the artwork swallowed still gets the whole pass."""
        counted, swallowed = self._burst({293: 3, 171: None})
        assert len(counted) - 1 == 4
        assert len(swallowed) - 1 == DROPS_PER_PASS

    def test_a_card_bigger_than_the_line_does_not_stack_its_tail_on_its_head(self) -> None:
        """The stride orders one circuit; the second walks the same points again.

        So a card of sixteen would put its last four on the exact pixels its
        first four went to — the thing `DROP_STRIDE` exists to prevent, applied
        to a card big enough to come round. The later circuits land between the
        first one's points instead.
        """
        line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        spots = drop_points(line, 0)
        head, tail = spots[:LINE_POINTS], spots[LINE_POINTS:]
        assert not set(head) & set(tail)
        # Between, not merely elsewhere: each one sits inside the gap it came from.
        gap = math.dist(line[0], line[1])
        for index, spot in enumerate(tail):
            assert 0 < math.dist(spot, head[index]) < gap

    def test_the_stride_still_covers_the_whole_line(self) -> None:
        """Coprime with the line, or a pass walks a few points over and over."""
        assert math.gcd(DROP_STRIDE, LINE_POINTS) == 1

    def test_a_part_filled_card_lands_its_troops_well_apart(self) -> None:
        """Three baby dragons 41 px apart are inside each other's 84 px rage radius."""
        line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        held = drop_points(line, 1)[:3]
        for index, (x, y) in enumerate(held):
            assert all(math.hypot(x - a, y - b) > 150 for a, b in held[index + 1 :])

    def test_a_planned_line_along_the_village_edge_is_used(self) -> None:
        """The percentages of the top-left flank, which is a line the loop can push out."""
        plan = AttackPlan(steps=[_step("troops", *_LINE_ENDS)])
        assert planned_line(plan) == DEPLOY_LINES["top_left"]

    def test_a_planned_line_across_the_village_falls_back_to_a_flank(self) -> None:
        """Its midpoint sits on the middle, where push_out has no direction to move it."""
        plan = AttackPlan(steps=[_step("troops", (25, 30), (75, 70))])
        assert planned_line(plan) is None

    def test_a_line_only_half_drawn_falls_back_too(self) -> None:
        """A `troops` step with one end is no line, and neither is no plan at all."""
        assert planned_line(AttackPlan(steps=[_step("troops", (37.5, 12.2))])) is None
        assert planned_line(None) is None

    def _runner(self) -> AttackRunner:
        return AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            thresholds=LootThresholds(),
        )

    def _played(
        self, steps: list[AttackStep], row: BattleRow, landed: list[int] | None = None
    ) -> tuple[list[str], list[float]]:
        """Run a tactic; what it did in order, and how long each pause really held.

        Everything that would touch the emulator is stubbed, so what comes back
        is the sequence of decisions rather than any taps.
        """
        runner = self._runner()
        acts: list[str] = []
        pauses: list[float] = []
        plan = AttackPlan(steps=steps)

        def settle(*_: object) -> None:
            # The real one empties what it just read, which is what lets a later
            # drop get a reading of its own.
            acts.append("settle")
            runner._sending = {}

        def act(step: AttackStep, _row: BattleRow, _line: list[tuple[int, int]]) -> None:
            acts.append(step.act)
            if step.act in ("siege", "hero"):
                runner._sending.update(
                    dict.fromkeys(_row.machine if step.act == "siege" else _row.heroes[:1], (0, 0))
                )

        with (
            patch.object(AttackRunner, "_act", side_effect=act),
            patch.object(AttackRunner, "_settle_drops", side_effect=settle),
            patch.object(AttackRunner, "_battle_ended", return_value=False),
            patch.object(attack.time, "sleep", side_effect=pauses.append),
        ):
            runner._play_tactic(plan, DEPLOY_LINES["top_left"], row)
        return acts, pauses

    def _row(self, **kw: object) -> BattleRow:
        fields: dict[str, object] = {
            "troops": [100, 200],
            "machine": [400],
            "heroes": [500, 600],
            "rages": [900],
            "freezes": [1000],
            "rage_count": 4,
            "frame": b"",
        }
        fields.update(kw)
        return BattleRow(**fields)  # type: ignore[arg-type]

    def test_a_tactic_is_played_in_the_order_it_was_written(self) -> None:
        """The loop imposes none of its own, which is the whole of the change.

        It used to be siege, then troops, then heroes, and only once all of them
        were down did anything on a clock get a turn — so a rage waited out the
        heroes for no reason a rage recognises, since it covers the troops and
        they are already walking.
        """
        acts, _ = self._played(
            [
                _step("siege", (30, 30)),
                _step("troops", *_LINE_ENDS),
                _step("hero", (25, 25), who="queen"),
                _step("wait", seconds=3),
                _step("rage", (40, 40)),
                _step("hero", (28, 28), who="king"),
            ],
            self._row(),
        )
        # The trailing settle is the last hero getting read too: a tactic that
        # drops after its first pause used to go unchecked from there on.
        assert acts == ["siege", "troops", "hero", "settle", "rage", "hero", "settle"]

    def test_a_pause_is_counted_from_the_move_before_it_finishing(self) -> None:
        """Which is what makes the whole tactic immune to how slowly it got here.

        The clocks used to run from the loop's own idea of when the battle
        opened, and that moves with how long Gemini took to answer: measured
        over 23 recorded rounds the gap those numbers were really about, troops
        down to rage cast, ran from 3.0 to 10.8 seconds while the planner asked
        for the same 14 every time.
        """
        runner = self._runner()
        clock = [0.0]
        pauses: list[float] = []
        plan = AttackPlan(
            steps=[_step("troops", *_LINE_ENDS), _step("wait", seconds=6), _step("rage", (40, 40))]
        )

        def act(step: AttackStep, *_: object) -> None:
            # Putting the army down is not free, and the pause after it is not
            # what should be paying for that.
            clock[0] += 5

        with (
            patch.object(AttackRunner, "_act", side_effect=act),
            patch.object(AttackRunner, "_settle_drops"),
            patch.object(AttackRunner, "_battle_ended", return_value=False),
            patch.object(attack.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(attack.time, "sleep", side_effect=pauses.append),
        ):
            runner._play_tactic(plan, DEPLOY_LINES["top_left"], self._row())
        # The whole six seconds, not six minus the five the deployment took.
        assert pauses == [6]

    def test_the_one_reading_of_the_burst_happens_inside_the_first_pause(self) -> None:
        """A pause is the only idle time in a battle, so the check is free there.

        Measured on one recorded round, putting the army down took 9.15 seconds
        of which about 1.5 was tapping: six captures at roughly 0.7 s each plus
        the settles that exist only so those captures have something to see.
        A refused drop costs nothing to find late, because the game does not
        consume the card it refused.
        """
        acts, _ = self._played(
            [
                _step("siege", (30, 30)),
                _step("wait", seconds=4),
                _step("rage", (40, 40)),
                _step("wait", seconds=4),
            ],
            self._row(),
        )
        # Once, in the first pause long enough to hide it, and never again.
        assert acts.count("settle") == 1
        assert acts == ["siege", "settle", "rage"]

    def test_a_pause_too_short_to_hide_a_capture_is_left_alone(self) -> None:
        """A check that overruns the pause it sits in is the lateness steps exist to remove."""
        acts, _ = self._played(
            [_step("siege", (30, 30)), _step("wait", seconds=1), _step("rage", (40, 40))],
            self._row(),
        )
        # Nothing is read during the short pause; the reading falls to the end.
        assert acts == ["siege", "rage", "settle"]

    def test_a_tactic_stops_once_the_battle_has_ended_under_it(self) -> None:
        """Some card slots sit exactly where the result screen draws 回營.

        The old schedule checked before every move it played, at a capture
        apiece. This checks inside the pauses instead, where the capture is free
        — but it still has to check, or a tactic outliving its battle taps its
        way out of the village.
        """
        runner = self._runner()
        acts: list[str] = []
        plan = AttackPlan(
            steps=[_step("troops", *_LINE_ENDS), _step("wait", seconds=4), _step("rage", (40, 40))]
        )
        with (
            patch.object(AttackRunner, "_act", side_effect=lambda step, *a: acts.append(step.act)),
            patch.object(AttackRunner, "_battle_ended", return_value=True),
            patch.object(attack.time, "sleep"),
        ):
            runner._play_tactic(plan, DEPLOY_LINES["top_left"], self._row())
        assert acts == ["troops"]

    def test_a_hero_step_takes_the_next_card_unless_it_names_nobody(self) -> None:
        """Nothing on the row says who is on which card, so order is the only handle.

        **`unknown` means every hero still in hand, not the next one.** A tactic
        written before any army was seen — `plans.flat()` cannot know the row —
        has no way to name them one at a time, and taking a single card for each
        such step sent one hero and left the rest in their cards for the whole
        battle: measured live, `1 of 2 one-off card(s) never landed` on an army
        carrying a machine and three heroes.

        A step naming a hero the row does not carry, one upgrading so its card
        is simply gone, runs out of cards and sends nothing rather than shifting
        the rest along.
        """
        assert self._sent([_step("hero", (20, 20), who="queen")] * 3) == [[500], [600], []]
        assert self._sent([_step("hero", (20, 20))]) == [[500, 600]]

    def test_spell_steps_with_nothing_between_them_become_one_cast(self) -> None:
        """The prompt asks for one step per spell and the planner writes one per bottle.

        Measured on its first live reply: `rage → rage → rage → rage`, and each
        step selects the card again and reads it back, about 3.4 s apiece. Those
        four spread from 11 s to 21 s into the battle against a rage that lasts
        18, so the first had nearly expired before the last went down.
        """
        four = [_step("rage", (10 * n + 10, 20)) for n in range(4)]
        played = merged([*four, _step("wait", seconds=5), _step("freeze", (50, 50))])
        assert [step.act for step in played] == ["rage", "wait", "freeze"]
        assert len(played[0].at) == 4

    def test_two_casts_a_pause_apart_are_left_apart(self) -> None:
        """Rage now and rage again when the push reaches the next ring is a real tactic.

        Only neighbours with nothing between them are folded, because nothing
        separates those but the loop's own cost.
        """
        spread = [_step("rage", (20, 20)), _step("wait", seconds=6), _step("rage", (60, 40))]
        assert merged(spread) == spread

    def _sent(self, steps: list[AttackStep]) -> list[list[int]]:
        """Which cards each `hero` step put on the field, in order."""
        runner = self._runner()
        sent: list[list[int]] = []
        with (
            patch.object(
                AttackRunner, "_drop_at", side_effect=lambda cards, spot: sent.append(list(cards))
            ),
            patch.object(AttackRunner, "_settle_drops"),
            patch.object(attack.time, "sleep"),
        ):
            runner._play_tactic(
                AttackPlan(steps=steps), DEPLOY_LINES["top_left"], self._row(heroes=[500, 600])
            )
        return sent

    def test_an_ability_reaches_the_heroes_the_tactic_sent_and_no_others(self) -> None:
        """A tap on a card whose hero never landed deploys it instead of firing.

        And it has to survive a reading: those empty what they just checked, so
        an `ability` step after one used to reach nobody at all. Which heroes are
        out is remembered separately from which drops are still waiting to be
        read, because the two questions have different lifetimes.
        """
        sent = _step("hero", (20, 20), who="queen")
        assert self._fired([sent, _step("ability")]) == [(500, CARD_ROW_Y)]
        # The same thing with a reading in between, which is where it broke.
        after_reading = [sent, _step("wait", seconds=4), _step("ability")]
        assert self._fired(after_reading) == [(500, CARD_ROW_Y)]

    def test_an_ability_fires_the_hero_it_names_and_not_the_others(self) -> None:
        """Firing them all spends abilities the plan meant to hold.

        On `hero queen → hero king → ability queen → wait 20s → ability king`
        the king's went at 2 s instead of 22. Live it showed as
        你已經用過這項英雄技能了 on the later step — the game refusing a hero
        the earlier one had already spent.
        """
        early = [
            _step("hero", (20, 20), who="queen"),
            _step("hero", (30, 30), who="king"),
            _step("ability", who="queen"),
        ]
        assert self._fired(early) == [(500, CARD_ROW_Y)]
        # And the other one when its own step comes round.
        assert self._fired([*early, _step("ability", who="king")]) == [(600, CARD_ROW_Y)]

    def test_a_card_the_row_grew_mid_battle_is_emptied_rather_than_carried_home(self) -> None:
        """Measured live: a ninth card in colour at 62%, holding 23, that the plan never saw.

        An event handed out troops after the army was down, so no step named
        them. Whatever put it there does not matter — the question is only
        whether anything is still deployable, and the battle poll was taking
        that frame anyway. It is the same guard against a plan that simply left
        a card out.
        """
        runner = self._runner()
        runner._line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        poured: list[list[int]] = []
        with (
            patch.object(attack, "card_groups", return_value=[[171], [900], [939]]),
            # The `xN` corner: the newcomer and the spell carry one, the hero does not.
            patch.object(attack, "counted_cards", return_value=[171, 939]),
            patch.object(
                AttackRunner, "_cast", side_effect=lambda cards, *a: poured.append(cards)
            ),
        ):
            runner._dump_leftovers()
        # Both cards carrying an `xN`, and not the hero standing on the field.
        # The spell goes out too: one nobody cast is one carried home, and
        # excluding it by the x it sat at when the row was read is exactly what
        # a row that has since grown a card makes wrong.
        assert poured == [[171, 939]]

    def test_nothing_left_in_a_card_is_left_alone(self) -> None:
        """The ordinary case, and it must not tap anything at all."""
        runner = self._runner()
        runner._line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        with (
            patch.object(attack, "card_groups", return_value=[[900]]),
            patch.object(attack, "counted_cards", return_value=[]),
            patch.object(AttackRunner, "_cast") as poured,
        ):
            runner._dump_leftovers()
        poured.assert_not_called()

    def _fired(self, steps: list[AttackStep]) -> list[tuple[int, int]]:
        """Which cards the tactic's last tap went to."""
        runner = self._runner()
        tapped: list[list[tuple[int, int]]] = []

        def settle(*_: object) -> None:
            runner._onfield += list(runner._sending)
            runner._sending = {}

        with (
            patch.object(AttackRunner, "_settle_drops", side_effect=settle),
            patch.object(AttackRunner, "_battle_ended", return_value=False),
            patch.object(
                type(runner.adb),
                "tap_many",
                side_effect=lambda points, *a, **k: tapped.append(list(points)),
            ),
            patch.object(attack.time, "sleep"),
        ):
            runner._play_tactic(
                AttackPlan(steps=steps), DEPLOY_LINES["top_left"], self._row(heroes=[500, 600])
            )
        return tapped[-1]

    def _scouted(self, readings: list[ScoutView | None], offered: list[bool]) -> int:
        """Poll the scout screen over canned readings; how many times 下一個 was tapped."""
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(AttackRunner, "_tap") as tapped,
            patch.object(attack, "read_scout", side_effect=readings),
            patch.object(attack, "skip_offered", side_effect=offered),
            patch.object(attack.time, "sleep"),
        ):
            runner._scout(timeout=60)
        return tapped.call_count

    def test_a_search_still_running_is_waited_out(self) -> None:
        """正在搜尋對手 has no 下一個 on it, and nothing is owed to a search but patience."""
        found = ScoutView(loot=LootOffer(gold=1, elixir=1, dark=1), can_skip=True)
        assert self._scouted([None] * 8 + [found], [False] * 8 + [True]) == 0

    def test_an_opponent_whose_loot_will_not_read_is_swapped_for_another(self) -> None:
        """Standing on one is what lets the countdown start the battle with the army in hand.

        Measured over 72 groups of consecutive scout frames, every opponent that
        eventually read did so within 2 consecutive misses and the two that never
        read ran to 17 — so a run this long is a panel, not a slow frame.
        """
        found = ScoutView(loot=LootOffer(gold=1, elixir=1, dark=1), can_skip=True)
        assert self._scouted([None] * UNREADABLE_SKIPS + [found], [True] * UNREADABLE_SKIPS) == 1

    def test_the_button_separates_the_two_screens_read_scout_cannot(self) -> None:
        """The premise the swap rests on, on the two frames that actually differ.

        `read_scout` answers None for both of these, so nothing else on either
        screen can say which one a run is looking at.
        """
        assert skip_offered((FRAMES / "searching.png").read_bytes()) is False
        smudged = (FRAMES / "scout_smudged_digit.png").read_bytes()
        assert read_scout(smudged) is None
        assert skip_offered(smudged) is True

    def test_a_search_given_up_on_is_left_through_the_button(self) -> None:
        """Walking away leaves a live countdown to start the battle without an army.

        That is what the timeout used to land on only after the countdown had
        expired; a swap can now put a seconds-old opponent on screen instead, so
        leaving has to be deliberate. 結束戰鬥 is only safe while 下一個 is up,
        which is exactly what the last frame `_scout` read answers.
        """
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(AttackRunner, "_tap") as tapped,
            patch.object(AttackRunner, "_open_attack_menu", return_value=b""),
            patch.object(attack, "read_stock", return_value=None),
            patch.object(attack, "army_strength", return_value=None),
            patch.object(attack, "read_scout", return_value=None),
            patch.object(attack, "skip_offered", return_value=True),
            patch.object(attack.time, "sleep"),
            # The deadline as well as the sleeps, or the poll spins for a real
            # thirty seconds: the first reading sets it and the rest walk past it.
            patch.object(attack.time, "monotonic", side_effect=[0, *range(0, 200, 7)]),
        ):
            report = runner.run()
        assert "等不到對手畫面" in report.message
        assert tapped.call_args_list[-1].args[0] == END_BATTLE

    def _verdict(self, opening: LootOffer, readings: list[ScoutView | None]) -> bool:
        return self._watched(opening, readings)[0]

    def _watched(
        self, opening: LootOffer, readings: list[ScoutView | None]
    ) -> tuple[bool, AttackRunner]:
        """Run the battle wait against canned panel readings, with the clock removed.

        A reading of None here stands for the result screen, which is what ends
        the wait: a panel that merely will not read no longer does, since a
        battlefield showing through one is not a battle that has ended. The
        trailing Falses are for `_leave_result`, which polls the same button.
        """
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(AttackRunner, "_tap"),
            patch.object(attack, "read_scout", side_effect=readings),
            patch.object(
                attack,
                "battle_over",
                side_effect=[view is None for view in readings] + [False] * RESULT_ATTEMPTS,
            ),
            patch.object(attack.time, "sleep"),
        ):
            # Whatever the abilities saw counts too, which is the whole point.
            runner._battle_ended("ability")
            return runner._wait_out_battle(opening), runner

    def test_a_battle_won_before_the_first_poll_still_counts(self) -> None:
        """100% three stars, but over so fast that only the ability check saw the loot fall."""
        opening = LootOffer(gold=1031321, elixir=420990, dark=2525)
        during = ScoutView(loot=LootOffer(gold=149101, elixir=19976, dark=120), can_skip=False)
        assert self._verdict(opening, [during, None])

    def test_loot_that_never_moves_is_still_reported_as_a_failure(self) -> None:
        """The case this check exists for: an army that never reached the village."""
        opening = LootOffer(gold=1031321, elixir=420990, dark=2525)
        stuck = ScoutView(loot=opening, can_skip=False)
        assert not self._verdict(opening, [stuck, stuck, None])

    def test_a_battle_nobody_ever_read_is_not_called_a_success(self) -> None:
        assert not self._verdict(LootOffer(gold=1, elixir=1, dark=1), [None, None])

    def test_a_panel_nobody_could_read_is_told_apart_from_one_that_never_moved(self) -> None:
        """Both come back False, and only one of them is an army that never landed.

        Measured over one twelve-round run, two rounds polled a panel that would
        not resolve on any frame and had in fact taken 800k and 1.1M. `_seen` is
        what keeps the message for those off the deployment alarm.
        """
        opening = LootOffer(gold=1031321, elixir=420990, dark=2525)
        stuck = ScoutView(loot=opening, can_skip=False)
        assert self._watched(opening, [None, None])[1]._seen is None
        assert self._watched(opening, [stuck, None])[1]._seen == opening

    def _zoomed(self, box: tuple[int, int, int, int] | None) -> tuple[MagicMock, list[str]]:
        """Run the pre-battle zoom against a village measured at `box`."""
        runner = self._runner()
        runner.adb = MagicMock()
        with (
            patch.object(attack, "village_box", return_value=box),
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack.logger, "warning") as warned,
        ):
            runner._settle_zoom(b"")
        return runner.adb, [str(call.args[0]) for call in warned.call_args_list]

    def test_the_camera_is_put_back_at_the_far_zoom_every_round(self) -> None:
        """Asked for rather than checked, because there is nothing to check against.

        The game reports no zoom level, and reading one off a frame was defeated
        twice — the deployment boundary shrinks as buildings fall, and the
        village ground gets covered by whatever panel the game opens. Zooming out
        past the limit does nothing, so the way to be there is to ask, and the
        pinch goes out whether or not the frame could be measured.
        """
        adb, warned = self._zoomed(None)
        # Aimed at the game's own display, or the same two fingers land on the
        # launcher MuMu keeps on the others and switch away from the game.
        adb.zoom.assert_called_once_with(
            "out", attack.ZOOM_PINCHES, attack.COC_PACKAGE, self._runner().display
        )
        assert not warned

    def test_a_village_too_short_to_fit_is_the_evidence_the_camera_drifted(self) -> None:
        """The safety net is also the only detector, and the height is the signal.

        A village grown too big for the screen is clipped top and bottom, so it
        reads short whatever the opponent's layout — while the width says
        nothing, because every round faces a different village. Swept over one
        live day the healthy rounds ran 500 to 572 tall and the single round
        that deployed nothing came back 411.
        """
        assert not self._zoomed((54, 114, 1562, 667))[1]
        clipped = self._zoomed((85, 135, 1566, 546))[1]
        assert any("far zoom" in line for line in clipped)

    def _settled(self, box: tuple[int, int, int, int]) -> list[tuple[int, int]]:
        """Every drag `_settle_camera` asks for, given a village measured at `box`."""
        return self._dragged(box, lambda runner: runner._settle_camera(b""))[0]

    def _dragged(
        self,
        box: tuple[int, int, int, int] | None,
        move: Callable[[AttackRunner], object],
        taken: float = 1.0,
    ) -> tuple[list[tuple[int, int]], AttackRunner]:
        """Every drag `move` asks for, with the game taking `taken` of each one."""
        runner = self._runner()
        swipes: list[tuple[int, int]] = []
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack, "village_box", return_value=box),
            patch.object(
                attack,
                "view_shift",
                lambda _before, _after, drift: (round(drift[0] * taken), round(drift[1] * taken)),
            ),
            patch.object(attack.time, "sleep"),
            patch.object(
                type(runner.adb),
                "swipe",
                lambda _self, start, end, _ms, _display: swipes.append((
                    end[0] - start[0],
                    end[1] - start[1],
                )),
            ),
        ):
            move(runner)
        return swipes, runner

    def test_a_camera_left_off_centre_is_dragged_back(self) -> None:
        """Measured live: knocked 96 px left, one drag put it back within 15."""
        # Middle (704, 400), so the village has to move 96 px to the right.
        assert self._settled((104, 100, 1304, 700))[0] == (96, 0)

    def test_a_camera_already_on_the_village_is_left_alone(self) -> None:
        """The game centres every attack itself; dragging a good camera can only hurt."""
        assert self._settled((200, 120, 1380, 680)) == []

    def test_a_frame_that_will_not_measure_leaves_the_camera_alone(self) -> None:
        assert self._settled(None) == []
        assert self._cleared(DEPLOY_LINES["bottom_left"], None)[0] == []

    def _cleared(
        self,
        preset: tuple[tuple[int, int], ...],
        box: tuple[int, int, int, int] | None,
        taken: float = 1.0,
    ) -> tuple[list[tuple[int, int]], AttackRunner]:
        return self._dragged(box, lambda runner: runner._clear_flank(b"", preset), taken)

    # A village filling the playfield, which is the ordinary case: measured over
    # nine battles the red line spans 530 to 575 px of the 595 there are.
    FULL_VILLAGE = (270, 115, 1330, 673)

    def test_a_lower_flank_with_no_ground_left_drags_the_village_up(self) -> None:
        """Under 30 px below the village is what lost a whole army without deploying it."""
        swipes, runner = self._cleared(DEPLOY_LINES["bottom_left"], self.FULL_VILLAGE)
        assert swipes == [(0, -83)]
        # And the coordinates everything downstream holds follow the camera.
        assert runner._panned == (0, -83)
        assert runner._middle == (800, 317)
        assert runner._onscreen(DEPLOY_LINES["bottom_left"]) == ((230, 347), (600, 577))

    def test_an_upper_flank_with_no_ground_left_drags_the_village_down(self) -> None:
        swipes, runner = self._cleared(DEPLOY_LINES["top_right"], self.FULL_VILLAGE)
        assert swipes == [(0, 100)]
        assert runner._panned == (0, 100)

    def test_a_village_the_flank_already_clears_is_left_alone(self) -> None:
        """Dragging a camera that is fine can only take room off the other side."""
        swipes, runner = self._cleared(DEPLOY_LINES["bottom_left"], (400, 200, 1200, 560))
        assert swipes == []
        assert runner._panned == (0, 0)

    def test_a_drag_the_game_swallowed_moves_no_coordinates(self) -> None:
        """A card left selected turns the same swipe into a deployment, and nothing pans."""
        _, runner = self._cleared(DEPLOY_LINES["bottom_left"], self.FULL_VILLAGE, taken=0)
        assert runner._panned == (0, 0)

    def test_only_the_part_of_the_drag_the_game_took_is_recorded(self) -> None:
        """Measured live, a drag of 98 px was taken as 100 and one of 81 as 72."""
        _, runner = self._cleared(DEPLOY_LINES["top_right"], self.FULL_VILLAGE, taken=0.6)
        assert runner._panned == (0, 60)

    def test_the_rage_goes_where_the_plan_drew_it(self) -> None:
        """It used to be slid onto the fighting, and that reading arrived too late to use.

        `_onto_army` took two frames a moment apart and found the busiest patch
        of what changed between them — three to four seconds of capturing and
        decoding, all of it after the moment the plan asked for the bottle, on a
        spell that lasts eighteen. What replaces it is the property that made
        the prediction possible in the first place: troops walk toward
        defences, so the planner is asked for the heaviest defences on the side
        it chose and the bottles go there.
        """
        runner = self._runner()
        targets = ((500, 300), (700, 420))
        with (
            patch.object(attack.time, "sleep"),
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack, "card_count", return_value=1),
            patch.object(attack, "live_cards", return_value=()),
            patch.object(AdbController, "tap") as tapped,
            patch.object(AdbController, "tap_many") as placed,
        ):
            runner._cast([939], targets, b"")
        assert tapped.call_args.args[:2] == (939, attack.CARD_ROW_Y)
        assert placed.call_args.args[0] == [targets[0], targets[1]]

    def test_a_planned_bottle_landing_inside_another_is_moved_off_it(self) -> None:
        """The planner is given the footprint and overlaps its points regardless.

        These five are one live reply, and four of their ten pairs sit inside one
        another. Rage does not stack, so each of those pairs buys one bottle's
        worth of ground for two bottles. The closest of the four is (768, 495)
        against (800, 378): 121 px apart, which clears the ellipse's 240 px axis
        and sits just inside its 120 px one — the sort of call a model reading a
        screenshot cannot make, which is why the prompt saying "do not overlap"
        does not settle it and this does.

        All five still get a bottle, each within a quarter of a footprint of
        where it was asked for. Three of them used to be dropped instead, and
        the caller topped the cargo back up off a fixed grid — which buys ground
        wherever that grid happens to run rather than where the planner looked.
        """
        planned = [(448, 522), (608, 450), (560, 585), (768, 495), (800, 378)]
        placed = spaced(planned)
        assert len(placed) == len(planned)
        for spot, asked in zip(placed, planned, strict=True):
            assert abs(spot[0] - asked[0]) <= RAGE_SPAN[0] // 4
            assert abs(spot[1] - asked[1]) <= RAGE_SPAN[1] // 4
        assert not self._overlapping(placed)

    def test_the_planners_own_block_opens_out_rather_than_collapsing(self) -> None:
        """Measured on 24 live rounds out of 24, and it cost half the cargo every one.

        `prompts/attack_plan.md` asks for 15% by 13% of the screen between
        bottles and the planner draws 13% by 10%, which puts the horizontal
        neighbour at 0.75 of a footprint and the vertical one at 0.56. Dropping
        those left the diagonal pair and nothing else, and the caller filled the
        two empty slots off a fixed grid: on one village attacked from the top
        left, a bottle went to (1000, 300) behind it. Opening the block out
        keeps all four over the ground the planner picked, and nothing tops the
        cargo up from anywhere else any more.
        """
        block = [(560, 252), (768, 252), (560, 342), (768, 342)]
        placed = spaced(block)
        assert len(placed) == 4
        assert not self._overlapping(placed)
        # Still one block on the same ground, opened out to the pitch a bottle
        # really covers rather than the tighter one it was drawn at.
        assert max(x for x, _ in placed) - min(x for x, _ in placed) >= RAGE_SPAN[0]
        assert max(y for _, y in placed) - min(y for _, y in placed) >= RAGE_SPAN[1]

    def test_a_bottle_is_never_walked_more_than_a_footprint_from_where_it_was_asked(self) -> None:
        """One push moves up to a footprint, and eight of them compound.

        These four points are one tight cluster, and uncapped the last of them
        walks from (823, 396) out to (608, 212) — 1.78 footprints, 215 px left
        and 184 px up, well off the ground the planner was looking at. That is
        the same wasted bottle the nudge exists to prevent, reached from the
        other side. Swept over 20 000 random sets the worst was 1.81; capped it
        is 1.00 by construction. A point that cannot be cleared inside that is
        dropped rather than walked out there.
        """
        crowd = [(832, 449), (843, 376), (809, 357), (823, 396)]
        placed = spaced(crowd)
        assert not self._overlapping(placed)
        # The fourth has nowhere inside a footprint to go, so it is dropped
        # rather than walked out to (608, 212), which is what used to happen.
        assert len(placed) == 3
        # Bounded against the footprint itself rather than against
        # `NUDGE_REACH`, so raising that constant fails this instead of moving
        # the goalposts with it.
        for spot in placed:
            asked = min(
                crowd, key=lambda point: (spot[0] - point[0]) ** 2 + (spot[1] - point[1]) ** 2
            )
            reach = (
                ((spot[0] - asked[0]) / RAGE_SPAN[0]) ** 2
                + ((spot[1] - asked[1]) / RAGE_SPAN[1]) ** 2
            ) ** 0.5
            assert reach <= 1.0
        assert NUDGE_REACH <= 1.0

    def test_pulling_a_bottle_onto_the_playfield_is_not_charged_to_its_nudge(self) -> None:
        """The camera pan can hand `spaced` a point that is already off the map.

        `_clear_flank` moves the record by up to `FLANK_ROOM`, and `_deploy` runs
        `_onscreen` over the plan's points before this sees them, so one drawn
        near the bottom of the village arrives under the card row and is clamped
        back up. That clamp is not a push this made, and charging it to
        `NUDGE_REACH` spent almost the whole budget before the first one: the
        point below was dropped with a clash beside it while the identical point
        with no clash was returned untouched.
        """
        crowded = attack._clear_of((900, 810), [(1000, 690)])
        alone = attack._clear_of((900, 810), [])
        assert alone == (900, 700)
        assert crowded is not None
        assert not self._overlapping([crowded, (1000, 690)])

    @staticmethod
    def _overlapping(
        placed: list[tuple[int, int]],
    ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        """Every pair of bottles close enough that the second one buys nothing."""
        return [
            (one, two)
            for index, one in enumerate(placed)
            for two in placed[index + 1 :]
            if ((one[0] - two[0]) / RAGE_SPAN[0]) ** 2 + ((one[1] - two[1]) / RAGE_SPAN[1]) ** 2
            < 1
        ]

    def test_the_flat_plans_grid_is_already_spaced(self) -> None:
        """The fallback tactic's bottles must not crowd each other, or it loses half its cargo."""
        grid = [point.pixels() for point in plans.flat().acts("rage")[0].at]
        assert spaced(grid) == grid
        assert grid[1][0] - grid[0][0] == RAGE_SPAN[0]

    def _casts(self, alive: list[list[int]]) -> int:
        """How many passes `_cast` makes, given what the row reads after each one."""
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(AttackRunner, "_tap"),
            patch.object(type(runner.adb), "tap_many"),
            patch.object(attack, "card_count", return_value=5),
            patch.object(attack, "live_cards", side_effect=alive) as reads,
            patch.object(attack.time, "sleep"),
        ):
            runner._cast(
                [1060], ((520, 300), (760, 300), (520, 420), (760, 420), (1000, 300)), b""
            )
            return reads.call_count

    def test_a_card_still_holding_a_bottle_is_offered_the_run_again(self) -> None:
        """x5 to x1 used to read as a success, because the corner had repainted."""
        assert self._casts([[1060], []]) == 2

    def test_a_card_that_emptied_is_not_asked_twice(self) -> None:
        assert self._casts([[]]) == 1

    def test_a_one_off_drop_keeps_pushing_out_while_that_moves_it(self) -> None:
        """The middle of the line first, then further from the village, as it always was."""
        line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        spots = single_spots(line)
        assert len(spots) == DEPLOY_ATTEMPTS
        assert spots[0] == line[len(line) // 2]
        pushed = [math.hypot(x - 800, y - 400) for x, y in spots if (x, y) not in line]
        assert len(pushed) >= 3
        assert pushed == sorted(pushed)

    def test_a_one_off_drop_pinned_on_the_map_edge_moves_along_the_line(self) -> None:
        """A spot already on the map's edge clamps back onto itself, so pushing is no retry.

        Measured live on a flank fitted to the village: a siege machine refused
        at (266, 199) was pushed four more times, came back within two pixels of
        itself every time, and its unit was never deployed at all.
        """
        line = deploy_line(LINE_POINTS, (581, 80), (273, 187), (150, 380))
        spots = single_spots(line)
        assert len(spots) == DEPLOY_ATTEMPTS
        # Every push clamps straight back, so the retries come off the line.
        assert sum(spot in line for spot in spots) >= DEPLOY_ATTEMPTS - 1
        # And no two of them are the same drop.
        for index, (x, y) in enumerate(spots):
            assert all(math.hypot(x - a, y - b) >= 20 for a, b in spots[index + 1 :])

    def test_a_refused_hero_still_gets_the_spot_the_probe_proved(self) -> None:
        """The plan's point goes ahead of the shared ladder, not over its first rung.

        `single_spots[0]` is the midpoint of the line the troops have already
        been spread along, so it is the one spot with evidence behind it.
        Replacing it with the plan's point cost a named hero both that spot and
        one of its retries.
        """
        runner = self._runner()
        line = deploy_line(LINE_POINTS)
        shared = single_spots(line, runner._middle)
        aimed: list[tuple[int, int]] = []

        def refuse(
            _self: object, taps: list[tuple[int, int]], display: object, gap: float = 0
        ) -> None:
            # Taps alternate card, spot, card, spot; only the spots matter here.
            aimed.extend(taps[1::2])

        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(AttackRunner, "_landed", return_value=([], [])),
            patch.object(AdbController, "tap_many", autospec=True, side_effect=refuse),
            patch.object(attack.time, "sleep"),
        ):
            runner._drop_singles([700], line, "hero", {700: (123, 456)})
        assert aimed[0] == (123, 456)
        # Every shared rung still follows, the probed midpoint included.
        assert aimed[1:] == shared

    def test_a_refused_flank_leaves_the_other_three_to_try(self) -> None:
        """The plan's own side goes first behind its line, then the flanks it did not pick."""
        plan = AttackPlan(steps=[_step("troops", (25, 30), (75, 70))], deploy_from="bottom_right")
        # That line runs across the village, so only the named flanks are left.
        assert planned_line(plan) is None
        candidates = deploy_candidates(plan)
        assert candidates[0] == DEPLOY_LINES["bottom_right"]
        assert sorted(candidates) == sorted(DEPLOY_LINES.values())

    def test_a_step_carries_every_field_even_where_it_does_not_use_them(self) -> None:
        """A field with a default is optional in the schema, and Gemini leaves those out.

        Three runs running once answered a drop line's start and no end; a
        fourth named five rage points, no freeze point and no hero at all,
        against a screen holding a freeze bottle and four hero cards. So a step
        that needs no points still says so rather than omitting the field, and a
        `wait` still names a hero it has nothing to do with.
        """
        assert set(AttackStep.model_json_schema()["required"]) == {"act", "who", "at", "seconds"}
        pause = _step("wait", seconds=4)
        assert (pause.who, pause.at, pause.seconds) == ("unknown", [], 4)

    def test_a_usable_planned_line_is_tried_before_any_flank(self) -> None:
        plan = AttackPlan(steps=[_step("troops", (62.5, 12.2), (85.6, 42.2))])
        candidates = deploy_candidates(plan)
        assert candidates[0] == planned_line(plan)
        assert len(candidates) == len(DEPLOY_LINES) + 1

    def test_card_groups_keep_troops_apart_from_heroes_and_spells(self) -> None:
        """Troops, the one-off drops and the spells, told apart by the wider gaps.

        Only the first boundary carries any weight: the loop reads group 0 as
        the troops and flattens everything after it, so where the siege machine
        falls does not matter. It lands with the heroes because the gap between
        them is really 16 px, which only ever read as a group boundary while a
        dark seam was breaking the card beside it into pieces.
        """
        groups = card_groups((FRAMES / "cards_full.png").read_bytes())
        assert [len(group) for group in groups] == [4, 5, 2]
        assert groups[0] == [171, 293, 413, 534]

    def test_the_empty_slot_the_row_ends_with_is_not_a_card(self) -> None:
        """It has no level badge, and it arrived downstream as one more hero to drop."""
        groups = card_groups((FRAMES / "cards_with_empty_slot.png").read_bytes())
        assert [len(group) for group in groups] == [3, 5, 2]
        assert 1416 not in [slot for group in groups for slot in group]

    def test_a_hero_whose_artwork_breaks_the_row_is_still_a_card(self) -> None:
        """A dark card cut in two by the brightness strip, put back together.

        The third hero's own artwork broke this row into 46 px and 63 px pieces,
        both under `CARD_MIN_WIDTH`, so the card vanished: five rounds of a live
        run reported "3 of 3 hero card(s) landed" with four heroes on the
        screen, and that hero never left its card in any of them. The five
        rounds after the fix reported 4 of 4.

        Everything outside the card row is blacked out in this frame. The
        opponent's village behind it is 2.7 MB of PNG on its own, well past what
        the repo will carry, and every reader here works inside y 700-890.
        """
        groups = card_groups((FRAMES / "cards_dark_hero.png").read_bytes())
        assert groups == [[171, 293, 413], [557, 683, 804, 925, 1046], [1181, 1302]]
        # What the loop actually reads off it: three troop cards, then the siege
        # machine and all four heroes, with only the two spells counted.
        rest = [slot for group in groups[1:] for slot in group]
        spells = counted_cards((FRAMES / "cards_dark_hero.png").read_bytes(), rest)
        assert spells == [1181, 1302]
        assert [slot for slot in rest if slot not in spells] == [557, 683, 804, 925, 1046]

    def test_spells_are_told_apart_from_heroes_by_their_count(self) -> None:
        """Spells carry an xN in the corner; heroes and the siege machine do not."""
        frame = (FRAMES / "cards_full.png").read_bytes()
        assert counted_cards(frame, [678, 815, 925, 1046, 1167, 1302, 1423]) == [1302, 1423]

    def test_a_spell_card_reports_how_many_it_holds(self) -> None:
        frame = (FRAMES / "cards_full.png").read_bytes()
        assert card_count(frame, 1302) == 5
        assert card_count(frame, 1423) == 1

    def test_a_count_over_pale_artwork_reads_as_unknown(self) -> None:
        """Two frames of the same x12 card: one resolves, the other says it cannot.

        The pale illustration merges into the count, and what is left of the `x`
        used to be matched against the templates with no tolerance at all — so
        the unreadable one came back as a card of a hundred and twenty-one
        rather than as a card nobody could count.
        """
        assert card_count((FRAMES / "cards_full.png").read_bytes(), 171) == 12
        assert card_count((FRAMES / "cards_with_empty_slot.png").read_bytes(), 171) is None

    def test_freeze_is_told_apart_from_rage_by_its_cyan(self) -> None:
        assert freeze_cards((FRAMES / "cards_full.png").read_bytes(), [1302, 1423]) == [1423]

    def test_a_spell_that_is_merely_green_is_not_freeze(self) -> None:
        """Cyan is high green *and* high blue, and only the green half was asked.

        Heal's bottle is green with none of the blue, so the missing half would
        have read it as freeze and held it back for the defences — where what a
        heal is for is the troops, which is where a spell this call does not
        claim already goes. The two colours here are freeze's own measured
        (141, 224, 242) and the same green with the blue taken out of it.
        """

        def card(colour: tuple[int, int, int]) -> bytes:
            frame = Image.new("RGB", (1600, 900), (20, 20, 20))
            frame.paste(Image.new("RGB", (100, 80), colour), (1373, 786))
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            return buffer.getvalue()

        assert freeze_cards(card((141, 224, 242)), [1423]) == [1423]
        assert freeze_cards(card((141, 224, 60)), [1423]) == []

    def test_a_spent_card_is_not_offered_again(self) -> None:
        """A spent card turns greyscale; brightness alone does not separate the two."""
        frame = (FRAMES / "cards_partly_spent.png").read_bytes()
        slots = [171, 293, 413, 534, 678, 804, 925, 1046, 1167, 1302, 1423]
        assert live_cards(frame, slots) == [s for s in slots if s != 293]

    def test_a_quoted_env_value_loses_its_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text('GEMINI_API_KEY="AQ.secret"\n', encoding="utf-8")
            assert dotenv_value("GEMINI_API_KEY", env) == "AQ.secret"
            assert dotenv_value("ABSENT", env) == ""


class RunnerStateTests(unittest.TestCase):
    """Stopping a headless run, and saying who is driving, out of one file.

    The window has a stop button; a run put in the background by whatever started
    it has nothing, and killing the process never reaches the `KeyboardInterrupt`
    handler that leaves the game somewhere the next run can start from. The same
    file answers the question nothing on this machine used to record at all:
    whether anything is driving the emulator, which a second session had to be
    told in the chat and now reads for itself.
    """

    def test_a_stop_is_asked_for_and_read_through_the_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch.object(commands, "STATE_PATH", state), commands.claim("attack"):
                assert not commands.stop_requested()
                commands.stop()
                assert commands.stop_requested()

    def test_a_run_that_finished_leaves_behind_what_it_was(self) -> None:
        """`idle` is a record rather than an absence, which is the half a flag
        could not do: a file that existed only while a run did cannot separate
        "nothing has run here" from "one just finished", and the second is what
        a session asking for the screen actually wants to know.
        """
        with tempfile.TemporaryDirectory() as folder:
            state, log = Path(folder) / "state.json", Path(folder) / "run"
            with patch.object(commands, "STATE_PATH", state):
                with commands.claim("walls", log):
                    held = commands.read_state()
                    assert held is not None
                    assert (held.status, held.command, held.pid) == (
                        "running",
                        "walls",
                        os.getpid(),
                    )
                after = commands.read_state()
            assert after is not None
            assert (after.status, after.command, after.log) == ("idle", "walls", log)
            assert after.ended is not None

    def test_a_claim_takes_over_a_stop_the_last_run_never_read(self) -> None:
        """Stopping means the run going now, never the next one to start. One
        left standing by a run that was killed would otherwise end the next one
        at zero rounds, reported as a stop nobody asked for.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch.object(commands, "STATE_PATH", state):
                commands._write_state(RunnerState(status="stopping", pid=1, command="attack"))
                with commands.claim("attack"):
                    assert not commands.stop_requested()

    def test_a_file_deleted_by_hand_stops_the_run_that_wrote_it(self) -> None:
        """The escape hatch for somebody whose agent died mid-run and whose only
        other move is finding a pid in the task manager.

        The `idle` written on the way out is the receipt that it really stood
        down, and it still carries `started` — which the file cannot supply,
        being the very thing that was deleted.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            self.enterContext(patch.object(commands, "STATE_PATH", state))
            with commands.claim("attack"):
                state.unlink()
                assert commands.stop_requested()
            back = commands.read_state()
            assert back is not None
            assert (back.status, back.command) == ("idle", "attack")
            assert back.started is not None

    def test_a_missing_file_is_no_stop_to_a_process_that_never_wrote_one(self) -> None:
        """A fresh machine has no state file, so reading its absence as a stop
        would end every first run on it before it started.
        """
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(commands, "STATE_PATH", Path(folder) / "state.json"),
        ):
            assert not commands.stop_requested()

    def test_a_half_written_file_is_not_a_stop(self) -> None:
        """Two processes share this file and `stop` is a read-modify-write, so a
        truncated read is a moment rather than a fault. `should_stop` is polled
        from inside a battle, where standing down on one costs the army.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch.object(commands, "STATE_PATH", state), commands.claim("attack"):
                state.write_text('{"status": "runn', encoding="utf-8")
                assert not commands.stop_requested()

    def test_a_torn_read_at_claim_time_still_writes_a_record(self) -> None:
        """`read_state` recovers an unreadable file as "running, and ours", which
        is what keeps a battle from standing down over a moment. Keying the
        reentrancy test on that instead of on this process's own record had
        `claim` yield without writing anything: the run drove the emulator
        unrecorded, `stop` read the last run's `idle` and said there was nothing
        to stop, and deleting the file was not a stop either — every documented
        way out gone at once, silently.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            state.write_text("", encoding="utf-8")
            self.enterContext(patch.object(commands, "STATE_PATH", state))
            with commands.claim("attack"):
                held = commands.read_state()
                assert held is not None
                assert (held.status, held.command) == ("running", "attack")
                commands.stop()
                assert commands.stop_requested()

    def test_the_scratch_file_belongs_to_one_process(self) -> None:
        """Two processes write this file by design: `ai_coc stop` exists to run
        against a loop that is writing at its own claim and release. One shared
        scratch name has them truncating each other's half-built copy, and on
        Windows the loser's `os.replace` raises `PermissionError` out of
        `_release` inside `claim`'s `finally` — past `run.answer`, so the run
        ends with no `result.json`, which is the one signal telling a clean stop
        from a killed process.
        """
        with tempfile.TemporaryDirectory() as folder:
            self.enterContext(patch.object(commands, "STATE_PATH", Path(folder) / "state.json"))
            with patch.object(commands.os, "replace"):
                commands._write_state(RunnerState(status="idle"))
            assert [path.name for path in Path(folder).iterdir()] == [
                f"state.json.{os.getpid()}.tmp"
            ]

    def test_a_claim_this_process_holds_is_left_to_its_outermost_owner(self) -> None:
        """The window runs every pass as its own `commands.*` call inside the
        claim its automation cycle holds, so a nested release would publish
        `idle` between passes and read, from outside, as a free emulator.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch.object(commands, "STATE_PATH", state), commands.claim("automation"):
                with commands.claim("collect"):
                    pass
                held = commands.read_state()
                assert held is not None
                assert (held.status, held.command) == ("running", "automation")

    def test_a_release_leaves_a_claim_that_is_not_its_own_alone(self) -> None:
        """A run that outlived its own record must not wipe out the one after
        it, which is what another process claiming in between looks like.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch.object(commands, "STATE_PATH", state):
                with commands.claim("attack"):
                    commands._write_state(
                        RunnerState(status="running", pid=os.getpid() + 1, command="walls")
                    )
                held = commands.read_state()
            assert held is not None
            assert (held.status, held.command) == ("running", "walls")

    def test_the_barracks_wait_gives_up_the_moment_it_is_stood_down(self) -> None:
        """A minute slept through in one go reads as a stop that did nothing."""
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch.object(commands, "STATE_PATH", state), commands.claim("attack"):
                commands.stop()
                started = time.monotonic()
                assert commands._rest(commands.IDLE_REST)
            assert time.monotonic() - started < commands.IDLE_REST / 2

    def test_stopping_nothing_says_so_rather_than_writing_a_request(self) -> None:
        """The other half a flag could not do: it wrote a file whether or not
        anything was listening, so a stop that landed and one that fell on an
        idle machine read the same.
        """
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(commands, "STATE_PATH", Path(folder) / "state.json"),
        ):
            assert "沒有指令在跑" in commands.stop()
            assert not commands.stop_requested()

    def test_the_wall_command_answers_the_same_file(self) -> None:
        """One file for every long loop rather than a mechanism each."""
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            report = MagicMock(message="")
            report.paid.return_value = 0
            with (
                patch.object(commands, "STATE_PATH", state),
                patch.object(commands, "_controller"),
                patch.object(commands, "current_world", return_value="day"),
                patch.object(commands, "WallRunner") as runner,
            ):
                runner.return_value.run.return_value = report
                commands.walls(WallOptions())
            assert runner.call_args.kwargs["should_stop"] is commands.stop_requested

    def test_a_stopped_wall_run_says_so_and_keeps_what_it_bought(self) -> None:
        """The runner counts batches; whether this call was stopped is the
        command's own fact, so the prefix goes on here rather than in the loop.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"

            def asked_mid_run() -> MagicMock:
                commands.stop()
                report = MagicMock(message="升級了 3 面城牆")
                report.paid.return_value = 0
                return report

            with (
                patch.object(commands, "STATE_PATH", state),
                patch.object(commands, "_controller"),
                patch.object(commands, "current_world", return_value="day"),
                patch.object(commands, "WallRunner") as runner,
                commands.claim("walls"),
            ):
                runner.return_value.run.side_effect = asked_mid_run
                result = commands.walls(WallOptions())
            assert result.message == "已停止，升級了 3 面城牆"

    def test_a_headless_run_hands_the_state_to_the_runner(self) -> None:
        """The interface was there all along; only the window ever passed it.

        Which is the whole bug: `should_stop` defaults to never stopping, so a
        run started from a terminal read as one that simply could not be stopped.
        """
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with (
                patch.object(commands, "STATE_PATH", state),
                patch.object(commands, "_controller"),
                patch.object(commands, "current_world", return_value="day"),
                patch.object(commands, "_planner", return_value=None),
                patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
                patch.object(commands, "FrameTicker"),
                patch.object(commands, "AttackRunner") as runner,
            ):
                commands.attack(AttackOptions(rounds=1))
            assert runner.call_args.kwargs["should_stop"] is commands.stop_requested


class InRoundRestartTests(unittest.TestCase):
    """A restart inside the round moves the game, and only the runner is told."""

    def test_the_cart_follows_the_runner_onto_the_new_display(self) -> None:
        """Otherwise a later round asks a display that no longer exists.

        `AttackRunner` reassigns its own display when the idle-disconnect dialog
        sends it through `restart_game`, and `commands.attack` holds a second
        copy that nothing updates. Measured twice in one night: `_empty_cart`
        asked the display the game had left, `screencap` answered `Status: -2`,
        and the whole series died with `result.json` never written.
        """
        fresh = MagicMock(name="the display the game came back on")
        controller = MagicMock(name="the controller that came back")
        with (
            patch.object(commands, "_controller"),
            patch.object(commands, "_settle_game", return_value=MagicMock(name="at the start")),
            patch.object(commands, "_pick_world", return_value="night"),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "FrameTicker") as ticker,
            patch.object(commands, "AttackRunner") as runner,
            patch.object(commands, "_empty_cart") as cart,
        ):
            runner.return_value.run.return_value = MagicMock(
                stock_full=False, attacked=None, phases=1
            )
            runner.return_value.played = None
            runner.return_value.adb = controller
            runner.return_value.display = fresh
            commands.attack(AttackOptions(world="night", rounds=1))
        # Both halves: a scheduled restart builds a fresh controller as well as a
        # fresh display, so dropping either from the handover puts one of them
        # back on the emulator that went away.
        assert cart.call_args.args[2] is controller
        assert cart.call_args.args[3] is fresh
        # The ticker captures from its own thread and swallows what it cannot
        # reach, so a stale one costs warnings nobody reads rather than the run.
        assert ticker.return_value.__enter__.return_value.display is fresh


class RestartEveryTests(unittest.TestCase):
    """Restarting the emulator on a schedule, because MuMu drops frames.

    The mechanism is a few lines; what is worth testing is the two decisions
    underneath them — what gets counted, and what the loop waits for before it
    calls the emulator ready.
    """

    @staticmethod
    def _fought() -> MagicMock:
        return MagicMock(stock_full=False, attacked=MagicMock(), phases=0)

    @staticmethod
    def _idle() -> MagicMock:
        return MagicMock(stock_full=False, attacked=None, phases=0)

    def _play(
        self,
        reports: list[MagicMock],
        options: AttackOptions,
        every: int,
        played: AttackPlan | None = None,
    ) -> MagicMock:
        """Run the loop over a fixed list of rounds and hand back the restart mock."""
        with (
            patch.object(commands, "_controller"),
            patch.object(commands, "current_world", return_value="day"),
            patch.object(commands, "_planner", return_value=None),
            patch.object(
                commands.ConfigStore, "load", return_value=AppConfig(restart_every=every)
            ),
            patch.object(commands, "FrameTicker"),
            patch.object(commands, "_rest", return_value=False),
            patch.object(commands, "AttackRunner") as runner,
            patch.object(commands, "_restart_emulator", return_value=True) as restart,
        ):
            runner.return_value.run.side_effect = reports
            runner.return_value.played = played
            self.series = commands.attack(options)
        return restart

    def test_every_round_leaves_its_own_line_in_the_plan_log(self) -> None:
        """`--plan-out` keeps whichever round went last and overwrites the rest.

        Which line a given round drew is otherwise unanswerable: `run.log` has a
        one-line summary of it, and the frames cannot stand in, since a drop is
        over inside the gap between two captures. One file rather than one per
        round, because `--repeat 0` runs all night.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "plans.jsonl"
            self._play(
                [self._fought()] * 3,
                AttackOptions(rounds=3, plan_log=log),
                every=0,
                played=plans.flat(),
            )
            lines = [
                PlayedPlan.model_validate_json(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
            assert [entry.round for entry in lines] == [1, 2, 3]
            assert lines[1].plan == plans.flat()

    def test_a_round_that_never_got_a_plan_writes_no_line(self) -> None:
        """A round can end before there is a tactic to write down, and that is not
        an error: an empty line would read as a plan that drew nothing.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "plans.jsonl"
            self._play([self._fought()], AttackOptions(rounds=1, plan_log=log), every=0)
            assert not log.exists()

    def test_a_round_does_not_inherit_the_last_one_s_plan(self) -> None:
        """One runner plays every round, so `played` has to be cleared with the rest.

        Rounds that never reach the planner are ordinary — no opponent above the
        thresholds, an army under `MIN_ARMY_RATIO`, the attack menu not opening.
        Carried over, each would be filed under its own number holding the
        previous round's tactic, which is worse than the absent file it replaces:
        a `--plan-in` or flat-fallback series writes identical plans round after
        round, so nothing downstream could tell a stale copy from a real one.
        """
        runner = AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            thresholds=LootThresholds(),
        )
        runner._played = plans.flat()
        with patch.object(AttackRunner, "_open_attack_menu", return_value=None):
            runner.run()
        assert runner.played is None

    def test_the_restart_counts_battles_rather_than_rounds(self) -> None:
        """A round spent waiting for barracks did not tire the emulator out.

        Five rounds, two of which fought nothing. Counting rounds would restart
        after the second one; counting battles waits until the fourth, which is
        where the second battle actually finished.
        """
        rounds = [self._idle(), self._fought(), self._idle(), self._fought(), self._fought()]
        restart = self._play(rounds, AttackOptions(rounds=5), every=2)
        assert len(self.series.root) == 5
        assert restart.call_count == 1

    def test_zero_on_the_flag_is_not_the_same_as_leaving_it_out(self) -> None:
        """Omitting it keeps the configured value; passing zero turns it off.

        The same distinction the loot overrides carry, and for the same reason:
        a run being watched needs a way to skip the restart without editing the
        file every other run reads.
        """
        assert self._play([self._fought()] * 2, AttackOptions(rounds=2), every=1).call_count == 1
        off = AttackOptions(rounds=2, restart_every=0)
        assert self._play([self._fought()] * 2, off, every=1).call_count == 0

    def test_a_restart_that_never_came_back_keeps_the_rounds_already_played(self) -> None:
        """Raising instead would leave `result.json` empty on `cli.py`'s side,
        which reports a night of farming as nothing at all.
        """
        with (
            patch.object(commands, "_controller"),
            patch.object(commands, "current_world", return_value="day"),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig(restart_every=1)),
            patch.object(commands, "FrameTicker"),
            patch.object(commands, "AttackRunner") as runner,
            patch.object(commands, "_restart_emulator", return_value=False),
        ):
            runner.return_value.run.side_effect = [self._fought(), self._fought()]
            series = commands.attack(AttackOptions(rounds=2))
        assert len(series.root) == 1

    def test_the_restart_waits_for_a_village_rather_than_for_the_process(self) -> None:
        """`ensure_coc` is satisfied by a pid, which exists seconds after the
        `monkey` while the village is not on screen for much longer. So the wait
        is for a frame the loop could actually play from.
        """
        adb = MagicMock()
        runner, ticker = MagicMock(), MagicMock()
        with (
            patch.object(commands, "launch"),
            patch.object(commands, "MuMuAdapter") as mumu,
            patch.object(commands.time, "sleep"),
            patch.object(commands, "stop_requested", return_value=False),
            # The village test rather than the storages: the game reopens on
            # whichever village it was closed on, and `read_stock` answers on
            # both, so it can say a village is up but never which one.
            patch.object(commands, "current_world", side_effect=[None, None, "day"]),
            patch.object(commands, "idle_disconnected", return_value=False),
            patch.object(commands, "loading_screen", return_value=False),
        ):
            mumu.return_value.controller.return_value = adb
            assert commands._restart_emulator(runner, ticker)
        assert adb.screenshot.call_count == 3
        # Both objects and both fields, or `--shot-every` spends the rest of the
        # night timing out against a display that no longer exists.
        assert (runner.adb, runner.display) == (adb, adb.display_for.return_value)
        assert (ticker.adb, ticker.display) == (adb, adb.display_for.return_value)
        # A restarted game comes back zoomed in, and every coordinate in this
        # project was measured at the far limit — without this the run keeps
        # going and deploys nothing for the rest of the night.
        adb.zoom.assert_called_once_with(
            "out", commands.ZOOM_PINCHES, commands.COC_PACKAGE, adb.display_for.return_value
        )

    def test_a_game_with_no_window_yet_is_waited_out_rather_than_given_up_on(self) -> None:
        """`display_for` raises while the game has no focused window, which is
        the ordinary state of one still loading rather than a failure.
        """
        adb = MagicMock()
        adb.display_for.side_effect = [AdbControlError("not on a display"), MagicMock()]
        with (
            patch.object(commands, "launch"),
            patch.object(commands, "MuMuAdapter") as mumu,
            patch.object(commands.time, "sleep"),
            patch.object(commands, "stop_requested", return_value=False),
            patch.object(commands, "current_world", return_value="day"),
        ):
            mumu.return_value.controller.return_value = adb
            assert commands._restart_emulator(MagicMock(), MagicMock())
        assert adb.display_for.call_count == 2

    def test_a_village_that_never_paints_gives_up_instead_of_waiting_forever(self) -> None:
        """The patience is sized for the bad case but it is still finite.

        Measured live, one restart had the village up in 22 seconds and the next
        one on the same machine had not got there in 120 — so this path is real,
        and a run that hangs in it is worse than one that ends holding the rounds
        it played.
        """
        adb = MagicMock()
        with (
            patch.object(commands, "launch"),
            patch.object(commands, "MuMuAdapter") as mumu,
            patch.object(commands.time, "sleep"),
            patch.object(commands, "stop_requested", return_value=False),
            patch.object(commands, "current_world", return_value=None),
            patch.object(commands, "idle_disconnected", return_value=False),
            patch.object(commands, "loading_screen", return_value=False),
        ):
            mumu.return_value.controller.return_value = adb
            assert not commands._restart_emulator(MagicMock(), MagicMock())
        assert adb.screenshot.call_count == commands.RESTART_POLLS

    def test_an_emulator_that_will_not_come_back_does_not_take_the_series_with_it(self) -> None:
        """`launch` raises for exactly the states this exists to recover from —
        an instance MuMu dropped, a game that never started — and a raise here
        would leave `cli.py` writing an empty `result.json` over a night's work.
        """
        with (
            patch.object(commands, "launch", side_effect=RuntimeError("找不到任何 MuMu instance")),
            patch.object(commands, "MuMuAdapter"),
        ):
            assert not commands._restart_emulator(MagicMock(), MagicMock())


class StopAtOverrideTests(unittest.TestCase):
    """`--stop-at` against the file's percentage, with the split every override here has."""

    def _stop_at(self, options: AttackOptions) -> int:
        """Run one round and hand back the percentage the runner was built with."""
        with (
            patch.object(commands, "_controller"),
            patch.object(commands, "current_world", return_value="day"),
            patch.object(commands, "_planner", return_value=None),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig(stop_at=85)),
            patch.object(commands, "FrameTicker"),
            patch.object(commands, "_rest", return_value=False),
            patch.object(commands, "AttackRunner") as runner,
        ):
            runner.return_value.run.return_value = MagicMock(
                stock_full=False, attacked=MagicMock(), phases=0
            )
            commands.attack(options)
        return runner.call_args.kwargs["stop_at"]

    def test_zero_on_the_flag_is_not_the_same_as_leaving_it_out(self) -> None:
        """Omitting it keeps the file's percentage; zero never stands the run down.

        Zero is what a test battle against a village the farming has just
        filled needs: every storage is past the line, so the file's value would
        end the series before the code under test ever ran.
        """
        assert self._stop_at(AttackOptions()) == 85
        assert self._stop_at(AttackOptions(stop_at=0)) == 0
        assert self._stop_at(AttackOptions(stop_at=100)) == 100


class LaunchTests(unittest.TestCase):
    """Bringing the game up, tearing down only as much as was asked for.

    The adapters have done all three of these since long before anything could
    reach them, so what is worth testing is the wiring: each scope restarting
    exactly what it names and nothing else.
    """

    def setUp(self) -> None:
        """Stub the settle step, which wants a real frame.

        `launch` now waits for a village and pinches the camera back out before
        it answers. What these tests are about is the lifecycle wiring — which
        scope tears down what — so feeding that step a fake screenshot would only
        be testing the fake.
        """
        patcher = patch.object(commands, "_settle_game")
        self.settled = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _mumu(*, running: bool = True) -> MagicMock:
        instance = MagicMock(
            index=0, adb_serial="127.0.0.1:16384", coc_running=running, android_started=True
        )
        mumu = MagicMock()
        mumu.enumerate_instances.return_value = [instance]
        mumu.ensure_coc.return_value = instance
        return mumu

    def test_the_game_is_left_at_a_village_and_the_far_zoom(self) -> None:
        """A pid is not a screen anything can be aimed at.

        Every command after this one uses coordinates measured against a village
        at the far zoom limit, so `launch` is where the game is brought to that
        state — and the report says whether it got there, because a game still
        on its loading screen answers by silently missing whatever it aims at.
        """
        mumu = self._mumu()
        with patch.object(commands, "MuMuAdapter", return_value=mumu):
            report = commands.launch("none")
        self.settled.assert_called_once()
        mumu.controller.assert_called_once_with("127.0.0.1:16384")
        assert report.at_village

    def test_a_village_that_never_painted_is_reported_rather_than_raised(self) -> None:
        """The process is up, so the caller may still have something to do with
        it; the one thing it must not do is assume the screen is ready.
        """
        mumu = self._mumu()
        self.settled.return_value = None
        with patch.object(commands, "MuMuAdapter", return_value=mumu):
            report = commands.launch("none")
        assert not report.at_village
        assert "村莊沒有出現" in report.message

    def test_the_ordinary_case_restarts_nothing(self) -> None:
        mumu = self._mumu()
        with patch.object(commands, "MuMuAdapter", return_value=mumu):
            report = commands.launch("none")
        mumu.restart_instance.assert_not_called()
        mumu.restart_coc.assert_not_called()
        mumu.ensure_coc.assert_called_once_with(0)
        assert report.was_running

    def test_a_cold_machine_says_so_rather_than_claiming_it_was_already_up(self) -> None:
        """This scope does the same work either way; only the report tells them apart."""
        mumu = self._mumu(running=False)
        with patch.object(commands, "MuMuAdapter", return_value=mumu):
            report = commands.launch("none")
        assert not report.was_running
        assert "開起來" in report.message

    def test_restarting_the_game_leaves_the_emulator_alone(self) -> None:
        mumu = self._mumu()
        with patch.object(commands, "MuMuAdapter", return_value=mumu):
            commands.launch("game")
        mumu.restart_coc.assert_called_once()
        mumu.restart_instance.assert_not_called()

    def test_restarting_the_emulator_waits_for_it_to_really_go_down(self) -> None:
        """`control restart` returns the moment it is sent, so the state read
        straight after is still the old one — and an instance still reporting
        itself started skips `ensure_coc`'s whole boot wait, which aims a launch
        at an emulator on its way down. Missing from the listing is not down
        either: `ensure_coc` raises on an index it cannot find.
        """
        up = MagicMock(
            index=0, adb_serial="127.0.0.1:16384", coc_running=True, android_started=True
        )
        down = MagicMock(
            index=0, adb_serial="127.0.0.1:16384", coc_running=True, android_started=False
        )
        mumu = MagicMock()
        mumu.enumerate_instances.return_value = [up]
        # Still up on the first look, missing from the listing on the second,
        # and only then down: the wait ends on the third and not before.
        mumu.instance.side_effect = [up, None, down]
        mumu.ensure_coc.return_value = down
        with (
            patch.object(commands, "MuMuAdapter", return_value=mumu),
            patch.object(commands, "SHUTDOWN_GAP", 0),
        ):
            commands.launch("emulator")
        assert mumu.instance.call_count == 3
        mumu.restart_instance.assert_called_once_with(0)
        mumu.restart_coc.assert_not_called()

    def test_no_emulator_at_all_is_an_error_rather_than_a_report(self) -> None:
        mumu = MagicMock()
        mumu.enumerate_instances.return_value = []
        with patch.object(commands, "MuMuAdapter", return_value=mumu), pytest.raises(RuntimeError):
            commands.launch("none")


class RunLogTests(unittest.TestCase):
    """One directory per run, which used to be a convention in a skill file.

    The layout came from `.agents/skills/farm/references/running.md`, built with
    shell redirection, so it existed only for the reader who could have built it
    anyway. A person running the CLI by hand, or using the window, got a
    rotating file and nothing else.
    """

    def _run(self, folder: str, command: str = "attack", *, recording: bool = False) -> RunLog:
        with patch.object(models, "LOG_DIR", Path(folder)):
            return RunLog.open(command, recording=recording)

    def test_a_directory_is_named_for_when_and_what(self) -> None:
        """A plain listing has to read as a history, or a run from three days
        ago is only findable by opening them.
        """
        with tempfile.TemporaryDirectory() as folder:
            run = self._run(folder, "walls")
            assert run.directory.parent == Path(folder)
            assert run.directory.name.endswith("-walls")
            assert run.directory.is_dir()

    def test_nothing_is_recorded_unless_it_was_asked_for(self) -> None:
        """None rather than an empty directory: every loop reads None as "do not
        save", and a directory that is always there says nothing about whether
        this run was worth recording.
        """
        with tempfile.TemporaryDirectory() as folder:
            assert self._run(folder).frames is None

    def test_recording_gets_a_frames_directory_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            frames = self._run(folder, recording=True).frames
            assert frames is not None
            assert frames.is_dir()

    def test_the_answer_lands_beside_the_log_that_explains_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run = self._run(folder)
            run.answer('{"ok": true}')
            assert (run.directory / "result.json").read_text(encoding="utf-8") == '{"ok": true}'

    def test_a_second_run_takes_the_first_ones_file_away(self) -> None:
        """The window opens a run per job, so without the removal its tenth job
        would still be writing into the first job's directory as well.
        """
        with tempfile.TemporaryDirectory() as folder:
            first = self._run(folder, "attack")
            second = self._run(folder, "walls")
            # A logger of its own: `configure_logging` touches the root one, and
            # a test has no business rearranging where the suite's logging goes.
            log = logging.getLogger("run-log-test")
            _attach_run(log, first)
            _attach_run(log, second)
            log.info("only the second run")
            for handler in log.handlers:
                handler.close()
            assert "only the second run" in second.log_path.read_text(encoding="utf-8")
            assert "only the second run" not in first.log_path.read_text(encoding="utf-8")

    def test_a_second_setup_call_leaves_the_level_someone_chose(self) -> None:
        """The window calls `configure_logging` again for every job it starts.

        The 執行紀錄 selector lowers the root logger at runtime, so a second call
        that fell through to `root.setLevel` would undo it — right after the user
        asked for DEBUG to capture prompts. The guard therefore cannot key off
        any of the sinks: which of them exist depends on the build, and
        PyInstaller ships `--windowed`, where there is no stderr and so no
        console handler at all.
        """
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        root.handlers.clear()
        try:
            with patch.object(logging_setup.sys, "stderr", None):
                configure_logging()
                root.setLevel(logging.DEBUG)
                configure_logging()
                assert root.level == logging.DEBUG
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)


class WindowToggleTests(unittest.TestCase):
    """The two checkboxes beside the preview, with no Qt event loop involved.

    Called unbound against a stand-in for `self`, which is all a slot that only
    touches its own attributes needs. This is the coverage whose absence let a
    real regression through: a checkbox added below `_toggle_live_view` took
    that method's timer branch with it, so unchecking 即時畫面 no longer stopped
    the preview and the new checkbox started and stopped it instead. The whole
    suite stayed green, because nothing here touches the window at all.
    """

    def test_the_live_toggle_still_owns_the_timer(self) -> None:
        """Matched whole rather than call by call, because the interval is half
        the point: a bare `start()` runs the preview every time round the event
        loop, which is what `LIVE_INTERVAL` exists to avoid.
        """
        window = MagicMock()
        MainWindow._toggle_live_view(window, True)
        assert window.mock_calls == [
            call.settings.setValue("live_view", True),
            call.live_timer.start(LIVE_INTERVAL),
        ]
        window.reset_mock()
        MainWindow._toggle_live_view(window, False)
        assert window.mock_calls == [
            call.settings.setValue("live_view", False),
            call.live_timer.stop(),
        ]

    def test_the_recording_toggle_touches_nothing_but_its_setting(self) -> None:
        """What reads it is `run_attack`, when it opens the run. A round already
        under way keeps whatever it started with, and the preview is none of its
        business.
        """
        window = MagicMock()
        MainWindow._toggle_record_frames(window, True)
        # The whole call list, not a list of things it did not do: the
        # regression this class exists for was a branch appearing where none
        # belonged, and naming `live_timer` alone would miss the next one.
        assert window.mock_calls == [call.settings.setValue("record_frames", True)]


def _menu(price: int, gold: int = 887) -> WallMenu:
    """A wall menu whose buttons sit where a row of this width puts them."""
    return WallMenu(gold=(gold, 700), elixir=(gold + 176, 700), add=(gold - 176, 700), price=price)


def _stock() -> VillageStock:
    """Storage bars that read, which is how every loop here tests for the village."""
    return VillageStock(gold=9_000_000, elixir=9_000_000, dark=100_000)


class WallMenuTests(unittest.TestCase):
    """Live building menus, masked down to the button row the parser reads.

    Nothing here is a picture of a wall, and that is the point: every level
    repaints one, so what is read is the UI the game paints on top of the village.
    """

    def test_a_wall_is_the_one_building_priced_in_both_resources(self) -> None:
        menu = wall_menu((FRAMES / "wall_menu_plain.png").read_bytes())
        assert menu is not None
        assert menu.price == 1_600_000
        assert (menu.gold, menu.elixir) == ((887, 700), (1063, 700))
        # 升級更多 on this menu, 新增城牆 on a batch: the same place in the row
        # and the same effect, which is what saves the loop from telling the two
        # menus apart. The wall ring sits on the far side of the elixir button
        # and is never worked out at all, so it cannot be tapped by mistake.
        assert menu.add == (711, 700)

    def test_a_building_that_takes_one_resource_is_not_a_wall(self) -> None:
        """A dark elixir drill: one price, one icon, and so no wall menu here."""
        assert wall_menu((FRAMES / "wall_menu_elixir_only.png").read_bytes()) is None

    def test_the_button_row_is_located_rather_than_written_down(self) -> None:
        """A batch with under ten walls left to add loses 新增城牆+10.

        The row is laid out from the middle of the screen outwards, so losing one
        button moves every other button half a pitch. Written down, the loop
        would be tapping 升級 where 聖水 now is.
        """
        menu = wall_menu((FRAMES / "wall_menu_short_row.png").read_bytes())
        assert menu is not None
        assert menu.price == 4_800_000
        assert (menu.gold, menu.elixir, menu.add) == ((799, 700), (975, 700), (623, 700))

    def test_a_price_the_village_cannot_afford_is_still_read(self) -> None:
        """The game writes it in red, and red is not a reason to stop reading it.

        The loop sizes its own batch, so it needs the number rather than the
        warning; a price that came back as None here would have the whole menu
        read as "not a wall".
        """
        menu = wall_menu((FRAMES / "wall_menu_unaffordable.png").read_bytes())
        assert menu is not None
        assert menu.price == 11_200_000

    def test_a_menu_on_its_own_is_not_a_dialog(self) -> None:
        assert game_dialog((FRAMES / "wall_menu_plain.png").read_bytes()) is None

    def test_the_exit_prompt_is_the_spend_dialog_in_the_very_same_pixels(self) -> None:
        """Which is why the reader hands back both buttons and picks neither.

        確定退出遊戲嗎 and 升級城牆 are one panel with one green 確定, and only the
        caller knows which question it just asked. A reader that reached for 確定
        on its own would sooner or later close the game.
        """
        spend = game_dialog((FRAMES / "wall_spend_dialog.png").read_bytes())
        leaving = game_dialog((FRAMES / "wall_exit_dialog.png").read_bytes())
        assert spend is not None
        assert spend == leaving
        assert spend.confirm == (973, 562)
        assert spend.cancel == (623, 572)


class HomeTests(unittest.TestCase):
    """Getting back to a village, and how long an unreadable frame is worth waiting on."""

    def _backs(
        self,
        run: shared.GameRunner,
        reads: list[VillageStock | None],
        loading: list[bool] | None = None,
    ) -> int:
        """Walk `_home` over these `read_stock` answers; how many times it pressed back.

        `loading` is what each frame reads as before the storages are asked; a
        frame that is the loading screen never reaches them.
        """
        with (
            patch.object(shared.time, "sleep"),
            patch.object(run, "_frame", return_value=b""),
            patch.object(shared, "idle_disconnected", return_value=False),
            patch.object(
                shared, "loading_screen", side_effect=loading or [False] * shared.HOME_TRIES
            ),
            patch.object(shared, "game_dialog", return_value=None),
            patch.object(shared, "read_stock", side_effect=reads),
            # Asked one frame earlier than the storages and about a different
            # thing: which of the two villages this is. Kept in step with the
            # answers above, since a frame whose storages read is a village.
            patch.object(
                shared, "current_world", side_effect=["day" if seen else None for seen in reads]
            ),
            # The first village that reads pinches the camera back out, which
            # wants a real emulator. What these cases are about is `back`.
            patch.object(shared.GameRunner, "_settle_zoom"),
            patch.object(AdbController, "back") as back,
        ):
            run._home()
        return back.call_count

    def _runner(self) -> shared.GameRunner:
        return shared.GameRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
        )

    def test_a_game_that_may_still_be_starting_is_waited_on(self) -> None:
        """Nothing is pressed at a launch: there is no village there to press back on."""
        held = VillageStock(gold=1, elixir=1, dark=1)
        assert self._backs(self._runner(), [None] * 4 + [held]) == 0

    def test_a_panel_is_backed_out_of_at_once_once_a_village_has_read(self) -> None:
        """`_sweep` opens one on most of its taps, and the launch patience is not owed to it.

        Measured over three live wall runs, the blanket patience put 26, 26 and
        34 seconds between the tap that opened a building panel and the first
        `back` — 50 to 80 seconds of a 150 second run, waiting out a launch that
        had finished before the run started.
        """
        held = VillageStock(gold=1, elixir=1, dark=1)
        run = self._runner()
        assert self._backs(run, [held]) == 0
        assert self._backs(run, [None, None, None, held]) == 3

    def test_a_loading_screen_is_waited_on_even_after_a_village_has_read(self) -> None:
        """A session the server dropped mid-run reloads from here, and `back` at it is aimed at nothing."""
        held = VillageStock(gold=1, elixir=1, dark=1)
        run = self._runner()
        assert self._backs(run, [held]) == 0
        assert self._backs(run, [held], loading=[True, True, False]) == 0

    def _sailing(self, run: shared.GameRunner, seen: list[str], landed: str) -> MagicMock:
        """Walk `_home` over these worlds with the crossing answering `landed`."""
        with (
            patch.object(shared.time, "sleep"),
            patch.object(run, "_frame", return_value=b""),
            patch.object(shared, "idle_disconnected", return_value=False),
            patch.object(shared, "loading_screen", return_value=False),
            patch.object(shared, "game_dialog", return_value=None),
            patch.object(shared, "current_world", side_effect=seen * shared.HOME_TRIES),
            patch.object(
                shared, "read_stock", return_value=VillageStock(gold=1, elixir=1, dark=1)
            ),
            patch.object(shared.GameRunner, "_settle_zoom"),
            patch.object(shared, "cross", return_value=landed) as sailed,
        ):
            run._home()
        return sailed

    def test_a_crossing_that_never_lands_is_not_tried_again(self) -> None:
        """`cross` is already the patient one: it swipes to the corner and tries three spots.

        Retried once per attempt this method would spend `HOME_TRIES` whole
        crossings on a boat nobody can reach — about twenty minutes, against
        fifty seconds for the worst path here before it, and none of it
        interruptible since `_home` never reads the state file.
        """
        assert self._sailing(self._runner(), ["night"], "night").call_count == 1

    def test_a_crossing_that_lands_carries_on_into_the_village(self) -> None:
        assert self._sailing(self._runner(), ["night", "day"], "day").call_count == 1

    def test_a_restart_puts_the_launch_patience_back(self) -> None:
        """The one branch that stops `back` being pressed at a game that is cold again.

        A loop that waits minutes on barracks meets the idle-disconnect dialog
        sooner or later, and the answer to it is a restart — so the frames right
        after one are a launch, whatever this runner had already seen.
        """
        held = VillageStock(gold=1, elixir=1, dark=1)
        run = self._runner()
        assert self._backs(run, [held]) == 0
        with (
            patch.object(shared.time, "sleep"),
            patch.object(run, "_frame", return_value=b""),
            # Dropped on the first frame, then a launch nothing may press at.
            patch.object(shared, "idle_disconnected", side_effect=[True, False, False, False]),
            patch.object(shared, "loading_screen", return_value=False),
            patch.object(shared, "game_dialog", return_value=None),
            patch.object(shared, "read_stock", side_effect=[None, None, held]),
            patch.object(shared, "current_world", side_effect=[None, None, "day"]),
            patch.object(shared, "restart_game", return_value=run.display),
            patch.object(shared.GameRunner, "_settle_zoom"),
            patch.object(AdbController, "back") as back,
        ):
            run._home()
        assert back.call_count == 0


class WallRunnerTests(unittest.TestCase):
    """The arithmetic between the taps, with the emulator taken out."""

    def _runner(self, **fields: object) -> WallRunner:
        return WallRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            **fields,
        )

    def _sized(
        self, runner: WallRunner, opening: WallMenu, answers: list[WallMenu], purse: int
    ) -> tuple[WallBatch | None, MagicMock]:
        with (
            patch.object(walls.time, "sleep"),
            patch.object(runner, "_after_tap", return_value=b""),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(walls, "wall_menu", side_effect=answers),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            return runner._sized(opening, purse), tapped

    def test_several_named_walls_are_compared_rather_than_the_first_one_taken(self) -> None:
        """One named wall is not a choice, and `_pick` exists to make one.

        `--at` used to take a single spot and hand `_pick` a price of zero for
        it, which it then dutifully chose. Measured live, that paid 9 000 000
        for a wall while 4 000 000 ones stood in the same village — the sweep
        was skipped, and so was the comparison that gives the loot its value.

        Finding walls is the half a pair of eyes does in one look and the sweep
        does by tapping a grid and hoping; comparing what they ask is the half
        the loop does better.
        """
        runner = self._runner(at=[(200, 300), (400, 300), (600, 300)])
        menus = {
            (200, 300): WallMenu(price=9000000, gold=(0, 0), elixir=(0, 0), add=(0, 0)),
            (400, 300): WallMenu(price=4000000, gold=(0, 0), elixir=(0, 0), add=(0, 0)),
            (600, 300): None,
        }
        tapped: list[tuple[int, int]] = []

        def after(point: tuple[int, int], label: str) -> bytes:
            tapped.append(point)
            return b""

        with (
            patch.object(WallRunner, "_after_tap", side_effect=after),
            patch.object(shared, "read_stock", return_value=_stock()),
            patch.object(walls, "wall_menu", side_effect=lambda _png: menus[tapped[-1]]),
        ):
            found = runner._candidates()
        # Every named spot is read, and the one that opens nothing is dropped
        # rather than guessed at: it was found by eye on a frame the game has
        # since moved.
        assert tapped == [(200, 300), (400, 300), (600, 300)]
        assert [(w.point, w.price) for w in found] == [
            ((200, 300), 9000000),
            ((400, 300), 4000000),
        ]
        # And the cheapest is what gets bought, which is the lowest-level wall.
        assert runner._pick({w.point: w.price for w in found}) == (400, 300)

    def test_a_spot_that_opened_a_building_is_backed_out_of_before_the_next_tap(self) -> None:
        """Every spot here is a raw village coordinate, so one that misses a wall
        opens whatever building is standing there — and a full-screen panel
        swallows every tap after it. That guard was `_neighbours`'s alone while
        the only other caller was a hand-named spot; a finder that answers in
        guesses makes the miss the ordinary case rather than the corner.
        """
        runner = self._runner(at=[(200, 300), (400, 300)])
        # The first tap opened a barracks: no storage bars, so no village.
        stocks = iter([None, _stock()])
        with (
            patch.object(WallRunner, "_after_tap", return_value=b""),
            patch.object(shared, "read_stock", side_effect=lambda _png: next(stocks)),
            patch.object(walls, "wall_menu", return_value=_menu(1_600_000)),
            patch.object(runner, "_home", return_value=_stock()) as home,
        ):
            found = runner._candidates()
        # Backed out once, and the wall behind the panel still gets read.
        assert home.call_count == 1
        assert [w.point for w in found] == [(400, 300)]

    def test_gemini_spots_are_priced_off_their_own_menus(self) -> None:
        """The finder answers in guesses, and every guess is opened and priced
        before it can be spent on. That verification is the whole reason an
        imperfect finder is usable: a wrong point costs one tap and one capture,
        so asking for more spots than the run needs is the right shape.
        """
        runner = self._runner(ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()))
        answer = ScreenSpots(
            spots=[ScreenPoint(x_pct=25, y_pct=30), ScreenPoint(x_pct=50, y_pct=40)]
        )
        menus = iter([None, _menu(1_600_000)])
        with (
            patch.object(GeminiClient, "generate_structured", return_value=answer),
            patch.object(WallRunner, "_after_tap", return_value=b""),
            patch.object(WallRunner, "_frame", return_value=b""),
            patch.object(shared, "read_stock", return_value=_stock()),
            patch.object(walls, "wall_menu", side_effect=lambda _png: next(menus)),
            patch.object(runner, "_scan") as scan,
        ):
            found = runner._candidates()
        # The percentages become pixels the same way every other Gemini answer
        # does, the miss is dropped, and the sweep is never reached because one
        # spot did open a wall.
        assert [w.point for w in found] == [(800, 360)]
        scan.assert_not_called()

    def test_a_village_gemini_could_not_place_falls_through_to_the_sweep(self) -> None:
        """A village whose walls nobody could name still has walls, and the
        alternative to two and a half minutes of grid taps is a run that buys
        nothing at all.
        """
        runner = self._runner(ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()))
        with (
            patch.object(GeminiClient, "generate_structured", side_effect=RuntimeError("no key")),
            patch.object(WallRunner, "_frame", return_value=b""),
            patch.object(runner, "_scan", return_value=[]) as scan,
        ):
            assert runner._candidates() == []
        scan.assert_called_once()

    def test_a_stopped_run_starts_no_further_batch(self) -> None:
        """Between batches is the only safe place: a batch is a menu, a
        confirmation and a storage read, and leaving mid-way strands a dialog
        over the village. Whatever it already bought stays bought, because a
        wall upgrades the moment it is paid for and there is nothing to undo.
        """
        runner = self._runner(at=[(500, 300)], should_stop=lambda: True)
        with patch.object(
            runner, "_home", return_value=VillageStock(gold=99999999, elixir=99999999, dark=1)
        ):
            report = runner.run()
        assert not report.upgrades

    def test_a_stop_during_the_scan_does_not_wait_for_the_whole_sweep(self) -> None:
        """The scan is the longest unguarded stretch of a run not told where to
        start, and it is the opening of every run without `--at`: a grid tap
        costs a settle and a capture, and each wall it lands on costs four more.
        Leaving here is safe in a way that leaving a batch is not, because the
        sweep already backs out of whatever each tap opened.
        """
        runner = self._runner(should_stop=lambda: True)
        swept = iter([((580, 140), b""), ((740, 140), b"")])
        with (
            patch.object(walls.time, "sleep"),
            patch.object(runner, "_sweep", return_value=swept),
            patch.object(walls, "wall_menu", return_value=_menu(1_600_000)),
        ):
            found = runner._scan()
        assert not found
        # The second point is still sitting there, so the sweep was cut short
        # rather than walked to the end and thrown away.
        assert next(swept, None) is not None

    def test_the_scan_looks_beside_each_wall_the_sweep_lands_on(self) -> None:
        """Walls sit 45 px apart and the sweep steps 160, so it passes over three
        of them between samples — and a section is not one level. Measured on a
        live village the strip held 32 walls at 1 600 000 and three at 4 000 000,
        the sweep's six samples all landed on the 4 000 000 ones, which are the
        level the town hall caps, and the run bought nothing at all.
        """
        runner = self._runner()
        # The sweep's own sample, then the four taps around it: one wall three
        # levels lower, and three misses.
        answers = [_menu(4_000_000), _menu(1_600_000), None, None, None]
        with (
            patch.object(walls.time, "sleep"),
            patch.object(runner, "_after_tap", return_value=b""),
            patch.object(runner, "_sweep", return_value=iter([((580, 140), b"")])),
            patch.object(
                shared, "read_stock", return_value=VillageStock(gold=0, elixir=0, dark=0)
            ),
            patch.object(walls, "wall_menu", side_effect=answers),
        ):
            found = runner._scan()
        assert min(wall.price for wall in found) == 1_600_000
        assert len(found) == 2

    def test_a_neighbour_tap_that_opened_a_screen_is_backed_out_of(self) -> None:
        """The sweep reads the storages after every one of its own taps for this
        reason, and the neighbours are the first taps in this loop that go in on
        raw village coordinates. A neighbour can be a barracks as easily as a
        wall, and a tap that opened a full screen would leave the next three
        landing somewhere inside it.
        """
        runner = self._runner()
        held = VillageStock(gold=0, elixir=0, dark=0)
        with (
            patch.object(walls.time, "sleep"),
            patch.object(runner, "_after_tap", return_value=b""),
            patch.object(runner, "_sweep", return_value=iter([((580, 140), b"")])),
            # The first neighbour opened a full screen; the rest are the village.
            patch.object(shared, "read_stock", side_effect=[None, held, held, held]),
            patch.object(
                walls, "wall_menu", side_effect=[_menu(4_000_000), _menu(1_600_000), None, None]
            ),
            patch.object(runner, "_home", return_value=held) as home,
        ):
            found = runner._scan()
        home.assert_called_once()
        assert min(wall.price for wall in found) == 1_600_000

    def test_a_price_that_does_not_move_means_the_batch_holds_one_wall(self) -> None:
        """升級更多 makes a batch of the wall already selected, so it costs the same.

        That is the whole of telling a plain menu from a batch: the loop taps the
        same button either way and reads the answer, rather than trying to
        recognise which of the two rows it is looking at.
        """
        runner = self._runner()
        batch, tapped = self._sized(
            runner, _menu(1_600_000), [_menu(1_600_000), _menu(4_800_000)], purse=5_000_000
        )
        assert batch is not None
        assert (batch.unit, batch.count) == (1_600_000, 3)
        assert len(tapped.call_args.args[0]) == 2

    def test_the_unit_price_is_what_one_more_wall_adds(self) -> None:
        runner = self._runner()
        batch, tapped = self._sized(
            runner, _menu(4_800_000), [_menu(6_400_000), _menu(9_600_000)], purse=10_000_000
        )
        assert batch is not None
        assert (batch.unit, batch.count) == (1_600_000, 6)
        # Four walls in the batch already, six affordable: two more taps.
        assert len(tapped.call_args.args[0]) == 2

    def test_a_batch_the_game_would_not_grow_is_reported_at_its_real_size(self) -> None:
        """It stops at the last wall of that level and says so with a notice."""
        runner = self._runner()
        batch, _ = self._sized(
            runner, _menu(1_600_000), [_menu(3_200_000), _menu(4_800_000)], purse=16_000_000
        )
        assert batch is not None
        assert batch.count == 3

    def test_a_batch_already_dearer_than_the_purse_is_left_alone(self) -> None:
        """Growing it is the only thing this can do, and it is already too big."""
        runner = self._runner()
        batch, tapped = self._sized(
            runner, _menu(16_000_000), [_menu(17_600_000)], purse=5_000_000
        )
        assert batch is None
        tapped.assert_not_called()

    def test_a_storage_that_never_moved_is_not_an_upgrade(self) -> None:
        """Confirming the dialog is not proof: a tap the game swallows raises nothing.

        Without this the loop counts walls it never bought, and the report says
        the run succeeded.
        """
        runner = self._runner()
        stock = VillageStock(gold=10_000_000, elixir=1_000_000, dark=0)
        with (
            patch.object(walls.time, "sleep"),
            patch.object(runner, "_after_tap", return_value=b""),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(runner, "_tap"),
            patch.object(runner, "_confirm", return_value=True),
            patch.object(
                runner,
                "_sized",
                return_value=WallBatch(menu=_menu(3_200_000), unit=1_600_000, count=2),
            ),
            patch.object(walls, "wall_menu", return_value=_menu(1_600_000)),
            patch.object(runner, "_home", return_value=stock),
        ):
            assert runner._buy((100, 100), stock) is None

    def test_a_batch_that_was_paid_for_stops_being_the_cheapest(self) -> None:
        """The next round moves to another wall rather than pushing this one on.

        A batch left selected shows its next level's price, which is dearer than
        every wall still at the old level — so re-reading it is all it takes to
        keep one section of the village from running away from the rest.
        """
        runner = self._runner(rounds=2)
        stock = VillageStock(gold=99_000_000, elixir=0, dark=0)
        walls_found = [
            WallCandidate(point=(100, 100), price=1_600_000),
            WallCandidate(point=(200, 200), price=1_600_000),
        ]
        bought = WallUpgrade(unit=1_600_000, count=2, resource="gold")
        with (
            patch.object(runner, "_home", return_value=stock),
            patch.object(runner, "_scan", return_value=walls_found),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(runner, "_buy", return_value=bought) as buy,
            patch.object(walls, "wall_menu", return_value=_menu(14_400_000)),
        ):
            report = runner.run()
        assert [call.args[0] for call in buy.call_args_list] == [(100, 100), (200, 200)]
        assert report.walls == 4


class HomeHudTests(unittest.TestCase):
    """The village's own overlay, masked down to what is read off it."""

    def test_collector_markers_are_found_by_size_not_by_what_they_stand_on(self) -> None:
        """Colour alone answers the storage bars, the shop button and a spell factory.

        A collector is repainted at every level and sits wherever the player put
        it, so nothing here looks at one — only at the marker floated above it,
        which is the same pixels on every village.
        """
        markers = collect_bubbles((FRAMES / "home_markers.png").read_bytes())
        assert len(markers) == 11
        assert {marker.resource for marker in markers} == {"gold", "elixir", "dark"}
        # Every one on the collectors, none on the storage bars carrying the very
        # same icons a few hundred pixels to the right.
        assert all(940 <= x <= 1420 and 180 <= y <= 470 for x, y in (m.point for m in markers))

    def test_the_builder_counter_is_split_on_its_slash(self) -> None:
        """Idle over total. The slash is not a digit and is found by matching badly."""
        assert free_builders((FRAMES / "home_markers.png").read_bytes()) == (1, 5)

    def test_a_wide_leading_digit_still_fits_the_builder_box(self) -> None:
        """0/6, where the box was cut from a 1/5 and a 1 is the narrowest digit.

        The number is centred, so the leading 0 inks from x 792 and the old edge
        at 795 took three columns off it: 23 bits from its template against 5
        with them restored, either side of the tolerance. So this village read as
        having no counter at all while every builder on it was busy.
        """
        assert free_builders((FRAMES / "home_builders_busy.png").read_bytes()) == (0, 6)

    def test_a_frame_with_no_village_on_it_has_no_builders(self) -> None:
        assert free_builders((FRAMES / "wall_spend_dialog.png").read_bytes()) is None

    def test_the_builder_panel_is_read_off_its_progress_bars(self) -> None:
        """9小時23分鐘, 19小時12分鐘, 21小時38分鐘 and 1天17小時, in seconds.

        Two units and two ladders: only the first character of the first unit is
        matched, and the second number follows it one step down. The frame keeps
        the panel's own column and blacks out the rest; whole, it is 3 MB.
        """
        queue = builder_jobs((FRAMES / "builder_panel.png").read_bytes())
        assert queue is not None
        assert queue.running == 4
        assert queue.remaining == [33780, 69120, 77880, 147600]

    def test_a_village_with_no_panel_up_has_no_queue(self) -> None:
        """The button toggles, so "no panel" is what a second tap is for."""
        assert builder_jobs((FRAMES / "home_markers.png").read_bytes()) is None
        assert builder_jobs((FRAMES / "home_storages.png").read_bytes()) is None


class BuildingUpgradeTests(unittest.TestCase):
    """Menus for the buildings that are not walls."""

    def test_a_single_resource_menu_offers_one_upgrade(self) -> None:
        offers = upgrade_buttons((FRAMES / "menu_with_gem_plate.png").read_bytes())
        assert [(offer.resource, offer.price) for offer in offers] == [("elixir", 60_000)]

    def test_a_button_on_a_blue_plate_is_never_an_upgrade(self) -> None:
        """加速所有同類項目 spends a magic item and carries a potion the elixir test
        answers outright, so without the plate check this menu reports an
        "upgrade costing 1 elixir" and a loop taps the item away. The wall ring
        is the same button in a different hat.
        """
        frame = (FRAMES / "menu_with_gem_plate.png").read_bytes()
        assert len(upgrade_buttons(frame)) == 1
        assert wall_menu(frame) is None

    def test_an_eight_figure_price_is_drawn_too_short_for_the_loot_panel_floor(self) -> None:
        """A price is shrunk to fit its button, so how tall a digit is depends on
        how many of them there are: five figures are drawn 16 px tall, seven 13
        to 14, and eight 12 — of which the digits with no ascender are 11. At the
        loot panel's floor those drop out one at a time and whatever survives is
        reported as the price, which had 英雄殿堂's own 10 400 000 reading as 14
        and left `ai_coc upgrade` unable to see any eight-figure upgrade at all.
        """
        offers = upgrade_buttons((FRAMES / "hero_hall_menu.png").read_bytes())
        assert [(offer.resource, offer.price) for offer in offers] == [("elixir", 10_400_000)]

    def test_a_shorter_floor_does_not_invent_prices_on_the_menus_that_already_read(self) -> None:
        """Swept over 548 recorded frames, the lower floor changed four readings
        and every one of them was this same eight-figure price. Nothing that read
        before stopped reading, and nothing unreadable became a number.
        """
        plain = upgrade_buttons((FRAMES / "wall_menu_plain.png").read_bytes())
        assert [(offer.resource, offer.price) for offer in plain] == [
            ("gold", 1_600_000),
            ("elixir", 1_600_000),
        ]
        assert upgrade_buttons((FRAMES / "home_storages.png").read_bytes()) == []
        assert upgrade_buttons((FRAMES / "army_screen.png").read_bytes()) == []

    def test_a_price_the_reader_only_half_resolved_is_not_a_shorter_price(self) -> None:
        """`PRICE_TOLERANCE` was defined and never passed, so a row this reader
        got partway through came back as whatever survived rather than as
        nothing — which is how 10 400 000 was reported as 14. A truncated price
        is the dangerous kind of wrong: one that keeps seven of its eight digits
        still clears `MIN_PRICE` and gets spent against.
        """
        # Ink shaped like no digit at all. Without a tolerance the reader still
        # names its nearest template and hands the number back; the real ones
        # this rejects are patches of village that read as a stray 1.
        mask = [
            [(x + y) % 2 == 0 and 4 <= x < 18 and 6 <= y < 24 for x in range(40)]
            for y in range(30)
        ]
        assert digits_from(mask) == 0
        assert digits_from(mask, PRICE_TOLERANCE) is None

    def test_the_upgrade_sheet_is_told_from_grass_by_the_storage_bars(self) -> None:
        """A building confirms on a full-screen sheet whose 確認 is green — and so
        is a village, all over. What separates them is that the sheet covers the
        storage bars and a village does not.
        """
        assert upgrade_sheet((FRAMES / "upgrade_sheet.png").read_bytes()) == (1121, 783)
        assert upgrade_sheet((FRAMES / "home_markers.png").read_bytes()) is None
        assert upgrade_sheet((FRAMES / "menu_with_gem_plate.png").read_bytes()) is None


class SweepGridTests(unittest.TestCase):
    """Where a sweep may tap, which is not everywhere the grid can reach."""

    def test_the_staggered_grid_lands_between_the_plain_one(self) -> None:
        """Buildings are narrower than the grid steps, so one pass is a sample.
        英雄殿堂 sits 86 px from the nearest plain point and was walked past
        twice; the staggered grid puts a point 14 px from it.
        """
        hall = (990, 430)

        def nearest(points: list[tuple[int, int]]) -> float:
            return min(math.dist(point, hall) for point in points)

        plain = [(x, y) for y in SWEEP_Y for x in SWEEP_X]
        staggered = [
            (x + SWEEP_STAGGER[0], y + SWEEP_STAGGER[1]) for y in SWEEP_Y for x in SWEEP_X
        ]
        assert round(nearest(plain)) == 86
        assert round(nearest(staggered)) == 14

    def test_no_staggered_point_lands_on_the_game_hud(self) -> None:
        """The plain grid stops at x 1220 to stay left of the storage bars, which
        start at 1260 — and the staggered one would put (1300, 200) on the exact
        corner of the dark elixir reading. Those points are dropped rather than
        clamped, since a clamped one lands where the other pass already went.
        """
        kept = [
            (x + SWEEP_STAGGER[0], y + SWEEP_STAGGER[1])
            for y in SWEEP_Y
            for x in SWEEP_X
            if x + SWEEP_STAGGER[0] <= SWEEP_LIMIT[0] and y + SWEEP_STAGGER[1] <= SWEEP_LIMIT[1]
        ]
        assert kept, "the staggered pass has to keep some points"
        assert max(point[0] for point in kept) <= SWEEP_X[-1]
        # The button row a tap opens starts at 622.
        assert max(point[1] for point in kept) < 622
        assert round(min(math.dist(point, (990, 430)) for point in kept)) == 14


class PinchTests(unittest.TestCase):
    """The two-finger gesture, which `input` cannot express at all."""

    def _events(self) -> list[tuple[int, int, int]]:
        return pinch_events(((500, 450), (760, 450)), ((1100, 450), (840, 450)), steps=2)

    def test_the_gesture_carries_btn_touch(self) -> None:
        """Without it the whole thing is accepted, reported, and ignored — which
        is what a first attempt looked like, several times over, on all three of
        the device nodes MuMu publishes.
        """
        events = self._events()
        assert (EV_KEY, BTN_TOUCH, 1) in events
        assert (EV_KEY, BTN_TOUCH, 0) in events

    def test_a_point_goes_down_with_its_axes_swapped(self) -> None:
        """The device reports x to 900 and y to 1600 against a 1600x900 screen,
        so a screen point goes down as (y, x). Measured by tapping (430, 990)
        through this path and watching the building at screen (990, 430) open.
        """
        events = self._events()
        # The first finger starts at screen (500, 450): x carries 450, y carries 500.
        assert (EV_ABS, ABS_MT_POSITION_X, 450) in events
        assert (EV_ABS, ABS_MT_POSITION_Y, 500) in events

    def test_every_finger_is_lifted_by_id(self) -> None:
        """A tracking id left live holds the touch down, and the next gesture
        then reads as one finger moving rather than two.
        """
        assert self._events().count((EV_ABS, ABS_MT_TRACKING_ID, -1)) == 2

    def test_the_move_is_broken_into_steps(self) -> None:
        """One jump from start to end reads as a teleport and the game keeps the
        scale it started at.
        """
        assert self._events().count((EV_SYN, SYN_REPORT, 0)) == 4


class HeroGemButtonTests(unittest.TestCase):
    """The one button in the hall that spends gems, and why it read as a price."""

    def test_a_hero_already_upgrading_offers_gems_rather_than_a_price(self) -> None:
        """立即完成 sits where 升級 was, on the same green plate, with a number
        beside it — and that number is a gem count.

        Read as a price it is a cheap one, so it cleared the affordability check
        and its plate was live, which left `hero --upgrade warden` one tap from
        finishing an upgrade with gems. Measured live it came back as
        `warden 268 dark upgradable=True` against a village holding 11 359 gems.
        The card still appears, because the hero is on the screen and the hall
        has to keep reading as the hall.
        """
        cards = {
            card.hero: card
            for card in hero_cards((FRAMES / "hero_hall_finishing.png").read_bytes())
        }
        warden = cards["warden"]
        assert (warden.price, warden.resource, warden.upgradable) == (None, None, False)
        # And the four beside it are untouched: same plate, same place, a real
        # price in dark elixir.
        assert [(c.price, c.resource) for c in cards.values() if c.hero != "warden"] == [
            (4500, "dark"),
            (5850, "dark"),
            (11700, "dark"),
            (9000, "dark"),
        ]

    def test_the_elixir_hero_is_not_mistaken_for_a_gem_button(self) -> None:
        """Both are drawn on the same green plate, so the icon is what separates
        them: swept over both committed halls the elixir drop reads -120 of
        `green - max(red, blue)` and every dark one -7 to -9, against +48 for the
        gem. Widening what a gem looks like would take 大守護者 with it.
        """
        for name in ("hero_hall", "hero_hall_scrolled"):
            cards = {card.hero: card for card in hero_cards((FRAMES / f"{name}.png").read_bytes())}
            assert (cards["warden"].price, cards["warden"].resource) == (1_360_000, "elixir"), name
            assert cards["warden"].upgradable, name

    def test_a_gem_button_is_reported_as_a_hero_already_being_raised(self) -> None:
        """Which is what it is, and it is the message the loop already had: a
        card with no price stops before the affordability check and before
        anything is tapped.
        """
        runner = HeroRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            hero="warden",
        )
        cards = {
            card.hero: card
            for card in hero_cards((FRAMES / "hero_hall_finishing.png").read_bytes())
        }
        blocked = runner._blocked(cards["warden"], VillageStock(gold=0, elixir=0, dark=999_999))
        assert blocked is not None
        assert "升級中" in blocked


class HeroHallTests(unittest.TestCase):
    """The 英雄殿堂 screen, and the button on the village that opens it."""

    def test_every_card_reads_its_hero_and_what_the_next_level_costs(self) -> None:
        """The banner is the identifying mark, and it is UI rather than artwork:
        the same flat colour at level 1 as at level 100, and the same wherever
        the row has been scrolled to.
        """
        cards = hero_cards((FRAMES / "hero_hall.png").read_bytes())
        assert [(card.hero, card.price, card.resource) for card in cards] == [
            ("king", 4_000, "dark"),
            ("queen", 4_800, "dark"),
            ("minion_prince", 4_400, "dark"),
            ("warden", 1_360_000, "elixir"),
            ("champion", 8_000, "dark"),
        ]

    def test_the_sixth_hero_is_only_there_once_the_row_has_scrolled(self) -> None:
        """Five of the six cards fit, so the last one is off screen until it is
        scrolled to — and a card only half on screen is not read at all, because
        the price beside its middle would be its neighbour's.
        """
        cards = hero_cards((FRAMES / "hero_hall_scrolled.png").read_bytes())
        assert [card.hero for card in cards] == [
            "queen",
            "minion_prince",
            "warden",
            "champion",
            "duke",
        ]
        assert (cards[-1].price, cards[-1].resource) == (56_000, "dark")

    def test_a_screen_of_coloured_plates_is_not_the_hall(self) -> None:
        """我的軍隊 stands its heroes on tall coloured plates of their own, and a
        run that took those for hall cards walked into that screen, called it the
        Hero Hall and stopped sweeping for the real one. A banner matching no
        hero is dropped rather than reported as an unknown one, which is what
        leaves this screen answering nothing at all.
        """
        assert hero_cards((FRAMES / "army_screen.png").read_bytes()) == []
        assert hero_cards((FRAMES / "home_storages.png").read_bytes()) == []
        assert hero_cards((FRAMES / "upgrade_sheet.png").read_bytes()) == []

    def test_an_arrow_is_read_before_it_is_tapped(self) -> None:
        """The end the row has run out of is not drawn, and a card stands where
        that arrow was — so a run that tapped blindly would open a hero instead
        of scrolling.
        """
        left = (FRAMES / "hero_hall.png").read_bytes()
        right = (FRAMES / "hero_hall_scrolled.png").read_bytes()
        assert (can_scroll(left, SCROLL_LEFT), can_scroll(left, SCROLL_RIGHT)) == (False, True)
        assert (can_scroll(right, SCROLL_LEFT), can_scroll(right, SCROLL_RIGHT)) == (True, False)

    def test_the_way_in_is_placed_by_the_icon_beside_it(self) -> None:
        """英雄殿堂's own button is a gold crown, which is artwork. What is read
        is the resource icon on the 升級 beside it, and the row's fixed pitch
        does the rest — its price is deliberately not wanted, because needing one
        would only be a second way to miss a menu that is really there.
        """
        assert hall_buttons((FRAMES / "hero_hall_menu.png").read_bytes()) == [(1151, 700)]

    def test_the_way_in_is_not_a_fixed_number_of_places_along(self) -> None:
        """The row gains and loses buttons with the building's state. The same
        menu came up five buttons wide with a 強化 running, putting the hall one
        place right of 升級, and four wide without it — which slid every button
        half a pitch and left the gem-plated 強化英雄 in that place instead. A
        reader that only looked one place along reported nothing on the second
        layout, and the sweep walked past the hall it had just opened.
        """
        assert hall_buttons((FRAMES / "hero_hall_menu_short.png").read_bytes()) == [(1238, 700)]

    def test_a_screen_with_no_menu_on_it_offers_no_way_in(self) -> None:
        assert hall_buttons((FRAMES / "hero_hall.png").read_bytes()) == []
        assert hall_buttons((FRAMES / "home_storages.png").read_bytes()) == []
        assert hall_buttons((FRAMES / "army_screen.png").read_bytes()) == []

    def test_the_confirmation_covers_the_cards_and_confirms_where_it_is_sought(self) -> None:
        """The whole spending half of the command rests on these two readings.

        `_start` calls a frame that still has cards on it a tap that never
        landed, and it needs 確認 inside the span `upgrade_sheet` searches — the
        same one a building's sheet uses. A hero's sheet is a different screen,
        so that it lands in the same place is a measurement rather than a given.
        """
        sheet = (FRAMES / "hero_upgrade_sheet.png").read_bytes()
        assert hero_cards(sheet) == []
        assert upgrade_sheet(sheet) == (1121, 783)


class TargetFinderTests(unittest.TestCase):
    """The one finder all three loops share, with Gemini and the emulator taken out."""

    def _runner(self, **fields: object) -> WallRunner:
        return WallRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            **fields,
        )

    def _answer(self, *points: tuple[float, float]) -> ScreenSpots:
        return ScreenSpots(spots=[ScreenPoint(x_pct=x, y_pct=y) for x, y in points])

    def test_a_point_in_the_button_row_is_dropped_rather_than_tapped(self) -> None:
        """Below `SWEEP_LIMIT` is the row of buttons the last selection left on
        screen, so a tap there presses one of them instead of choosing something
        on the map.

        Measured live, four of twelve answers landed in it and each opened
        whatever that row happened to be offering. Telling the model about the
        floor works — 0 of 14 on the next run — but the prompt is the
        optimisation and this is the guarantee.
        """
        runner = self._runner(ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()))
        # 47% of 900 is 423, which is on the map; 78% is 702, which is not.
        answer = self._answer((50, 47), (47, 78), (60, 30))
        with (
            patch.object(GeminiClient, "generate_structured", return_value=answer),
            patch.object(WallRunner, "_frame", return_value=b""),
        ):
            spots = runner._spotted("城牆", "散開", 3)
        assert spots == [(800, 423), (960, 270)]

    def test_no_client_answers_nothing_so_the_sweep_still_runs(self) -> None:
        """Without a key every loop here works exactly as it did before this
        existed, which is what keeps the sweep underneath rather than beside it.
        """
        assert self._runner()._spotted("城牆", "散開", 12) == []


class GeminiTierTests(unittest.TestCase):
    """Two model tiers in the settings file."""

    def test_the_lite_tier_defaults_to_its_own_model(self) -> None:
        """The whole point of a second tier is that it is a different model."""
        assert AppConfig().gemini.lite.model == DEFAULT_LITE_MODEL
        assert AppConfig().gemini.main.model != DEFAULT_LITE_MODEL

    def test_the_key_has_nowhere_to_live_in_the_settings_file(self) -> None:
        """`ConfigStore.save` writes every field of every nested model, so a key
        field here would put an empty slot in a plaintext file — beside the DPAPI
        store built to keep it out of one. The client carries it instead.
        """
        assert "api_key" not in AppConfig().model_dump_json()
        assert "api_key" not in GeminiSetting.model_fields


class BuildingNameTests(unittest.TestCase):
    """Reading which building a menu belongs to, which no parser here can do."""

    def test_the_name_strip_is_a_slice_of_the_frame_rather_than_the_frame(self) -> None:
        """A per-candidate call is only affordable because of what it sends: the
        band is 4% of the frame's area, and it is a fixed one — measured across
        nine live menus, every label sat inside it whatever was selected.
        """
        frame = (FRAMES / "hero_hall_menu.png").read_bytes()
        strip = Image.open(io.BytesIO(name_strip(frame)))
        assert strip.size == (900, 65)
        assert len(strip.tobytes()) / len(Image.open(io.BytesIO(frame)).tobytes()) < 0.05

    def test_no_model_leaves_the_name_unread_rather_than_guessed(self) -> None:
        """Which is what a run with no API key gets, and every caller treats it
        as ordinary: the report falls back to the coordinate it always used.
        """
        runner = UpkeepRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
        )
        assert runner._name(b"") == BuildingName()

    def test_only_skips_a_dearer_building_with_the_wrong_name(self) -> None:
        """The opposite of what this loop does unasked, and deliberately: without
        a name the dearest affordable upgrade is the best use of a scarce
        builder, and with one the caller knows better than the price does.
        """
        runner = UpkeepRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            only="金礦",
        )
        offers = [
            BuildCandidate(
                point=(100, 100),
                resource="gold",
                price=9_900_000,
                building=BuildingName(name="箭塔", level=15),
            ),
            BuildCandidate(
                point=(200, 200),
                resource="gold",
                price=720_000,
                building=BuildingName(name="金礦", level=12),
            ),
        ]
        assert runner._pick(offers, 20_000_000, 0).point == (200, 200)
        # An unread name matches nothing, which is the safe direction for a
        # filter that decides where a builder goes.
        blank = [offer.model_copy(update={"building": BuildingName()}) for offer in offers]
        assert runner._pick(blank, 20_000_000, 0) is None


class UpkeepRunnerTests(unittest.TestCase):
    """`upgrade` runs the same three finders in the same order as the wall loop."""

    def _runner(self, **fields: object) -> UpkeepRunner:
        return UpkeepRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            **fields,
        )

    def test_spotted_buildings_are_priced_off_their_own_menus(self) -> None:
        """The finder answers in guesses and each one is opened before it counts.

        It also reaches ground the sweep cannot: measured live, only two of the
        nine buildings it found sat within 45 px of a grid point, against a grid
        that stops at x 1220 and y 500 on a 1600x900 screen.
        """
        runner = self._runner(ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()))
        answer = ScreenSpots(
            spots=[ScreenPoint(x_pct=50, y_pct=40), ScreenPoint(x_pct=80, y_pct=50)]
        )
        offers = iter([[], [UpgradeButton(resource="gold", point=(700, 700), price=9_900_000)]])
        with (
            patch.object(GeminiClient, "generate_structured", return_value=answer),
            patch.object(UpkeepRunner, "_after_tap", return_value=b""),
            patch.object(UpkeepRunner, "_frame", return_value=b""),
            patch.object(shared, "read_stock", return_value=_stock()),
            patch.object(upkeep, "wall_menu", return_value=None),
            patch.object(upkeep, "upgrade_buttons", side_effect=lambda _png: next(offers)),
            patch.object(runner, "_scan") as scan,
        ):
            found = runner._buildings()
        assert [(o.point, o.price) for o in found] == [((1280, 450), 9_900_000)]
        scan.assert_not_called()

    def test_a_wall_the_finder_answered_is_never_offered_to_a_builder(self) -> None:
        """A wall upgrades instantly and ties up no builder, so putting one on a
        wall is the single mistake this loop exists to avoid. That check used to
        sit inside the sweep; every finder has to pass it now.
        """
        runner = self._runner(at=[(500, 300)])
        with (
            patch.object(UpkeepRunner, "_after_tap", return_value=b""),
            patch.object(shared, "read_stock", return_value=_stock()),
            patch.object(upkeep, "wall_menu", return_value=_menu(1_600_000)),
            patch.object(upkeep, "upgrade_buttons", return_value=[]),
        ):
            assert runner._buildings() == []

    def test_a_village_the_finder_could_not_place_falls_through_to_the_sweep(self) -> None:
        """A village whose buildings nobody can name still has buildings."""
        runner = self._runner()
        with patch.object(runner, "_scan", return_value=[]) as scan:
            assert runner._buildings() == []
        scan.assert_called_once()


class HeroRunnerTests(unittest.TestCase):
    """The arithmetic between the taps, with the emulator taken out."""

    def _runner(self, **fields: object) -> HeroRunner:
        return HeroRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            **fields,
        )

    def _duke(self, **fields: object) -> HeroCard:
        """A card the hall is really offering, which is what these tests are about.

        `upgradable` has to be said out loud because its default is False: a
        card built without it is one whose button is greyed, and every test
        below would stop on that rather than on the thing it is checking.
        """
        fields.setdefault("upgradable", True)
        return HeroCard(hero="duke", point=(1407, 648), price=56_000, resource="dark", **fields)

    def test_the_hall_is_asked_for_before_the_village_is_swept(self) -> None:
        """One building, so the first guess that opens it ends the search.

        This is where the finder is worth the most: the sweep's grid steps over
        this building entirely on its first pass — measured, it sits 86 px from
        the nearest grid point — so finding it costs up to 52 taps across two
        passes, each with a settle and a capture. Measured live, the first guess
        opened it.
        """
        runner = self._runner(ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()))
        answer = ScreenSpots(spots=[ScreenPoint(x_pct=62, y_pct=48)])
        with (
            patch.object(GeminiClient, "generate_structured", return_value=answer),
            patch.object(HeroRunner, "_frame", return_value=b""),
            patch.object(HeroRunner, "_after_tap", return_value=b"hall") as tapped,
            patch.object(runner, "_try_menu", return_value=b"cards"),
            patch.object(runner, "_sweep") as swept,
        ):
            assert runner._open() == b"cards"
        assert tapped.call_args.args[0] == (992, 432)
        swept.assert_not_called()

    def test_a_hall_the_finder_missed_still_gets_swept_for(self) -> None:
        """The sweep stays underneath, because a hall nobody can place is still
        standing somewhere on the map.
        """
        runner = self._runner(ai=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting()))
        answer = ScreenSpots(spots=[ScreenPoint(x_pct=10, y_pct=10)])
        with (
            patch.object(GeminiClient, "generate_structured", return_value=answer),
            patch.object(HeroRunner, "_frame", return_value=b""),
            patch.object(HeroRunner, "_after_tap", return_value=b""),
            patch.object(runner, "_try_menu", return_value=None),
            patch.object(runner, "_home", return_value=_stock()),
            patch.object(runner, "_sweep", return_value=iter(())) as swept,
        ):
            assert runner._open() is None
        assert swept.call_count == 2

    def test_a_button_the_hall_has_greyed_is_not_an_upgrade_that_can_be_started(self) -> None:
        """A capped hero keeps its price and loses its button, so the price says
        nothing about whether it can be raised.

        Measured live: 飛龍公爵 at 15 asked for 220 000 dark against a village
        holding exactly 220 000, and the game answered the tap with a red line
        saying to raise the Hero Hall to 11 first — so the run had the money and
        still could not spend it. Reported through the price alone it came back
        as "no confirmation sheet came up", which reads as a swallowed tap.
        """
        runner = self._runner(hero="duke")
        report = HeroReport()
        with patch.object(runner, "_bring_on") as brought:
            message = runner._raise(
                report,
                {"duke": self._duke(upgradable=False)},
                VillageStock(gold=0, elixir=0, dark=999_999),
            )
        brought.assert_not_called()
        assert "停用" in message
        assert report.started is None

    def test_an_upgrade_the_village_cannot_pay_for_is_never_tapped(self) -> None:
        """This is the guard, not a courtesy. The game answers an upgrade it
        cannot charge for with a gem purchase, and nothing below the prompt layer
        would stop a loop walking into one.
        """
        runner = self._runner(hero="duke")
        report = HeroReport()
        with patch.object(AdbController, "tap") as tapped:
            message = runner._raise(
                report, {"duke": self._duke()}, VillageStock(gold=0, elixir=0, dark=55_999)
            )
        assert "資源不夠" in message
        assert report.started is None
        tapped.assert_not_called()

    def test_a_village_with_no_builder_free_never_looks_for_the_hall(self) -> None:
        """Every builder busy is the ordinary state of a farming village, and
        finding the hall costs minutes of tapping. The count is known before any
        of that, so a run that was asked to spend ends on it.
        """
        runner = self._runner(hero="duke")
        with (
            patch.object(runner, "_home", return_value=VillageStock(gold=0, elixir=0, dark=0)),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero, "free_builders", return_value=(0, 5)),
            patch.object(runner, "_open") as opened,
        ):
            report = runner.run()
        assert "都在忙" in report.message
        opened.assert_not_called()

    def test_a_run_that_only_reads_still_opens_the_hall_with_no_builder_free(self) -> None:
        """Reading costs nothing to be wrong about, and what each hero's next
        level costs is the half worth having when nothing can be started.
        """
        runner = self._runner()
        with (
            patch.object(runner, "_home", return_value=VillageStock(gold=0, elixir=0, dark=0)),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero, "free_builders", return_value=(0, 5)),
            patch.object(runner, "_open", return_value=b"") as opened,
            patch.object(runner, "_walk", return_value={}),
            patch.object(runner, "_close"),
        ):
            runner.run()
        opened.assert_called_once()

    def test_a_hero_already_being_upgraded_has_no_button_to_tap(self) -> None:
        """Its card is on the screen with a countdown where the button was, which
        is why a price that does not read leaves the card in the list rather than
        out of it.
        """
        runner = self._runner(hero="duke")
        report = HeroReport()
        with patch.object(AdbController, "tap") as tapped:
            message = runner._raise(
                report,
                {"duke": HeroCard(hero="duke", point=(1407, 648))},
                VillageStock(gold=0, elixir=0, dark=200_000),
            )
        assert "正在升級中" in message
        tapped.assert_not_called()

    def test_a_storage_that_never_moved_is_not_an_upgrade(self) -> None:
        """Confirming the sheet is not proof: a tap the game swallows raises
        nothing at all, and the sheet closes the same way either way.
        """
        runner = self._runner(hero="duke")
        report = HeroReport()
        held = VillageStock(gold=0, elixir=0, dark=200_000)
        with (
            patch.object(hero, "hero_cards", return_value=[self._duke()]),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(runner, "_start", return_value=True),
            patch.object(runner, "_close"),
            patch.object(runner, "_home", return_value=held),
        ):
            message = runner._raise(report, {"duke": self._duke()}, held)
        assert report.started is None
        assert "只少了 0" in message

    def test_the_hall_is_read_again_before_the_button_is_tapped(self) -> None:
        """The walk ends wherever the row ran out, which is not where it was when
        the card was read — so tapping the remembered point would tap whichever
        card has slid into that place.
        """
        runner = self._runner(hero="duke")
        report = HeroReport()
        with (
            patch.object(hero, "hero_cards", return_value=[]),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(AdbController, "tap") as tapped,
        ):
            message = runner._raise(
                report, {"duke": self._duke()}, VillageStock(gold=0, elixir=0, dark=200_000)
            )
        assert "不在畫面上" in message
        tapped.assert_not_called()


class ClanTests(unittest.TestCase):
    """The clan chat, and the panel a donation request opens."""

    def test_a_friendly_challenge_is_not_a_donation_request(self) -> None:
        """偵察 is the same green at a larger size — measured 5154 px against
        增援's 4754 — so "the biggest green" opens somebody's village instead.
        The challenge is the one with 進攻 in red on its row.
        """
        frame = (FRAMES / "clan_request_and_challenge.png").read_bytes()
        assert reinforce_button(frame) == (477, 440)

    def test_a_village_is_not_a_chat_full_of_green_buttons(self) -> None:
        assert reinforce_button((FRAMES / "home_markers.png").read_bytes()) is None

    def test_the_donation_panel_is_read_from_its_own_top(self) -> None:
        """It floats: it is drawn against the request card that opened it, so the
        same panel sat 57 px further down on one live run than the other. Fixed
        rows would have read the cards off the gap above them.
        """
        low = (FRAMES / "clan_donate_low.png").read_bytes()
        high = (FRAMES / "clan_donate_high.png").read_bytes()
        assert panel_top(low) == 166
        assert panel_top(high) == 109
        assert len(donatable_cards(low)) == 10
        assert len(donatable_cards(high)) == 14

    def test_a_greyed_card_is_not_offered(self) -> None:
        """Four of the fourteen are greyscale on one frame and none on the other,
        which is what a card the village cannot give looks like.
        """
        assert len(donatable_cards((FRAMES / "clan_donate_low.png").read_bytes())) == 10
        assert donatable_cards((FRAMES / "home_markers.png").read_bytes()) == []


if __name__ == "__main__":
    unittest.main()
