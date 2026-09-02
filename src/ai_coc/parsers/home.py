"""Read the home village's own overlay: what is waiting to be collected, and who is free.

Like `parsers.building`, none of this looks at the village underneath. A
collector is repainted at every level and sits wherever the player put it, so
what is read is the marker the game floats above it — a rounded speech bubble
carrying the resource's own icon, drawn in the same pixels whatever it is
standing on and wherever that happens to be.

The markers are found by colour and then held to a size, which is what separates
them from the village. Two of the three are the icon itself, because a gold coin
and an elixir drop are both far more saturated than anything the ground is
painted in; dark elixir's is nearly black, so what is measured there is the
bubble's own orange plate instead, which nothing else on the map matches either.
"""

from __future__ import annotations

import logging

from PIL import Image, ImageChops

from ai_coc.models import BuildQueue, ResourceBubble
from ai_coc.parsers.frame import open_frame

# The digit reader and the mask it wants both live in `scout`, which owns the
# templates. Nothing here is worth a second copy of either.
from ai_coc.parsers.scout import _nearest, _ink_mask, _signature, split_numbers, _glyph_columns

logger = logging.getLogger(__name__)

# Where a marker can be. Outside this is the game's own furniture — the button
# columns down either side and the row along the bottom — and a marker cannot be
# under it because the furniture is drawn on top. The storage bars in the top
# right are the same, and they carry the very icons being looked for.
VILLAGE_AREA = (110, 80, 1480, 780)
STORAGE_BARS = (1260, 0, 1600, 330)

# What each marker is made of: the channel ranges its own pixels fall in, and the
# size the patch of them comes out at. Measured on live frames before and after
# the camera was dragged — the coin segments to 21x16, the drop to 18x25 and the
# orange plate to 38x41, each to within a pixel or two every time. The three
# differ because they are three different shapes, not because of the camera.
#
# **The size is doing most of the work.** Colour alone answers thirteen patches
# of gold and nine of magenta on a single village frame — storage bars, the shop
# button, spell factories, a burning building — and every one of them is either
# far larger or far smaller than a marker. Held to a size, what is left is
# exactly the markers on screen.
#
# `fill` is the tiebreak for the two that come closest: a marker's icon is a
# solid shape and fills half its own box or more, where the village patches that
# survive the size test are outlines and speckle at 0.32 and under.
MARKERS = (
    ("gold", ((236, 255), (181, 234), (0, 69)), (18, 24), (13, 20), 0.40),
    ("elixir", ((196, 255), (0, 114), (176, 255)), (16, 22), (21, 28), 0.50),
    ("dark", ((201, 255), (56, 129), (0, 74)), (35, 41), (37, 45), 0.38),
)

# 1/5 beside the builder's head, in the same white the storage bars use. The
# slash between the two numbers is not a digit and that is how it is found:
# swept over every recorded frame carrying this counter, a digit lands within 14
# of its template and the slash between 38 and 41, so anything over the line
# separates one number from the other.
#
# **The box was cut from a `1/5`, and `1` is the one digit narrow enough to fit
# it.** The number is centred, so a wide leading digit hangs off the left: a
# leading `0` inks from x 792 and the old edge at 795 took three columns off it,
# putting it 23 bits from its template against 5 with them restored. That is
# either side of the tolerance, so a village at `0/6` read as no counter at all.
# The leading digit is at its floor for any edge from 792 down to 774, and at
# 772 the counter plate's own frame (inked at 771 to 773) arrives as a fourth
# glyph; 783 is the middle of that range and leaves the 9 px of margin the right
# edge already had.
BUILDER_BOX = (783, 33, 860, 66)
# Midway between the worst digit that resolves confidently and the first reading
# that must not be believed. Two frames of one `0/5` counter read their `5` at 24
# against a `9` at 25, once each way round, so that pair is a coin flip: 24 is
# where a wrong answer starts, not where a right one ends. Nothing between 14 and
# 23 changes any recorded frame, so the line goes in the middle of that gap
# rather than at either edge of it.
BUILDER_TOLERANCE = 19

# Tapping that counter opens the panel listing every upgrade the village has
# running, and tapping it again closes it — the button toggles rather than
# opens, which is why the runner reads the panel before deciding it failed.
BUILDER_BUTTON = (745, 48)

# Each running upgrade gets a progress bar with its remaining time drawn over
# it, and **the bar is the only thing that says which rows those are**. The panel
# carries two more sections under them, 建議升級 and 其他升級, whose rows carry a
# price in the same place in the same white — so a reader going by the text alone
# would report a gold cost as a countdown. Nothing below the running rows has a
# bar. Nothing reads the names beside them either: they are Chinese, they are
# different in every village, and none of them is needed to answer when a
# builder comes free.
#
# Measured on live panels the bars share one column and one pair of colours: the
# unfilled track is a flat (47, 47, 47) and the filled part a bright green, and
# together they cover the column edge to edge. Rows sit 48 px apart, so anything
# within BAR_GAP of a bar already found is the same bar found again a row down.
BAR_LEFT, BAR_RIGHT = 870, 1010
BAR_TRACK, BAR_TRACK_SPREAD = 47, 12
BAR_COVERAGE = 0.8
BAR_GAP = 20
PANEL_TOP, PANEL_BOTTOM = 150, 700

