import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pytest

from ai_coc import plans
from ai_coc.ui import attack
from ai_coc.models import (
    ProbeRay,
    LootOffer,
    ScoutView,
    AttackPlan,
    AdbEndpoint,
    ScreenPoint,
    StockLimits,
    VillageStock,
    AttackTimings,
    DisplayTarget,
    BoundarySurvey,
    LootThresholds,
)
from ai_coc.prompts import PROMPTS, PROMPT_DIR, render
from ai_coc.ui.attack import (
    PLAYFIELD,
    RAGE_PATH,
    DEPLOY_END,
    LINE_POINTS,
    DEPLOY_LINES,
    DEPLOY_START,
    ABANDON_BUTTON,
    DROPS_PER_PASS,
    DEPLOY_ATTEMPTS,
    AttackRunner,
    push_out,
    deploy_line,
    drop_points,
    planned_line,
    deploy_candidates,
)
from ai_coc.adapters.adb import AdbController, focused_display, physical_display
from ai_coc.parsers.scout import (
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
from ai_coc.parsers.village import parse_village
from ai_coc.adapters.secrets import dotenv_value
from ai_coc.parsers.boundary import (
    DEPLOY_BOUND,
    VILLAGE_GRID,
    fitted_line,
    boundary_line,
    boundary_reach,
)
from ai_coc.adapters.database import Database

FRAMES = Path(__file__).parent / "frames"

# A plan has to carry both ends of its line, so tests that do not care about
# the line still have to supply one.
_LINE = {
    "deploy_start": ScreenPoint(x_pct=37.5, y_pct=12.2),
    "deploy_end": ScreenPoint(x_pct=14.4, y_pct=42.2),
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
        """Bright paving behind the panel used to add a digit to the end of every row."""
        view = read_scout((FRAMES / "scout_bright_backdrop.png").read_bytes())
        assert view is not None
        assert (view.loot.gold, view.loot.elixir, view.loot.dark) == (180728, 24752, 505)

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

    def test_the_boundary_is_read_over_two_village_themes(self) -> None:
        rays = [angle * 30 for angle in range(12)]
        for name in ("battle_boundary_grass.png", "battle_boundary_ice.png"):
            points = boundary_line((FRAMES / name).read_bytes(), rays)
            radii = [math.hypot(x - 800, y - 400) for x, y in points]
            assert len(points) >= 6, name
            # One closed curve, so nothing should sit near the middle of it.
            assert min(radii) > 200, (name, sorted(radii))


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

    def test_a_planned_line_along_the_village_edge_is_used(self) -> None:
        """The percentages of the top-left flank, which is a line the loop can push out."""
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=37.5, y_pct=12.2),
            deploy_end=ScreenPoint(x_pct=14.4, y_pct=42.2),
        )
        assert planned_line(plan) == DEPLOY_LINES["top_left"]

    def test_a_planned_line_across_the_village_falls_back_to_a_flank(self) -> None:
        """Its midpoint sits on the middle, where push_out has no direction to move it."""
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=25, y_pct=30),
            deploy_end=ScreenPoint(x_pct=75, y_pct=70),
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
        """Run the battle wait against canned panel readings, with the clock removed."""
        runner = self._runner()
        with (
            patch.object(AttackRunner, "_frame", return_value=b""),
            patch.object(AttackRunner, "_tap"),
            patch.object(attack, "read_scout", side_effect=readings),
            # The result screen is left through its own poll now, and these
            # canned frames are not images.
            patch.object(attack, "battle_over", return_value=False),
            patch.object(attack.time, "sleep"),
        ):
            # Whatever the abilities saw counts too, which is the whole point.
            runner._battle_view("ability")
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

    def test_the_freeze_no_longer_queues_behind_the_slowest_hero(self) -> None:
        """Cast after the last ability it sat out a champion's 45 seconds first."""
        played: list[str] = []
        timings = AttackTimings()
        moves = [
            (timings.seconds("champion"), "champion", lambda: played.append("champion")),
            (timings.seconds("queen"), "queen", lambda: played.append("queen")),
            (timings.freeze, "freeze", lambda: played.append("freeze")),
        ]
        on = ScoutView(loot=LootOffer(gold=1, elixir=1, dark=1), can_skip=False)
        with (
            patch.object(AttackRunner, "_battle_view", return_value=on),
            patch.object(attack.time, "sleep"),
        ):
            self._runner()._run_schedule(moves)
        assert played == ["queen", "freeze", "champion"]

    def test_a_refused_flank_leaves_the_other_three_to_try(self) -> None:
        """The plan's own side goes first behind its line, then the flanks it did not pick."""
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=25, y_pct=30),
            deploy_end=ScreenPoint(x_pct=75, y_pct=70),
            deploy_from="bottom_right",
        )
        # That line runs across the village, so only the named flanks are left.
        assert planned_line(plan) is None
        candidates = deploy_candidates(plan)
        assert candidates[0] == DEPLOY_LINES["bottom_right"]
        assert sorted(candidates) == sorted(DEPLOY_LINES.values())

    def test_both_ends_of_the_line_are_required_of_the_planner(self) -> None:
        """Gemini answered three runs running with a start and no end; half a line is none."""
        required = set(AttackPlan.model_json_schema()["required"])
        assert {"deploy_start", "deploy_end"} <= required

    def test_a_usable_planned_line_is_tried_before_any_flank(self) -> None:
        plan = AttackPlan(
            deploy_start=ScreenPoint(x_pct=62.5, y_pct=12.2),
            deploy_end=ScreenPoint(x_pct=85.6, y_pct=42.2),
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


if __name__ == "__main__":
    unittest.main()
