"""Read a number the game wrote on the screen, by matching each digit to a template.

Four readers in this package need the same thing — the loot panel, the storage
bars, a building's price, a hero's price — and none of them can afford a vision
call for it: the scout screen expires in 30 seconds, and a wall run reads a
price once a batch. Position, font and size are all fixed by the 1600x900
layout, so a digit is normalised to a bit pattern and compared against ten of
them.

**This lived inside `parsers.scout` and three other modules reached into it by
private name** — `home` for five of them, `building` and `hero` for one each.
That is the shape of a shared engine that never got its own file: the module it
sat in is named for a screen, so every import of it read as one reader borrowing
from another rather than as four readers standing on one thing.

Nothing here knows what it is reading. What counts as ink depends on what the
text is painted over, so the mask is built by the caller and handed in:
`ink_mask` is right for a number written across a village and wrong for one on a
button's own khaki plate, which `parsers.building` separates by colour instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from collections.abc import Iterator

# The digits are near-white with a black outline; the village behind them is not.
INK_BRIGHTNESS = 200
INK_SATURATION = 70
# Bright background speckle passes that test but never spans a digit's height.
# It is the same line twice over in `signature`: what a whole span has to reach
# to be measured at all, and what an unbroken band of one has to reach before the
# rest of that span is taken for speckle and trimmed off.
MIN_GLYPH_ROWS = 12
# How tall a patch of ink has to be to be left in the mask at all. Lower than
# `MIN_GLYPH_ROWS` and deliberately so: this one throws ink away rather than
# giving up on it, and a digit that a frame has broken in half must survive to
# fail its row. `scout_smudged_digit` is one — its 0 comes apart into halves of
# about nine rows, and dropping either reads 1 746 707 back as 174 677 with
# nothing to show anything went wrong. Every speckle measured, the icon bleed
# included, is five rows or fewer, so the line sits between at eight.
SPECKLE_ROWS = 8

# Every glyph is normalised to this many pixels and compared as a bit pattern.
CELL_WIDTH, CELL_HEIGHT = 10, 14
# What one digit measures across. Swept over the recorded frames, every digit
# that read correctly spans 7 px — which is only ever "1" — to 17, so a span
# wider than that is two of them touching rather than one wide glyph. The floor
# is what keeps a cut from carving a sliver off the side of a real digit.
MIN_GLYPH_WIDTH = 7
MAX_GLYPH_WIDTH = 18
# What `row_glyphs` reports for a span no width could make a digit of. Above
# every tolerance in this package rather than a flag, so each caller goes on
# treating it exactly as it treats any glyph it cannot read.
NOT_A_GLYPH = 999
# A cut is only believed when both halves come out looking like real digits,
# because the village showing through the panel produces wide blobs too and
# splitting one of those invents a digit out of nothing. Measured, the 74 that
# needs cutting comes apart as 7 at 12 bits and 4 at 4, while the two blobs in
# the recorded frames come apart at 52/63 and 30/52. The line sits between them
# and nearer the blobs: a span left uncut is still judged on its own merits,
# where a wrong cut hands back a number nobody can tell is wrong.
SPLIT_TOLERANCE = 25
TEMPLATES = {
    "0": 0b00011111000111111110011111111111111111111111001111111100111111110011111111001111111100111111110011111111001111111111111101111111100001111000,
    "1": 0b01111111111111111111011111111100011111110001111111000111111100011111110001111111000111111100011111110001111111000111111100011111110001111110,
    "2": 0b00001111100111111111111111111110000011110000001111000001111100001111110111111100111111100011111000001111100000111111111111111111111111111111,
    "3": 0b01111100000111111110011111111000000111100000011110000001110000111111000011111111000001111100000011110001111111111111111011111111000100000000,
    "4": 0b00011111000001111100001111110000111111100111011110011101111001110111101111011110111111111011111111111111111110000001111000000111000000011100,
    "5": 0b01111111100111111110111111100011110000001111000000111111000011111111100011111111000001111100000111110001111110011111111001111110000110000000,
    "6": 0b00001111100111111110011111111001111000000111000000111100000011111111101111111111111111111111110001111111000111111111111101111111110011111110,
    "7": 0b11111111111111111111000001111100000111110000011110000011111000001111000001111100000111100000111110000011110000011111000001111000000111100000,
    "8": 0b00001111000111111110011111111001110011101111001110011100111001111111000111111110011111111111100011111110001111111100111101111111100111111110,
    "9": 0b01111111100111111110011111111011110011101111001111111111111111111111111111111111111111111100000011110000001110000111111001111111100111110000,
}


def ink_mask(
    band: Image.Image, brightness: int = INK_BRIGHTNESS, saturation: int = INK_SATURATION
) -> list[list[bool]]:
    """One row of text reduced to the pixels belonging to its digits."""
    width, height = band.size
    # Raw bytes rather than getdata(): three per RGB pixel, and typed as integers.
    data = band.tobytes()
    mask: list[list[bool]] = []
    for y in range(height):
        row: list[bool] = []
        for offset in range(y * width * 3, (y + 1) * width * 3, 3):
            high = max(data[offset], data[offset + 1], data[offset + 2])
            low = min(data[offset], data[offset + 1], data[offset + 2])
            row.append(high > brightness and high - low < saturation)
        mask.append(row)
    return mask


def _patches(mask: list[list[bool]]) -> Iterator[list[tuple[int, int]]]:
    """Every 8-connected patch of ink in a mask, as the cells it holds.

    Not `parsers.regions.patches`, which is the other connected-component scan
    in this package and answers a different question on a different input: that
    one walks a byte mask of a whole frame looking for coloured shapes, where
    this walks a boolean mask of one row of text looking for speckle to throw
    away. Merging them would mean converting a representation on every call to
    the one that runs per digit.
    """
    height, width = len(mask), len(mask[0])
    seen = [[False] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue
            seen[y][x] = True
            stack, cells = [(y, x)], []
            while stack:
                cy, cx = stack.pop()
                cells.append((cy, cx))
                for ny in range(max(cy - 1, 0), min(cy + 2, height)):
                    for nx in range(max(cx - 1, 0), min(cx + 2, width)):
                        if mask[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((ny, nx))
            yield cells


def glyph_columns(mask: list[list[bool]], *, speckle: bool = True) -> list[tuple[int, int]]:
    """Column spans of ink; the thousands separator is simply a wider gap.

    `speckle=False` is for a row that is not only digits: 小 and its like are
    drawn as separate short strokes, and dropping them is the difference between
    reading 9小時 23分鐘 and reading nothing. Only `parsers.home` wants it, and
    only because it matches those characters on purpose.

    Ink too short to be part of a digit is dropped before the columns are cut,
    and it has to be dropped **as a patch** rather than where it stands alone:
    `MIN_GLYPH_ROWS` already throws away a lone speckle, but one that merely
    shares a column with a digit joins whatever is on the other side of it into
    a single span, and nothing downstream can undo that. Measured live, the dark
    resource icon bled a five-row blob past the panel's left edge into the
    column beside the 1 of 10 428, the two came out as one glyph matching 3, and
    **the whole opponent read as no opponent**: the round never deployed, the
    countdown started the battle anyway, and the army was lost along with the two
    rounds spent tapping at a battle nothing recognised.
    """
    kept = [row.copy() for row in mask]
    if speckle:
        for cells in _patches(mask):
            rows = [y for y, _ in cells]
            if max(rows) - min(rows) + 1 >= SPECKLE_ROWS:
                continue
            for y, x in cells:
                kept[y][x] = False
    width = len(mask[0])
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width + 1):
        inked = x < width and any(row[x] for row in kept)
        if inked and start is None:
            start = x
        elif not inked and start is not None:
            spans.append((start, x))
            start = None
    return spans


def _tallest_band(rows: list[int]) -> list[int]:
    """The longest unbroken run of row numbers in a sorted list."""
    bands: list[list[int]] = [[rows[0]]]
    for y in rows[1:]:
        if y == bands[-1][-1] + 1:
            bands[-1].append(y)
        else:
            bands.append([y])
    return max(bands, key=len)


def signature(
    mask: list[list[bool]], left: int, right: int, floor: int = MIN_GLYPH_ROWS
) -> int | None:
    """Normalise one glyph to a CELL_WIDTH x CELL_HEIGHT bit pattern.

    A glyph is measured from its tallest unbroken band of rows rather than from
    its first inked row to its last. What lies outside that band is the panel's
    own furniture and the village showing through it, and it does not have to
    touch a digit to ruin it: two lit pixels four rows under the 9 of 297 906
    stretched the normalised cell by a quarter, and the glyph came out 51 bits
    off its template — failing a row that was perfectly legible, and with it the
    loot reading for a whole battle. Measured over 421 recorded frames, taking
    the band reads nine rows that failed outright and changes nothing that
    already read.

    A band shorter than a digit is not trimmed to, because there the short band
    *is* the digit and the speckle is what is left standing. `scout_smudged_digit`
    is one such: trimmed, its broken 0 drops out of the row without a trace and
    1 746 707 reads back as 174 677. Those fall through to the whole extent,
    which matches nothing and fails the row — which is the point of them.
    """
    rows = [y for y, row in enumerate(mask) if any(row[left:right])]
    if len(rows) < floor:
        return None
    band = _tallest_band(rows)
    if len(band) >= floor:
        rows = band
    glyph = Image.frombytes(
        "L",
        (right - left, rows[-1] + 1 - rows[0]),
        bytes(
            255 if mask[y][x] else 0
            for y in range(rows[0], rows[-1] + 1)
            for x in range(left, right)
        ),
    )
    bits = 0
    for value in glyph.resize((CELL_WIDTH, CELL_HEIGHT), Image.Resampling.BILINEAR).tobytes():
        bits = bits << 1 | int(value > 127)
    return bits


def nearest(pattern: int) -> tuple[str, int]:
    """The digit this bit pattern sits closest to, and how many bits off it is."""
    digit = min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ pattern).bit_count())
    return digit, (TEMPLATES[digit] ^ pattern).bit_count()


def _match(mask: list[list[bool]], left: int, right: int, floor: int) -> tuple[str, int] | None:
    """One column span as the digit it matches best and how far off that was."""
    pattern = signature(mask, left, right, floor)
    return None if pattern is None else nearest(pattern)


def _split(mask: list[list[bool]], left: int, right: int, floor: int) -> list[tuple[str, int]]:
    """Two digits the mask never separated, cut where both halves read best.

    Nothing guarantees a gap between two digits: measured on a live panel, the
    74 of 741 829 came through as a single 30 px span — 7 is 13 px wide and 4 is
    17, and they touch — which matched "3" at 47 bits. That row was then read as
    1 829, an opponent worth 741k skipped for being poor.

    Only cuts leaving both halves the size of a digit are tried — wide enough to
    be one, and no wider than one. The ceiling is what keeps three touching
    digits from being read as two: a 39 px run of 164 comes apart into a 6 at 24
    bits and a 4 at 14, both well inside the tolerance, and the row is then
    quietly 64. Twelve of the runs that can be built from the recorded digits do
    that, and most of them failed their row outright before splitting existed —
    so an unbounded cut turns a re-read into a wrong number, which is the trade
    this reader exists to refuse. Bounded, no cut through a run that wide leaves
    both halves small enough and the span fails whole. What still slips through
    is a narrow run containing a 1, since two touching 1s are 14 px and look like
    one digit; catching those needs a per-digit width rather than one ceiling.

    Of the cuts that qualify, the one whose worse half reads best wins. Scoring
    on the worse half rather than the total is what stops a cut leaving one
    excellent digit and one unrecognisable smear from beating an even one — and
    it is what separates two touching digits from a blob of village, which comes
    apart badly whichever way it is cut. Nothing at all comes back when no cut
    clears `SPLIT_TOLERANCE`, leaving the span to be judged whole as it was.
    """
    best: tuple[int, list[tuple[str, int]]] | None = None
    first = max(left + MIN_GLYPH_WIDTH, right - MAX_GLYPH_WIDTH)
    last = min(right - MIN_GLYPH_WIDTH, left + MAX_GLYPH_WIDTH)
    for cut in range(first, last + 1):
        halves = [_match(mask, left, cut, floor), _match(mask, cut, right, floor)]
        if None in halves:
            continue
        read = [half for half in halves if half is not None]
        worst = max(distance for _, distance in read)
        if worst <= SPLIT_TOLERANCE and (best is None or worst < best[0]):
            best = (worst, read)
    return best[1] if best else []


def row_glyphs(mask: list[list[bool]], floor: int = MIN_GLYPH_ROWS) -> Iterator[tuple[str, int]]:
    """Each glyph on a row as the digit it matches best and how far off that was.

    The distance is what the two callers disagree about, so it comes back with
    the digit rather than being judged here.

    **A span too wide to be a digit is not one, whatever it resembles.** One that
    no cut could be believed for used to be matched whole and handed back at
    face value, and a run of village that wide resembles something: measured on
    a live panel, 47 px of it came back as a 1 at 41 bits — over the tolerance,
    so the row survived — and the same run cleaned of its speckle came back as a
    1 at 33, under it, which turned a correct 505 into 5 051. The reading is
    still reported, because both callers already know what to do with a glyph
    that matches badly, but the distance says what the width does.
    """
    for left, right in glyph_columns(mask):
        if right - left > MAX_GLYPH_WIDTH and (halves := _split(mask, left, right, floor)):
            yield from halves
            continue
        match = _match(mask, left, right, floor)
        if match is not None:
            yield (match[0], NOT_A_GLYPH) if right - left > MAX_GLYPH_WIDTH else match


def digits_from(
    mask: list[list[bool]], tolerance: int | None = None, floor: int = MIN_GLYPH_ROWS
) -> int | None:
    """The number a mask of ink spells, or None where it does not read as one.

    `tolerance` gives up on the whole row as soon as one glyph is a poor match,
    which is what keeps a storage bar's fill highlight from being read as a
    digit, and is how a screen that is not the home village reads as no screen.

    `floor` is how tall a glyph has to be to be measured at all, and it is a
    parameter because **the game shrinks a number to fit its box**. Measured on
    a building menu, a five-figure price is drawn 16 px tall, a seven-figure one
    13 to 14, and an eight-figure one 12 — of which the digits with no ascender
    are 11. At `MIN_GLYPH_ROWS` every one of those short digits drops out, and
    what is left reads as a number: 10 400 000 came back as 14.

    The mask is handed in rather than built here because what counts as ink
    depends on what the text is painted over. `ink_mask` is right for a number
    the game writes across a village and wrong for one written on a button's own
    plate, which `parsers.building` has to separate by colour instead.
    """
    digits = ""
    for digit, distance in row_glyphs(mask, floor):
        if tolerance is not None and distance > tolerance:
            return None
        digits += digit
    return int(digits) if digits else None


def split_numbers(mask: list[list[bool]], tolerance: int) -> list[int]:
    """The numbers on a row, cut wherever a glyph is too poor a match to be one.

    What separates the two numbers of `305/305` or `1/5` is not a gap but a
    character that is not a digit at all, and it is found by how badly it
    matches rather than by being recognised: measured, every real digit on either
    of those rows lands within 18 of its template while the slash between them
    reads 25 on the army screen and 40 on the builder counter.
    """
    numbers: list[str] = [""]
    for left, right in glyph_columns(mask):
        pattern = signature(mask, left, right)
        if pattern is None:
            continue
        digit, distance = nearest(pattern)
        if distance > tolerance:
            numbers.append("")
            continue
        numbers[-1] += digit
    return [int(value) for value in numbers if value]
