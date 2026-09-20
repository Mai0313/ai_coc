"""What every loop needs before it can do anything: a frame, a tap, and the village.

Each of these loops opens the same way — make sure the home village is actually
on screen — and each of them can arrive to find it is not. The game may still be
loading, a panel may be covering it, a dialog may be standing over it, or the
session may have been dropped for idling, which is what a loop that spends
minutes waiting on barracks or on an army walks into sooner or later.

Kept Qt-free like everything else under `ui/` that is not a widget, so
`commands.py` can drive any of it against the live game without a window.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging
from pathlib import Path
from collections.abc import Callable

from pydantic import BaseModel, PrivateAttr

from ai_coc.models import World, ScreenSpots, VillageStock, DisplayTarget, StorageCapacity
from ai_coc.prompts import render
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import ZOOM_PINCHES, AdbController, AdbControlError
from ai_coc.parsers.scout import (
    read_stock,
    loading_screen,
    storage_capacity,
    idle_disconnected,
    read_builder_stock,
)
from ai_coc.parsers.world import current_world
from ai_coc.parsers.building import game_dialog

from .world import cross, park_camera

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

logger = logging.getLogger(__name__)

# How long a menu takes to open. Measured against this emulator by walking the
# menus by hand: a capture taken 0.8 s after the tap already had the new menu on
# it, and this leaves room for the emulator being busy.
MENU_SETTLE = 1.0
# How long the game takes to charge for something and repaint what it charged
# for, measured the same way. Walls, buildings and heroes each measured it for
# themselves and all three came out the same.
BUY_SETTLE = 1.5

# Where a tap could land on something worth opening. Buildings move between
# villages and the camera moves within one, so nothing can be remembered: what
# is on the map is found by tapping and reading what the game opens. The row of
# buttons a selection opens runs along the bottom of the screen, so a sweep that
# reached down there would be pressing the previous tap's own buttons; the left
# and right edges are the game's own button columns for the same reason.
SWEEP_X = (260, 420, 580, 740, 900, 1060, 1220)
SWEEP_Y = (140, 260, 380, 500)
# Half a grid step, for the second pass a caller makes when the first missed
# what it was looking for. Buildings are three to five tiles across — 70 to
# 120 px on this camera — against a grid that steps 160 by 120, so a small one
# fits between four points with room to spare. Staggered, the same grid lands on
# the middle of each of those gaps.
SWEEP_STAGGER = (SWEEP_X[1] - SWEEP_X[0]) // 2, (SWEEP_Y[1] - SWEEP_Y[0]) // 2
# How far a tap may go before it stops being the village. The plain grid stays
# inside this by construction; a staggered one has to be held to it, and its
# last column would otherwise land on the storage bars — which start at x 1260,
# with the dark elixir reading beginning at exactly (1300, 200).
SWEEP_LIMIT = (SWEEP_X[-1], 620)
# The same floor as a percentage, which is what a finder answers in. Below it is
# the row of buttons the previous selection left on screen, so a point there taps
# that row rather than the map — measured, four of twelve answers landed in it
# and each one opened whatever the last menu's buttons happened to be. Telling
# the model about it works (0 of 14 on the next run), and the filter below is
# what makes that a guarantee rather than an improvement.
SPOT_FLOOR = SWEEP_LIMIT[1] * 100 // 900
# One call against one still frame. Long enough for a slow answer, short enough
# that a hung one falls through to the sweep rather than holding the run.
SPOT_TIMEOUT = 60

# Getting back to the village, which four different things can be in the way of,
# and they do not want the same treatment.
#
# A game still loading wants waiting on and nothing else — measured, about twenty
# seconds from a cold launch — so nothing is pressed until the patience runs out.
# A panel a tap opened wants `back`. A dialog wants 取消. And a dropped session
# wants 重新登入遊戲, after which the game reloads from scratch.
#
# **The patience is only owed to a game that might still be starting**, which is
# what `_seen_village` settles: once a village has read, an unreadable frame is a
# panel and waiting on it buys nothing. That distinction is worth a private
# attribute because `_sweep` spends most of its taps opening panels — its grid
# steps 160 px over buildings 70 to 120 px across — and each one used to cost the
# full patience before the first `back`. Measured over three live wall runs the
# stalls were 26, 26 and 34 seconds, which is 50 to 80 of a 150 second run spent
# waiting for a launch that had already happened.
HOME_TRIES = 20
LOADING_PATIENCE = 10
LOAD_WAIT = 2.0
BACK_SETTLE = 1.2
# Closing the game and opening it again, which is what the idle-disconnect
# dialog is answered with. How long to leave between the two, and how much of
# the reload to sit through before looking at the screen again.
RESTART_SETTLE = 2.0
RELOAD_WAIT = 15.0


# Where to tap to drop a storage bar's 最大儲存量 tooltip open, one per bar down
# the corner. The x sits inside the bar and well right of the builder base's
# gems row, whose green + spans 1345 to 1385 and opens the shop — the one thing
# on any of these rows that a stray tap must not reach. The y is each bar's own
# middle, the same rows `read_stock` reads its digits from.
#
# The tooltip takes a moment to animate in and is a toggle rather than a popup,
# so a capture taken too early reads the frame before it opened and costs the
# whole row: `CAPACITY_TRIES` is what pays for that, and for a tooltip somebody
# left open before the run started.
STOCK_BAR_X = 1400
STOCK_BAR_Y = (52, 136, 219)
TOOLTIP_SETTLE = 1.0
CAPACITY_TRIES = 2


def spell_out(seconds: int) -> str:
    """A countdown as the game writes it, for a message a person reads."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分鐘"
    return f"{minutes} 分鐘"


