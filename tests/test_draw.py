"""`ai_coc draw`: the pointing grid, where a plan lands on a frame, and the picture of both.

The frames are the two the default tactics are discussed on, a builder base
countdown whose row still has its machine, and one opponent on a scenery with
red of its own.
"""

from __future__ import annotations

import io
import sys
import json
import math
from typing import TYPE_CHECKING, get_args
import logging
from pathlib import Path
import itertools
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageChops
import pytest

from ai_coc import cli, plans, drawing, commands, logging_setup
from ai_coc.ui import attack
from ai_coc.models import (
    CardRow,
    BaseGrid,
    FrameLog,
    NightPlan,
    AttackPlan,
    AttackStep,
    AdbEndpoint,
    DrawOptions,
    DrawOutcome,
    PlannedDrop,
    ScreenPoint,
    DisplayTarget,
    LootThresholds,
)
from ai_coc.drawing import CARD_ROW_TOP, legend, row_line, draw_plan
from ai_coc.ui.attack import CARD_ROW_Y, AttackRunner, planned_drops
from ai_coc.adapters.adb import AdbController
from ai_coc.parsers.boundary import boundary_diamond

if TYPE_CHECKING:
    from collections.abc import Callable

FRAMES = Path(__file__).parent / "frames"
DAY = (FRAMES / "battle_flat_reference.png").read_bytes()
NIGHT = (FRAMES / "night_flat_reference.png").read_bytes()
# The camera's shift when `battle_flat_reference.png` was taken, off that
# battle's own log (2026-11-21 00:28): `now (0, 102) off where the battle opened`.
DAY_CAMERA = (0, 102)


def _near(point: tuple[int, int], line: list[tuple[int, int]]) -> float:
    """How far a point is from a polyline."""
    best = math.inf
    for (ax, ay), (bx, by) in itertools.pairwise(line):
        dx, dy = bx - ax, by - ay
        share = ((point[0] - ax) * dx + (point[1] - ay) * dy) / max(dx * dx + dy * dy, 1)
        share = min(max(share, 0.0), 1.0)
        best = min(best, math.dist(point, (ax + dx * share, ay + dy * share)))
    return best


class TestBoundaryDiamond:
    def test_the_home_reference_fits_the_diamond_its_grid_was_approved_on(self) -> None:
        """The corners the user looked at on 2026-11-21, found by hand then: within two pixels."""
        grid = boundary_diamond(DAY)
        assert grid is not None
        approved = {
            "top": (761, 93),
            "right": (1322, 514),
            "bottom": (877, 848),
            "left": (316, 427),
        }
        for corner, (x, y) in approved.items():
            found = getattr(grid, corner)
            assert abs(found[0] - x) <= 2, corner
            assert abs(found[1] - y) <= 2, corner
        assert grid.empty == []

    def test_a_base_that_is_not_square_says_which_corner_is_bare_ground(self) -> None:
        """The builder base reference: its line stops 146 px short of the diamond's right corner.

        Its upper left edge is the shortest outermost edge measured, 51 px, so
        this is also what holds `DIAMOND_RUN` from above.
        """
        grid = boundary_diamond(NIGHT)
        assert grid == BaseGrid(
            top=(896, 184), right=(1281, 472), bottom=(831, 810), left=(447, 521), empty=["right"]
        )

    def test_red_scenery_beside_the_village_is_not_an_edge(self) -> None:
        """A red pier ran 47 to 49 px along the map's own direction and put the left corner at x 3."""
        grid = boundary_diamond((FRAMES / "battle_red_scenery.png").read_bytes())
        assert grid is not None
        assert (grid.left, grid.right) == ((189, 438), (1409, 438))

    def test_a_frame_with_no_outline_draws_no_grid(self) -> None:
        """A village, and a battle far enough along that the line has gone to pieces."""
        for name in ("hud_day", "hud_night", "battle_in_progress", "night_battle", "attack_menu"):
            assert boundary_diamond((FRAMES / f"{name}.png").read_bytes()) is None, name

    def test_the_grid_is_cut_along_the_diamond_from_its_top_corner(self) -> None:
        grid = BaseGrid(top=(800, 100), right=(1200, 400), bottom=(800, 700), left=(400, 400))
        assert grid.at(0, 0) == grid.top
        assert grid.at(1, 0) == grid.right
        assert grid.at(0, 1) == grid.left
        assert grid.at(1, 1) == grid.bottom
        assert grid.at(0.5, 0.5) == (800, 400)


