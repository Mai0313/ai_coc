"""How far the battle camera moved, read by sliding one frame over the other.

Measuring the drag rather than measuring the village twice: `village_box` trims
to a profile of red that is as much village trim as boundary, and on a live drag
of 98 px the game took in full it reported 23. The search runs along the drag
rather than over the plane, because that is where the camera can have gone, and
a drag the game swallowed — which is what a swipe becomes once a card is
selected — reads as (0, 0). **A pair it cannot make out at all is a third
answer, None**, because the two are opposite instructions to both callers.
"""

from __future__ import annotations

import logging

from PIL import ImageStat, ImageChops

from ai_coc.parsers.frame import open_frame

logger = logging.getLogger(__name__)

# Where `view_shift` looks and how far. The box is battlefield in the middle of
# the screen with none of the UI in it, and the limit leaves room for the crop to
# slide without running off the edges. Fifteen steps put a 110 px drag — the most
# the attack loop ever asks for — within 10 px, which is half what the answer is
# used for.
ALIGN_BOX = (450, 250, 1150, 620)
ALIGN_STEPS = 15
ALIGN_LIMIT = 1.4

# Where `pinch_zoomed` compares the frames either side of a pinch: the playfield
# between the HUD's bars and button columns, so nothing fixed on screen pulls the
# match back to no shift, at an eighth of the resolution, which shifts of
# hundreds of pixels do not need.
ZOOM_BAND = (150, 150, 1400, 750)
ZOOM_REDUCE = 8
# The shifts a pinch read as one finger can leave, in screen pixels. That finger
# travels 350 px sideways at `FULL_PINCH` and flings past it (412 to 416 px per
# pinch on 2026-11-17, #667), and against the map's edge the camera then runs
# along it: (264, 192) and (368, 64) on the same day, or only a few pixels into
# a corner. Searched as far as 1000 px, zooms of 2.5 matched better than their
# own picture at the far end, where only a fifth of the band still overlaps.
ZOOM_SHIFT_X, ZOOM_SHIFT_Y = 600, 240
# How much the picture has to change to have zoomed at all: a still builder base
# differs by 0.4 to 1.3, a pinch read as a drag into the map's corner by 0.4, and
# the smallest zoom swept below by 11.9, a pinch in recorded live by 21.9.
ZOOM_CHANGE = 5
# How much better than no shift the best shift may match before the change reads
# as a drag rather than a zoom. Recorded live on 2026-11-17: 32 pinches in the game
# read as one finger 0.08 to 0.22, and 9 pinches in from the far zoom 0.76 to
# 0.87. Over the 333 frames of both villages that read as one (`tests/frames`
# and the run frames kept that day): synthetic drags of 24 to 416 px, sideways
# and along the map's edge, at most 0.32, and zooms of 1.3 to 2.5 about the
# pinch's centre at least 0.59.
ZOOM_RATIO = 0.4
# How much a whole frame, reduced, may differ between two captures of one
# screen. A reward chest a tap apart differs by 0.1 to 0.5 (2026-11-17), two
# committed frames of one by 3.4, and the chest glowing open after its last tool
# by 7.0 to 7.3, which is why one more tap lands on it; the chest against its
# opening flash by 192, and against its first reward by 31 to 32.
SCREEN_REDUCE = 10
SCREEN_CHANGE = 12


