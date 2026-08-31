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

from ai_coc.models import VillageStock, DisplayTarget
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import AdbController, AdbControlError
from ai_coc.parsers.scout import read_stock, idle_disconnected
from ai_coc.parsers.world import current_world
from ai_coc.parsers.building import game_dialog

from .world import cross

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# How long a menu takes to open. Measured against this emulator by walking the
# menus by hand: a capture taken 0.8 s after the tap already had the new menu on
# it, and this leaves room for the emulator being busy.
MENU_SETTLE = 1.0

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
# How many pinches to spend putting the camera back at the far zoom. `view`
# measured one as covering the whole range and a second as doing nothing, so
# this is that plus a spare — three seconds against a run that spends minutes
# sweeping the village.
ZOOM_PINCHES = 2


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


class GameRunner(BaseModel):
    """Captures, taps, and getting back to a village that can be tapped."""

    adb: AdbController
    display: DisplayTarget
    # Asked between whole units of work, never inside one, for the reason
    # `AttackRunner` has the same field: a batch abandoned halfway leaves the
    # game on a screen the next run has to dig itself out of. What a unit is
    # belongs to each loop — a wall batch here, an opponent there.
    should_stop: Callable[[], bool] = lambda: False
    # Where to keep every frame the loop reads, for a run being studied afterwards.
    frame_dir: Path | None = None

    _captures: int = PrivateAttr(default=0)
    # Whether a village has ever read on this runner, which is what says the game
    # has finished starting. Cleared when the game is restarted, since that is the
    # one moment a cold launch can be under way again.
    _seen_village: bool = PrivateAttr(default=False)

    def _tap(self, point: tuple[int, int]) -> None:
        self.adb.tap(point[0], point[1], self.display)

    def _frame(self, label: str) -> bytes:
        """One capture, kept on disk when the run is being recorded."""
        png = self.adb.screenshot(self.display)
        if self.frame_dir is not None:
            self._captures += 1
            (self.frame_dir / f"{self._captures:04d}_{label}.png").write_bytes(png)
        return png

    def _after_tap(self, point: tuple[int, int], label: str) -> bytes:
        self._tap(point)
        time.sleep(MENU_SETTLE)
        return self._frame(label)

    def _settle_zoom(self) -> None:
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

        Once per run, hung off the first village that reads, because that is the
        moment the loop knows it is looking at the village and before it has
        tapped anything on it. A pinch between wall batches would only be
        spending three seconds to confirm what this one already settled.
        """
        self.adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, self.display)

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
                continue
            dialog = game_dialog(png)
            if dialog is not None:
                logger.info("A dialog is covering the village; answering 取消")
                self._tap(dialog.cancel)
                time.sleep(BACK_SETTLE)
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
            # `_home` reads no stop flag.
            if current_world(png) == "night":
                logger.warning("The game came up on the builder base; sailing home first")
                if cross(self.adb, self.display, "day") == "day":
                    continue
                logger.warning("The crossing never landed; there is no home village to work on")
                return None
            stock = read_stock(png)
            if stock is not None:
                if not self._seen_village:
                    self._settle_zoom()
                self._seen_village = True
                return stock
            if not self._seen_village and attempt < LOADING_PATIENCE:
                time.sleep(LOAD_WAIT)
                continue
            self.adb.back(self.display)
            time.sleep(BACK_SETTLE)
        logger.warning("Never got back to a village that could be tapped")
        return None

    def _sweep(
        self, label: str, offset: tuple[int, int] = (0, 0)
    ) -> Iterator[tuple[tuple[int, int], bytes]]:
        """Tap across the village, handing back the frame each tap opened.

        This is the only way to find anything on the map. Every building is its
        own artwork and gets repainted at each level, layouts differ between
        villages, and the camera moves — so what is there is discovered by
        tapping and reading, never by recognising or by remembering.

        A tap that opened a screen rather than a menu is backed out of before the
        next one goes in, because everything after it would otherwise land
        somewhere on that screen. A barracks and a laboratory both do this. The
        storage bars are the test: a building menu leaves them readable and a
        full-screen panel does not.

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
        for y in SWEEP_Y:
            for x in SWEEP_X:
                spot = (x + offset[0], y + offset[1])
                if spot[0] > SWEEP_LIMIT[0] or spot[1] > SWEEP_LIMIT[1]:
                    continue
                png = self._after_tap(spot, f"{label}_{spot[0]:04d}_{spot[1]:04d}")
                if read_stock(png) is None:
                    logger.info("The tap at (%d, %d) covered the village; backing out", *spot)
                    if self._home() is None:
                        return
                    continue
                yield spot, png