def _runner(world: str, plan: AttackPlan | NightPlan) -> AttackRunner:
    return AttackRunner(
        adb=AdbController(endpoint=AdbEndpoint(port=16384)),
        display=DisplayTarget(logical_id="1", physical_id="2"),
        world=world,
        thresholds=LootThresholds(),
        plan=plan,
    )


def _field_taps(
    frame: bytes, play: Callable[[list[list[tuple[int, int]]]], None]
) -> list[list[tuple[int, int]]]:
    """Every burst the runner sends while `play` runs on a screen that never changes, card taps left out."""
    bursts: list[list[tuple[int, int]]] = []

    def burst(_self: object, points: list[tuple[int, int]], *_rest: object, **_kw: object) -> None:
        if field := [point for point in points if point[1] != CARD_ROW_Y]:
            bursts.append(field)

    def tap(_self: object, x: int, y: int, *_rest: object) -> None:
        burst(_self, [(x, y)])

    with (
        patch.object(AttackRunner, "_frame", return_value=frame),
        patch.object(AttackRunner, "_settle_zoom", lambda _self, shot: shot),
        patch.object(AttackRunner, "_settle_camera", lambda _self, shot: shot),
        patch.object(AttackRunner, "_tap", lambda _self, point: None),
        patch.object(AdbController, "tap_many", burst),
        patch.object(AdbController, "tap", tap),
        patch.object(AdbController, "swipe", lambda *_args: None),
        patch.object(attack.time, "sleep"),
    ):
        play(bursts)
    return bursts


