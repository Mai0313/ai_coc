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

from pydantic import BaseModel, PrivateAttr

from ai_coc.models import VillageStock, DisplayTarget
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import AdbController, AdbControlError
from ai_coc.parsers.scout import read_stock, idle_disconnected
from ai_coc.parsers.building import game_dialog

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

# Getting back to the village, which four different things can be in the way of,
# and they do not want the same treatment.
#
# A game still loading wants waiting on and nothing else — measured, about twenty
# seconds from a cold launch — so nothing is pressed until the patience runs out.
# A panel a tap opened wants `back`. A dialog wants 取消. And a dropped session
# wants 重新登入遊戲, after which the game reloads from scratch.
HOME_TRIES = 20
LOADING_PATIENCE = 10
LOAD_WAIT = 2.0
BACK_SETTLE = 1.2
# Closing the game and opening it again, which is what the idle-disconnect
# dialog is answered with. How long to leave between the two, and how much of
# the reload to sit through before looking at the screen again.
RESTART_SETTLE = 2.0
RELOAD_WAIT = 15.0


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
    # Where to keep every frame the loop reads, for a run being studied afterwards.
    frame_dir: Path | None = None

    _captures: int = PrivateAttr(default=0)

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
        that pays for an upgrade. So a frame that reads as nothing at all is
        waited on rather than pressed at, since far more often than a panel it is
        the game still loading.

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
                continue
            dialog = game_dialog(png)
            if dialog is not None:
                logger.info("A dialog is covering the village; answering 取消")
                self._tap(dialog.cancel)
                time.sleep(BACK_SETTLE)
                continue
            stock = read_stock(png)
            if stock is not None:
                return stock
            if attempt < LOADING_PATIENCE:
                time.sleep(LOAD_WAIT)
                continue
            self.adb.back(self.display)
            time.sleep(BACK_SETTLE)
        logger.warning("Never got back to a village that could be tapped")
        return None

    def _sweep(self, label: str) -> Iterator[tuple[tuple[int, int], bytes]]:
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
        """
        for y in SWEEP_Y:
            for x in SWEEP_X:
                png = self._after_tap((x, y), f"{label}_{x:04d}_{y:04d}")
                if read_stock(png) is None:
                    logger.info("The tap at (%d, %d) covered the village; backing out", x, y)
                    if self._home() is None:
                        return
                    continue
                yield (x, y), png
