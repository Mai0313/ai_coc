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

from PIL import Image, ImageDraw, ImageChops

from ai_coc.models import MapFrame

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)
# The middle of the battle map at the camera every battle opens on, and the one
# origin every other measurement here is taken from. Cast twelve rays out of it
# across an emptied village and the two clean opposite pairs came back 359/367
# and 361/359, so it is right to within a few pixels sideways; the vertical pair
# was 66 apart, which hints it belongs lower, but that was the home camera and
# one pair of rays is not enough to move a constant everything else is tuned to.
VILLAGE_CENTRE = (800, 400)

# Read off live frames by overlaying candidates until they sat on the ground's
# own edge, across the two themes with the most contrast against their
# surroundings. The camera does not move between battles, so this is a constant
# rather than something to detect; see `MapFrame` for what it is good enough for.
VILLAGE_GRID = MapFrame(centre=VILLAGE_CENTRE, half_width=675, half_height=337)
# Troops go down outside the grid as well as on it, so the ground the game will
# accept a drop on is wider than the grid the buildings sit in. The preset flanks
# are what measure it: `top_left` starts at (600, 110), well outside the grid and
# depositing troops long before any of this was read off the screen, and five
# tiles is the smallest margin that leaves it comfortably inside. It still keeps
# out the corner a hero was pushed into after four refusals, at (30, 175).
DEPLOY_BOUND = VILLAGE_GRID.grown(5)

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
# A ray stops at the playfield edge, and where the boundary lies beyond that edge
# it never meets it — so whatever red it grazed on the way, a wall or a building's
# trim, becomes its answer. Measured across two live surveys of twelve rays each,
# a crossing that matched the game sat between 0.62 and 0.99 of the distance the
# ray was able to travel, while every reading that turned out to be inside the
# village sat at 0.45 or less. Half is the gap between them, and a ray under it
# reports nothing rather than something wrong.
MIN_REACH_RATIO = 0.5


# Where the village sits on screen, taken from the same red stroke. Four pieces
# of UI are painted in the game's own red and would otherwise drag the box out to
# the screen edge: the loot panel, our resource bars, 結束戰鬥 and 摧毀率.
UI_PANELS = ((0, 0, 320, 280), (1320, 0, 1600, 200), (0, 620, 230, 712), (1320, 600, 1600, 712))
# The stroke is thin, so the profiles are taken over bands rather than whole
# columns: averaged over 600 rows a two-pixel crossing rounds away to nothing.
PROFILE_BANDS = 32
# What is left after the panels is a speckle of village trim, so the box is cut
# where this fraction of the stroke lies outside it rather than at its very last
# pixel. Measured over nine battles and both theme fixtures, that put the middle
# of the village within 35 px of the screen centre every time.
PROFILE_EDGE = 0.02
# A box far too small to be a village is a stray red button on a screen that has
# no boundary on it at all: the attack menu reads 217x67 that way. Measured, a
# real one spans 853 to 1166 across and 510 to 574 down.
MIN_VILLAGE_SPAN = (400, 250)


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
    reached = 0
    run: list[tuple[int, int]] = []
    for step in range(RAY_STEP, 1200, RAY_STEP):
        x = round(centre[0] + dx * step)
        y = round(centre[1] + dy * step)
        if not (left <= x <= right and top <= y <= bottom):
            break
        reached = step
        if _stroke(data, (y * width + x) * 3):
            run.append((x, y))
            continue
        if run and len(run) <= MAX_STROKE_RUN:
            furthest = run[-1]
        run = []
    if run and len(run) <= MAX_STROKE_RUN:
        furthest = run[-1]
    if furthest is None:
        return None
    radius = math.hypot(furthest[0] - centre[0], furthest[1] - centre[1])
    if radius < reached * MIN_REACH_RATIO:
        logger.debug(
            "Ray %.0f crossed at %.0f of %d travelled, too far in to be the boundary",
            degrees,
            radius,
            reached,
        )
        return None
    return furthest


def _trimmed(profile: list[int]) -> tuple[int, int] | None:
    """The span holding all but `PROFILE_EDGE` of a profile at each end."""
    total = sum(profile)
    if not total:
        return None
    cut = total * PROFILE_EDGE
    run = 0
    low = 0
    for index, value in enumerate(profile):
        run += value
        if run >= cut:
            low = index
            break
    run = 0
    high = len(profile) - 1
    for index in range(len(profile) - 1, -1, -1):
        run += profile[index]
        if run >= cut:
            high = index
            break
    return (low, high) if high > low else None


