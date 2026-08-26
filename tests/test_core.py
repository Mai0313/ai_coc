import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from collections.abc import Callable

from PIL import Image, ImageDraw
import pytest
from pydantic import ValidationError

from ai_coc import plans
from ai_coc.ui import walls, attack
from ai_coc.models import (
    ProbeRay,
    WallMenu,
    AppConfig,
    LootOffer,
    ScoutView,
    WallBatch,
    AttackPlan,
    AdbEndpoint,
    ScreenPoint,
    StockLimits,
    WallUpgrade,
    VillageStock,
    AttackTimings,
    DisplayTarget,
    LootOverrides,
    WallCandidate,
    BoundarySurvey,
    GeminiSettings,
    LootThresholds,
)
from ai_coc.prompts import PROMPTS, PROMPT_DIR, render
from ai_coc.ui.walls import WallRunner
from ai_coc.ui.attack import (
    PLAYFIELD,
    RAGE_PATH,
    RAGE_SPAN,
    DEPLOY_END,
    DROP_STRIDE,
    LINE_POINTS,
    DEPLOY_LINES,
    DEPLOY_START,
    PLAN_TIMEOUT,
    ABANDON_BUTTON,
    DROPS_PER_PASS,
    DEPLOY_ATTEMPTS,
    RESULT_ATTEMPTS,
    AttackRunner,
    spaced,
    push_out,
    deploy_line,
    drop_points,
    planned_line,
    single_spots,
    deploy_candidates,
)
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import AdbController, focused_display, physical_display
from ai_coc.parsers.clan import panel_top, donatable_cards, reinforce_button
from ai_coc.parsers.home import free_builders, collect_bubbles
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
    field_units,
    card_drained,
    freeze_cards,
    army_strength,
    counted_cards,
    attack_menu_open,
)
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
from ai_coc.parsers.building import wall_menu, game_dialog, upgrade_sheet, upgrade_buttons
from ai_coc.adapters.database import Database

FRAMES = Path(__file__).parent / "frames"

