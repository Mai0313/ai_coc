"""Crossing between the game's two villages, which is a boat and nothing else.

There is no menu for this. The only way over is a boat moored at one corner of
each map, so the crossing is a tap at a place on the ground — and a place on the
ground is exactly what this project refuses to remember, because the camera
moves and what is under a coordinate changes with it.

**What makes a remembered spot safe here is that the camera clamps.** The game
will not pan past the edge of its own map, so swiping hard at one corner ends at
the same view whatever the camera was doing before; measured with `view_shift`, the
home village stops moving after the second swipe and the builder base after the
fourth, and every swipe after that reports (0, 0). From that view each boat sits
at one fixed pixel.

**Nothing here recognises the boat.** It is a sprite and the game dresses it up
for events, so it is tapped rather than looked for, and `parsers.world` is what
says whether the tap worked. A tap that landed on the water instead simply
leaves the world unchanged and the next candidate spot is tried. That is the
pattern `hero --at` already uses on a building that two taps in a row select
alternately.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import Crossing
from ai_coc.constants import COC_PACKAGE
from ai_coc.parsers.world import current_world

if TYPE_CHECKING:
    from ai_coc.models import World, DisplayTarget
    from ai_coc.adapters.adb import AdbController

logger = logging.getLogger(__name__)

# Keyed by the village being sailed **to**. Each drag starts mid-screen so
# neither end of it lands on the game's own button columns, and points away from
# the corner being revealed, because it is the map moving under a fixed finger.
CROSSINGS: dict[World, Crossing] = {
    # Leaving the home village: the boat is on the water off the bottom-left
    # shore. The candidates stay up and left of the measured spot on purpose —
    # the clan capital's own vessel is moored below and to the right of it, and
    # tapping that one opens a village this project has no business in.
    "night": Crossing(
        start=(500, 600), drift=(700, -350), spots=((400, 625), (382, 610), (416, 634))
    ),
    # Leaving the builder base: the boat is at the pier off the top-right. The
    # candidates stay left of the measured spot, because the game's right-hand
    # button column starts at x 1490 and overlaps the boat's stern.
    "day": Crossing(
        start=(1100, 300), drift=(-700, 350), spots=((1440, 545), (1415, 555), (1432, 522))
    ),
}
# The measured clamp plus slack: over-swiping a clamped camera costs a second and
# does nothing at all, while under-swiping leaves the boat off screen and the
# whole crossing fails.
SWIPES = 5
SWIPE_MS = 500
SWIPE_SETTLE = 1.2

# The crossing plays an animation and reloads the other village. Measured, one
# tap was answered four seconds later; this polls rather than sleeping the worst
# case, since a crossing that already happened costs nothing to notice early.
SAIL_POLLS = 8
SAIL_GAP = 1.5

# Every coordinate in this project was measured at the game's far zoom, and the
# swiping above leaves the camera at a corner whether or not the boat was found.
# Zooming out is what puts it back, and it centres the village as a side effect,
# so it goes in on both paths.
CROSS_ZOOM_PINCHES = 2


def cross(adb: AdbController, display: DisplayTarget, want: World) -> World | None:
    """Sail to `want`, and answer which village the game was left on.

    Already being there is the ordinary case and costs one capture: no swipe, no
    tap, nothing moved. None means no village could be read at all, which is not
    the same as being in the wrong one — a loading screen and a panel over the
    bars both land there — so the caller is meant to get to a village first
    rather than treat it as a failed crossing.
    """
    here = current_world(adb.screenshot(display))
    if here == want:
        return here
    if here is None:
        logger.warning("No village is on screen; there is nothing to sail from")
        return None
    logger.info("On the %s village and the %s one was asked for; sailing", here, want)
    crossing = CROSSINGS[want]
    landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
    for _ in range(SWIPES):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)
    try:
        for spot in crossing.spots:
            adb.tap(spot[0], spot[1], display)
            for _ in range(SAIL_POLLS):
                time.sleep(SAIL_GAP)
                if (arrived := current_world(adb.screenshot(display))) == want:
                    logger.info("Landed on the %s village from the boat at %s", want, spot)
                    return arrived
            logger.info("The tap at %s did not sail; trying the next spot", spot)
        logger.warning("None of the %d candidate spots found the boat", len(crossing.spots))
        return current_world(adb.screenshot(display))
    finally:
        adb.zoom("out", CROSS_ZOOM_PINCHES, COC_PACKAGE, display)