def view_shift(before: bytes, after: bytes, drift: tuple[int, int]) -> tuple[int, int] | None:
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

    **None is the third answer, and it exists because the tie-break had to pick
    one and the two callers wanted opposite picks.** A box with nothing in it to
    compare scores every offset identically, and `min` over `(score, offset)` is
    lexicographic — so the winner was decided by the sign of the drag it was
    handed, and the two villages hand it mirrored drags. The same tie therefore
    read as the camera having arrived on the home village and as the largest
    step the search tries on the builder base: measured on a flat pair, the home
    village's drift answers (0, 0) and the builder base's (-392, 196), and on a
    flat *black* box the far end instead, (-980, 490), since there the padding
    matches. Neither is a reading. `park_camera` wants to keep swiping and
    `AttackRunner._pan` wants to leave its record alone, and both of those are
    what they already do with an answer they cannot use.

    Ties mean a near-flat box on everything recorded here: 41 of 90 committed
    frames tie on at least one drift and not one of them has more than four grey
    levels inside `ALIGN_BOX`, so exact equality is the whole test and no
    tolerance constant is wanted. **A true move landing midway between two
    search nodes ties as well**, on about 3 readings in 1152 over textured
    frames, and that is the price rather than a defect: it costs the park one of
    its ten swipes and `_pan` a warning.

    **The scores are a dict keyed by offset, and that is load-bearing rather
    than a container choice.** Two steps round to the same offset on a small
    drag — a drift of 5 rounds sixteen steps onto eight offsets — so a list of
    `(score, offset)` pairs would hold the winner twice and this tie test would
    read a perfectly good reading as unreadable. Measured against that shape, a
    true move of 1, 3 and 5 px answered None while 8 and 11 came back correctly,
    which is worst exactly where `_clear_flank`'s smallest drags live. The
    `continue` above only saves the redundant crops; the dict is what keeps the
    answer right.
    """
    first = open_frame(before).convert("L").crop(ALIGN_BOX)
    second = open_frame(after).convert("L")
    scored: dict[tuple[int, int], int] = {}
    for step in range(ALIGN_STEPS + 1):
        moved = (
            round(drift[0] * step / ALIGN_STEPS * ALIGN_LIMIT),
            round(drift[1] * step / ALIGN_STEPS * ALIGN_LIMIT),
        )
        if moved in scored:
            continue
        window = second.crop((
            ALIGN_BOX[0] + moved[0],
            ALIGN_BOX[1] + moved[1],
            ALIGN_BOX[2] + moved[0],
            ALIGN_BOX[3] + moved[1],
        ))
        apart = ImageChops.difference(first, window).histogram()
        scored[moved] = sum(i * v for i, v in enumerate(apart))
    best = min(scored, key=lambda offset: scored[offset])
    if sum(1 for score in scored.values() if score == scored[best]) > 1:
        logger.warning("Nothing in the align box to compare; how far the camera went is unread")
        return None
    return best


def screen_changed(before: bytes, after: bytes) -> bool:
    """Whether two captures show different screens, rather than one screen a moment apart."""
    first, second = (open_frame(png).convert("L").reduce(SCREEN_REDUCE) for png in (before, after))
    return ImageStat.Stat(ImageChops.difference(first, second)).mean[0] > SCREEN_CHANGE


def pinch_zoomed(before: bytes, after: bytes) -> bool:
    """Whether a pinch changed the scale, rather than nothing or only where the camera stands.

    The game sometimes reads every pinch as its first finger alone (#667),
    from right after a builder base battle until the next one, and pans the
    camera the way that finger drags: sideways, or along the map's edge once
    it meets it, or not at all against a corner. A zoom about the middle
    changes the picture in a way no one shift lines up, so a change some shift
    matches far better than none is that drag. Unlike `view_shift`, this weighs
    the best shift against no shift at all, searches the plane, and compares
    only the overlap, since a slide of 800 px leaves `ALIGN_BOX` nothing in
    common.
    """
    first = open_frame(before).convert("L").crop(ZOOM_BAND).reduce(ZOOM_REDUCE)
    second = open_frame(after).convert("L").crop(ZOOM_BAND).reduce(ZOOM_REDUCE)
    width, height = first.size

    def apart(dx: int, dy: int) -> float:
        left, right = max(0, -dx), width - max(0, dx)
        top, bottom = max(0, -dy), height - max(0, dy)
        window = second.crop((left + dx, top + dy, right + dx, bottom + dy))
        return ImageStat.Stat(
            ImageChops.difference(first.crop((left, top, right, bottom)), window)
        ).mean[0]

    still = apart(0, 0)
    if still < ZOOM_CHANGE:
        return False
    reach_x, reach_y = ZOOM_SHIFT_X // ZOOM_REDUCE, ZOOM_SHIFT_Y // ZOOM_REDUCE
    shifted = min(
        apart(dx, dy)
        for dx in range(-reach_x, reach_x + 1)
        for dy in range(-reach_y, reach_y + 1)
        if (dx, dy) != (0, 0)
    )
    return shifted >= ZOOM_RATIO * still
