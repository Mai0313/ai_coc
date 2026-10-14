"""Village housekeeping: empty the collectors.

A job with nothing to do with fighting and everything to do with a village not
standing still. A collector fills up whether anyone is watching and then stops,
so a village left alone for hours was idle for most of them.

Collecting is the shortest loop in the project — tapping a marker collects it
outright, no menu and nothing to confirm.

Kept Qt-free like the rest of `ui/` outside the widgets, so `commands.py` can
drive it against the live game without a window.
"""

from __future__ import annotations

import time
import logging

from ai_coc.models import CollectReport, ResourceBubble
from ai_coc.ui.world import SWIPE_MS, SWIPE_SETTLE
from ai_coc.ui.runner import GameRunner
from ai_coc.parsers.home import collect_bubbles

logger = logging.getLogger(__name__)


# How far apart the taps go, and how long to leave the game to swallow them
# before looking again. Wider than the attack loop's spacing because nothing here
# is racing a battle timer, and a tap the game drops costs a collector.
COLLECT_GAP = 0.15
COLLECT_SETTLE = 2.0
# How many times to look again. A marker vanishes once it is collected, so a
# second read is what catches the taps the game did not take — and it stops as
# soon as a read comes back empty, which is the normal way out after one pass.
COLLECT_PASSES = 3
# The parked camera leaves one corner of the home village under the storage
# bars and above `VILLAGE_AREA`. Measured 2026-09-26: collectors in the top-right
# corner stood at y 35 to 200 with their dark markers behind the bars, and a pass
# found none of the six markers on screen. So the passes run again after this
# drag, the park's own direction reversed, which puts that corner in the open.
# It starts in the forest past the village's right edge, where no building can
# be under the finger.
FAR_CORNER_SWIPE = ((1380, 540), (820, 820))


def _one_per_kind(markers: list[ResourceBubble]) -> list[tuple[int, int]]:
    """The first marker of each resource, which is all a pass needs to tap.

    A tap on one marker collects every collector of its kind: measured
    2026-10-13, one elixir tap emptied all six elixir markers and left the dark
    ones. Tapping the rest landed on whatever stood under them once they were
    gone, and selected a wall.
    """
    kinds: dict[str, tuple[int, int]] = {}
    for marker in markers:
        kinds.setdefault(marker.resource, marker.point)
    return list(kinds.values())


class UpkeepRunner(GameRunner):
    """Runs the jobs a village needs doing that have nothing to do with fighting."""

    def collect(self) -> CollectReport:
        """Tap one collector marker of each resource, and report what the storages gained.

        The markers are re-read between passes rather than tapped from one list,
        because that read is the only evidence a tap landed: a collected marker
        disappears. The gains come from the storage bars for the same reason —
        counting markers would report a full storage as a haul.
        """
        report = CollectReport(outcome="nothing_to_collect")
        before = self._home()
        if before is None:
            report.outcome = self._lost()
            return report
        # The set of markers the last pass tapped. A pass that finds the same set
        # again made no progress, which is what a full storage looks like: the
        # game keeps drawing the marker because there is something in there, and
        # goes on refusing to move it. Without this the passes would all be spent
        # tapping it.
        tapped: set[tuple[int, int]] = set()
        for view in range(2):
            if view:
                if self._home() is None:
                    break
                logger.info("Dragging the camera into the far corner for the markers the park hid")
                self.adb.swipe(*FAR_CORNER_SWIPE, SWIPE_MS, self.display)
                time.sleep(SWIPE_SETTLE)
            for index in range(COLLECT_PASSES):
                # Every pass but the first re-checks the village first. An empty
                # read means "all collected" and "the screen is not a village any
                # more" equally, and one of those is worth recovering from —
                # measured, a run walked into the idle-disconnect dialog between
                # two passes.
                if index and self._home() is None:
                    break
                markers = collect_bubbles(self._frame("markers"))
                standing = {marker.point for marker in markers}
                if not markers or standing == tapped:
                    break
                logger.info(
                    "%d marker(s) waiting: %s",
                    len(markers),
                    "/".join(f"{m.resource}@{m.point[0]},{m.point[1]}" for m in markers),
                )
                self.adb.tap_many(_one_per_kind(markers), self.display, gap=COLLECT_GAP)
                report.markers += len(markers)
                tapped = standing
                time.sleep(COLLECT_SETTLE)
        # Through `_home` rather than a bare capture, for the storages. A marker
        # that turned out to be a building leaves its menu open, and this does
        # not close it: a building menu leaves the storage bars readable, and
        # `_home` only presses `back` on a frame whose storages will not read.
        after = self._home()
        if after is None:
            report.outcome = "stock_unread"
            return report
        report.gold = after.gold - before.gold
        report.elixir = after.elixir - before.elixir
        report.dark = after.dark - before.dark
        if report.markers:
            report.outcome = "collected"
        return report
