"""Where the fighting is on the battlefield, read from what moved between two captures.

A rage belongs on the troops, and by the time one is due the troops are nowhere
near where they were dropped. Measured over a recorded battle, the fighting
walked from (610, 305) five seconds in to (934, 456) a minute later — roughly a
rage footprint every fifteen seconds — so a point chosen while the plan was
being drawn is a whole bottle behind the army by the time it lands.

Nothing on screen names a troop. The game draws no health bar over an undamaged
one, and the units are smaller and far less distinct than the buildings they
walk among. What the fighting does do is change from moment to moment, where a
village under no attack barely moves at all, so two captures a moment apart
locate it without having to recognise anything in either of them.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw, ImageChops

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)
# The same ground the boundary reader looks at: below this the card row starts,
# and a card greying out is not the battle moving.
PLAYFIELD = (30, 105, 1570, 700)
# Painted over the scene rather than part of it, and all of it changes on its
# own: the loot panel counts down as loot is taken, our own storages count up,
# the battle clock ticks every second and 摧毀率 climbs. Left in, the four of
# them drag the reading towards whichever corner happens to be busiest.
UI_PANELS = (
    (0, 0, 340, 300),
    (1290, 0, 1600, 200),
    (600, 0, 1000, 110),
    (0, 620, 800, 720),
    (1300, 600, 1600, 720),
)

# How much a pixel has to change to count as having moved. Low enough for a
# troop crossing grass, high enough that the game's ambient shimmer — water,
# torches, flags — does not fill the mask on a village nobody is attacking.
MOTION_INK = 45
# What a believable reading looks like, measured over 124 recorded frames of
# three battles: a village with an army fighting through it moved 8k to 162k
# pixels, and leaving a battle moved 338k or more. Under the floor there is
# nothing on the field worth aiming at; over the ceiling the screen has changed
# wholesale and this is not a battlefield at all.
#
# The ceiling used to sit at 40k on the reasoning that a busy reading was an
# explosion rather than an army. A live run refused a perfectly good one at 123k
# that way — the army had just brought a row of walls down, which is exactly the
# moment a rage is for. What that reading needed was not to be thrown away but
# to be read differently; see `army_centre`.
MOTION_FLOOR = 1_500
MOTION_CEILING = 250_000

# The change is graded onto this grid, each cell 40 px across, and the busiest
# window of it is the answer. The window is 240x123 px, which is one rage
# footprint: the question being asked is where a bottle goes, so the patch worth
# finding is a bottle wide.
GRID = (40, 22)
WINDOW = (6, 3)

# Where `view_shift` looks and how far. The box is battlefield in the middle of
# the screen with none of the UI in it, and the limit leaves room for the crop to
# slide without running off the edges. Fifteen steps put a 110 px drag — the most
# the attack loop ever asks for — within 10 px, which is half what the answer is
# used for.
ALIGN_BOX = (450, 250, 1150, 620)
ALIGN_STEPS = 15
ALIGN_LIMIT = 1.4


def _moved(before: bytes, after: bytes) -> Image.Image:
    """The pixels that changed between two captures, with the UI blanked out."""
    first = Image.open(io.BytesIO(before)).convert("L")
    second = Image.open(io.BytesIO(after)).convert("L")
    for image in (first, second):
        if image.size != SCREEN_SIZE:
            raise ValueError(f"戰場判讀只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    mask = ImageChops.difference(first, second).point(lambda v: 255 if v > MOTION_INK else 0)
    blank = ImageDraw.Draw(mask)
    left, top, right, bottom = PLAYFIELD
    for box in (
        (0, 0, mask.width, top),
        (0, bottom, mask.width, mask.height),
        (0, 0, left, mask.height),
        (right, 0, mask.width, mask.height),
        *UI_PANELS,
    ):
        blank.rectangle(box, fill=0)
    return mask


def army_centre(before: bytes, after: bytes) -> tuple[int, int] | None:
    """The busiest patch of the battlefield, or None when nothing readable is happening.

    The busiest patch and not the middle of everything that moved, which is the
    whole of it: a base fires back across its entire width while the army works
    at one edge of it, so the average lands on the town hall. Measured on a live
    frame where a row of walls had just come down, the two disagreed by 260 px —
    the average sat in the middle of the village and the busiest window sat on
    the troops that had done it.

    The buildings coming down count as much as the troops themselves, and that
    is wanted rather than tolerated: it is where the army is doing its work.
    """
    mask = _moved(before, after)
    lit = mask.histogram()[255]
    if not MOTION_FLOOR <= lit <= MOTION_CEILING:
        logger.info("%d pixel(s) moved, which is no army; nothing to aim at", lit)
        return None
    # BOX averages each cell into one byte in C, so the search below walks 880
    # of them rather than 1.4 million pixels.
    grid = mask.resize(GRID, Image.Resampling.BOX).tobytes()
    across, down = GRID
    wide, tall = WINDOW
    best = (0, 0, 0)
    for top in range(down - tall + 1):
        for left in range(across - wide + 1):
            busy = sum(
                grid[(top + dy) * across + left + dx] for dy in range(tall) for dx in range(wide)
            )
            if busy > best[0]:
                best = (busy, left, top)
    if not best[0]:
        # Every cell rounded away, which is what a barely-lit mask spread thinly
        # over the whole playfield does: a cell is 1640 px, so three lit ones in
        # it average to less than half a level and BOX hands back a zero. Left
        # alone the search would then answer with its first window, which is a
        # corner of the map and the one place a rage certainly does nothing.
        logger.info("%d pixel(s) moved, too thinly spread to be an army", lit)
        return None
    centre = (
        round((best[1] + wide / 2) * SCREEN_SIZE[0] / across),
        round((best[2] + tall / 2) * SCREEN_SIZE[1] / down),
    )
    logger.info("%d pixel(s) moved, busiest around %s", lit, centre)
    return centre


def view_shift(before: bytes, after: bytes, drift: tuple[int, int]) -> tuple[int, int]:
    """How far the view really moved when the camera was dragged by `drift`.

    Measuring the village twice cannot do this. `village_box` trims to a profile
    of red that is as much village trim as boundary, so it wanders: on a live
    drag of 98 px the game took in full, the box said 23 while sliding one frame
    over the other said 100.

    A search along the drag rather than over the plane, because that is where the
    camera can have gone. It costs fifteen crops and answers (0, 0) for a drag
    the game swallowed — which is what a swipe becomes once a card is selected,
    and the one case where believing the drag would put every later coordinate
    somewhere the camera never went.
    """
    first = Image.open(io.BytesIO(before)).convert("L").crop(ALIGN_BOX)
    second = Image.open(io.BytesIO(after)).convert("L")
    best: tuple[float, tuple[int, int]] = (float("inf"), (0, 0))
    for step in range(ALIGN_STEPS + 1):
        moved = (
            round(drift[0] * step / ALIGN_STEPS * ALIGN_LIMIT),
            round(drift[1] * step / ALIGN_STEPS * ALIGN_LIMIT),
        )
        window = second.crop((
            ALIGN_BOX[0] + moved[0],
            ALIGN_BOX[1] + moved[1],
            ALIGN_BOX[2] + moved[0],
            ALIGN_BOX[3] + moved[1],
        ))
        apart = ImageChops.difference(first, window).histogram()
        best = min(best, (sum(i * v for i, v in enumerate(apart)), moved))
    return best[1]
