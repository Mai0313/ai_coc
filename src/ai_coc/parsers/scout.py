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

from ai_coc.models import LootOffer, ScoutView, VillageStock

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)

# The loot panel sits under the opponent's name, one row per resource. It is
# transparent, so the village behind it shows through to the right of the digits
# and its brighter speckles segment as glyphs of their own. The box cannot simply
# be pulled in tight against the text: loot runs to seven figures, and the widest
# reading measured ran to x 207, so this leaves room for that and no more.
PANEL_LEFT, PANEL_RIGHT = 74, 215
ROW_BOUNDS = ((126, 156), (173, 203), (220, 250))
# What is left of the village inside the box is dropped by how badly it matches:
# measured over 21 rows of live frames, all 115 real digits land within 28 bits
# of their template while the blobs read 42 and 57, so the line sits between.
LOOT_DIGIT_TOLERANCE = 35
# The dark row is dimmer than the other two and on the scout screen it peaks at
# 206, where the shared INK_BRIGHTNESS of 200 left almost none of it standing:
# the glyphs came out too short to measure, the row read as nothing, and the
# whole opponent was judged on dark=0.
LOOT_INK_BRIGHTNESS = 190

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
# Every real card carries its level in a badge at the bottom-left corner. The
# empty slot the row ends with does not: it is a dashed outline with the
# battlefield showing through, so it segments as a card of its own whenever the
# ground behind it is bright enough, and then arrives downstream as one more
# hero — which cost a run all five of that card's attempts, tapping nothing.
# Measured on a full row, every real card lights 0.15 of that badge or more and
# the empty slot lights none of it at all.
BADGE_LEFT, BADGE_RIGHT = -44, -4
BADGE_TOP, BADGE_BOTTOM = 838, 874
BADGE_BRIGHTNESS = 175
BADGE_LIT = 0.05

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

# `305/305` on the 我的軍隊 screen, which is the last point before the search fee
# is charged. The troop icon before it and the slash between the two numbers are
# not digits, and that is exactly how they are found: every real digit matches a
# template within ARMY_DIGIT_TOLERANCE (measured 12-18) while the slash reads 25
# and the icon 74, so anything over the line splits one number from the next.
ARMY_BOX = (700, 192, 880, 230)
ARMY_INK_BRIGHTNESS = 200
ARMY_DIGIT_TOLERANCE = 22

# What a drop that landed leaves behind, which is the only evidence the game
# gives that can be trusted. A counted card repaints its `xN` corner, and it does
# so even where the number itself will not read: measured live, a corner whose
# card lost something differs in 368 to 1139 of its pixels, and one whose card
# did not differs in exactly none, selecting the card included.
#
# The red banner used to stand in for this and cannot. Measured live, a troop
# tapped inside the boundary is as often swallowed in silence as it is answered
# with 你無法在紅線區域內派遣部隊, while 請選擇其他兵種, 已部署所有兵力 and 該法術
# 已用完 are the same red in the same place — and so is a burning building, which
# put four flanks in a row through a push they never needed.
CARD_CORNER_INK = 40
CARD_CORNER_PIXELS = 20

# A hero's card does not empty when the hero lands: it turns into the ability
# button and keeps its colour, so `live_cards` cannot tell one that went down
# from one still waiting. The health bar the game draws over the card can —
# measured, it fills 0.33 to 0.37 of this strip while a hero still in the card
# leaves at most 0.02. It takes a second or so to appear, so read it after a wait.
#
# The bar sits above the card row, which means the battlefield shows through this
# strip until the bar is drawn, and grass is green too. A brightness-and-hue test
# read it as a bar on every hero card of a village whose ground was bright, which
# is a hero reported as landed while it sits in its card — the exact failure this
# reader exists to catch. Blue is what separates them: measured over the two,
# the bar runs (101, 231, 9) and grass (131, 184, 53), so the bar is both greener
# and has almost no blue in it at all where grass keeps a third of a channel.
HERO_BAR_TOP, HERO_BAR_BOTTOM = 714, 738
HERO_BAR_HALF_WIDTH = 50
HERO_BAR_GREEN = 0.15
HERO_BAR_MIN_GREEN = 200
HERO_BAR_MAX_BLUE = 30

