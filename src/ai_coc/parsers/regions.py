"""Find the connected patches of one colour on a frame, and measure each of them.

The other half of what every reader in this package is built out of, beside
`parsers.glyphs`. Three of them want the same thing — a collector marker, the
green 確認 on an upgrade sheet, the 增援 button in the clan chat — and none of
them can look for the thing itself: a collector is repainted at every level and
stands wherever the player put it, so what is found is the marker the game
floats above it. A marker is a colour and a size, and that is all this answers.

**It lived inside `parsers.home` and two other modules imported it by private
name.** `home` is named for a screen, so `building` and `clan` reaching into it
read as one reader borrowing from another rather than as three readers standing
on one scan.

Colour alone is never enough, and what is enough differs for each of the three,
so nothing here decides: a caller gets every patch back and holds it to a shape
of its own. `parsers.home`'s `MARKERS` is where that costs the most and says
what it measured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import ImageChops

from ai_coc.models import Patch

if TYPE_CHECKING:
    from PIL import Image


def mask(image: Image.Image, ranges: tuple[tuple[int, int], ...]) -> bytes:
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


def patches(data: bytes, width: int, height: int) -> list[Patch]:
    """Every connected patch in a mask.

    Row by row, joining each span to any span it touches on the row above,
    corners included, through a union-find — so a shape that closes back on
    itself comes out as one patch rather than two.
    """
    roots: dict[int, Patch] = {}
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
            roots[index] = Patch.row(start, end, row)
            for before_left, before_right, before in previous:
                if before_left <= end + 1 and before_right >= start - 1:
                    keep, drop = find(before), find(index)
                    if keep != drop:
                        parent[drop] = keep
                        roots[keep].absorb(roots.pop(drop))
            current.append((start, end, index))
        previous = current
    return list(roots.values())