# The time sits in the band directly above its own bar, right-aligned.
TIME_BOX = (860, 1015)
TIME_HEIGHT = 26
# A digit here matches its template within 16 while every unit character misses
# by 49 or more, so this line only has to sit between the two.
TIME_DIGIT_TOLERANCE = 30

# 天, 小時 and 分鐘, as the seconds one of each is worth. Only the **first**
# character of a unit is matched, which is what keeps this to three templates:
# 小 and 分 each lead a two-character unit, and the second unit on a row is
# always the next step down the ladder rather than something to be read.
#
# Measured across every recorded panel, one unit's own readings land within 16
# bits of each other while the nearest other unit is 49 away and the nearest
# digit 54, so the same 30 the digits use separates these too.
UNIT_TEMPLATES = {
    86400: 694176028518715082074175994823591921588995,
    3600: 1362459995062295920913326796806252659743,
    60: 98001493513730352222404855677119927554332,
}
UNIT_LADDER = (86400, 3600, 60, 1)


class _Patch:
    """One connected patch of pixels, grown a row at a time. Not a model: it
    lives for the length of one scan and never leaves this module.
    """

    __slots__ = ("bottom", "count", "left", "right", "top")

    def __init__(self, left: int, right: int, row: int) -> None:
        self.left, self.right, self.top, self.bottom = left, right, row, row
        self.count = right - left + 1

    def absorb(self, other: _Patch) -> None:
        self.left = min(self.left, other.left)
        self.right = max(self.right, other.right)
        self.top = min(self.top, other.top)
        self.bottom = max(self.bottom, other.bottom)
        self.count += other.count

    @property
    def middle(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def sized(self, width: tuple[int, int], height: tuple[int, int], fill: float) -> bool:
        across, down = self.right - self.left + 1, self.bottom - self.top + 1
        return (
            width[0] <= across <= width[1]
            and height[0] <= down <= height[1]
            and self.count / (across * down) >= fill
        )


def _mask(image: Image.Image, ranges: tuple[tuple[int, int], ...]) -> bytes:
    """The pixels inside all three channel ranges, one byte each.

    Built through `point`, which walks the image in C against a lookup table, so
    what Python is left to scan is a byte per pixel that is almost entirely zero.
    Testing three channels in Python instead cost 0.78 s a frame, which is longer
    than the tap it is deciding.
    """
    lit: Image.Image | None = None
    for band, (low, high) in zip(image.split(), ranges, strict=True):
        inside = band.point([255 if low <= value <= high else 0 for value in range(256)])
        lit = inside if lit is None else ImageChops.multiply(lit, inside)
    return lit.tobytes() if lit is not None else b""


def _runs(data: bytes, base: int, width: int) -> list[tuple[int, int]]:
    """The horizontal spans of lit pixels on one row of a mask.

    `bytes.find` skips the dark stretches in C, which is nearly all of every row.
    """
    spans: list[tuple[int, int]] = []
    x = 0
    while x < width:
        start = data.find(255, base + x, base + width)
        if start < 0:
            break
        end = start
        while end + 1 < base + width and data[end + 1] == 255:
            end += 1
        spans.append((start - base, end - base))
        x = end - base + 1
    return spans


def _patches(data: bytes, width: int, height: int) -> list[_Patch]:
    """Every connected patch in a mask.

    Row by row, joining each span to any span it touches on the row above,
    corners included, through a union-find — so a shape that closes back on
    itself comes out as one patch rather than two.
    """
    roots: dict[int, _Patch] = {}
    parent: list[int] = []

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    previous: list[tuple[int, int, int]] = []
    for row in range(height):
        current: list[tuple[int, int, int]] = []
        for start, end in _runs(data, row * width, width):
            index = len(parent)
            parent.append(index)
            roots[index] = _Patch(start, end, row)
            for before_left, before_right, before in previous:
                if before_left <= end + 1 and before_right >= start - 1:
                    keep, drop = find(before), find(index)
                    if keep != drop:
                        parent[drop] = keep
                        roots[keep].absorb(roots.pop(drop))
            current.append((start, end, index))
        previous = current
    return list(roots.values())


def _in_storage_bars(point: tuple[int, int]) -> bool:
    left, top, right, bottom = STORAGE_BARS
    return left <= point[0] <= right and top <= point[1] <= bottom


def collect_bubbles(png: bytes) -> list[ResourceBubble]:
    """Every collector marker standing on this frame, in reading order.

    A marker is what the game puts above a collector that has something waiting,
    and tapping one collects it — no menu, no confirmation. It disappears once
    collected, so a second read is what says whether a tap landed.
    """
    image = open_frame(png)
    left, top, right, bottom = VILLAGE_AREA
    village = image.crop(VILLAGE_AREA)
    width, height = right - left, bottom - top
    found: list[ResourceBubble] = []
    for resource, ranges, across, down, fill in MARKERS:
        for patch in _patches(_mask(village, ranges), width, height):
            if not patch.sized(across, down, fill):
                continue
            middle = (patch.middle[0] + left, patch.middle[1] + top)
            if not _in_storage_bars(middle):
                found.append(ResourceBubble(resource=resource, point=middle))
    return sorted(found, key=lambda bubble: (bubble.point[1], bubble.point[0]))


def _bar_tops(image: Image.Image) -> list[int]:
    """The top row of each progress bar in the panel's own column.

    Every fourth pixel is enough to tell a bar from anything else: it has to run
    the whole column, and nothing else in the panel does.
    """
    width = BAR_RIGHT - BAR_LEFT
    data = image.crop((BAR_LEFT, PANEL_TOP, BAR_RIGHT, PANEL_BOTTOM)).tobytes()
    wanted = len(range(0, width, 4)) * BAR_COVERAGE
    tops: list[int] = []
    for row in range(PANEL_BOTTOM - PANEL_TOP):
        base = row * width * 3
        covered = 0
        for offset in range(base, base + width * 3, 12):
            red, green, blue = data[offset], data[offset + 1], data[offset + 2]
            track = all(abs(value - BAR_TRACK) < BAR_TRACK_SPREAD for value in (red, green, blue))
            if track or (green > 140 and green - red > 40 and green - blue > 60):
                covered += 1
        y = PANEL_TOP + row
        if covered >= wanted and (not tops or y - tops[-1] > BAR_GAP):
            tops.append(y)
    return tops


def _remaining(image: Image.Image, bar_top: int) -> int | None:
    """The seconds written above one progress bar, or None where they will not read.

    A row reads as a number, a unit, and usually a second number in the next unit
    down — 9小時 23分鐘, or 1天 17小時. The second unit is never matched, because
    the game writes them in descending order and adjacent, so knowing the first
    settles it.
    """
    box = (TIME_BOX[0], bar_top - TIME_HEIGHT, TIME_BOX[1], bar_top)
    mask = _ink_mask(image.crop(box))
    numbers: list[int] = []
    digits = ""
    scale: int | None = None
    # Speckle is left in: half of what this row has to read is Chinese, and 小
    # is three short strokes that the digit reader's filter takes for noise.
    for left, right in _glyph_columns(mask, speckle=False):
        signature = _signature(mask, left, right)
        if signature is None:
            continue
        digit, distance = _nearest(signature)
        if distance <= TIME_DIGIT_TOLERANCE:
            digits += digit
            continue
        if digits:
            numbers.append(int(digits))
            digits = ""
        if scale is not None or len(numbers) != 1:
            continue
        # The first character after the first number is the one unit worth
        # matching; everything after it follows from the ladder.
        unit = min(
            UNIT_TEMPLATES, key=lambda seconds: (UNIT_TEMPLATES[seconds] ^ signature).bit_count()
        )
        if (UNIT_TEMPLATES[unit] ^ signature).bit_count() <= TIME_DIGIT_TOLERANCE:
            scale = unit
    if digits:
        numbers.append(int(digits))
    if scale is None or not numbers:
        return None
    below = UNIT_LADDER[UNIT_LADDER.index(scale) + 1]
    return numbers[0] * scale + (numbers[1] * below if len(numbers) > 1 else 0)


def builder_jobs(png: bytes) -> BuildQueue | None:
    """What the builder panel says is running, soonest first.

    None means the panel is not on screen at all, which is what a caller that
    tapped a button that toggles needs to be told apart from a village with
    nothing being built.

    A row whose time will not read is counted but left out of the times rather
    than guessed at, which is why both numbers are reported: the two disagreeing
    is worth seeing rather than hiding.
    """
    image = open_frame(png)
    tops = _bar_tops(image)
    if not tops:
        return None
    found = [_remaining(image, top) for top in tops]
    return BuildQueue(running=len(tops), remaining=sorted(s for s in found if s is not None))


def free_builders(png: bytes) -> tuple[int, int] | None:
    """Idle builders over total, or None where this screen is not showing them.

    The game counts the ones standing around, not the ones at work, so zero here
    means every builder is busy and nothing new can be started.

    **None costs each caller something different, and the cheap-looking one is
    the expensive one.** `UpkeepRunner.upgrade` and `HeroRunner` both stand the
    run down on it, which is loud and harmless. `WallRunner` does the opposite:
    its check is `counted is not None and counted[0] == 0`, so an unread counter
    silently never fires the "every builder is busy" branch and the run walks the
    rest of the village at forty seconds a wall, buying nothing. That is why the
    box above is sized to the widest number the counter can hold rather than to
    the one it happened to be showing when it was cut.
    """
    found = split_numbers(_ink_mask(open_frame(png).crop(BUILDER_BOX)), BUILDER_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]
