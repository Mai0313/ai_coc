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

from PIL import ImageChops

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
