"""Which of the game's two villages is on screen, counted off the top row of plates.

The game keeps two villages and reopens on whichever one it was closed on, so no
command can assume which it is looking at. That matters more than it sounds:
`read_stock` answers with three numbers on the builder base as readily as on the
home village, because the builder base has no dark elixir and its gems bar sits
at exactly the y the dark row is read from — measured, a builder base holding
10 152 gems reports `dark=410152`, the green `+` plate beside the number reading
as a leading 4. So every loop's own "am I on the village" test says yes in the
wrong world, and a sweep would walk the wrong map.

**What separates them is the shield.** The home village carries a 護盾 plate in
the row along the top; the builder base does not, and structurally cannot: the
builder base is real-time matchmaking against another live player, and a shield
that stopped you being attacked there would contradict the mode's own design.
That is a stronger guarantee than anything else on the screen offers, and it is
why the count below is safe to lean on where the obvious candidates are not.

**Two candidates were measured and thrown away, both for the same reason** — they
pass today on this account and would break silently on another one, or on this
one later:

- *The builder base's trophy plate.* It sits in the top-left corner where the
  home village shows a league badge, and this account's home village is
  unranked, so the box is empty there. Over 4364 recorded home frames the plate
  read on none of them, which looked conclusive and was not: the day the account
  is ranked, that box grows a number and every home village reads as a builder
  base.
- *Counting resource rows.* The home village's gems row does not resolve as
  digits at all — measured, 0 of 237 home village frames — so there is nothing
  to count, and a night world that ever gained dark elixir would break it anyway.

What is counted is the little blue `i` badge the game floats over each plate.
Three of them is the home village, two is the builder base. Measured: 233 real
home village frames all read 3 and 11 builder base frames all read 2, while
4360 frames of everything else — battles, dialogs, the matchmaker, loading —
read 0. It is also **a better village test than `read_stock`**, which is the
one it replaces: two of those home frames had the camera at a map corner with
the dark row unreadable, so `read_stock` called them "not a village" while this
read them correctly.

Nothing here reads the boat. The boat is the only way to cross between the two,
so it has to be *tapped*, but a boat is a sprite and the game dresses it up for
events; `ui.world` taps at it and asks this module whether the tap worked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from ai_coc.parsers.frame import open_frame

if TYPE_CHECKING:
    from ai_coc.models import World

logger = logging.getLogger(__name__)

# The badge is a rounded blue plate with a white `i` on it. Measured down a
# column through one, its plate runs (61, 149, 205) to (44, 101, 180) while the
# grass under the row reads (37, 68, 12) and the builder's own hat beside it
# (156, 121, 99) — so blue over red is what separates it, and by a long way.
#
# The row is laid out from the middle of the screen outwards and holds a
# different number of plates in each village, so every badge sits at a different
# x in the two. Nothing here reads a fixed position: the band is swept and what
# is found is counted.
BADGE_BAND = (12, 32)
BADGE_LEFT, BADGE_RIGHT = 380, 1300
BADGE_BLUE = 170
BADGE_OVER_RED = 100
BADGE_COVERAGE = 0.7
# The white `i` splits each badge into two runs of about 8 px with roughly 10 px
# between them, so the halves are rejoined before anything is counted. Measured,
# a whole badge spans 25 to 26 px and the nearest neighbouring badge is 175 px
# away, so nothing rejoins two badges into one.
BADGE_GAP = 14
BADGE_WIDTH = (20, 40)

# Three plates on the home village (boost, builders, shield) against two on the
# builder base (boost, builders). Anything else is not a village at all.
BADGES_PER_WORLD: dict[int, World] = {3: "day", 2: "night"}


def info_badges(png: bytes) -> list[tuple[int, int]]:
    """Where each plate's blue `i` badge sits along the top row."""
    # Raw bytes rather than `load()`, which is what every other parser here
    # does: three per RGB pixel, and typed as integers.
    band = open_frame(png).crop((BADGE_LEFT, BADGE_BAND[0], BADGE_RIGHT, BADGE_BAND[1]))
    width, rows = band.size
    data = band.tobytes()
    lit = [
        x + BADGE_LEFT
        for x in range(width)
        if sum(
            1
            for offset in range(x * 3, len(data), width * 3)
            if data[offset + 2] > BADGE_BLUE and data[offset + 2] - data[offset] > BADGE_OVER_RED
        )
        >= rows * BADGE_COVERAGE
    ]
    spans: list[tuple[int, int]] = []
    for x in lit:
        if spans and x - spans[-1][1] <= BADGE_GAP:
            spans[-1] = (spans[-1][0], x)
        else:
            spans.append((x, x))
    return [span for span in spans if BADGE_WIDTH[0] <= span[1] - span[0] + 1 <= BADGE_WIDTH[1]]


def current_world(png: bytes) -> World | None:
    """Which village this frame is showing, or None when it is not showing one.

    **None is a third answer rather than a failure**, and callers have to keep it
    apart from "the home village": a loading screen, a battle, a dialog and the
    matchmaker all land here, and none of them is evidence about which village is
    underneath. Measured, all 4360 of those read no badges at all, so None here
    really does mean "not a village" rather than "a village I could not place".
    """
    badges = info_badges(png)
    world = BADGES_PER_WORLD.get(len(badges))
    if world is None:
        logger.debug("No village on screen; %d plate badge(s) in the top row", len(badges))
    return world