# The village's own storages, on the four bars down the home screen's right edge.
# Only the first three are read; the fourth is gems. The numbers are right-aligned
# against the icons, so the box reaches far enough left for eight digits and their
# separators, which is more than any storage holds. Behind them sits the bar's own
# fill highlight, which moves with the amount stored and is the reason for the
# tolerance: measured, a real digit here matches within 11 while the highlight
# never resolves into one at all.
STOCK_LEFT, STOCK_RIGHT = 1300, 1512
STOCK_ROW_BOUNDS = ((33, 72), (117, 156), (200, 239))
STOCK_DIGIT_TOLERANCE = 22

# The 回營 button on the battle result screen. It is the one screen a farming
# loop reliably ends on and the one it could not get off: the button only comes
# alive once the stars have finished flying in, so the single tap fired the
# moment the loot panel disappeared landed on nothing, and four runs in a row
# then found the village covered and stood down without attacking. Measured, the
# button fills 0.33 of this box in green against at most 0.05 of any other
# screen, the attack menu's own buttons included.
RETURN_HOME_BOX = (690, 738, 910, 796)
RETURN_HOME_GREEN = 0.15

# 還在嗎 / 你因閒置過久而中斷連線. A loop that spends minutes waiting for barracks
# will meet this, and nothing else clears it: the game stops responding to taps
# until 重新登入遊戲 is pressed. Measured, its flat grey panel fills 0.97 of this
# box where a village reads 0.14 and even the result screen only 0.45.
IDLE_DIALOG_BOX = (400, 340, 1200, 560)
IDLE_DIALOG_DARK = 0.7