class TestPlannedDrops:
    """The picture's numbers against what the loop itself taps on the same frame."""

    def test_the_flat_plan_lands_where_the_recorded_battle_put_it(self) -> None:
        """The battle the home reference frame came from, off its own log of taps."""
        tactic = planned_drops(plans.flat(), DAY, DAY_CAMERA)
        drops = {(step.act, step.seconds): step for step in tactic.steps if step.act != "ability"}
        assert tactic.fitted
        assert drops["troops", 0].lines[0][0] == (533, 365)
        assert drops["troops", 0].lines[0][-1] == (366, 487)
        assert drops["hero", 0].at == [(340, 402)]
        assert drops["siege", 0].at == [(176, 506)]
        assert drops["rage", 7].at == [(550, 429), (390, 519)]
        assert drops["freeze", 15].at == [(800, 522), (640, 402), (960, 402)]
        # Four bottles over two cards of two: the second card starts where the
        # first ran out, so four of the plan's six points take one each (#693).
        assert drops["rage", 16].at == [(710, 519), (550, 609), (870, 429), (870, 609)]
        assert drops["rage", 16].unused == [(1030, 519), (710, 339)]

    def _played_as_drawn(self, plan: AttackPlan) -> list[PlannedDrop]:
        """Play `plan` through the runner on the home reference and hold every step's taps to the picture's."""
        runner = _runner("day", plan)
        runner._panned = DAY_CAMERA
        played: list[tuple[AttackStep, int, list[list[tuple[int, int]]]]] = []
        act = AttackRunner._act

        def play(bursts: list[list[tuple[int, int]]]) -> None:
            def step(self: AttackRunner, one: AttackStep, *args: object, **kw: object) -> None:
                before = len(bursts)
                act(self, one, *args, **kw)
                played.append((one, kw["remaining"], bursts[before:]))

            def settled(self: AttackRunner, *_args: object) -> None:
                self._sending = {}

            with (
                patch.object(AttackRunner, "_act", step),
                patch.object(AttackRunner, "_settle_drops", settled),
                patch.object(AttackRunner, "_battle_ended", return_value=False),
                # One attempt per cast: the cards on a still frame never empty.
                patch.object(attack, "in_battle", return_value=False),
            ):
                runner._deploy(DAY)

        _field_taps(DAY, play)
        tactic = planned_drops(plan, DAY, DAY_CAMERA)
        assert [one.act for one, _, _ in played] == [drop.act for drop in tactic.steps]
        compared = 0
        for (one, remaining, bursts), drop in zip(played, tactic.steps, strict=True):
            taps = [point for burst in bursts for point in burst]
            if one.act in ("hero", "siege"):
                assert set(taps) == set(drop.at), one
            elif one.act == "troops":
                assert taps, one
                assert max(_near(point, drop.lines[0]) for point in taps) <= 2, one
            elif one.act in ("rage", "freeze"):
                # The last cast of a kind taps each card once more than it holds.
                bottles = [
                    point
                    for burst in bursts
                    for point in (burst[:-1] if remaining == 1 else burst)
                ]
                assert bottles == drop.at, one
            else:
                continue
            compared += 1
        assert compared == sum(1 for drop in tactic.steps if drop.act != "ability")
        return tactic.steps

    def test_every_step_of_the_flat_plan_is_what_the_runner_taps(self) -> None:
        """`planned_drops` repeats how the runner strings a tactic together, so the runner is played against it."""
        assert len(self._played_as_drawn(plans.flat())) == 9

    def test_a_plan_that_leans_on_the_runners_corrections_is_drawn_with_them(self) -> None:
        """A hero and a machine aimed inside the troops' line, and a rage cast wider than its first card.

        The flat plan needs neither: its one-off drops sit on the line, and its
        early rage is two bottles off a card of four.
        """

        def step(act: str, *at: tuple[float, float], seconds: int = 0) -> AttackStep:
            points = [ScreenPoint(x_pct=x, y_pct=y) for x, y in at]
            return AttackStep(act=act, who="unknown", at=points, seconds=seconds)

        inside, further = (28.75, 36.4), (25.0, 40.9)
        plan = AttackPlan(
            steps=[
                step("hero", inside),
                step("troops", (28.13, 24.44), (14.38, 42.22)),
                step("siege", further),
                step("wait", seconds=3),
                step("rage", (30, 30), (45, 30), (60, 30), (30, 50), (45, 50)),
                step("wait", seconds=3),
                step("rage", (60, 50)),
            ]
        )
        hero, _troops, siege, early, late = self._played_as_drawn(plan)
        camera = DAY_CAMERA[1]
        assert hero.at != [(round(inside[0] * 16), round(inside[1] * 9) + camera)]
        assert siege.at != [(round(further[0] * 16), round(further[1] * 9) + camera)]
        # Four off the first rage card and the fifth off the second, whose last bottle is the late one.
        assert (len(early.at), len(late.at)) == (5, 1)

    @pytest.mark.parametrize("name", ["night_flat_reference", "night_countdown"])
    def test_the_builder_base_plan_is_what_the_runner_taps(self, name: str) -> None:
        """One row with no machine on it and one with: the machine's spot, then the three lanes."""
        frame = (FRAMES / f"{name}.png").read_bytes()
        runner = _runner("night", plans.night_flat())
        lanes: list[list[list[tuple[int, int]]]] = []

        def play(_bursts: list[list[tuple[int, int]]]) -> None:
            def spread(self: AttackRunner, _troops: object, anchors: object, pushed: int) -> None:
                lanes.append(self._lanes(anchors, pushed))  # type: ignore[arg-type]

            with patch.object(AttackRunner, "_spread_night", spread):
                runner._deploy_night(frame)

        bursts = _field_taps(frame, play)
        machine, troops = planned_drops(plans.night_flat(), frame).steps
        assert lanes == [troops.lines]
        assert troops.seconds == plans.night_flat().troops_after
        assert machine.cards == len(bursts[:1])
        if bursts:
            assert bursts[0] == machine.at

    def test_a_plan_that_aims_its_machine_sends_it_there(self) -> None:
        plan = plans.night_flat().model_copy(
            update={"hero_points": [ScreenPoint(x_pct=20, y_pct=40)]}
        )
        frame = (FRAMES / "night_countdown.png").read_bytes()
        assert planned_drops(plan, frame).steps[0].at == [(320, 360)]

    def test_the_drag_the_loop_would_make_is_said_only_before_it_has_made_it(self) -> None:
        """`village_box` reads a drag on the dragged frame too (#65), which would be a second one."""
        assert planned_drops(plans.flat(), DAY).drag == 50
        assert planned_drops(plans.flat(), DAY, DAY_CAMERA).drag is None

    def test_the_army_is_counted_off_the_frames_own_card_row(self) -> None:
        """Two troop cards, a siege machine and four heroes, two rage cards of four and two, one freeze of three."""
        tactic = planned_drops(plans.flat(), DAY, DAY_CAMERA)
        assert tactic.row == CardRow(troops=[12, 7], singles=5, rages=[4, 2], freezes=[3])
        sent = {(step.act, step.who): step.cards for step in tactic.steps if step.act != "ability"}
        # The plan names its first hero and nothing on the row says who a card is:
        # the named step takes the first hero card and `unknown` the other three.
        assert sent["hero", "queen"] == 1
        assert sent["hero", "unknown"] == 3
        assert sent["siege", "unknown"] == 1
        assert sent["troops", "unknown"] == 2

    def test_a_frame_with_no_card_row_draws_no_bottle_as_cast(self) -> None:
        """Nothing says how many bottles there are, so every point of the plan stays unplaced."""
        blank = io.BytesIO()
        Image.new("RGB", (1600, 900), (40, 90, 40)).save(blank, format="PNG")
        tactic = planned_drops(plans.flat(), blank.getvalue())
        spells = [step for step in tactic.steps if step.act in ("rage", "freeze")]
        assert tactic.row is None
        assert [len(step.unused) for step in spells] == [2, 3, 6]
        assert all(step.at == [] for step in spells)
        assert all(step.cards is None for step in tactic.steps)
        assert not tactic.fitted
        # And the picture's notes tell that army from one that was counted.
        counted = planned_drops(plans.flat(), DAY, DAY_CAMERA)
        assert row_line(tactic.row) != row_line(counted.row)
        assert legend(tactic.steps)[2:] != legend(counted.steps)[2:]


