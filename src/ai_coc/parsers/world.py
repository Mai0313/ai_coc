"""Which of the game's two villages is on screen, read off the green + buttons in the corner.

The game keeps two villages and reopens on whichever one it was closed on, so no
command can assume which it is looking at. That matters more than it sounds:
`read_stock` answers with three numbers on the builder base as readily as on the
home village, because the builder base has no dark elixir and its gems bar sits
at exactly the y the dark row is read from — the gem count read as if it were
dark elixir. So every loop's own "am I on the village" test says yes in the
wrong world, and a sweep would walk the wrong map.

**What separates them is the shield.** Since the 2026-10 overhaul the column of
bars in the top right ends, on the home village, in a 護盾 bar under the gems
bar, each with a green + on its left; the builder base's column stops at its
gems bar, because it has no dark elixir and no shield, and structurally cannot
have one: the builder base is real-time matchmaking against another live player,
and a shield that stopped you being attacked there would contradict the mode's
own design. So the two villages put their + buttons at different heights — the
builder base's one where the home village draws its dark bar, the home
village's two lower down — and which heights carry one is the answer.

**The button is read as a green plate with a white cross on it, both at once.**
Either alone matched something else: the build panel's progress bars fill the
home village's gems box with green (0.68 of it), and the white settings and shop
pages fill the cross's strokes. Measured on every frame recorded on 2026-10-13
and every committed one, a real button reads 0.64 green and its strokes 1.00
white; the most anything else reaches on both at once is 0.34 green with no
cross (the hero upgrade sheet) and 0.29 green with a 0.40 cross (a builder base
button dimmed under a storage tooltip, which answers None like any other cover).

The old answer, the row of blue `i` badges over the builder, laboratory and
shield plates along the top, went away with those plates.

**None is the answer whenever the column is covered or moved**, and that is
wanted: the build panel shifts the whole column left, and the attack menu,
the army screen, a battle and the loading screen cover or replace it. None of
those is evidence about which village is underneath.

Nothing here reads the boat. The boat is the only way to cross between the two,
so it has to be *tapped*, but a boat is a sprite and the game dresses it up for
events; `ui.world` taps at it, or at the marker `parsers.home.boat_marker` finds
over it, and asks this module whether the tap worked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from ai_coc.parsers.frame import open_frame

if TYPE_CHECKING:
    from PIL import Image

    from ai_coc.models import World

logger = logging.getLogger(__name__)

# The middle of each + button's horizontal stroke: the builder base's gems bar,
# then the home village's gems bar and its shield bar under it. The bars are 62
# px apart and the buttons sit at their left ends, all at the same x.
NIGHT_GEMS_PLUS = 170
DAY_GEMS_PLUS = 233
DAY_SHIELD_PLUS = 296
PLUS_X = 1347
# The plate around the cross, and the cross's two strokes, measured from the
# middle above: the plate runs 1325-1368 across and 43 rows down, the vertical
# stroke is 1344-1350 and the horizontal one 1337-1357 by seven rows.
PLUS_PLATE = (-19, -18, 19, 20)
PLUS_UPRIGHT = (-2, -9, 3, 6)
PLUS_ACROSS = (-9, -2, 10, 3)
# Midway between what a real button reads and the most anything else reaches
# at once; see the module docstring for both sides.
PLUS_GREEN = 0.45
PLUS_CROSS = 0.8


def _share(image: Image.Image, y: int, offsets: tuple[int, int, int, int], green: bool) -> float:
    """How much of one box around a button is the plate's green, or the cross's white."""
    left, top, right, bottom = offsets
    data = image.crop((PLUS_X + left, y + top, PLUS_X + right, y + bottom)).tobytes()
    hits = 0
    for offset in range(0, len(data), 3):
        red, grn, blue = data[offset], data[offset + 1], data[offset + 2]
        if green:
            hits += grn >= 140 and grn - red >= 60 and grn - blue >= 90
        else:
            hits += min(red, grn, blue) >= 200 and max(red, grn, blue) - min(red, grn, blue) <= 20
    return hits / (len(data) // 3)


def plus_button(image: Image.Image, y: int) -> bool:
    """Whether the corner column draws a green + button with its cross at this height."""
    return (
        _share(image, y, PLUS_PLATE, green=True) >= PLUS_GREEN
        and _share(image, y, PLUS_UPRIGHT, green=False) >= PLUS_CROSS
        and _share(image, y, PLUS_ACROSS, green=False) >= PLUS_CROSS
    )


def current_world(png: bytes) -> World | None:
    """Which village this frame is showing, or None when it is not showing one.

    **None is a third answer rather than a failure**, and callers have to keep it
    apart from "the home village": a loading screen, a battle, a dialog, the
    build panel and the matchmaker all land here, and none of them is evidence
    about which village is underneath.

    **The shield's button is enough on its own, and the home gems button only
    with the builder base's absent.** The shield is the structural difference;
    the gems button's height is open map on the builder base, so it is believed
    alone only where nothing says builder base — which is what still names a
    home village whose shield button something drifted over.
    """
    image = open_frame(png)
    night = plus_button(image, NIGHT_GEMS_PLUS)
    if plus_button(image, DAY_SHIELD_PLUS) or (plus_button(image, DAY_GEMS_PLUS) and not night):
        return "day"
    if night:
        return "night"
    logger.debug("No village on screen; no + button in the corner column")
    return None
