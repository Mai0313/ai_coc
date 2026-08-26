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

import io
import logging

from PIL import Image, ImageChops

from ai_coc.models import ResourceBubble

# The digit reader and the mask it wants both live in `scout`, which owns the
# templates. Nothing here is worth a second copy of either.
from ai_coc.parsers.scout import _ink_mask, split_numbers

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)

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
# slash between the two numbers is not a digit and that is how it is found: swept
# over live frames every real digit matches within 14 while the slash reads 40,
# so anything over the line separates one number from the other.
BUILDER_BOX = (795, 33, 860, 66)
BUILDER_TOLERANCE = 22


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
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"採集標記座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
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


def free_builders(png: bytes) -> tuple[int, int] | None:
    """Idle builders over total, or None where this screen is not showing them.

    The game counts the ones standing around, not the ones at work, so zero here
    means every builder is busy and nothing new can be started.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"工人計數座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    found = split_numbers(_ink_mask(image.crop(BUILDER_BOX)), BUILDER_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]
