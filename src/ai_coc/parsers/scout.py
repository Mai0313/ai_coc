"""Read a scouted opponent's screen: the loot on offer and whether it can be skipped.

The scout screen only stands for 30 seconds before the game forces the battle to
start and takes the 下一個 button away, and every skipped opponent would cost
another call. Position, font and size are fixed by the 1600x900 layout, so the
digits are matched against templates here instead of going through Gemini.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
import logging

from PIL import Image

from ai_coc.models import LootOffer, ScoutView

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)

# The loot panel sits under the opponent's name, one row per resource.
PANEL_LEFT, PANEL_RIGHT = 74, 270
ROW_BOUNDS = ((126, 156), (173, 203), (220, 250))

# The game paints these buttons in one saturated orange that nothing behind them
# comes close to, so a box around either doubles as a check on which screen is
# up: measured, the button's own screen reads 0.49 and 0.63, every other 0.004.
BUTTON_ORANGE = 0.2
NEXT_BUTTON_BOX = (1380, 595, 1525, 668)
FIND_MATCH_BOX = (150, 635, 400, 695)

# A troop card keeps its artwork in colour while it still has something to put on
# the field and turns fully greyscale once it is spent. Measured across live
# frames, a spent card reads 0 and a live one 44 or more, which brightness alone
# does not separate at all.
CARD_TOP, CARD_BOTTOM = 760, 860
CARD_HALF_WIDTH = 45
CARD_SPENT_SATURATION = 10

# The row is laid out in groups — troops, siege machine, heroes, spells — and
# only the boundaries are dependable: measured, cards sit 11 px apart inside a
# group and 25 px or more apart across one.
CARD_GROUP_GAP = 20
# Cards measure 109-112 px across, though a dark seam over one leaves an 87 px
# piece, so the floor stays low. Once the narrow fragments are gone every real
# card sits at least CARD_EDGE_GAP from its neighbour — which is what exposes
# the row's backing plate where it runs to the screen edge, since that abuts the
# first card instead of keeping a gap.
CARD_MIN_WIDTH = 80
CARD_EDGE_GAP = 5
CARD_LIT_BRIGHTNESS = 60

# Troop and spell cards carry an `xN` count in their top-right corner; hero and
# siege cards do not. Measured, that corner reads 0.21 of its pixels as white or
# more on a counted card and at most 0.03 on an uncounted one. Reading the number
# itself is not reliable — on a card over a pale illustration the count merges
# into the artwork — but its presence is.
COUNT_TOP, COUNT_BOTTOM = 748, 772
COUNT_LEFT, COUNT_RIGHT = 6, 58
COUNT_WHITE_RATIO = 0.10
# The count itself is readable on a card with a dark, saturated illustration and
# not on a pale one, so `card_count` reports a failed read rather than a guess.
# It needs a higher floor than the loot panel: the card art is brighter than a
# village. `x` leads every count and is 16 px wide, which is the sanity check.
COUNT_INK_BRIGHTNESS = 225
COUNT_X_WIDTH = (13, 19)

# Freeze is the one spell CoC draws in cyan — rage, heal, clone and invisibility
# are all violet or pink. Measured, a freeze card's lower half reads
# (141, 224, 242) against rage's (163, 137, 201), so green separates them.
SPELL_ART_TOP, SPELL_ART_BOTTOM = 790, 860
SPELL_ART_HALF_WIDTH = 40
FREEZE_GREEN = 190

# The digits are near-white with a black outline; the village behind them is not.
INK_BRIGHTNESS = 200
INK_SATURATION = 70
# Bright background speckle passes that test but never spans a digit's height.
MIN_GLYPH_ROWS = 12

# Every glyph is normalised to this many pixels and compared as a bit pattern.
CELL_WIDTH, CELL_HEIGHT = 10, 14
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


def _ink_mask(band: Image.Image, brightness: int = INK_BRIGHTNESS) -> list[list[bool]]:
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
            row.append(high > brightness and high - low < INK_SATURATION)
        mask.append(row)
    return mask


def _glyph_columns(mask: list[list[bool]]) -> list[tuple[int, int]]:
    """Column spans of ink; the thousands separator is simply a wider gap."""
    width = len(mask[0])
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width + 1):
        inked = x < width and any(row[x] for row in mask)
        if inked and start is None:
            start = x
        elif not inked and start is not None:
            spans.append((start, x))
            start = None
    return spans


def _signature(mask: list[list[bool]], left: int, right: int) -> int | None:
    """Normalise one glyph to a CELL_WIDTH x CELL_HEIGHT bit pattern."""
    rows = [y for y, row in enumerate(mask) if any(row[left:right])]
    if len(rows) < MIN_GLYPH_ROWS:
        return None
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


def _read_row(image: Image.Image, top: int, bottom: int) -> int | None:
    mask = _ink_mask(image.crop((PANEL_LEFT, top, PANEL_RIGHT, bottom)))
    digits = ""
    for left, right in _glyph_columns(mask):
        signature = _signature(mask, left, right)
        if signature is None:
            continue
        digits += min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
    return int(digits) if digits else None


def _orange_ratio(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    data = image.crop(box).tobytes()
    orange = sum(
        data[i] > 190 and 110 < data[i + 1] < 205 and data[i + 2] < 110
        for i in range(0, len(data), 3)
    )
    return orange / (len(data) // 3)


def attack_menu_open(png: bytes) -> bool:
    """Whether the 多人遊戲 menu is up with its 尋找對戰目標 button.

    The attack loop opens with taps that only mean anything on the home village.
    An agent job that finished somewhere else would otherwise send them into
    whatever screen was left showing.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    return _orange_ratio(image, FIND_MATCH_BOX) >= BUTTON_ORANGE