def battle_over(png: bytes) -> bool:
    """Whether the battle result screen is up with its 回營 button waiting."""
    data = Image.open(io.BytesIO(png)).convert("RGB").crop(RETURN_HOME_BOX).tobytes()
    green = sum(
        data[i + 1] > 150 and data[i + 1] - data[i] > 45 and data[i + 1] - data[i + 2] > 60
        for i in range(0, len(data), 3)
    )
    return green / (len(data) // 3) >= RETURN_HOME_GREEN


def idle_disconnected(png: bytes) -> bool:
    """Whether the idle-disconnect dialog is covering the game."""
    data = Image.open(io.BytesIO(png)).convert("RGB").crop(IDLE_DIALOG_BOX).tobytes()
    panel = sum(
        max(data[i], data[i + 1], data[i + 2]) < 95
        and max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2]) < 30
        for i in range(0, len(data), 3)
    )
    return panel / (len(data) // 3) >= IDLE_DIALOG_DARK


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


def _row_glyphs(mask: list[list[bool]]) -> Iterator[tuple[str, int]]:
    """Each glyph on a row as the digit it matches best and how far off that was.

    The distance is what the two callers disagree about, so it comes back with
    the digit rather than being judged here.
    """
    for left, right in _glyph_columns(mask):
        signature = _signature(mask, left, right)
        if signature is None:
            continue
        digit = min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
        yield digit, (TEMPLATES[digit] ^ signature).bit_count()


def _read_row(
    image: Image.Image, box: tuple[int, int, int, int], tolerance: int | None = None
) -> int | None:
    """The number written across one row, or None where it does not read as one.

    `tolerance` gives up on the whole row as soon as one glyph is a poor match,
    which is what keeps a storage bar's fill highlight from being read as a
    digit, and is how a screen that is not the home village reads as no screen.
    """
    digits = ""
    for digit, distance in _row_glyphs(_ink_mask(image.crop(box))):
        if tolerance is not None and distance > tolerance:
            return None
        digits += digit
    return int(digits) if digits else None


def _read_loot_row(image: Image.Image, box: tuple[int, int, int, int]) -> int | None:
    """One row of the loot panel, with the village showing through it dropped.

    A poor match is skipped rather than failing the row, which is the opposite of
    `_read_row` and deliberately so: `read_scout` returning None means "no
    opponent on screen", so one speckle of village would leave the loop waiting
    out a search it had already paid for.
    """
    digits = "".join(
        digit
        for digit, distance in _row_glyphs(_ink_mask(image.crop(box), LOOT_INK_BRIGHTNESS))
        if distance <= LOOT_DIGIT_TOLERANCE
    )
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


def _badged(image: Image.Image, centre: int) -> bool:
    """Whether this slot carries a card's level badge, which the empty one does not."""
    data = image.crop((
        centre + BADGE_LEFT,
        BADGE_TOP,
        centre + BADGE_RIGHT,
        BADGE_BOTTOM,
    )).tobytes()
    lit = sum(
        max(data[i], data[i + 1], data[i + 2]) > BADGE_BRIGHTNESS for i in range(0, len(data), 3)
    )
    return lit / (len(data) // 3) >= BADGE_LIT


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
    spans = [span for span in spans if _badged(image, (span[0] + span[1]) // 2)]
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


def _corner(image: Image.Image, slot: int) -> bytes:
    return (
        image
        .crop((slot + COUNT_LEFT, COUNT_TOP, slot + COUNT_RIGHT, COUNT_BOTTOM))
        .convert("L")
        .tobytes()
    )


def card_drained(before: bytes, after: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these counted cards actually put something on the field.

    The `xN` corner is repainted whenever a card loses one, which answers the
    question the loop keeps asking — did that drop land — without needing to read
    the number, and without believing a banner that means four different things.
    """
    first = Image.open(io.BytesIO(before)).convert("RGB")
    second = Image.open(io.BytesIO(after)).convert("RGB")
    drained: list[int] = []
    for centre in slots:
        moved = sum(
            abs(a - b) > CARD_CORNER_INK
            for a, b in zip(_corner(first, centre), _corner(second, centre), strict=False)
        )
        if moved >= CARD_CORNER_PIXELS:
            drained.append(centre)
    return drained


def field_units(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these cards have their hero alive on the field, by its health bar.

    A hero card is the one that says nothing otherwise: it stays lit and stays
    counted once the hero is down, because it has become the ability button.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    down: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - HERO_BAR_HALF_WIDTH,
            HERO_BAR_TOP,
            centre + HERO_BAR_HALF_WIDTH,
            HERO_BAR_BOTTOM,
        )).tobytes()
        green = sum(
            data[i + 1] > HERO_BAR_MIN_GREEN
            and data[i + 2] < HERO_BAR_MAX_BLUE
            and data[i + 1] - data[i] > 60
            for i in range(0, len(data), 3)
        )
        if green / (len(data) // 3) >= HERO_BAR_GREEN:
            down.append(centre)
    return down


def army_strength(png: bytes) -> tuple[int, int] | None:
    """Trained and total army size off the 我的軍隊 screen, or None if not on it.

    Checked before the attack is confirmed, because the search fee is charged
    after that and an army still being trained is not worth paying it for.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    mask = _ink_mask(image.crop(ARMY_BOX), ARMY_INK_BRIGHTNESS)
    numbers: list[str] = [""]
    for left, right in _glyph_columns(mask):
        signature = _signature(mask, left, right)
        if signature is None:
            continue
        digit = min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
        if (TEMPLATES[digit] ^ signature).bit_count() > ARMY_DIGIT_TOLERANCE:
            numbers.append("")
            continue
        numbers[-1] += digit
    found = [value for value in numbers if value]
    if len(found) != 2:
        return None
    return int(found[0]), int(found[1])


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
    gold, elixir, dark = (
        _read_loot_row(image, (PANEL_LEFT, top, PANEL_RIGHT, bottom)) for top, bottom in ROW_BOUNDS
    )
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


def read_stock(png: bytes) -> VillageStock | None:
    """The village's own storages, or None when this screenshot is not showing them.

    Anything other than the home village reads as None, as does a home village
    with a panel over the bars, so a caller is meant to treat it as "not now"
    rather than as an empty village. Three rows all resolving into digits is
    itself the evidence that the home screen is up.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"儲量條座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    gold, elixir, dark = (
        _read_row(image, (STOCK_LEFT, top, STOCK_RIGHT, bottom), STOCK_DIGIT_TOLERANCE)
        for top, bottom in STOCK_ROW_BOUNDS
    )
    if gold is None or elixir is None or dark is None:
        return None
    stock = VillageStock(gold=gold, elixir=elixir, dark=dark)
    logger.info("Village holds gold=%d elixir=%d dark=%d", stock.gold, stock.elixir, stock.dark)
    return stock