# A plan has to carry both ends of its line and each of the three lists the
# planner is asked to fill, so tests that do not care about any of them still
# have to supply them. They are required on purpose: a field with a default is
# optional in the JSON schema, and that is how a live run came back naming
# neither a hero nor a freeze point against a screen holding four hero cards.
_ANSWERED = {"rage_points": [], "freeze_points": [], "heroes": []}
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
        assert plan.timings is not None

    def test_the_flat_plan_draws_the_line_the_loop_used_to_hold_in_constants(self) -> None:
        """It has to reproduce the old fallback, or the default quietly changed."""
        assert planned_line(plans.flat()) == DEPLOY_LINES["top_left"]

    def test_a_plan_survives_being_written_out_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plan.json"
            path.write_text(plans.flat().model_dump_json(indent=2), encoding="utf-8")
            assert plans.load(path) == plans.flat()

    def test_the_ai_is_not_allowed_to_invent_hero_timings(self) -> None:
        """`timings` is on the schema, so the model can fill it; a still frame cannot know."""
        assert "timings" in AttackPlan.model_json_schema()["properties"]
        answered = AttackPlan(**_LINE, timings=AttackTimings(queen=30, warden=5))
        assert answered.model_copy(update={"timings": None}).timings is None

    def test_a_plans_own_timings_beat_the_ones_the_runner_was_built_with(self) -> None:
        """A written plan is the whole tactic, so its schedule is the one that fires."""
        plan = AttackPlan(**_LINE, timings=AttackTimings(queen=7))
        assert (plan.timings or AttackTimings()).seconds("queen") == 7

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
                timings=AttackTimings(queen=9),
                gemini_model="gemini-not-the-default",
            )
            store.save(saved)
            assert store.load() == saved

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

    def test_any_one_full_storage_stops_the_farming(self) -> None:
        """Unlike the loot thresholds: loot past a full storage is thrown away."""
        stock = VillageStock(gold=15000000, elixir=400000, dark=1000)
        assert StockLimits(stop_gold=15000000, stop_elixir=15000000).reached(stock) == ["金幣"]

    def test_a_stop_limit_left_at_zero_watches_nothing(self) -> None:
        assert not StockLimits().reached(VillageStock(gold=99999999, elixir=1, dark=1))

    def test_deploy_line_runs_the_whole_flank(self) -> None:
        points = deploy_line(8)
        assert len(points) == 8
        assert points[0] == DEPLOY_START
        assert points[-1] == DEPLOY_END

    def test_ability_timing_is_per_hero_not_per_slot(self) -> None:
        """An upgrading hero has no card at all, so every slot after it shifts."""
        timings = AttackTimings(queen=1, warden=30)
        assert timings.seconds("queen") == 1
        assert timings.seconds("warden") == 30
        assert timings.seconds("unknown") == timings.unknown

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
        assert len(set(first)) == DROPS_PER_PASS
        assert first[0] != second[0]

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

    def _verdict(self, opening: LootOffer, readings: list[ScoutView | None]) -> bool:
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
            return runner._wait_out_battle(opening)

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

    def test_the_freeze_no_longer_queues_behind_the_slowest_hero(self) -> None:
        """Cast after the last ability it sat out a champion's 45 seconds first."""
        played: list[str] = []
        timings = AttackTimings()
        # The heroes land twenty seconds into the attack; their abilities run
        # from there, the freeze from the opening.
        opened, landed = 0.0, 20.0
        moves = [
            (landed + timings.seconds("champion"), "champion", lambda: played.append("champion")),
            (landed + timings.seconds("queen"), "queen", lambda: played.append("queen")),
            (opened + timings.freeze, "freeze", lambda: played.append("freeze")),
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
        """Troops, siege machine, heroes and spells, told apart by the wider gaps."""
        groups = card_groups((FRAMES / "cards_full.png").read_bytes())
        assert [len(group) for group in groups] == [4, 1, 4, 2]
        assert groups[0] == [171, 293, 413, 534]

    def test_the_empty_slot_the_row_ends_with_is_not_a_card(self) -> None:
        """It has no level badge, and it arrived downstream as one more hero to drop."""
        groups = card_groups((FRAMES / "cards_with_empty_slot.png").read_bytes())
        assert [len(group) for group in groups] == [3, 1, 4, 2]
        assert 1416 not in [slot for group in groups for slot in group]

    def test_spells_are_told_apart_from_heroes_by_their_count(self) -> None:
        """Spells carry an xN in the corner; heroes and the siege machine do not."""
        frame = (FRAMES / "cards_full.png").read_bytes()
        assert counted_cards(frame, [678, 815, 925, 1046, 1167, 1302, 1423]) == [1302, 1423]

    def test_a_spell_card_reports_how_many_it_holds(self) -> None:
        frame = (FRAMES / "cards_full.png").read_bytes()
        assert card_count(frame, 1302) == 5
        assert card_count(frame, 1423) == 1

    def test_a_count_over_pale_artwork_reads_as_unknown(self) -> None:
        """The giant's illustration merges into its count, and a guess is worse."""
        assert card_count((FRAMES / "cards_full.png").read_bytes(), 171) is None

    def test_freeze_is_told_apart_from_rage_by_its_cyan(self) -> None:
        assert freeze_cards((FRAMES / "cards_full.png").read_bytes(), [1302, 1423]) == [1423]

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

    def test_the_upgrade_sheet_is_told_from_grass_by_the_storage_bars(self) -> None:
        """A building confirms on a full-screen sheet whose 確認 is green — and so
        is a village, all over. What separates them is that the sheet covers the
        storage bars and a village does not.
        """
        assert upgrade_sheet((FRAMES / "upgrade_sheet.png").read_bytes()) == (1121, 783)
        assert upgrade_sheet((FRAMES / "home_markers.png").read_bytes()) is None
        assert upgrade_sheet((FRAMES / "menu_with_gem_plate.png").read_bytes()) is None


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
