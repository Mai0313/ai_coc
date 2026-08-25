"""The headless side of the application: what `cli.py` runs when given a command.

Nothing here builds a window. Everything below the `ui/` package is already
Qt-free, so the only thing the window was ever providing was the wiring — find
the emulator, resolve the display it put the game on, hand over the API key —
and that is what these functions are. A feature that can only be reached through
a widget cannot be run against the live game while it is being worked on, which
is the whole reason the attack loop grew a `frame_dir` at the same time.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING
import logging

from pydantic import PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    MapEdge,
    ProbeRay,
    AppConfig,
    MapSurvey,
    AttackReport,
    FrameReading,
    LootOverrides,
    BoundarySurvey,
    GeminiSettings,
    LootThresholds,
)
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.parsers.scout import (
    card_count,
    live_cards,
    read_scout,
    read_stock,
    card_groups,
    field_units,
    card_drained,
    freeze_cards,
    army_strength,
    counted_cards,
    attack_menu_open,
    idle_disconnected,
)
from ai_coc.adapters.config import ConfigStore
from ai_coc.adapters.secrets import SecretStore
from ai_coc.parsers.boundary import PLAYFIELD, VILLAGE_CENTRE, boundary_reach

from .ui.attack import CARD_ROW_Y, DROP_SETTLE, SINGLE_DROP_DELAY, AttackRunner

if TYPE_CHECKING:
    from pathlib import Path

    from ai_coc.adapters.adb import AdbController

logger = logging.getLogger(__name__)


def _controller() -> AdbController:
    """The first MuMu instance, with Clash of Clans already up on it."""
    mumu = MuMuAdapter()
    instances = mumu.enumerate_instances()
    if not instances:
        raise RuntimeError("找不到任何 MuMu instance")
    return mumu.controller(mumu.ensure_coc(instances[0].index).adb_serial)


def _planner(config: AppConfig) -> GeminiClient | None:
    """The saved key, or None so the attack falls back to its fixed flank."""
    try:
        key = SecretStore().load()
    except Exception:
        logger.warning("No saved API key could be read", exc_info=True)
        return None
    if not key:
        logger.info("No API key is saved; the attack will use the fixed flank and spell grid")
        return None
    return GeminiClient(
        settings=GeminiSettings(
            api_key=key, model=config.gemini_model, base_url=config.gemini_endpoint
        )
    )


def attack(
    frame_dir: Path | None = None,
    plan_in: Path | None = None,
    plan_out: Path | None = None,
    minimums: LootOverrides | None = None,
) -> AttackReport:
    """One pass of the attack loop, with no window in the way.

    Thresholds, storage limits and ability timings all come from the shared
    config file, so a run started here plays the same way as one started from
    the window. `minimums` overrides the loot thresholds for this run alone,
    which is how a loop being studied gets the old behaviour back: all three at
    zero is "attack the first opponent shown", and skipping nothing is what puts
    the code worth watching on screen.

    `plan_in` replaces the AI entirely — the loop plays that file and asks for
    nothing — and `plan_out` writes down whichever plan actually ran, so a battle
    worth repeating can be repeated and one worth arguing with can be edited.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    plan = plans.load(plan_in) if plan_in else None
    config = ConfigStore().load()
    runner = AttackRunner(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        thresholds=(minimums or LootOverrides()).over(config.thresholds),
        stock=config.stock,
        abilities=config.timings,
        ai=None if plan else _planner(config),
        plan=plan,
        frame_dir=frame_dir,
    )
    report = runner.run()
    if plan_out is not None and runner.played is not None:
        plan_out.write_text(runner.played.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Wrote the plan that ran to %s", plan_out)
    logger.info("Attack finished: %s", report.message)
    return report


# Twelve rays is the whole village at 30 degree steps, and two drops on each is
# what fits: a refused drop costs no troop but does cost the capture that reads
# the card afterwards, so a wider sweep runs past the end of the battle.
SURVEY_RAYS = tuple(angle * 30 for angle in range(12))
# How far either side of the predicted line the two drops go. Wider than the
# margin `fitted_line` already adds, so a ray that agrees really does straddle it.
SURVEY_MARGIN = 60


class _BoundarySurvey(AttackRunner):
    """An attack that surveys the deployment boundary instead of fighting.

    The boundary reader is the least certain thing in the loop: its colour
    thresholds were measured on four village themes and the map diamond was
    calibrated by eye. This is how to find out whether it still agrees with the
    game — on a new theme, after a game update, or when a battle goes wrong for
    reasons the recorded frames do not explain. It costs a battle that is thrown
    away, which costs nothing but the time.
    """

    survey: BoundarySurvey = BoundarySurvey()

    def _deploy(self, frame: bytes) -> None:
        card = card_groups(frame)[0][0]
        battle = self._wait_for_battle()
        if battle is None:
            logger.warning("Never reached the battle; nothing to survey")
            return
        for degrees in SURVEY_RAYS:
            reach = boundary_reach(battle, degrees)
            if reach is None:
                self.survey.unread.append(degrees)
                logger.info("Ray %.0f: the reader found no boundary", degrees)
                continue
            radius = math.hypot(reach[0] - VILLAGE_CENTRE[0], reach[1] - VILLAGE_CENTRE[1])
            dx, dy = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
            verdicts: list[bool] = []
            for offset in (-SURVEY_MARGIN, SURVEY_MARGIN):
                point = (
                    round(VILLAGE_CENTRE[0] + dx * (radius + offset)),
                    round(VILLAGE_CENTRE[1] + dy * (radius + offset)),
                )
                before = self._frame(f"{degrees:03.0f}deg-{offset:+d}-before")
                self.adb.tap_many([(card, CARD_ROW_Y), point], self.display, gap=SINGLE_DROP_DELAY)
                time.sleep(DROP_SETTLE)
                after = self._frame(f"{degrees:03.0f}deg-{offset:+d}")
                # The card is what the survey measures, because the warning
                # banner it used to read is raised by four different things and
                # not raised at all by a tap the game simply swallows.
                verdicts.append(not card_drained(before, after, [card]))
            ray = ProbeRay(
                degrees=degrees,
                predicted=round(radius),
                inside_refused=verdicts[0],
                outside_refused=verdicts[1],
            )
            self.survey.rays.append(ray)
            logger.info(
                "Ray %.0f: predicted %d, inside %s, outside %s -> %s",
                degrees,
                ray.predicted,
                "refused" if ray.inside_refused else "ACCEPTED",
                "refused" if ray.outside_refused else "accepted",
                "agrees" if ray.agrees else "DISAGREES",
            )


def probe(frame_dir: Path | None = None) -> BoundarySurvey:
    """Survey where drops are really accepted, and compare it to what the reader says.

    Spends a battle to answer one question the recorded frames cannot: whether
    the red line the parser found is the line the game is enforcing.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    runner = _BoundarySurvey(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        thresholds=LootThresholds(),
        frame_dir=frame_dir,
    )
    runner.run()
    logger.info("Boundary survey: %s", runner.survey.agreement)
    return runner.survey


# Six rays, picked for where the map's own edge falls inside the playfield. The
# vertical pair runs off the top and bottom of the screen long before it reaches
# the diamond, so it would measure the UI rather than the map.
MAP_RAYS = (0.0, 45.0, 135.0, 180.0, 225.0, 315.0)
# How far each probe steps back in from the screen edge, and how many it may take
# before the ray is given up on. A refused probe costs no troop, only the capture
# that reads the card, so the limit is the battle's own three minutes.
MAP_STEP = 45
MAP_PROBES = 8


class _MapSurvey(AttackRunner):
    """An attack spent measuring how far out the game will still take a drop.

    `VILLAGE_GRID` was calibrated by overlaying candidates on live frames until
    they sat on the ground's own edge, which is the sort of number a screenshot
    cannot argue with. This is the argument: walk inwards along a ray until a
    troop lands, and that is where the map really ends. It is worth re-running
    after a game update, a theme that repaints the ground, or any change to the
    emulator's resolution.
    """

    survey: MapSurvey = MapSurvey()
    # Which cards the probes may spend, read once off the full row.
    _troops: list[int] = PrivateAttr(default_factory=list)

    def _edge(self, degrees: float, shot: bytes) -> tuple[MapEdge | None, bytes]:
        """Walk one ray inwards from the screen edge until a drop lands."""
        left, top, right, bottom = PLAYFIELD
        dx, dy = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        # The furthest this ray gets before the playfield stops it.
        limit = min(
            (edge - VILLAGE_CENTRE[axis]) / step
            for axis, step, edge in (
                (0, dx, right if dx > 0 else left),
                (1, dy, bottom if dy > 0 else top),
            )
            if abs(step) > 1e-9
        )
        for probe in range(MAP_PROBES):
            radius = limit - probe * MAP_STEP
            if radius <= 0:
                break
            point = (
                round(VILLAGE_CENTRE[0] + dx * radius),
                round(VILLAGE_CENTRE[1] + dy * radius),
            )
            card = next(iter(live_cards(shot, self._troops)), None)
            if card is None:
                logger.info("Every troop card is spent; the survey stops here")
                return None, shot
            self.adb.tap_many([(card, CARD_ROW_Y), point], self.display, gap=SINGLE_DROP_DELAY)
            time.sleep(DROP_SETTLE)
            before, shot = shot, self._frame(f"{degrees:03.0f}deg-{round(radius):04d}")
            if card_drained(before, shot, [card]):
                logger.info("Ray %.0f: accepted at %d of %d", degrees, radius, limit)
                # Accepted at the very first probe means the screen ran out
                # before the map did, so this ray measured the playfield.
                return MapEdge(
                    degrees=degrees,
                    reached=None if probe == 0 else round(radius),
                    predicted=round(limit),
                ), shot
        logger.info("Ray %.0f: nothing landed anywhere along it", degrees)
        return None, shot

    def _deploy(self, frame: bytes) -> None:
        self._troops = card_groups(frame)[0]
        battle = self._wait_for_battle()
        if battle is None:
            logger.warning("Never reached the battle; nothing to survey")
            return
        shot = battle
        for degrees in MAP_RAYS:
            edge, shot = self._edge(degrees, shot)
            if edge is not None:
                self.survey.edges.append(edge)


def bounds(frame_dir: Path | None = None) -> MapSurvey:
    """Spend a battle finding where the map really ends, and fit a diamond to it."""
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    runner = _MapSurvey(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        thresholds=LootThresholds(),
        frame_dir=frame_dir,
    )
    runner.run()
    logger.info("Map survey: %s", runner.survey.summary)
    return runner.survey


def capture(out_dir: Path, count: int = 1, gap: float = 1.5) -> list[Path]:
    """Save frames off the live game, for measuring a screen the parsers cannot read yet.

    A burst rather than a single shot: the screens worth measuring are the ones
    that only exist while something is happening, and those cannot be reached by
    asking for one frame at the right moment.
    """
    adb = _controller()
    display = adb.display_for(COC_PACKAGE)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index in range(count):
        path = out_dir / f"frame_{index:03d}.png"
        path.write_bytes(adb.screenshot(display))
        logger.info("Saved %s", path)
        saved.append(path)
        if index + 1 < count:
            time.sleep(gap)
    return saved


def read(png: bytes) -> FrameReading:
    """Put one frame through every parser at once and report what each one saw."""
    groups = card_groups(png)
    slots = [slot for group in groups for slot in group]
    return FrameReading(
        scout=read_scout(png),
        stock=read_stock(png),
        army=army_strength(png),
        attack_menu=attack_menu_open(png),
        idle_dialog=idle_disconnected(png),
        card_groups=groups,
        counted=counted_cards(png, slots),
        freezes=freeze_cards(png, slots),
        live=live_cards(png, slots),
        on_field=field_units(png, slots),
        counts={slot: card_count(png, slot) for slot in slots},
    )
