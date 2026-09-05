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
why the row's own reach is safe to lean on where the obvious candidates are not:
the shield's plate is the one that pushes the row out to its widest.

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

What is read is the little blue `i` badge the game floats over each plate — but
**how far the row reaches, not how many badges are in it.** The row is centred,
so a third plate pushes both ends outwards: measured over every frame on this
machine, the home village's badges sit at x 516, 719 and 933 and the builder
base's at 628-644 and 830-846. One visible badge therefore places the row, and
the 933 end is the shield's own, which the builder base cannot have at all.

**Counting them was wrong, and it was wrong in the direction that costs.** A
gem shower — the game's own reward animation — drifts across the top row and
covers a plate for a few seconds, which took the home village's count from three
to two and answered "builder base" for it. Swept over 561 recorded frames on
this machine, 35 read all three badges and 8 read fewer: 5 as `[516, 719]` with
the shield covered, and one each as `[933]`, `[516, 933]` and `[516]`. Six of
those eight the count called the wrong village outright, and the loop sailed
away from the village it was on. Reading the span leaves seven of the eight
correct, because an occluded badge shortens the row without moving the ends that
are still visible; the eighth is the lone `[516]`, which stays None for the
reason below.

A lone badge is not enough on its own: the loading screen puts one 20 px patch
of a character's blue tunic at x 509, seven pixels from where the home village's
leftmost really sits. So a single badge answers None unless it is the shield's,
which nothing else on any recorded frame reaches.

It is also **a better village test than `read_stock`**, which is the one it
replaces: two home frames had the camera at a map corner with the dark row
unreadable, so `read_stock` called them "not a village" while this reads them
correctly.

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
# x in the two. Nothing here reads a fixed position: the band is swept and
# whatever is found is reported with its place, which is what `current_world`
# reads the village off.
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

# Where the row starts tells the two apart, because it is centred and the home
# village has one plate more. Measured: the home village's leftmost badge sits at
# 516 on every frame here and the builder base's at 628 to 644, so the line goes
# between them with 60 px of room on each side — far more than the 16 px the
# builder base's own two readings differ by.
ROW_LEFT_SPLIT = 576
# The shield's own plate, and the one feature that needs no comparison: the
# builder base is real-time matchmaking against a live player, so it has no
# shield and its row never reaches this far. Measured, its rightmost badge tops
# out at 846.
SHIELD_BADGE_LEFT = 900


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
    underneath. Measured, all 515 of those on this machine read no badges at all,
    so None really does mean "not a village" rather than "a village I could not
    place".
    """
    badges = info_badges(png)
    if not badges:
        logger.debug("No village on screen; no plate badges in the top row")
        return None
    centres = [(left + right) // 2 for left, right in badges]
    if centres[-1] >= SHIELD_BADGE_LEFT:
        return "day"
    if len(centres) < 2:
        logger.debug("One badge at x=%d is not enough to place the row", centres[0])
        return None
    return "day" if centres[0] < ROW_LEFT_SPLIT else "night"
