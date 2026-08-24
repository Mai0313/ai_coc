"""Read the deployment boundary the game draws around a village under attack.

The red stroke is the game's own answer to the question the attack loop keeps
paying troops to guess: how far out a drop has to land before it is accepted. It
is drawn for the whole battle rather than only while a card is selected, so one
screenshot is enough, and it is UI painted over the scene rather than part of it,
so a village theme changes the ground under it but not the stroke itself.

What comes back is a reach along a ray rather than a polygon. The loop never
needs to know the shape: it needs a line to spread troops along, and the furthest
crossing in a given direction is exactly the point to drop just outside of.
"""

from __future__ import annotations

import io
import math
import logging

from PIL import Image

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)
# The middle of the battle map at the camera every battle opens on.
VILLAGE_CENTRE = (800, 400)

# Measured on the stroke across four village themes: red sits between 130 and
# 215 while `red - max(green, blue)` runs 85 to 105, against -20 to -30 for the
# grass beside it. The upper red bound is what keeps the game's brighter oranges
# out — a wall highlight reads (255, 71, 0) and a fire (255, 140, 24), both of
# which clear the score but not the ceiling.
STROKE_RED = (125, 220)
STROKE_SCORE = 55

# Where a ray may look. Below the playfield the card row starts, and a stroke
# found there is a card, not a boundary.
PLAYFIELD = (30, 105, 1570, 700)
# The stroke is one to two pixels wide, so a ray steps a pixel at a time.
RAY_STEP = 1
# A ray crosses the stroke in a pixel or two and a wall in far more, which is
# what tells them apart once the colour test has let both through. It matters
# where the boundary lies outside the playfield: the ray then stops at the edge
# having never met it, and without this the last wall it grazed would be
# returned as the boundary and put the drop inside the village.
MAX_STROKE_RUN = 4


def _stroke(data: bytes, offset: int) -> bool:
    red, green, blue = data[offset], data[offset + 1], data[offset + 2]
    return STROKE_RED[0] <= red <= STROKE_RED[1] and red - max(green, blue) >= STROKE_SCORE


def boundary_reach(
    png: bytes | Image.Image, degrees: float, centre: tuple[int, int] = VILLAGE_CENTRE
) -> tuple[int, int] | None:
    """The outermost point of the boundary along one ray, or None if it never crosses.

    Outermost rather than first: the stroke is a closed stair-step, so a ray
    leaving the middle can cross it several times where the village juts out.
    Only the last crossing bounds the ground the loop may drop on.
    """
    image = png if isinstance(png, Image.Image) else Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"邊界判讀只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    # Raw bytes rather than a pixel accessor, the same way `parsers.scout` reads.
    data = image.tobytes()
    width = image.width
    left, top, right, bottom = PLAYFIELD
    dx = math.cos(math.radians(degrees))
    dy = math.sin(math.radians(degrees))
    furthest: tuple[int, int] | None = None
    run: list[tuple[int, int]] = []
    for step in range(RAY_STEP, 1200, RAY_STEP):
        x = round(centre[0] + dx * step)
        y = round(centre[1] + dy * step)
        if not (left <= x <= right and top <= y <= bottom):
            break
        if _stroke(data, (y * width + x) * 3):
            run.append((x, y))
            continue
        if run and len(run) <= MAX_STROKE_RUN:
            furthest = run[-1]
        run = []
    if run and len(run) <= MAX_STROKE_RUN:
        furthest = run[-1]
    return furthest


# A reading far short of what its neighbours found is a wall the ray grazed on
# its way out, not the boundary — the colour test alone cannot separate the two,
# because a wall's edge highlight is as thin as the stroke. The boundary is one
# closed curve, so the readings belong to it only if they sit near each other,
# and this is the fraction of the middle reading below which one is dropped.
OUTLIER_RATIO = 0.6


def boundary_line(png: bytes, degrees: list[float], margin: int = 24) -> list[tuple[int, int]]:
    """Points just outside the boundary along each of these rays, worst readings dropped.

    `margin` pushes each point clear of the stroke itself, since the boundary is
    the last refused tile rather than the first allowed one.

    This is a better opening guess than a fixed flank, not a guarantee: a ray
    whose boundary lies past the playfield edge never meets it at all, and the
    deploy loop keeps probing and pushing out behind this for exactly that case.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    reaches = {angle: boundary_reach(image, angle) for angle in degrees}
    radii = sorted(
        math.hypot(point[0] - VILLAGE_CENTRE[0], point[1] - VILLAGE_CENTRE[1])
        for point in reaches.values()
        if point is not None
    )
    if not radii:
        logger.info("No boundary found on any of %d ray(s)", len(degrees))
        return []
    floor = radii[len(radii) // 2] * OUTLIER_RATIO
    points: list[tuple[int, int]] = []
    for angle, reach in reaches.items():
        if reach is None:
            continue
        if math.hypot(reach[0] - VILLAGE_CENTRE[0], reach[1] - VILLAGE_CENTRE[1]) < floor:
            logger.debug("Ray %.0f found %s, too far in to be the boundary", angle, reach)
            continue
        dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        points.append((round(reach[0] + dx * margin), round(reach[1] + dy * margin)))
    logger.info("Boundary read on %d of %d ray(s)", len(points), len(degrees))
    return points
