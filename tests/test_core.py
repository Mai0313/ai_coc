import io
import json
import math
import time
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch
from collections.abc import Callable, Sequence

from PIL import Image, ImageDraw
import pytest
from pydantic import ValidationError

from ai_coc import plans, models, commands, logging_setup
from ai_coc.ui import hero, walls, attack
from ai_coc.ui import runner as shared
from ai_coc.models import (
    RunLog,
    HeroCard,
    ProbeRay,
    WallMenu,
    AppConfig,
    HeroOrder,
    LootOffer,
    ScoutView,
    WallBatch,
    AttackPlan,
    HeroReport,
    PlayedPlan,
    AdbEndpoint,
    ScreenPoint,
    StockLimits,
    WallOptions,
    WallUpgrade,
    VillageStock,
    AttackOptions,
    DisplayTarget,
    LootOverrides,
    WallCandidate,
    BoundarySurvey,
    GeminiSettings,
    LootThresholds,
)
from ai_coc.prompts import PROMPTS, PROMPT_DIR, render
from ai_coc.ui.hero import HeroRunner
from ai_coc.ui.walls import WallRunner
from ai_coc.ui.attack import (
    PLAYFIELD,
    RAGE_PATH,
    RAGE_SPAN,
    DEPLOY_END,
    END_BATTLE,
    DROP_STRIDE,
    LINE_POINTS,
    DEPLOY_LINES,
    DEPLOY_START,
    PLAN_TIMEOUT,
    ABANDON_BUTTON,
    DROPS_PER_PASS,
    DEPLOY_ATTEMPTS,
    RESULT_ATTEMPTS,
    UNREADABLE_SKIPS,
    AttackRunner,
    spaced,
    push_out,
    deploy_line,
    drop_points,
    planned_line,
    single_spots,
    deploy_candidates,
)
from ai_coc.ui.runner import SWEEP_X, SWEEP_Y, SWEEP_LIMIT, SWEEP_STAGGER
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
from ai_coc.parsers.field import view_shift, army_centre
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
    digits_from,
    field_units,
    card_drained,
    freeze_cards,
    skip_offered,
    army_strength,
    counted_cards,
    attack_menu_open,
)
from ai_coc.ui.main_window import LIVE_INTERVAL, MainWindow
from ai_coc.adapters.config import ConfigStore
from ai_coc.parsers.village import parse_village
from ai_coc.adapters.secrets import dotenv_value
from ai_coc.parsers.boundary import (
    DEPLOY_BOUND,
    VILLAGE_GRID,
    fitted_line,
    village_box,
    boundary_line,
    boundary_reach,
)
from ai_coc.parsers.building import (
    PRICE_TOLERANCE,
    wall_menu,
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
_ANSWERED = {
    "rage_points": [],
    "freeze_points": [],
    "heroes": [],
    "rage_after": 15,
    "freeze_after": 30,
}
# What each hero's ability is worth waiting for, which used to be a table of
# constants in the settings file and is now the planner's answer per battle.
# Tests name a hero and mean its delay, so this is what keeps them readable.
_ABILITY_SECONDS = {
    "queen": 1,
    "king": 20,
    "warden": 30,
    "champion": 45,
    "minion_prince": 20,
    "duke": 20,
    "unknown": 20,
}


def _orders(*kinds: str) -> list[HeroOrder]:
    """Hero orders on those delays, all dropped on the same nominal spot."""
    return [
        HeroOrder(
            kind=kind, drop=ScreenPoint(x_pct=30, y_pct=30), ability_after=_ABILITY_SECONDS[kind]
        )
        for kind in kinds
    ]


_LINE = {
    "deploy_start": ScreenPoint(x_pct=37.5, y_pct=12.2),
    "deploy_end": ScreenPoint(x_pct=14.4, y_pct=42.2),
    **_ANSWERED,
}

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
        """One 回營 tap was not enough, and four runs in a row then stood down."""
        assert battle_over((FRAMES / "battle_result.png").read_bytes())
        assert not battle_over((FRAMES / "attack_menu.png").read_bytes())
        assert not battle_over((FRAMES / "scout_in_battle.png").read_bytes())

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
        plan = plans.flat()
        assert plan.deploy_start is not None
        assert plan.deploy_end is not None
        assert len(plan.rage_points) == len(RAGE_PATH)
        assert plan.freeze_points
        assert plan.rage_after
        assert plan.freeze_after

    def test_the_flat_plan_draws_the_line_the_loop_used_to_hold_in_constants(self) -> None:
        """It has to reproduce the old fallback, or the default quietly changed."""
        assert planned_line(plans.flat()) == DEPLOY_LINES["top_left"]

    def test_a_plan_survives_being_written_out_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plan.json"
            path.write_text(plans.flat().model_dump_json(indent=2), encoding="utf-8")
            assert plans.load(path) == plans.flat()

    def test_every_timing_on_an_attack_is_asked_for(self) -> None:
        """The planner has to answer all of them, and there is nowhere else to look.

        They used to be a table of constants in the settings file, chosen without
        a village on screen — which is the same guess the planner makes, minus
        the village. A field with a default is optional in the JSON schema, so
        anything the schedule cannot run without carries none.
        """
        required = set(AttackPlan.model_json_schema()["required"])
        assert {"rage_after", "freeze_after", "heroes"} <= required
        assert "ability_after" in set(HeroOrder.model_json_schema()["required"])

    def test_a_hero_carries_where_it_goes_as_well_as_when_it_fires(self) -> None:
        """One order per card, so a queen sent to clear the edge is expressible."""
        order = HeroOrder(kind="queen", drop=ScreenPoint(x_pct=20, y_pct=40), ability_after=2)
        assert order.drop.pixels() == (320, 360)

    def test_the_planner_is_asked_with_a_deadline_and_falls_back_without_one(self) -> None:
        """One call took 180.7 s and the three-minute battle it planned was over.

        The flat plan is right there and costs nothing, so a planner that will
        not answer inside the scout countdown is simply not waited for.
        """
        runner = AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            thresholds=LootThresholds(),
            ai=GeminiClient(settings=GeminiSettings(api_key="not-a-real-key")),
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
        assert config.stock != StockLimits()

    def test_settings_survive_being_written_out_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(path=Path(td) / "config.json")
            saved = AppConfig(
                thresholds=LootThresholds(min_gold=1, min_elixir=2, min_dark=3),
                gemini_model="gemini-not-the-default",
            )
            store.save(saved)
            assert store.load() == saved

    def test_a_setting_nothing_reads_any_more_is_dropped_from_the_file(self) -> None:
        """A key left behind reads as one still being honoured, and is not.

        `timings` stayed in every existing file for a release after every clock
        moved onto the plan, so someone editing 大守護者's thirty seconds there
        would have been editing nothing at all. Pydantic ignoring the key is
        what keeps the upgrade from failing; rewriting the file is what stops it
        lying about what the run will do.
        """
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(
                json.dumps({"restart_every": 7, "timings": {"queen": 1, "warden": 30}}),
                encoding="utf-8",
            )
            config = ConfigStore(path=path).load()
            written = json.loads(path.read_text(encoding="utf-8"))
        assert config.restart_every == 7
        assert "timings" not in written
        # What it parsed is what it wrote. Rewriting `AppConfig()` instead would
        # pass every other assertion here while wiping the user's settings, and
        # this call is the first thing that runs after an upgrade.
        assert written["restart_every"] == 7
        # And a key the file never had is filled in, so it reads as what this run
        # will actually do rather than as what happened to be saved once.
        assert written["keepalive_seconds"] == AppConfig().keepalive_seconds

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

    def test_every_prompt_the_code_asks_for_exists(self) -> None:
        expected = {
            "agent_profile",
            "agent_step",
            "attack_plan",
            "chat",
            "live_test",
            "locate_target",
            "reference_image",
            "vision",
        }
        assert expected <= set(PROMPTS)

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

    def test_grid_coordinates_round_trip(self) -> None:
        for point in (VILLAGE_GRID.centre, (600, 300), (1100, 500)):
            assert VILLAGE_GRID.pixel(VILLAGE_GRID.tile(point)) == point

    def test_the_grid_corners_are_the_diamond_vertices(self) -> None:
        cx, cy = VILLAGE_GRID.centre
        assert VILLAGE_GRID.pixel((0, 0)) == (cx, cy - VILLAGE_GRID.half_height)
        assert VILLAGE_GRID.pixel((44, 44)) == (cx, cy + VILLAGE_GRID.half_height)
        assert VILLAGE_GRID.pixel((44, 0)) == (cx + VILLAGE_GRID.half_width, cy)


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
        rays = [angle * 30 for angle in range(12)]
        for name in ("battle_boundary_grass.png", "battle_boundary_ice.png"):
            points = boundary_line((FRAMES / name).read_bytes(), rays)
            radii = [math.hypot(x - 800, y - 400) for x, y in points]
            assert len(points) >= 6, name
            # One closed curve, so nothing should sit near the middle of it.
            assert min(radii) > 200, (name, sorted(radii))


class FieldTests(unittest.TestCase):
    """Locating the fighting from what changed between two captures."""

    def _frame(self, *blobs: tuple[int, int, int, int]) -> bytes:
        image = Image.new("RGB", (1600, 900), (60, 120, 40))
        painter = ImageDraw.Draw(image)
        for box in blobs:
            painter.rectangle(box, fill=(230, 230, 230))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_the_reading_lands_on_what_moved(self) -> None:
        centre = army_centre(self._frame(), self._frame((560, 290, 800, 410)))
        assert centre is not None
        assert 560 <= centre[0] <= 800
        assert 290 <= centre[1] <= 410

    def test_the_busiest_patch_beats_the_average(self) -> None:
        """A base fires back across its whole width; the army works at one edge of it."""
        centre = army_centre(
            self._frame(), self._frame((400, 300, 560, 420), (1100, 250, 1400, 550))
        )
        assert centre is not None
        assert centre[0] > 1000

    def test_a_village_nobody_is_attacking_reads_as_nothing(self) -> None:
        assert army_centre(self._frame(), self._frame()) is None

    def test_a_shimmer_spread_over_the_whole_map_is_not_an_army(self) -> None:
        """Thin enough and every cell rounds away, which used to answer with a map corner."""
        # One pixel per mark, three to a cell, which averages to under half a
        # level and comes back from the grid as a zero.
        speckle = [(x, y, x, y) for x in range(50, 1540, 40) for y in range(115, 690, 14)]
        assert army_centre(self._frame(), self._frame(*speckle)) is None

    def test_leaving_the_battle_is_not_an_army(self) -> None:
        """Measured, a screen change moves 338k pixels where the busiest battle moved 162k."""
        assert army_centre(self._frame(), self._frame((60, 120, 1560, 690))) is None

    def test_the_panels_that_change_on_their_own_are_not_the_fighting(self) -> None:
        """The loot counts down, our storages climb and the clock ticks every second."""
        moved = self._frame((40, 120, 300, 260), (620, 20, 980, 100), (1310, 30, 1580, 180))
        assert army_centre(self._frame(), moved) is None

    def test_a_frame_of_another_resolution_is_rejected(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (1280, 720)).save(buffer, format="PNG")
        with pytest.raises(ValueError, match="1600x900"):
            army_centre(buffer.getvalue(), buffer.getvalue())

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
        limits = StockLimits(stop_gold=15_000_000, stop_elixir=15_000_000)
        assert limits.full(VillageStock(gold=15_000_000, elixir=400_000, dark=1000)) is None

    def test_farming_stops_once_every_watched_storage_is_full(self) -> None:
        limits = StockLimits(stop_gold=15_000_000, stop_elixir=15_000_000)
        full = limits.full(VillageStock(gold=15_000_000, elixir=15_400_000, dark=1000))
        # Dark is left unwatched at 0, so it neither stops the run nor holds it open.
        assert full == ["金幣", "聖水"]

    def test_a_stop_limit_left_at_zero_watches_nothing(self) -> None:
        assert StockLimits().full(VillageStock(gold=99999999, elixir=1, dark=1)) is None

    def test_deploy_line_runs_the_whole_flank(self) -> None:
        points = deploy_line(8)
        assert len(points) == 8
        assert points[0] == DEPLOY_START
        assert points[-1] == DEPLOY_END

    def test_a_hero_order_names_the_hero_rather_than_the_slot(self) -> None:
        """An upgrading hero has no card at all, so every slot after it shifts.

        Naming the hero on each order is what survives that: the loop matches
        orders to cards left to right and reads the delay off the order, so a
        row one card shorter costs the missing hero and nothing else.
        """
        orders = _orders("queen", "warden")
        assert [order.kind for order in orders] == ["queen", "warden"]
        assert [order.ability_after for order in orders] == [1, 30]

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
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=37.5, y_pct=12.2),
            deploy_end=ScreenPoint(x_pct=14.4, y_pct=42.2),
            **_ANSWERED,
        )
        assert planned_line(plan) == DEPLOY_LINES["top_left"]

    def test_a_planned_line_across_the_village_falls_back_to_a_flank(self) -> None:
        """Its midpoint sits on the middle, where push_out has no direction to move it."""
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=25, y_pct=30),
            deploy_end=ScreenPoint(x_pct=75, y_pct=70),
            **_ANSWERED,
        )
        assert planned_line(plan) is None

    def test_a_line_only_half_drawn_falls_back_too(self) -> None:
        assert planned_line(None) is None
        assert planned_line(None) is None

    def _runner(self) -> AttackRunner:
        return AttackRunner(
            adb=AdbController(endpoint=AdbEndpoint(port=16384)),
            display=DisplayTarget(logical_id="1", physical_id="2"),
            thresholds=LootThresholds(),
        )

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

    def _aimed(
        self, targets: tuple[tuple[int, int], ...], centre: tuple[int, int] | None
    ) -> tuple[tuple[int, int], ...]:
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack, "army_centre", return_value=centre),
            patch.object(attack.time, "sleep"),
        ):
            return runner._onto_army(targets)

    def test_the_rage_pattern_slides_onto_the_fighting(self) -> None:
        """The plan draws the shape; the screen says where the army has got to."""
        targets = ((500, 300), (700, 300), (500, 420), (700, 420))
        moved = self._aimed(targets, (700, 400))
        # The spacing is what keeps two bottles from overlapping, so it survives.
        assert [b[0] - a[0] for a, b in zip(targets, moved, strict=True)] == [100] * 4
        assert [b[1] - a[1] for a, b in zip(targets, moved, strict=True)] == [40] * 4

    def test_an_unreadable_field_leaves_the_planned_rage_where_it_was(self) -> None:
        """A bad shift is worse than a stale one: the plan at least aimed at the village."""
        targets = ((500, 300), (700, 420))
        assert self._aimed(targets, None) == targets

    def test_a_planned_bottle_landing_inside_another_is_dropped(self) -> None:
        """The planner is given the footprint and overlaps its points regardless.

        These five are one live reply, and four of their ten pairs sit inside one
        another. Rage does not stack, so each of those pairs buys one bottle's
        worth of ground for two bottles. The closest of the four is (768, 495)
        against (800, 378): 121 px apart, which clears the ellipse's 240 px axis
        and sits just inside its 120 px one — the sort of call a model reading a
        screenshot cannot make, which is why the prompt saying "do not overlap"
        does not settle it and this does.
        """
        planned = [(448, 522), (608, 450), (560, 585), (768, 495), (800, 378)]
        assert spaced(planned) == [(448, 522), (768, 495)]

    def test_the_fixed_grid_is_already_spaced(self) -> None:
        """Which is what makes it usable to top up whatever the planner's points lose."""
        assert spaced(RAGE_PATH) == list(RAGE_PATH)
        assert RAGE_PATH[1][0] - RAGE_PATH[0][0] == RAGE_SPAN[0]

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
            runner._cast([1060], RAGE_PATH[:5], b"")
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

    def _abilities(
        self, singles: list[int], on_field: list[int], kinds: list[str], spent: Sequence[int] = ()
    ) -> list[tuple[int, int]]:
        """What `_deploy` schedules an ability for, and how long after the opening.

        `singles` is the row's one-off cards left to right, `on_field` the ones
        the game drew a health bar over — which says a unit is out, not that it
        is a hero — and `spent` the ones that landed by going grey instead. A
        card in neither never left its card.
        """
        runner = self._runner()
        plan = plans.flat().model_copy(update={"heroes": _orders(*kinds)})
        moves: list[tuple[float, str, object]] = []
        opening: list[float] = []

        def dropped(
            cards: list[int], line: object, what: str, wanted: object = None
        ) -> tuple[list[int], list[int]]:
            bars = [card for card in cards if card in on_field]
            return [card for card in cards if card in on_field or card in spent], bars

        def schedule(opened: float, scheduled: list[tuple[float, str, object]]) -> None:
            opening.append(opened)
            moves.extend(scheduled)

        with (
            patch.object(AttackRunner, "_settle_camera", side_effect=lambda frame: frame),
            # The zoom that runs before it wants a real emulator to pinch.
            patch.object(AttackRunner, "_settle_zoom", side_effect=lambda frame: frame),
            patch.object(attack, "card_groups", return_value=[[100], singles]),
            patch.object(attack, "counted_cards", return_value=[]),
            patch.object(attack, "freeze_cards", return_value=[]),
            patch.object(AttackRunner, "_plan", return_value=plan),
            patch.object(AttackRunner, "_wait_for_battle", return_value=None),
            patch.object(AttackRunner, "_clear_flank", side_effect=lambda frame, preset: frame),
            patch.object(AttackRunner, "_usable_line", return_value=0),
            patch.object(AttackRunner, "_drop_singles", side_effect=dropped),
            patch.object(AttackRunner, "_spread_troops", return_value=[(0, 0)]),
            patch.object(AttackRunner, "_run_schedule", side_effect=schedule),
            patch.object(attack.time, "sleep"),
        ):
            runner._deploy(b"")
        return [(round(when - opening[0]), int(what.rsplit(" ", 1)[1])) for when, what, _ in moves]

    def test_a_leading_card_is_a_hero_when_the_plan_names_one_per_card(self) -> None:
        """An army carrying no siege machine puts a hero in the leader's slot.

        Nothing on the row separates the two: neither carries an `xN` and both
        sit in the same group, so the leader is picked off the game's own
        ordering — siege machine first — and the plan's own count is what
        corrects it. Without that correction the leading hero got no ability at
        all and every kind in the plan's list was read a slot off the card it
        names, so a queen's cloak went to whoever stood next to her.
        """
        cards = [600, 700, 800]
        played = self._abilities(cards, on_field=cards, kinds=["queen", "king", "champion"])
        assert [card for _, card in played] == cards
        assert [delay for delay, _ in played] == [1, 20, 45]

    def test_a_leading_siege_machine_is_not_given_a_hero_ability(self) -> None:
        """Two heroes named against three one-off cards is an army carrying one."""
        played = self._abilities(
            [600, 700, 800], on_field=[700, 800], kinds=["queen", "king"], spent=[600]
        )
        assert [card for _, card in played] == [700, 800]
        assert [delay for delay, _ in played] == [1, 20]

    def test_a_health_bar_over_the_siege_machine_does_not_make_it_a_hero(self) -> None:
        """The game draws one over a siege machine as readily as over a hero.

        Measured across all three rounds of a recorded run: the 攻城戰車 landed
        and `field_units` read its card on the very next frame. Asked as the
        hero test it used to be, that put a slot in front of every real hero —
        the queen went out on the king's twenty seconds where her cloak wants
        one. A warden stands in for that row's 亡靈王子 here: the row it was
        measured on held three heroes that all want about twenty seconds, so a
        shift among those three shows up in no timing at all.
        """
        cards = [436, 562, 683, 804, 928]
        played = self._abilities(cards, on_field=cards, kinds=["queen", "king", "warden", "duke"])
        assert [card for _, card in played] == cards[1:]
        assert [delay for delay, _ in played] == [1, 20, 30, 20]

    def test_a_leading_hero_the_game_refused_still_holds_its_slot(self) -> None:
        """The bar cannot speak for a card that never went down anywhere.

        A leader refused at all five spots draws no health bar, so the plan's
        own count answers instead: three heroes named against three one-off
        cards is an army with no siege machine, whether or not the leader made
        it onto the field. Without that the two behind it take the queen's and
        the king's timings while they hold the king and the champion.
        """
        played = self._abilities(
            [600, 700, 800], on_field=[700, 800], kinds=["queen", "king", "champion"]
        )
        assert [card for _, card in played] == [700, 800]
        assert [delay for delay, _ in played] == [20, 45]

    def test_with_no_plan_to_count_the_health_bar_is_still_what_answers(self) -> None:
        """There is nothing else to ask, and being wrong costs nothing there.

        Every card takes the same `unknown` delay without a plan, so a leader
        read the wrong way shifts no timing; the only cost is a tap on a card
        that has already been spent.
        """
        cards = [600, 700, 800]
        played = self._abilities(cards, on_field=cards, kinds=[])
        assert [card for _, card in played] == cards
        assert [delay for delay, _ in played] == [20, 20, 20]

    def test_a_plan_naming_a_count_that_fits_neither_row_says_so(self) -> None:
        """Two heroes against four one-off cards fits neither arithmetic.

        One fewer than the cards is an army carrying a siege machine and as many
        is one that is not; two fewer is a plan out of step with the row, which
        a `--plan-in` file replayed after two heroes went into upgrades gives —
        their cards simply disappear. Neither reading of the leader is safe
        then, so the run is told rather than left to find out from timings that
        are quietly a slot out.
        """
        cards = [600, 700, 800, 900]
        with self.assertLogs("ai_coc.ui.attack", level="WARNING") as caught:
            self._abilities(cards, on_field=cards, kinds=["queen", "king"])
        assert any("2 hero(es) against 4 one-off card(s)" in line for line in caught.output)

    def test_a_plan_one_hero_short_is_not_flagged_because_it_cannot_be_seen(self) -> None:
        """It is the same count as an army carrying a siege machine, and read as one.

        That costs the leading hero its ability. The bar it replaced got this
        one case right — and every battle this army fights wrong, since it does
        carry a siege machine and the game draws a bar over it.
        """
        cards = [600, 700, 800, 900]
        played = self._abilities(cards, on_field=cards, kinds=["queen", "king", "warden"])
        assert [card for _, card in played] == [700, 800, 900]

    def test_a_hero_that_never_left_its_card_is_not_given_an_ability(self) -> None:
        """An ability tap on a hero still in its card deploys it with nothing around it."""
        played = self._abilities(
            [600, 700, 800], on_field=[700], kinds=["queen", "king"], spent=[600]
        )
        assert [card for _, card in played] == [700]

    def _aimed_at(self, singles: list[int], orders: list[HeroOrder]) -> dict[int, object]:
        """Where `_deploy` sends each one-off card, given a plan naming these heroes."""
        runner = self._runner()
        plan = plans.flat().model_copy(update={"heroes": orders})
        sent: dict[int, object] = {}

        def dropped(
            cards: list[int], line: object, what: str, wanted: dict | None = None
        ) -> tuple[list[int], list[int]]:
            sent.update(wanted or {})
            return list(cards), list(cards)

        with (
            patch.object(AttackRunner, "_settle_camera", side_effect=lambda frame: frame),
            patch.object(AttackRunner, "_settle_zoom", side_effect=lambda frame: frame),
            patch.object(attack, "card_groups", return_value=[[100], singles]),
            patch.object(attack, "counted_cards", return_value=[]),
            patch.object(attack, "freeze_cards", return_value=[]),
            patch.object(AttackRunner, "_plan", return_value=plan),
            patch.object(AttackRunner, "_wait_for_battle", return_value=None),
            patch.object(AttackRunner, "_clear_flank", side_effect=lambda frame, preset: frame),
            patch.object(AttackRunner, "_usable_line", return_value=0),
            patch.object(AttackRunner, "_drop_singles", side_effect=dropped),
            patch.object(AttackRunner, "_spread_troops", return_value=[(0, 0)]),
            patch.object(AttackRunner, "_run_schedule"),
            patch.object(attack.time, "sleep"),
        ):
            runner._deploy(b"")
        return sent

    def test_a_refused_hero_still_gets_the_spot_the_probe_proved(self) -> None:
        """The plan's point goes ahead of the shared ladder, not over its first rung.

        `single_spots[0]` is the midpoint `_usable_line` already probed and the
        game already accepted, so it is the one spot with evidence behind it.
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

    def test_each_hero_goes_where_its_own_order_says(self) -> None:
        """Heroes do different jobs in one attack, and one shared spot cannot say so.

        A village usually wants one or two walking the outside to clear the
        stray buildings that pull an army off course, and the rest going in
        behind the push. The leading card is skipped here because the plan names
        one hero fewer than there are one-off cards, which is an army carrying a
        siege machine — and nothing plans where that goes.
        """
        edge = HeroOrder(kind="queen", drop=ScreenPoint(x_pct=25, y_pct=20), ability_after=2)
        middle = HeroOrder(kind="warden", drop=ScreenPoint(x_pct=45, y_pct=35), ability_after=30)
        sent = self._aimed_at([436, 562, 683], [edge, middle])
        assert sent == {562: (400, 180), 683: (720, 315)}

    def test_a_plan_that_names_every_card_aims_the_leader_too(self) -> None:
        """Then the leading card is a hero rather than a siege machine, so it has an order."""
        orders = [
            HeroOrder(kind="king", drop=ScreenPoint(x_pct=25, y_pct=20), ability_after=20),
            HeroOrder(kind="queen", drop=ScreenPoint(x_pct=45, y_pct=35), ability_after=2),
        ]
        assert self._aimed_at([436, 562], orders) == {436: (400, 180), 562: (720, 315)}

    def test_the_freeze_no_longer_queues_behind_the_slowest_hero(self) -> None:
        """Cast after the last ability it sat out a champion's 45 seconds first."""
        played: list[str] = []
        # The heroes land twenty seconds into the attack; their abilities run
        # from there, the freeze from the opening.
        opened, landed = 0.0, 20.0
        champion, queen = _orders("champion", "queen")
        moves = [
            (landed + champion.ability_after, "champion", lambda: played.append("champion")),
            (landed + queen.ability_after, "queen", lambda: played.append("queen")),
            (opened + plans.flat().freeze_after, "freeze", lambda: played.append("freeze")),
        ]
        with (
            patch.object(AttackRunner, "_battle_ended", return_value=False),
            patch.object(attack.time, "sleep"),
        ):
            self._runner()._run_schedule(opened, moves)
        assert played == ["queen", "freeze", "champion"]

    def _schedule_over(self, reads: bool, result_screen: bool) -> list[str]:
        """One scheduled move played against a canned frame."""
        played: list[str] = []
        view = ScoutView(loot=LootOffer(gold=1, elixir=1, dark=1), can_skip=False)
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(attack, "read_scout", return_value=view if reads else None),
            patch.object(attack, "battle_over", return_value=result_screen),
            patch.object(attack.time, "sleep"),
        ):
            self._runner()._run_schedule(0.0, [(0.0, "freeze", lambda: played.append("freeze"))])
        return played

    def test_an_unreadable_panel_is_not_a_battle_that_ended(self) -> None:
        """The battlefield shows through the panel, and then no row of it resolves.

        Measured on a live battle at 69% with two stars and every rage still in
        its card, the gold row of 485 715 read on none of its frames. Taken for
        the result screen, that abandons every spell and hero ability still to
        come — and it did, for five rounds of one recorded run.
        """
        assert self._schedule_over(reads=False, result_screen=False) == ["freeze"]

    def test_the_result_screen_stops_the_schedule(self) -> None:
        """Some card slots sit under its 回營 button, so this is what must not be tapped."""
        assert self._schedule_over(reads=False, result_screen=True) == []

    def test_a_refused_flank_leaves_the_other_three_to_try(self) -> None:
        """The plan's own side goes first behind its line, then the flanks it did not pick."""
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=25, y_pct=30),
            deploy_end=ScreenPoint(x_pct=75, y_pct=70),
            deploy_from="bottom_right",
            **_ANSWERED,
        )
        # That line runs across the village, so only the named flanks are left.
        assert planned_line(plan) is None
        candidates = deploy_candidates(plan)
        assert candidates[0] == DEPLOY_LINES["bottom_right"]
        assert sorted(candidates) == sorted(DEPLOY_LINES.values())

    def test_everything_the_planner_is_asked_for_is_required_of_it(self) -> None:
        """A field with a default is optional in the schema, and Gemini leaves those out.

        Three runs running answered a start and no end; a fourth named five rage
        points, no freeze point and no hero at all, against a screen holding a
        freeze bottle and four hero cards.
        """
        required = set(AttackPlan.model_json_schema()["required"])
        assert {"deploy_start", "deploy_end", "rage_points", "freeze_points", "heroes"} <= required

    def test_a_usable_planned_line_is_tried_before_any_flank(self) -> None:
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=62.5, y_pct=12.2),
            deploy_end=ScreenPoint(x_pct=85.6, y_pct=42.2),
            **_ANSWERED,
        )
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