class TestPicture:
    def _picture(self, *, against: bool = False) -> Image.Image:
        tactic = planned_drops(plans.flat(), DAY, DAY_CAMERA)
        png = draw_plan(DAY, boundary_diamond(DAY), tactic.steps, tactic.steps if against else [])
        return Image.open(io.BytesIO(png)).convert("RGB")

    def test_nothing_is_drawn_over_the_card_row(self) -> None:
        """The grid's lower corner lies under it, and the row is what the picture is read against."""
        picture = self._picture()
        frame = Image.open(io.BytesIO(DAY)).convert("RGB")
        row = (0, CARD_ROW_TOP, 1600, 900)
        assert ImageChops.difference(picture.crop(row), frame.crop(row)).getbbox() is None
        field = (0, 0, 1600, CARD_ROW_TOP)
        assert ImageChops.difference(picture.crop(field), frame.crop(field)).getbbox() is not None

    def test_the_picture_is_the_frame_with_nothing_added_to_it(self) -> None:
        """No notes under it: what a picture assumes is what the discussion is about (the user, 2026-11-21)."""
        assert self._picture().size == (1600, 900)

    def test_a_steps_label_carries_its_second_once_for_steps_sharing_a_place(self) -> None:
        """The first hero and the rest land on one spot at the opening; a comparison's marks have no clock."""
        together = drawing._worded([("#1", 0), ("#3", 0)])
        assert together.count("0") == 1
        assert together.index("#1") < together.index("#3")
        apart = drawing._worded([("#5", 7), ("#7", 16)])
        assert apart.index("7") < apart.index("#7") < apart.index("16")
        assert drawing._worded([(drawing.GHOST, None)]) == drawing.GHOST

    def test_every_placed_step_is_labelled_with_the_plans_second(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written: list[str] = []
        tag = drawing._tag

        def spy(draw: object, spots: object, text: str, *rest: object, **kw: object) -> object:
            written.append(text)
            return tag(draw, spots, text, *rest, **kw)

        monkeypatch.setattr(drawing, "_tag", spy)
        self._picture()
        steps = [text for text in written if text.startswith("#")]
        # One number a place a step lands on: two at the heroes' spot, the troops' line, the siege
        # machine, then two, three and four places for the spells.
        assert sum(text.count("#") for text in steps) == 2 + 1 + 1 + 2 + 3 + 4
        for seconds in ("0", "7", "15", "16"):
            assert any(text.split(" ")[-1].startswith(seconds) for text in steps), seconds

    def test_a_label_stands_clear_of_what_is_already_written(self) -> None:
        """A cell's number under a step's label would hide one of them."""
        draw = ImageDraw.Draw(Image.new("RGBA", (1600, 900)))
        font = drawing._font(20)
        taken: list[tuple[float, float, float, float]] = []
        first = drawing._tag(
            draw, [(800, 400), (800, 450)], "#6 15秒", (255, 255, 255), font, taken=taken
        )
        second = drawing._tag(
            draw, [(800, 400), (800, 450)], "5", (255, 255, 255), font, taken=taken
        )
        assert (first, second) == ((800, 400), (800, 450))
        # With nowhere free it is written anyway, at its own place.
        third = drawing._tag(
            draw, [(800, 400), (800, 450)], "x", (255, 255, 255), font, taken=taken
        )
        assert third == (800, 400)

    def test_a_plan_held_against_itself_draws_nothing_twice(self) -> None:
        """Only what the other plan does differently is marked."""
        assert (
            ImageChops.difference(self._picture(), self._picture(against=True)).getbbox() is None
        )

    def test_the_legend_numbers_what_is_placed_and_groups_it_by_the_plans_clock(self) -> None:
        lines = legend(planned_drops(plans.flat(), DAY, DAY_CAMERA).steps)
        assert [line.split(" ")[0] for line in lines] == ["0", "5", "7", "15", "16"]
        # Four placed steps at the opening, and the ability between them unnumbered.
        opening = [part.split(" ")[0] for part in lines[0].split(": ")[1].split(" → ")]
        assert [part for part in opening if part.startswith("#")] == ["#1", "#2", "#3", "#4"]
        assert len(opening) == 5
        assert not opening[1].startswith("#")

    def test_a_step_the_row_has_no_card_for_says_so(self) -> None:
        for act in ("machine", "siege", "hero"):
            step = PlannedDrop(act=act, seconds=0, at=[(398, 257)], cards=0)
            assert legend([step])[0] != legend([step.model_copy(update={"cards": 1})])[0], act


def _attack_run(tmp_path: Path, name: str, cameras: list[tuple[int, int]]) -> Path:
    """An attack run's directory as the runner leaves it: one battle a camera, each keeping its opening frame."""
    run = tmp_path / "logs" / name
    run.mkdir(parents=True)
    handler = logging.FileHandler(run / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(logging_setup.LOG_FORMAT, logging_setup.TIME_FORMAT))
    log = logging.getLogger("ai_coc")
    level = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        runner = _runner("day", plans.flat())
        runner.adb.frames = FrameLog(directory=run / "frames")
        for camera in cameras:
            runner._panned = camera
            runner._keep_opening(DAY)
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
        handler.close()
    return run


class TestDrawCommand:
    def _draw(self, tmp_path: Path, **asked: object) -> commands.DrawReport:
        return commands.draw(DrawOptions(**asked), FrameLog(directory=tmp_path / "own" / "frames"))

    def test_a_frame_is_drawn_into_the_runs_own_frames(self, tmp_path: Path) -> None:
        frame = FRAMES / "battle_flat_reference.png"
        report = self._draw(tmp_path, png=frame, camera=DAY_CAMERA)
        assert report.outcome == "drawn"
        assert report.image == tmp_path / "own" / "frames" / "drawn.png"
        assert Image.open(report.image).size[0] == 1600
        assert (report.plan, report.world, report.camera) == ("flat", "day", DAY_CAMERA)
        assert report.tactic == planned_drops(plans.flat(), DAY, DAY_CAMERA)
        assert report.grid == boundary_diamond(DAY)
        text = commands.draw_text(report)
        assert text.splitlines()[0] == str(report.image)
        # The middle of the diamond at (819, 471), 102 px higher on a centred village, as a plan writes a point.
        assert "5 (51.2, 41.0)" in text

    def test_a_name_is_read_for_the_village_asked_and_a_path_says_its_own(
        self, tmp_path: Path
    ) -> None:
        frame = FRAMES / "night_flat_reference.png"
        named = self._draw(tmp_path, png=frame, world="night")
        assert named.world == "night"
        assert [step.act for step in named.tactic.steps] == ["machine", "troops"]
        written = tmp_path / "mine.json"
        written.write_text(plans.night_flat().model_dump_json(), encoding="utf-8")
        assert self._draw(tmp_path, png=frame, plan=str(written)).world == "night"

    def test_the_grid_or_the_plan_can_be_drawn_alone(self, tmp_path: Path) -> None:
        frame = FRAMES / "battle_flat_reference.png"
        grid = self._draw(tmp_path, png=frame, only="grid")
        assert (grid.tactic, grid.plan, grid.world) == (None, None, None)
        assert grid.grid is not None
        plan = self._draw(tmp_path, png=frame, only="plan")
        assert plan.grid is None
        assert plan.tactic is not None
        # Leaving the grid out is not the same as failing to read it.
        assert "讀不到紅線" not in commands.draw_text(plan)
        unread = self._draw(tmp_path, png=FRAMES / "hud_day.png", only="grid")
        assert (unread.outcome, unread.grid) == ("drawn", None)
        assert "讀不到紅線" in commands.draw_text(unread)

    def test_an_attack_run_gives_the_frame_its_newest_battle_kept_and_the_cameras_shift(
        self, tmp_path: Path
    ) -> None:
        """Through the runner's own log line, so the two cannot come apart."""
        run = _attack_run(tmp_path, "2026-11-21-002800-attack-ref", [(0, 102), (0, -83)])
        report = self._draw(tmp_path, run=run)
        assert report.frame == run / "frames" / "opening.png"
        assert report.camera == (0, -83)

    def test_a_run_is_found_by_its_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The newest run carrying the label, and never the drawing run's own directory."""
        monkeypatch.setattr(commands, "LOG_DIR", tmp_path / "logs")
        _attack_run(tmp_path, "2026-11-20-090000-attack-ref", [(0, 102)])
        newest = _attack_run(tmp_path, "2026-11-21-090000-attack-ref", [(0, 0)])
        own = tmp_path / "logs" / "2026-11-21-100000-draw-ref" / "frames"
        own.mkdir(parents=True)
        report = commands.draw(DrawOptions(run=Path("ref")), FrameLog(directory=own))
        assert (report.frame, report.camera) == (newest / "frames" / "opening.png", (0, 0))
        # An earlier picture given the same label is newer than the battle and kept no such frame.
        (tmp_path / "logs" / "2026-11-21-095000-draw-ref" / "frames").mkdir(parents=True)
        again = commands.draw(DrawOptions(run=Path("ref")), FrameLog(directory=own))
        assert again.frame == report.frame
        assert (
            commands.draw(DrawOptions(run=Path("nobody")), FrameLog(directory=own)).image is None
        )

    def test_nothing_to_draw_on_is_an_outcome_of_its_own(self, tmp_path: Path) -> None:
        """A missing file, a run that played no battle, and one whose frames are gone."""
        assert self._draw(tmp_path, png=tmp_path / "gone.png").outcome == "no_frame"
        assert self._draw(tmp_path, run=_attack_run(tmp_path, "idle", [])).outcome == "no_frame"
        culled = _attack_run(tmp_path, "culled", [(0, 102)])
        (culled / "frames" / "opening.png").unlink()
        report = self._draw(tmp_path, run=culled)
        assert (report.outcome, report.image) == ("no_frame", None)
        assert commands.draw_text(report) == commands.DRAW_LINES["no_frame"]

    def test_the_grid_says_where_no_tap_can_land(self, tmp_path: Path) -> None:
        """After the camera's drag the lower corner and the middle of cell 9 lie under the card row."""
        report = self._draw(tmp_path, png=FRAMES / "battle_flat_reference.png", camera=DAY_CAMERA)
        places = commands._grid_places(report.grid)
        assert [name for name, at in places.items() if commands._off_limits(at)] == [
            "上角",
            "下角",
            "9",
        ]
        # Where four cells meet is a place of its own, since that is how a spot gets pointed at.
        assert places["5\u00b76\u00b78\u00b79"] == report.grid.at(2 / 3, 2 / 3)
        with_camera = commands.draw_text(report).splitlines()
        without = commands.draw_text(report.model_copy(update={"camera": (0, 0)})).splitlines()
        assert with_camera[-1] != without[-1]

    def test_every_outcome_has_a_line_of_its_own(self) -> None:
        assert set(commands.DRAW_LINES) == set(get_args(DrawOutcome))
        assert len(set(commands.DRAW_LINES.values())) == len(commands.DRAW_LINES)

    def test_the_other_plan_is_named_in_what_the_picture_says(self, tmp_path: Path) -> None:
        moved = json.loads(plans.flat().model_dump_json())
        next(step for step in moved["steps"] if step["act"] == "freeze")["at"][0]["x_pct"] = 30
        written = tmp_path / "moved.json"
        written.write_text(json.dumps(moved), encoding="utf-8")
        frame = FRAMES / "battle_flat_reference.png"
        report = self._draw(tmp_path, png=frame, plan=str(written), against="flat")
        assert report.against == "flat"
        plain = self._draw(tmp_path, png=frame, plan=str(written))
        assert len(commands.draw_text(report).splitlines()) == (
            len(commands.draw_text(plain).splitlines()) + 1
        )


class TestDrawCli:
    def _main(self, tmp_path: Path, *line: str) -> int:
        with (
            patch.object(sys, "argv", ["ai_coc", "draw", *line]),
            patch.object(cli.RunLog, "open", return_value=cli.RunLog(directory=tmp_path)),
            patch.object(cli, "configure_logging"),
        ):
            return cli.main()

    def test_it_draws_without_claiming_the_emulator(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`conftest` fails anything that reaches for an emulator, and a claim would write the state file."""
        frame = str(FRAMES / "night_flat_reference.png")
        assert self._main(tmp_path, frame, "--night", "--only", "plan") == 0
        assert capsys.readouterr().out.splitlines()[0] == str(tmp_path / "frames" / "drawn.png")
        assert not commands.STATE_PATH.exists()
        answer = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert (answer["outcome"], answer["world"], answer["only"]) == ("drawn", "night", "plan")

    def test_nothing_drawn_is_not_exit_zero(self, tmp_path: Path) -> None:
        assert self._main(tmp_path, str(tmp_path / "gone.png")) == 1

    def test_it_takes_a_frame_or_a_run_and_not_both(self, tmp_path: Path) -> None:
        for line in ([], ["shot.png", "--run", "ref"]):
            with pytest.raises(SystemExit):
                self._main(tmp_path, *line)

    def test_the_cameras_shift_reaches_the_command(self) -> None:
        asked = cli._parser().parse_args(["draw", "shot.png", "--camera", "0", "102"])
        assert tuple(asked.camera) == (0, 102)
        assert cli._parser().parse_args(["draw", "shot.png"]).plan == "flat"


class TestOpeningFrame:
    """The loop keeps the frame its first drop follows, with or without `--debug`."""

    def _flanked(self, frames: FrameLog | None, after: bytes = b"after the drag") -> AttackRunner:
        runner = _runner("day", plans.flat())
        runner.adb.frames = frames
        with (
            patch.object(AttackRunner, "_clear_flank", return_value=after),
            patch.object(attack, "fitted_line", return_value=None),
        ):
            runner._flank(b"as the battle opened", plans.flat())
        return runner

    def test_the_frame_after_the_cameras_drag_is_the_one_kept(self, tmp_path: Path) -> None:
        """A run that is not recording keeps it too, under one name for the whole run."""
        frames = FrameLog(directory=tmp_path / "frames", recording=False)
        self._flanked(frames, b"first battle")
        self._flanked(frames, b"second battle")
        assert [path.name for path in frames.directory.iterdir()] == ["opening.png"]
        assert (frames.directory / "opening.png").read_bytes() == b"second battle"

    def test_a_frame_that_will_not_write_does_not_cost_the_battle(self, tmp_path: Path) -> None:
        (tmp_path / "in-the-way").write_text("", encoding="utf-8")
        self._flanked(FrameLog(directory=tmp_path / "in-the-way" / "frames"))

    def test_a_runner_with_no_run_to_write_into_keeps_nothing(self) -> None:
        self._flanked(None)


class TestLaterFrame:
    """`--on`: the opening frame's grid and plan painted on a later frame of the same battle."""

    def _draw(self, tmp_path: Path, on: Path) -> commands.DrawReport:
        asked = DrawOptions(png=FRAMES / "battle_flat_reference.png", camera=DAY_CAMERA, on=on)
        return commands.draw(asked, FrameLog(directory=tmp_path / "own" / "frames"))

    def test_the_marks_are_measured_on_the_opening_frame_and_painted_on_the_other(
        self, tmp_path: Path
    ) -> None:
        later = FRAMES / "battle_in_progress.png"
        report = self._draw(tmp_path, later)
        assert report.grid == boundary_diamond(DAY)
        assert report.tactic == planned_drops(plans.flat(), DAY, DAY_CAMERA)
        picture = Image.open(report.image).convert("RGB")
        row = (0, CARD_ROW_TOP, 1600, 900)
        canvas = Image.open(later).convert("RGB")
        assert ImageChops.difference(picture.crop(row), canvas.crop(row)).getbbox() is None
        plain = commands.draw(
            DrawOptions(png=FRAMES / "battle_flat_reference.png", camera=DAY_CAMERA),
            FrameLog(directory=tmp_path / "plain" / "frames"),
        )
        assert len(commands.draw_text(report).splitlines()) == (
            len(commands.draw_text(plain).splitlines()) + 1
        )

    def test_a_later_frame_that_is_not_there_draws_nothing(self, tmp_path: Path) -> None:
        assert self._draw(tmp_path, tmp_path / "gone.png").outcome == "no_frame"


class TestPointsWithNoBottle:
    """Every point of the plan a bottle does not reach is drawn as bare, whatever the row holds."""

    def test_points_past_the_rows_bottles_are_bare_too(self) -> None:
        """Eight rage points for six bottles on two cards: the two past the bottles are bare."""
        spots = [(x, y) for y in (30, 45) for x in (20, 35, 50, 65)]
        plan = AttackPlan(
            steps=[
                AttackStep(
                    act="rage",
                    who="unknown",
                    at=[ScreenPoint(x_pct=x, y_pct=y) for x, y in spots],
                    seconds=0,
                )
            ]
        )
        [rage] = planned_drops(plan, DAY, DAY_CAMERA).steps
        every = [(x * 16, y * 9 + DAY_CAMERA[1]) for x, y in spots]
        assert rage.at == every[:6]
        assert rage.unused == every[6:]

    def test_a_row_without_the_spell_leaves_every_point_bare(self) -> None:
        with patch.object(attack, "counted_cards", return_value=[]):
            tactic = planned_drops(plans.flat(), DAY, DAY_CAMERA)
        spells = [step for step in tactic.steps if step.act in ("rage", "freeze")]
        assert [len(step.unused) for step in spells] == [2, 3, 6]
        assert all(step.at == [] and step.cards == 0 for step in spells)


def test_a_corners_label_steps_off_a_mark_standing_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flat plan's siege machine sits just outside the left corner, where 左角 is written."""
    grid = BaseGrid(top=(799, 50), right=(1384, 489), bottom=(800, 927), left=(215, 488))
    draw = ImageDraw.Draw(Image.new("RGBA", (1600, 900)))
    written: dict[str, tuple[float, float]] = {}
    tag = drawing._tag

    def spy(*args: object, **kw: object) -> tuple[float, float]:
        written[args[2]] = tag(*args, **kw)  # type: ignore[arg-type]
        return written[args[2]]  # type: ignore[index]

    monkeypatch.setattr(drawing, "_tag", spy)
    drawing._grid_labels(draw, grid, [])
    alone = written["左角"]
    assert alone == (215 - 62, 488)
    drawing._grid_labels(draw, grid, [(176 - 18, 489 - 18, 176 + 18, 489 + 18)])
    assert written["左角"] != alone
    assert written["右角"] == (1384 + 62, 489)