def restart_game(adb: AdbController, display: DisplayTarget) -> DisplayTarget:
    """Close the game and open it again, and say which display it came back on.

    This is how a dropped session is answered. The dialog offers 重新登入遊戲 and
    tapping it does exactly this — the game reloads either way, so the two cost
    the same seconds. What tapping it also costs is a hard-coded button position,
    which belongs to this emulator at this resolution and to nothing else; going
    through the package needs no coordinates at all.

    The display is resolved again because MuMu opens the game on a display of its
    own choosing and nothing promises it picks the same one. One that is not up
    yet is not an error: the old display comes back and the caller, which is
    already looping, asks again on its next pass.
    """
    adb.stop_app(COC_PACKAGE)
    time.sleep(RESTART_SETTLE)
    adb.launch_app(COC_PACKAGE)
    time.sleep(RELOAD_WAIT)
    try:
        return adb.display_for(COC_PACKAGE)
    except AdbControlError:
        logger.info("The game is not on a display yet; looking again shortly")
        return display


class ScreenRunner(BaseModel):
    """One emulator screen, and the two things every loop here does to it.

    **`AttackRunner` is the other subclass, and it is why this is a class at
    all.** It is not a `GameRunner` — it has no use for `_home`, since its way
    back to a village has to cope with a result screen, a matchmaker and a
    village it may have to sail to — but it declared these five fields with the
    same names, types and defaults, and both methods with the same bodies. The
    frame naming in particular (`0006_scout`, `0010_pass`) is written down in
    `CLAUDE.md` and in two of the project's skills, and a convention documented
    in three places and implemented in two is the drift `AGENTS.md` is a symlink
    to avoid.
    """

    adb: AdbController
    display: DisplayTarget
    # Asked between whole units of work, never inside one: a unit abandoned
    # halfway leaves the game on a screen the next run has to dig itself out of.
    # What a unit is belongs to each loop — a wall batch here, an opponent there.
    should_stop: Callable[[], bool] = lambda: False
    # Where to keep every frame the loop reads, for a run being studied afterwards.
    frame_dir: Path | None = None
    # Who to ask the one question the parsers cannot answer. None is an ordinary
    # answer everywhere: the finders fall back to the sweep and the attack loop
    # to its flat plan, which is what every loop here did before this existed.
    ai: GeminiClient | None = None

    _captures: int = PrivateAttr(default=0)

    def _tap(self, point: tuple[int, int]) -> None:
        self.adb.tap(point[0], point[1], self.display)

    def _after_tap(self, point: tuple[int, int], label: str) -> bytes:
        """Tap, let whatever it opens finish opening, and hand back what is there."""
        self._tap(point)
        time.sleep(MENU_SETTLE)
        return self._frame(label)

    def _frame(self, label: str) -> bytes:
        """One capture, kept on disk when the run is being recorded.

        Every screenshot a loop reads comes through here, so a recorded run is
        the whole of it in the order the loop saw it, each frame named for what
        it was being asked. Afterwards that is the only thing separating a frame
        the parser misread from a tap that never landed.
        """
        png = self.adb.screenshot(self.display)
        if self.frame_dir is not None:
            self._captures += 1
            (self.frame_dir / f"{self._captures:04d}_{label}.png").write_bytes(png)
        return png

    def read_storages(self) -> tuple[World | None, VillageStock | None]:
        """Which village is on screen and what its storages hold, in one capture.

        Both halves in one place because the second depends on the first: the
        two villages read through different readers, and `read_stock` on the
        builder base reports its gems bar as dark elixir. None for the world is
        a frame that is not a village at all, which is a different answer from a
        village whose bars this frame could not resolve.
        """
        png = self._frame("stock")
        world = current_world(png)
        if world is None:
            return None, None
        return world, (read_stock if world == "day" else read_builder_stock)(png)

    def read_ceilings(self, world: World) -> StorageCapacity | None:
        """What this village's storages hold when full, read off their own tooltips.

        **A partial read is thrown away rather than kept**, which is the whole
        reason this is a method and not a loop at the call site. Every one of
        these taps can be swallowed — a tooltip still animating in, a panel over
        the bars, a village the game had not finished painting — and a
        `StorageCapacity` holding some of its rows is worse than none in both
        directions. Empty, it watches nothing, so an overnight run farms straight
        past full storages and throws the loot away. Partial is worse still: gold
        and elixir failing while dark reads its 370 000 leaves a run standing
        down the moment dark passes 90% with the two big storages nearly empty.

        **The builder base's third row is never tapped.** That village has no
        dark elixir; what sits at that y is its gems bar, and the green + beside
        the number opens the shop. Two rows there is not a limitation — a
        resource with no ceiling is left out of every comparison, which is
        exactly right for one that does not exist.

        The tooltip is a toggle, so a row that reads nothing is left alone rather
        than tapped shut: the one way to read nothing on a village that has the
        bar is to have closed a tooltip that was already open, and the next
        attempt then opens it.

        **A row that fails both tries can leave its own tooltip up, and that is
        measured to be harmless.** The panel hangs *under* the bar that opened
        it, so it covers the rows below rather than its own — and only the last
        row read has nothing after it to close it, since the next row's first tap
        closes whatever is open. Measured on the fixtures: with the dark tooltip
        up `read_stock` still reads all three rows, and with the builder base's
        elixir tooltip up `read_builder_stock` still reads both, which is exactly
        the last row in each village. Closing on failure instead was tried and is
        worse — with the tooltip starting closed, which is the ordinary case, a
        row that fails twice would then end on an opening tap and leave the
        **gold** panel up, and that one does cover the rows under it.
        """
        rows = ("gold", "elixir", "dark") if world == "day" else ("gold", "elixir")
        found: dict[str, int] = {}
        for row, name in enumerate(rows):
            for _ in range(CAPACITY_TRIES):
                self._tap((STOCK_BAR_X, STOCK_BAR_Y[row]))
                time.sleep(TOOLTIP_SETTLE)
                held = storage_capacity(self._frame(f"capacity-{name}"), row)
                if held is None:
                    continue
                found[name] = held
                self._tap((STOCK_BAR_X, STOCK_BAR_Y[row]))
                time.sleep(TOOLTIP_SETTLE)
                break
        if len(found) < len(rows):
            logger.warning(
                "Only %d of %d storage ceilings read (%s)", len(found), len(rows), found or "none"
            )
            return None
        logger.info("Storage ceilings read as %s", found)
        return StorageCapacity(**found)


