"""How far the battle camera moved, read by sliding one frame over the other.

Measuring the drag rather than measuring the village twice: `village_box` trims
to a profile of red that is as much village trim as boundary, and on a live drag
of 98 px the game took in full it reported 23. The search runs along the drag
rather than over the plane, because that is where the camera can have gone, and
it answers nothing at all for a drag the game swallowed — which is what a swipe
becomes once a card is selected.
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
    first = open_frame(before).convert("L").crop(ALIGN_BOX)
    second = open_frame(after).convert("L")
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