class StopFlagTests(unittest.TestCase):
    """Stopping a headless run, which is a file because it cannot be a signal.

    The window has a stop button; a run put in the background by whatever started
    it has nothing, and killing the process never reaches the `KeyboardInterrupt`
    handler that leaves the game somewhere the next run can start from.
    """

    def test_the_flag_is_written_and_read_by_the_same_pair(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop"
            with patch.object(commands, "STOP_FLAG", flag):
                assert not commands.stop_requested()
                commands.stop()
                assert commands.stop_requested()

    def test_a_new_run_consumes_a_flag_the_last_one_left_behind(self) -> None:
        """Stopping means the loop running now, never the next one to start.

        Checked through a run that dies on its first real step, which is what
        says the flag is cleared before anything touches the game rather than
        somewhere along the way.
        """
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop"
            flag.write_text("", encoding="utf-8")
            with (
                patch.object(commands, "STOP_FLAG", flag),
                patch.object(commands, "_controller", side_effect=RuntimeError),
                pytest.raises(RuntimeError),
            ):
                commands.attack(AttackOptions())
            assert not flag.exists()

    def test_the_barracks_wait_gives_up_the_moment_it_is_stood_down(self) -> None:
        """A minute slept through in one go reads as a stop that did nothing."""
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop"
            flag.write_text("", encoding="utf-8")
            with patch.object(commands, "STOP_FLAG", flag):
                started = time.monotonic()
                assert commands._rest(commands.IDLE_REST)
            assert time.monotonic() - started < commands.IDLE_REST / 2

    def test_a_finished_run_takes_the_flag_with_it(self) -> None:
        """A flag still sitting there means no loop has picked it up yet, which
        is what makes the file worth looking at to tell whether a stop landed.
        Asked for mid-run, so the clear at the start cannot be what answers it.
        """
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop"

            def asked_mid_run() -> MagicMock:
                flag.write_text("", encoding="utf-8")
                return MagicMock(stock_full=True)

            with (
                patch.object(commands, "STOP_FLAG", flag),
                patch.object(commands, "_controller"),
                patch.object(commands, "_planner", return_value=None),
                patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
                patch.object(commands, "FrameTicker"),
                patch.object(commands, "AttackRunner") as runner,
            ):
                runner.return_value.run.side_effect = asked_mid_run
                commands.attack(AttackOptions(rounds=1))
            assert not flag.exists()

    def test_the_wall_command_answers_the_same_flag(self) -> None:
        """One flag for every long loop rather than a mechanism each."""
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop"
            report = MagicMock(message="")
            report.paid.return_value = 0
            with (
                patch.object(commands, "STOP_FLAG", flag),
                patch.object(commands, "_controller"),
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
            flag = Path(folder) / "stop"

            def asked_mid_run() -> MagicMock:
                flag.write_text("", encoding="utf-8")
                report = MagicMock(message="升級了 3 面城牆")
                report.paid.return_value = 0
                return report

            with (
                patch.object(commands, "STOP_FLAG", flag),
                patch.object(commands, "_controller"),
                patch.object(commands, "WallRunner") as runner,
            ):
                runner.return_value.run.side_effect = asked_mid_run
                result = commands.walls(WallOptions())
            assert result.message == "已停止，升級了 3 面城牆"
            assert not flag.exists()

    def test_a_headless_run_hands_the_flag_to_the_runner(self) -> None:
        """The interface was there all along; only the window ever passed it.

        Which is the whole bug: `should_stop` defaults to never stopping, so a
        run started from a terminal read as one that simply could not be stopped.
        """
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop"
            with (
                patch.object(commands, "STOP_FLAG", flag),
                patch.object(commands, "_controller"),
                patch.object(commands, "_planner", return_value=None),
                patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
                patch.object(commands, "FrameTicker"),
                patch.object(commands, "AttackRunner") as runner,
            ):
                commands.attack(AttackOptions(rounds=1))
            assert runner.call_args.kwargs["should_stop"] is commands.stop_requested


class RestartEveryTests(unittest.TestCase):
    """Restarting the emulator on a schedule, because MuMu drops frames.

    The mechanism is a few lines; what is worth testing is the two decisions
    underneath them — what gets counted, and what the loop waits for before it
    calls the emulator ready.
    """

    @staticmethod
    def _fought() -> MagicMock:
        return MagicMock(stock_full=False, attacked=MagicMock())

    @staticmethod
    def _idle() -> MagicMock:
        return MagicMock(stock_full=False, attacked=None)

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
            patch.object(commands, "read_stock", side_effect=[None, None, MagicMock(gold=1)]),
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
            "out",
            commands.RESTART_ZOOM_PINCHES,
            commands.COC_PACKAGE,
            adb.display_for.return_value,
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
            patch.object(commands, "read_stock", return_value=MagicMock(gold=1)),
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
            patch.object(commands, "read_stock", return_value=None),
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


class OnlineTests(unittest.TestCase):
    """Holding the session open, which is the one loop here with no natural end.

    Clash of Clans will not let anyone raid a village whose owner is online, and
    what keeps a session alive is input rather than a connection — so what is
    worth testing is that something really gets sent, that it is harmless, and
    that the only way out is the flag every other loop here answers.
    """

    def _run(self, stop_after: int, **kwargs: object) -> tuple[MagicMock, MagicMock]:
        """Idle until the given number of nudges, then ask it to stand down."""
        adb = MagicMock()

        def nudged(*_: object, **__: object) -> None:
            if adb.swipe.call_count >= stop_after:
                self.flag.write_text("", encoding="utf-8")

        adb.swipe.side_effect = nudged
        with (
            patch.object(commands, "STOP_FLAG", self.flag),
            patch.object(commands, "_controller", return_value=adb),
            patch.object(commands.ConfigStore, "load", return_value=AppConfig()),
            patch.object(commands, "_rest", return_value=False) as rest,
        ):
            self.report = commands.online(**kwargs)
        return adb, rest

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.flag = Path(self.folder.name) / "stop"

    def test_it_idles_until_the_flag_says_otherwise(self) -> None:
        """There is no round count and no storage ceiling to end this one, so a
        run that could not be stopped would have to be killed — and killing it
        leaves the game somewhere the next run cannot start from.
        """
        adb, _ = self._run(stop_after=3)
        assert adb.swipe.call_count == 3
        assert self.report.nudges == 3
        # Cleared at both ends like every other loop that reads it, or the next
        # run stands down before it has done anything.
        assert not self.flag.exists()

    def test_the_nudge_reverses_so_the_camera_does_not_walk(self) -> None:
        """A drag pans the village. Several hours of them in one direction would
        walk the view off the map, and every coordinate with it.
        """
        adb, _ = self._run(stop_after=2)
        first, second = (call.args[:2] for call in adb.swipe.call_args_list)
        assert first == (second[1], second[0])

    def test_the_flag_overrides_the_configured_interval(self) -> None:
        """Same shape as the loot thresholds: the file is the default and the
        flag is for one run.
        """
        _, rest = self._run(stop_after=1, seconds=5.0)
        rest.assert_called_once_with(5.0)

    def test_the_interval_comes_from_the_config_file_when_no_flag_is_given(self) -> None:
        _, rest = self._run(stop_after=1)
        rest.assert_called_once_with(AppConfig().keepalive_seconds)


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
        mumu.enumerate_instances.side_effect = [[up], [up], [], [down]]
        mumu.ensure_coc.return_value = down
        with (
            patch.object(commands, "MuMuAdapter", return_value=mumu),
            patch.object(commands, "SHUTDOWN_GAP", 0),
        ):
            commands.launch("emulator")
        assert mumu.enumerate_instances.call_count == 4
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

    def _backs(self, run: shared.GameRunner, reads: list[VillageStock | None]) -> int:
        """Walk `_home` over these `read_stock` answers; how many times it pressed back."""
        with (
            patch.object(shared.time, "sleep"),
            patch.object(run, "_frame", return_value=b""),
            patch.object(shared, "idle_disconnected", return_value=False),
            patch.object(shared, "game_dialog", return_value=None),
            patch.object(shared, "read_stock", side_effect=reads),
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
            patch.object(shared, "game_dialog", return_value=None),
            patch.object(shared, "read_stock", side_effect=[None, None, held]),
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

    def test_a_stopped_run_starts_no_further_batch(self) -> None:
        """Between batches is the only safe place: a batch is a menu, a
        confirmation and a storage read, and leaving mid-way strands a dialog
        over the village. Whatever it already bought stays bought, because a
        wall upgrades the moment it is paid for and there is nothing to undo.
        """
        runner = self._runner(at=(500, 300), should_stop=lambda: True)
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
            patch.object(walls, "read_stock", return_value=VillageStock(gold=0, elixir=0, dark=0)),
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
            patch.object(walls, "read_stock", side_effect=[None, held, held, held]),
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


if __name__ == "__main__":
    unittest.main()


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
