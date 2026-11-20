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
from pathlib import Path
import itertools
from unittest.mock import patch

from PIL import Image, ImageChops
import pytest

from ai_coc import cli, plans, commands
from ai_coc.ui import attack
from ai_coc.models import (
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
from ai_coc.drawing import CARD_ROW_TOP, legend, draw_plan
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
        # Four bottles over two cards of two: the second card starts one
        # target along, so one spot takes two and three of the plan's six none.
        assert drops["rage", 16].at == [(710, 519), (550, 609), (550, 609), (870, 429)]
        assert drops["rage", 16].unused == [(870, 609), (1030, 519), (710, 339)]

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

    def test_a_frame_with_no_card_row_keeps_every_point_of_the_plan(self) -> None:
        """Nothing says how many bottles there are, so nothing is called unused."""
        blank = io.BytesIO()
        Image.new("RGB", (1600, 900), (40, 90, 40)).save(blank, format="PNG")
        tactic = planned_drops(plans.flat(), blank.getvalue())
        spells = [step for step in tactic.steps if step.act in ("rage", "freeze")]
        assert [len(step.at) for step in spells] == [2, 3, 6]
        assert all(step.unused == [] and step.cards is None for step in spells)
        assert not tactic.fitted


class TestPicture:
    def _picture(self, *, against: bool = False) -> Image.Image:
        tactic = planned_drops(plans.flat(), DAY, DAY_CAMERA)
        png = draw_plan(
            DAY, boundary_diamond(DAY), tactic.steps, tactic.steps if against else [], ["一行"]
        )
        return Image.open(io.BytesIO(png)).convert("RGB")

    def test_nothing_is_drawn_over_the_card_row(self) -> None:
        """The grid's lower corner lies under it, and the row is what the picture is read against."""
        picture = self._picture()
        frame = Image.open(io.BytesIO(DAY)).convert("RGB")
        row = (0, CARD_ROW_TOP, 1600, 900)
        assert ImageChops.difference(picture.crop(row), frame.crop(row)).getbbox() is None
        field = (0, 0, 1600, CARD_ROW_TOP)
        assert ImageChops.difference(picture.crop(field), frame.crop(field)).getbbox() is not None

    def test_the_notes_go_on_a_strip_under_the_frame(self) -> None:
        picture = self._picture()
        assert picture.width == 1600
        assert picture.height > 900
        assert picture.crop((0, 900, 1600, picture.height)).getbbox() is not None

    def test_a_plan_held_against_itself_draws_nothing_twice(self) -> None:
        """Only what the other plan does differently is marked."""
        assert (
            ImageChops.difference(self._picture(), self._picture(against=True)).getbbox() is None
        )

    def test_the_legend_numbers_what_is_placed_and_groups_it_by_the_plans_clock(self) -> None:
        lines = legend(planned_drops(plans.flat(), DAY, DAY_CAMERA).steps)
        assert [line.split(" ")[0] for line in lines] == ["0", "5", "7", "15", "16"]
        # Four placed steps at the opening, and the ability between them unnumbered.
        assert [part.split(" ")[0] for part in lines[0].split(": ")[1].split(" → ")] == [
            "1",
            part := lines[0].split(" → ")[1].split(" ")[0],
            "2",
            "3",
            "4",
        ]
        assert not part.isdigit()
        assert lines[1].split(": ")[1][0] not in "0123456789"
        assert "3" in lines[4].split("瓶")[1]

    def test_a_row_with_no_machine_says_so(self) -> None:
        steps = [PlannedDrop(act="machine", seconds=0, at=[(398, 257)], cards=0)]
        assert legend(steps)[0] != legend([steps[0].model_copy(update={"cards": 1})])[0]


def _run(tmp_path: Path, name: str, lines: list[str], frames: list[str]) -> Path:
    """A recorded run's directory: its log, and `DAY` under each frame name."""
    run = tmp_path / "logs" / name
    (run / "frames").mkdir(parents=True)
    (run / "run.log").write_text(
        "".join(f"00:28:29 INFO    {line}\n" for line in lines), encoding="utf-8"
    )
    for frame in frames:
        (run / "frames" / f"{frame}.png").write_bytes(DAY)
    return run


def _saved(name: str) -> str:
    return f"ai_coc.models: Saved frame {name}.png"


# One home battle as the loop logs it: a centring drag before the plan, the
# flank's drag after it, then the first drop.
BATTLE = [
    "ai_coc.adapters.adb: Tap 127.0.0.1:5555 display 0 at (1411, 803)",
    _saved("0010_scout"),
    "ai_coc.adapters.adb: Pinch 127.0.0.1:5555 ((300, 450), (650, 450)) and ((1300, 450), (950, 450))",
    _saved("0011_zoomed"),
    _saved("0012_battle"),
    "ai_coc.adapters.adb: Swipe 127.0.0.1:5555 (800, 400) -> (800, 499) in 350 ms",
    _saved("0013_camera"),
    "ai_coc.ui.attack: Dragged the camera by (0, 99); the village moved (0, 102), "
    "now (0, 102) off where the battle opened",
    "ai_coc.adapters.adb: Tapping 2 points on 127.0.0.1:5555 display 0: (555, 800) (340, 402)",
    _saved("0014_settled"),
    "ai_coc.adapters.adb: Tap 127.0.0.1:5555 display 0 at (165, 800)",
]


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

    def test_a_recorded_run_gives_the_frame_before_its_first_drop_and_the_cameras_shift(
        self, tmp_path: Path
    ) -> None:
        run = _run(tmp_path, "2026-11-21-002800-attack-ref", BATTLE, ["0013_camera"])
        report = self._draw(tmp_path, run=run)
        assert report.frame == run / "frames" / "0013_camera.png"
        assert report.camera == (0, 102)

    def test_a_run_is_found_by_its_label_and_a_battle_by_its_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The newest run carrying the label, and never the drawing run's own directory."""
        monkeypatch.setattr(commands, "LOG_DIR", tmp_path / "logs")
        _run(tmp_path, "2026-11-20-090000-attack-ref", BATTLE, ["0013_camera"])
        second = [line.replace("001", "002") for line in BATTLE if "Dragged" not in line]
        newest = _run(
            tmp_path,
            "2026-11-21-090000-attack-ref",
            BATTLE + second,
            ["0013_camera", "0023_camera"],
        )
        own = tmp_path / "logs" / "2026-11-21-100000-draw-ref" / "frames"
        own.mkdir(parents=True)

        def draw(**asked: object) -> commands.DrawReport:
            return commands.draw(DrawOptions(run=Path("ref"), **asked), FrameLog(directory=own))

        last = draw()
        assert (last.frame, last.camera) == (newest / "frames" / "0023_camera.png", (0, 0))
        first = draw(battle=1)
        assert (first.frame, first.camera) == (newest / "frames" / "0013_camera.png", (0, 102))
        assert draw(battle=3).outcome == "no_frame"

    def test_nothing_to_draw_on_is_an_outcome_of_its_own(self, tmp_path: Path) -> None:
        """A missing file, and a run that kept no frames or whose frames are gone."""
        assert self._draw(tmp_path, png=tmp_path / "gone.png").outcome == "no_frame"
        bare = _run(tmp_path, "bare", [line for line in BATTLE if "Saved" not in line], [])
        assert self._draw(tmp_path, run=bare).outcome == "no_frame"
        culled = _run(tmp_path, "culled", BATTLE, [])
        report = self._draw(tmp_path, run=culled)
        assert (report.outcome, report.image) == ("no_frame", None)
        assert commands.draw_text(report) == commands.DRAW_LINES["no_frame"]

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