def village_box(png: bytes) -> tuple[int, int, int, int] | None:
    """The screen rectangle the game's own red line encloses, or None if it will not read.

    Nothing downstream should assume where the camera is pointing: `push_out`
    moves a drop away from the screen centre, the preset flanks are screen
    coordinates and the spell grid is spaced off the middle. Measured across nine
    battles and both theme fixtures the game opens every attack with the village
    already within 35 px of the centre — but reading it is what makes that a fact
    rather than an assumption, and a camera left anywhere else then shows up
    instead of quietly putting the whole army in the wrong place.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"村莊位置判讀只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    red, green, blue = image.split()
    score = ImageChops.subtract(red, ImageChops.lighter(green, blue))
    mask = ImageChops.multiply(
        score.point(lambda v: 255 if v >= STROKE_SCORE else 0),
        red.point(lambda v: 255 if STROKE_RED[0] <= v <= STROKE_RED[1] else 0),
    )
    blank = ImageDraw.Draw(mask)
    _left, top, _right, bottom = PLAYFIELD
    for box in ((0, 0, image.width, top), (0, bottom, image.width, image.height), *UI_PANELS):
        blank.rectangle(box, fill=0)
    width, height = image.size
    bands = mask.resize((width, PROFILE_BANDS), Image.Resampling.BOX).tobytes()
    columns = [sum(bands[row * width + x] for row in range(PROFILE_BANDS)) for x in range(width)]
    strips = mask.resize((PROFILE_BANDS, height), Image.Resampling.BOX).tobytes()
    rows = [sum(strips[y * PROFILE_BANDS : (y + 1) * PROFILE_BANDS]) for y in range(height)]
    across, down = _trimmed(columns), _trimmed(rows)
    if across is None or down is None:
        return None
    if across[1] - across[0] < MIN_VILLAGE_SPAN[0] or down[1] - down[0] < MIN_VILLAGE_SPAN[1]:
        logger.info("Red found on %s but far too small to be a village", (across, down))
        return None
    return (across[0], down[0], across[1], down[1])


# A reading far short of what its neighbours found is a wall the ray grazed on
# its way out, not the boundary — the colour test alone cannot separate the two,
# because a wall's edge highlight is as thin as the stroke. The boundary is one
# closed curve, so the readings belong to it only if they sit near each other,
# and this is the fraction of the middle reading below which one is dropped.
OUTLIER_RATIO = 0.6


def fitted_line(
    png: bytes, start: tuple[int, int], end: tuple[int, int], margin: int = 30
) -> tuple[tuple[int, int], ...] | None:
    """The same flank bent out to sit just outside this village's own red line.

    Three anchors rather than two, and that is the point: a village is a diamond,
    so a straight chord between two points on its boundary cuts back inside it
    across the middle. Fitting only the ends left every probe of the midpoint
    refused, which read as the whole flank being unusable — measured live, two
    flanks in a row were rejected at all five pushes that way before a third
    worked. The midpoint gets its own ray, so the line follows the village.

    None when any of the three rays never met the stroke, so the caller keeps the
    flank it had and probes its way out as before.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    middle = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
    moved: list[tuple[int, int]] = []
    radii: list[float] = []
    for point in (start, middle, end):
        angle = math.degrees(
            math.atan2(point[1] - VILLAGE_CENTRE[1], point[0] - VILLAGE_CENTRE[0])
        )
        reach = boundary_reach(image, angle)
        if reach is None:
            logger.info("No boundary on the ray through %s; keeping the preset flank", point)
            return None
        radii.append(math.hypot(reach[0] - VILLAGE_CENTRE[0], reach[1] - VILLAGE_CENTRE[1]))
        dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        moved.append((round(reach[0] + dx * margin), round(reach[1] + dy * margin)))
    # The boundary does not fold in on itself across one flank, so three anchors
    # at wildly different distances mean a ray matched a wall inside the village
    # rather than the stroke. `ai_coc probe` measured that happening on four of
    # twelve rays, and an anchor placed inside is the expensive direction to be
    # wrong in: the line cuts through the village and every probe on it is
    # refused. Falling back here costs only the preset flank the caller already
    # had, which is what this ran before it could read anything at all.
    if min(radii) < max(radii) * OUTLIER_RATIO:
        logger.info("Boundary radii %s disagree across the flank; keeping the preset", radii)
        return None
    logger.info("Flank %s-%s fitted to the boundary as %s", start, end, moved)
    return tuple(moved)


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