def card_groups(png: bytes) -> list[list[int]]:
    """Card centres in the battle row, split into the groups the game lays them out in.

    How many cards fall in each group depends on the army, so callers are meant
    to read the first group as the main troops and the rest as one-off drops,
    rather than trying to name which group is which.

    Only valid on a full row. A spent card greys out below the detection floor
    and the row fragments, at which point the battlefield visible past its ends
    reads as a card too.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    strip = image.crop((0, CARD_TOP, image.width, CARD_BOTTOM)).convert("L")
    columns = strip.resize((image.width, 1), Image.Resampling.BILINEAR).tobytes()
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(image.width + 1):
        lit = x < image.width and columns[x] > CARD_LIT_BRIGHTNESS
        if lit and start is None:
            start = x
        elif not lit and start is not None:
            if x - start >= CARD_MIN_WIDTH:
                spans.append((start, x))
            start = None
    if len(spans) > 1 and spans[1][0] - spans[0][1] < CARD_EDGE_GAP:
        spans = spans[1:]
    groups: list[list[int]] = []
    for index, (left, right) in enumerate(spans):
        if index == 0 or left - spans[index - 1][1] > CARD_GROUP_GAP:
            groups.append([])
        groups[-1].append((left + right) // 2)
    return groups


def counted_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these cards show an `xN` count, which is to say troops or spells.

    Heroes and the siege machine are the ones without it, which is what lets the
    attack loop drop those and leave spells alone.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    counted: list[int] = []
    for centre in slots:
        data = image.crop((
            centre + COUNT_LEFT,
            COUNT_TOP,
            centre + COUNT_RIGHT,
            COUNT_BOTTOM,
        )).tobytes()
        white = sum(
            max(data[i], data[i + 1], data[i + 2]) > INK_BRIGHTNESS + 25
            and max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2])
            < 60
            for i in range(0, len(data), 3)
        )
        if white / (len(data) // 3) >= COUNT_WHITE_RATIO:
            counted.append(centre)
    return counted


def card_count(png: bytes, slot: int) -> int | None:
    """The `xN` on one card, or None when the artwork behind it swallows the digits.

    Lets a one-off drop be tapped as many times as the card actually holds
    instead of a fixed guess. A pale illustration merges into the count, and
    that shows up as an `x` glyph of the wrong width, so it reports the failure.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    band = image.crop((slot + COUNT_LEFT, COUNT_TOP, slot + COUNT_RIGHT, COUNT_BOTTOM))
    mask = _ink_mask(band, COUNT_INK_BRIGHTNESS)
    spans = [(a, b) for a, b in _glyph_columns(mask) if b - a > 3]
    if not spans or not COUNT_X_WIDTH[0] <= spans[0][1] - spans[0][0] <= COUNT_X_WIDTH[1]:
        return None
    digits = ""
    for left, right in spans[1:]:
        signature = _signature(mask, left, right)
        if signature is None:
            continue
        digits += min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
    return int(digits) if digits else None


def freeze_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these spell cards hold freeze, the one spell worth holding back."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    frozen: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - SPELL_ART_HALF_WIDTH,
            SPELL_ART_TOP,
            centre + SPELL_ART_HALF_WIDTH,
            SPELL_ART_BOTTOM,
        )).tobytes()
        green = sum(data[i + 1] for i in range(0, len(data), 3)) / (len(data) // 3)
        if green > FREEZE_GREEN:
            frozen.append(centre)
    return frozen


def live_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these card-row positions still have something left to deploy.

    Lets the attack loop keep emptying only the cards that are not done yet,
    rather than guessing a tap count that a bulk troop card would outlast.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    live: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - CARD_HALF_WIDTH,
            CARD_TOP,
            centre + CARD_HALF_WIDTH,
            CARD_BOTTOM,
        )).tobytes()
        spread = sum(
            max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2])
            for i in range(0, len(data), 3)
        )
        if spread / (len(data) // 3) > CARD_SPENT_SATURATION:
            live.append(centre)
    return live


def read_scout(png: bytes) -> ScoutView | None:
    """The opponent on screen, or None when this screenshot shows no opponent at all.

    Tapping 下一個 leaves the game on 正在搜尋對手 for a moment, and that screen
    has no digits where the panel would be, so a failed read is how the caller
    learns to keep waiting rather than a separate screen classifier.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"戰利品面板座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    gold, elixir, dark = (_read_row(image, top, bottom) for top, bottom in ROW_BOUNDS)
    if gold is None or elixir is None or dark is None:
        return None
    view = ScoutView(
        loot=LootOffer(gold=gold, elixir=elixir, dark=dark),
        can_skip=_orange_ratio(image, NEXT_BUTTON_BOX) >= BUTTON_ORANGE,
    )
    logger.info(
        "Scouted gold=%d elixir=%d dark=%d skippable=%s",
        view.loot.gold,
        view.loot.elixir,
        view.loot.dark,
        view.can_skip,
    )
    return view