class GameRunner(ScreenRunner):
    """Captures, taps, and getting back to a village that can be tapped."""

    # Whether a village has ever read on this runner, which is what says the game
    # has finished starting. Cleared when the game is restarted, since that is the
    # one moment a cold launch can be under way again.
    _seen_village: bool = PrivateAttr(default=False)
    # Whether the camera has been put back this run. Cleared alongside the flag
    # above and for the same reason: a restarted game does not come back at the
    # zoom everything was measured at.
    _settled: bool = PrivateAttr(default=False)

    def _settle_zoom(self, world: World | None) -> None:
        """Put the camera back at the far zoom, once per village these loops see.

        **Every one of these loops taps buildings by screen coordinate**, and
        those were measured at the game's far zoom limit — the sweep grid, the
        button row on an opened menu, a remembered `--at`. A camera that has
        drifted off that zoom sends all of them somewhere else, and the failure
        is silent: a tap lands on the ground and the loop simply reports that
        nothing opened.

        The camera does drift. Whatever moves it has not been pinned down, and
        `AttackRunner` answers the same problem the same way, for the same
        reason: the game reports no zoom level and reading one off a frame was
        defeated twice, while zooming out past the limit costs nothing. So this
        asks rather than checks.

        Once per run, and **before the first storage read rather than after
        it**, because the storages are one of the things a drifted camera
        breaks. The bars are translucent where they are not full, so a bright
        enough background bleeds through and fills the gaps *between* the
        digits: measured on the home village at a zoomed-in camera, one of
        seven pan positions put the map's shoreline behind an elixir bar
        standing at 45%, and the 779 of 10 779 278 came through as a single
        55 px span — too wide to be a glyph, so the row answered NOT_A_GLYPH
        and `read_stock` answered None on a frame whose three numbers were
        perfectly legible to the eye. Hung off a successful read, this is the
        one step that would have fixed that camera and the only step that never
        runs on it — `_home` answers None with `back`, a clear village answers
        `back` with 確定退出遊戲嗎, the dialog gets 取消, and the run spends
        `HOME_TRIES` going round that circle before reporting a village it
        could not get back to.

        **It is the loot panel's bright-theme failure arriving from the other
        side, and it does not get the same answer.** There the ink floor had to
        be raised (`_lit_floor`), because an opponent's village theme is not
        ours to choose. This camera is, so what is fixed is the view rather
        than the reader.

        **So the village test here is `current_world` as well as the storages,
        and widening it is the point rather than swapping it.** The plate row
        along the top is UI at a fixed place and does not care where the camera
        is, so it answered day on that very frame; but it misses villages of
        its own, a gem shower drifting over a plate among them, and those the
        storages read. Either reading is a village, and a village is where this
        runs. `_settle_game` already opens the attack loop off the plate row,
        which is why the six commands that skip it could not recover.

        **But the two readings do not settle the same half.** Either says this
        is a village, so either is enough for the pinch; only the plate row says
        *which* village, and a park has to be told, because the two maps clamp
        in opposite corners. So a frame the storages alone recognised gets the
        scale and keeps whatever position it had — see the branch below.

        A pinch between wall batches would only be spending three seconds to
        confirm what this one already settled.

        **The pinch settles the scale and the park settles the position**, which
        used to be one thing and was not: the far zoom was taken to centre the
        village as well, and measured it does not move the camera at all. Every
        coordinate below this line is aimed at the map, so without the park they
        were valid only until something moved the camera — which is silent, and
        which every crossing, every stray swipe and every session that looked at
        something up close does. **Both are `park_camera`'s now**, because its
        own walk is only bounded at the far zoom and it is the one that has to
        know that.
        """
        # **A village nobody could name gets the scale and not the position.**
        # The two maps clamp in opposite corners, so a park has to be told which
        # village it is pushing, and the one branch that reaches here with no
        # world is the one where the plate row answered None and `read_stock` —
        # which cannot tell the two villages apart — is the only test left.
        # Picking a side there is a coin flip that runs the builder base away
        # from the one corner its boat and its 聖水車 are moored at. `view` has
        # made this call already, for the same reason and in the same shape: the
        # pinch is safe on any screen and the swipe is not.
        if world is None:
            logger.warning(
                "The storages read but the plate row could not place the village; "
                "zooming out without parking"
            )
            self.adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, self.display)
            return
        # **The answer is not acted on, and `_settled` is set either way.** A
        # park that fell short leaves the sweep grid worse rather than unusable,
        # and `_home` re-enters this on every one of its `HOME_TRIES` while
        # `_settled` is False — so returning here without setting it would spend
        # twenty full parks, minutes of swiping, on the one path that cannot
        # check a stop. The warning is in the log where a reader will find it.
        park_camera(self.adb, self.display, world)

    def _put_camera_back(self, world: World | None) -> None:
        """The settle, once a run, off whichever reading recognised the village.

        **Two callers because either reading can be the one that recognises
        it.** The plate row is the one that survives a drifted camera, which is
        why `_home` asks it first — but it misses villages of its own, a gem
        shower drifting over a plate among them, and those the storages read.
        Gated on the plate row alone, such a frame hands its caller a stock
        with the camera never put back, and every caller reads a returned stock
        as the measured coordinates being valid now. The settle would then land
        at whatever `_home` came next — usually `_opened` backing out partway
        through a walk — moving the map after the points that walk is spending
        were already chosen.
        """
        if not self._settled:
            self._settle_zoom(world)
            self._settled = True

    def _home(self) -> VillageStock | None:
        """The village's storages, once nothing is covering the village any more.

        The storage bars double as the check that the home village is up at all:
        `read_stock` answers None for every other screen, which is exactly what a
        tap that opened a barracks needs to be told.

        **`back` is only ever pressed on a frame that is not a clear village**,
        and that restriction is the whole reason this is not three lines.
        Measured live: on the home village `back` raises 確定退出遊戲嗎 — with a
        building menu open as readily as without — and that dialog is drawn as
        the same panel with 確定 in the same green in the same pixels as the one
        that pays for an upgrade. A frame that reads as nothing at all is
        therefore never a clear village, whatever else it turns out to be.

        **How long it is worth waiting on one depends on whether the game has
        finished starting**, and `_seen_village` is what says so. Before the
        first village reads, an unreadable frame is far more often a launch still
        under way than a panel, so it is waited on. After one has, a launch is
        over and the frame is a panel, so `back` goes in at once; see the
        constants above for what the old blanket patience cost a sweep.

        The two overlays are cleared rather than read past, because each dims
        the whole screen enough to stop the storage bars reading even though they
        stay perfectly legible to the eye. A dialog gets 取消 — any dialog, since
        the only one that can be standing here is that exit prompt and 取消 is
        the harmless answer to every other one the game raises too. A dropped
        session gets the game restarted; see `restart_game` for why not its own
        button.
        """
        for attempt in range(HOME_TRIES):
            png = self._frame("home")
            if idle_disconnected(png):
                logger.info("The session was dropped for idling; restarting the game")
                self.display = restart_game(self.adb, self.display)
                self._seen_village = False
                self._settled = False
                continue
            dialog = game_dialog(png)
            if dialog is not None:
                logger.info("A dialog is covering the village; answering 取消")
                self._tap(dialog.cancel)
                time.sleep(BACK_SETTLE)
                continue
            # Waited on whether or not a village has been seen: a session the
            # server dropped mid-run reloads from here, and a `back` pressed at
            # it is aimed at nothing. The patience is still `HOME_TRIES`, since
            # these loops are single passes measured in seconds; the attack
            # loop is the one that waits a whole outage out.
            if loading_screen(png):
                logger.info("The game is on its loading screen; waiting for it")
                time.sleep(LOAD_WAIT)
                continue
            # **The game reopens on whichever village it was closed on**, and
            # every loop that reaches this method is the home village's own —
            # its sweep grid, its building menus, its storages. Nothing below
            # could tell the difference: `read_stock` answers on the builder
            # base as readily, reading its gems bar as dark elixir, so a run
            # that arrived there would sweep the wrong map and report it as an
            # ordinary empty one. A night loop will want its own answer here;
            # until there is one, this method means the home village.
            #
            # **One crossing and then out**, because `cross` is already the
            # patient one: it swipes to the corner and tries three candidate
            # spots, about a minute in all. Left to `continue` on a failure this
            # loop would spend every one of its `HOME_TRIES` on another whole
            # crossing — twenty minutes against about fifty seconds for the
            # worst path here before this, and none of it interruptible, since
            # `_home` never reads the state file.
            world = current_world(png)
            if world == "night":
                logger.warning("The game came up on the builder base; sailing home first")
                if cross(self.adb, self.display, "day") == "day":
                    continue
                logger.warning("The crossing never landed; there is no home village to work on")
                return None
            # Before the read and not after it: see `_settle_zoom` for the
            # camera that makes the storages unreadable and for why nothing
            # below this line can recover from one.
            if world is not None and not self._settled:
                self._put_camera_back(world)
                continue
            stock = read_stock(png)
            if stock is not None:
                self._put_camera_back(world)
                self._seen_village = True
                return stock
            if not self._seen_village and attempt < LOADING_PATIENCE:
                time.sleep(LOAD_WAIT)
                continue
            self.adb.back(self.display)
            time.sleep(BACK_SETTLE)
        logger.warning("Never got back to a village that could be tapped")
        return None

    def _opened(
        self, points: Iterable[tuple[int, int]], label: str
    ) -> Iterator[tuple[tuple[int, int], bytes]]:
        """Tap each of these points on the village, handing back the frame each opened.

        Every point here is a raw village coordinate, so one that misses what it
        was aimed at opens whatever building is standing there — and a
        full-screen panel would swallow every tap after it. A barracks and a
        laboratory both do this. The storage bars are the test: a building menu
        leaves them readable and a full-screen panel does not, so a tap that
        covered the village is backed out of before the next one goes in, and
        the walk ends if the village cannot be got back at all.

        One walk for every caller, because every caller has the same problem: a
        sweep's grid point is a sample of whatever it lands on, a hand-named
        spot misses because the camera moved since somebody looked, and a
        spotted one misses because it was a guess. None of those is rare.

        **And one place to stop, for the same reason.** A tap costs a
        `MENU_SETTLE` and a capture, so a full sweep is a couple of minutes with
        nothing else to interrupt it — which is what `ai_coc upgrade` spends
        whenever there is no key to ask with or the finder comes back empty.
        Leaving here is safe in the way leaving a batch is not, because the walk
        has already backed out of whatever the last tap opened; `WallRunner._scan`
        made the same check at its own call site before this one existed, and
        still does, since it has a batch loop below the walk that this cannot see.
        """
        for spot in points:
            if self.should_stop():
                logger.info("Stop requested; ending the %s walk", label)
                return
            png = self._after_tap(spot, f"{label}_{spot[0]:04d}_{spot[1]:04d}")
            if read_stock(png) is None:
                logger.info("The tap at (%d, %d) covered the village; backing out", *spot)
                if self._home() is None:
                    return
                continue
            yield spot, png

    def _sweep(
        self, label: str, offset: tuple[int, int] = (0, 0)
    ) -> Iterator[tuple[tuple[int, int], bytes]]:
        """Tap across the village, handing back the frame each tap opened.

        This is the only way to find anything on the map without asking. Every
        building is its own artwork and gets repainted at each level, layouts
        differ between villages, and the camera moves — so what is there is
        discovered by tapping and reading, never by recognising or remembering.

        **The grid steps further than a small building is wide, so one pass is a
        sample rather than a search.** At `SWEEP_STAGGER` it lands between four
        of its own points, which is where whatever the first pass stepped over
        is: measured, 英雄殿堂 sits at (990, 430) with the nearest grid point 86
        px away and the staggered one 14 px away. A caller that has to find one
        particular building runs both, and pays for the second only when the
        first came back without it. A staggered point past `SWEEP_LIMIT` is
        dropped rather than clamped, because a clamped one lands on a point the
        other pass already covered.
        """
        yield from self._opened(
            (
                (x + offset[0], y + offset[1])
                for y in SWEEP_Y
                for x in SWEEP_X
                if x + offset[0] <= SWEEP_LIMIT[0] and y + offset[1] <= SWEEP_LIMIT[1]
            ),
            label,
        )

    def _spotted(self, what: str, notes: str, count: int) -> list[tuple[int, int]]:
        """Ask Gemini where these are on the village. Empty means nobody could say.

        **One finder for every loop here, because the answer is the same shape
        whatever was asked for.** What differs is the question and the check, and
        both stay with the caller: this returns points, and the caller taps each
        one and decides from what opened whether it was right. That division is
        what makes an imperfect finder usable at all — a wrong point costs one
        tap and one capture, about 1.7 s, so asking for more points than the run
        needs is the right shape rather than a waste.

        What it replaces is `_sweep`, which is the worst part of every loop that
        uses it: a blind grid, two and a half minutes, and a sample rather than a
        search. Measured against one live village, 12 of 12 answers opened a wall
        menu, the first of three answers for 英雄殿堂 opened the hall, and 9 of
        14 buildings had a priced upgrade on them — the whole call taking about
        10 s. **And it reaches ground the grid cannot**: of those nine, only two
        sat within 45 px of a grid point, while four were more than 75 px from
        one against buildings 70 to 120 px across.

        The sweep stays underneath all of it. A village whose walls nobody can
        place still has walls, and two and a half minutes of grid taps beats a
        run that buys nothing.
        """
        if self.ai is None:
            return []
        try:
            answer = self.ai.generate_structured(
                render("find_targets", what=what, notes=notes, count=count, floor=SPOT_FLOOR),
                ScreenSpots,
                self._frame("village"),
                SPOT_TIMEOUT,
            )
        except Exception:
            logger.warning("Gemini could not be asked where the targets are", exc_info=True)
            return []
        spots: list[tuple[int, int]] = []
        for spot in answer.spots:
            point = spot.pixels()
            # The prompt asks for this and the model obeys it, but a point in the
            # button row taps the previous selection's own buttons rather than
            # the map, and one that opens a shop is not something to find out by
            # trying. The instruction is the optimisation; this is the guarantee.
            if point[1] > SWEEP_LIMIT[1]:
                logger.info("Dropping (%d, %d): that is the button row, not the map", *point)
                continue
            spots.append(point)
        logger.info("Gemini put %d of %s at %s", len(spots), what, spots)
        return spots
