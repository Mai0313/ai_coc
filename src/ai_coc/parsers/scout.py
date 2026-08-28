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
# the only boundary that has to hold is the first one, because `_deploy` reads
# group 0 as the troops and flattens everything after it. Measured, cards sit
# 11 px apart inside a group, and across one the gaps are 34 (troops to siege),
# 16 (siege to heroes) and 25 (heroes to spells) — so the siege machine lands in
# with the heroes, which is exactly what the flattening is for. It only ever
# read as a group of its own while a seam was breaking the card beside it into
# pieces; see `CARD_SPAN`.
CARD_GROUP_GAP = 20
# Cards measure 109-112 px across, though a dark seam over one leaves an 87 px
# piece, so the floor stays low. Once the narrow fragments are gone every real
# card sits at least CARD_EDGE_GAP from its neighbour — which is what exposes
# the row's backing plate where it runs to the screen edge, since that abuts the
# first card instead of keeping a gap.
CARD_MIN_WIDTH = 80
CARD_EDGE_GAP = 5
# What one whole card spans, which is how a card broken in two is put back
# together. The strip is judged on a single averaged row of brightness, so a
# **dark band in a card's own artwork cuts that card in half** — and both halves
# can land under CARD_MIN_WIDTH, at which point the card is gone and nothing
# downstream can tell it was ever there. Measured live on a row of four heroes,
# the third came apart into 46 px and 63 px pieces and was dropped on every
# frame of a five-round run: all five battles reported "3 of 3 hero card(s)
# landed" while that hero sat in a card nothing knew about, and the five run
# afterwards reported 4 of 4. The first hero on the same row was cut the same
# way and survived only by luck, its remaining piece measuring 87.
#
# Two pieces are only joined where their combined span is one card wide, and
# only while neither is already wide enough to be a card on its own. Swept over
# 43 recorded frames a card at rest spans 105 to 112 px and every piece a seam
# leaves is 87 or less, so the floor sits between the two: a card that already
# reads is never joined to the speckle beside it, which on a battle frame is
# what would move its centre off the card and take every reader with it.
#
# The ceiling is that resting width rather than the 118 to 120 a **selected**
# card lights up to, because a card and the 11 px gap to its neighbour come to
# 121 and a ceiling reaching that far would join two cards into one. Nothing is
# selected when `card_groups` runs — `_deploy` reads the row before it taps
# anything — so the wider reading is out of its way.
CARD_SPAN = (100, 116)
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
# A real count's digits sit at 18 and 9 bits off their templates over this
# artwork while the scraps of it that survive read 31, so the line goes between.
COUNT_DIGIT_TOLERANCE = 30

# Freeze is the one spell CoC draws in cyan. Measured over the recorded rows, a
# freeze card's lower half reads (141, 224, 242) against rage's (163, 137, 201),
# and every other card that reaches this test sits at 145 of green or below.
#
# Cyan is high green **and** high blue, and only the green half of that used to
# be asked. What the missing half lets through is a spell that is merely green,
# and heal is one: its bottle is green with none of the blue, so it would read
# as freeze and be held back for the defences when what it is for is the troops
# — where a spell this call does not claim already goes. Measured, blue runs 242
# on freeze against at most 201 on everything else, so the line sits between.
# Heal itself has not been measured here, because this village has never flown
# one; the blue floor comes from what cyan is rather than from a sample of it,
# which is why it is set off freeze's own margin and not off a guess at heal's.
SPELL_ART_TOP, SPELL_ART_BOTTOM = 790, 860
SPELL_ART_HALF_WIDTH = 40
FREEZE_GREEN = 190
FREEZE_BLUE = 220

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
# read that as a bar — a hero reported as landed while it sits in its card, which
# is the exact failure this reader exists to catch. Swept over 126 recorded
# frames taken before anything had been deployed, across eleven battles and their
# themes, it called a hero landed on 52 of them; this test calls none.
#
# Blue is what separates them: measured over the two, the bar runs (101, 231, 9)
# and grass (131, 184, 53), so the bar is both greener and has almost no blue in
# it at all where grass keeps a third of a channel.
HERO_BAR_TOP, HERO_BAR_BOTTOM = 714, 738
HERO_BAR_HALF_WIDTH = 50
HERO_BAR_GREEN = 0.15
HERO_BAR_MIN_GREEN = 200
HERO_BAR_MAX_BLUE = 30

# The village's own storages, on the four bars down the home screen's right edge.
# Only the first three are read; the fourth is gems. The numbers are right-aligned
# against the icons, so the box reaches far enough left for eight digits and their
# separators, which is more than any storage holds.
#
# The tolerance was 22 on the reasoning that a real digit here matches within 11,
# and that turned out to be a measurement of one village rather than of the font:
# a gold row reading 10 649 867 matched every glyph but its 9, which came in at
# 25 and failed the whole row. It failed it on every frame, because the number
# does not change between them — so this was not a flicker to be retried, it was
# a village whose storages simply could not be read, and both callers treat that
# as "not now": the farming loop stops watching for a full storage and the wall
# loop cannot see what it has to spend.
#
# Swept over 48 live village frames the worst real glyph is that 25 and the rest
# sit at 15 or below, so the line goes above it. What the tolerance is not
# holding back is the other screens: measured over 18 frames of loading screens,
# the army screen and spend dialogs, not one produces a single glyph in any of
# these three boxes, so a screen that is not the home village reads as no screen
# whatever this is set to.
#
# The bars carry a gloss of their own along the top of the filled part, and it
# reaches into the row above the digits. Under the shared ink test it survives as
# a four-row blob, which is far too short to be a digit and would be thrown away
# for it — except that a blob landing in the gap between two digits joins them
# into one span too wide to be a glyph and too poor a match to be cut, and the
# whole row fails. Measured live on a village holding 130 480 dark, the gloss
# bridged the 1 and the 3 on every frame; every cut of that 24 px span left one
# half 33 bits or worse off its template, so the row read as nothing and `_home`
# spent five rounds pressing `back` at a village that was already up.
#
# The gloss is blue-grey, (143, 180, 203) give or take, where a digit is white —
# so what separates them is saturation rather than brightness, which the gloss
# clears by a handful of levels. Measured, the gloss reads 56 to 62 while all
# three rows of six recorded villages read the same number at every ceiling from
# 20 to 55, so the line goes between with room on both sides. Scoped to these
# rows because it is these bars the text is painted over; the loot panel and the
# army screen are painted over other things and keep the shared ceiling.
STOCK_LEFT, STOCK_RIGHT = 1300, 1512
STOCK_ROW_BOUNDS = ((33, 72), (117, 156), (200, 239))
STOCK_DIGIT_TOLERANCE = 30
STOCK_INK_SATURATION = 45

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
# It is the same line twice over in `_signature`: what a whole span has to reach
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
# What `_row_glyphs` reports for a span no width could make a digit of. Above
# every tolerance in this module rather than a flag, so each caller goes on
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


def _ink_mask(
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
    """Every 8-connected patch of ink in a mask, as the cells it holds."""
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


def _glyph_columns(mask: list[list[bool]], *, speckle: bool = True) -> list[tuple[int, int]]:
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


def _signature(
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


def _match(mask: list[list[bool]], left: int, right: int, floor: int) -> tuple[str, int] | None:
    """One column span as the digit it matches best and how far off that was."""
    signature = _signature(mask, left, right, floor)
    if signature is None:
        return None
    digit = min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
    return digit, (TEMPLATES[digit] ^ signature).bit_count()


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


def _row_glyphs(mask: list[list[bool]], floor: int = MIN_GLYPH_ROWS) -> Iterator[tuple[str, int]]:
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
    for left, right in _glyph_columns(mask):
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
    depends on what the text is painted over. `_ink_mask` is right for a number
    the game writes across a village and wrong for one written on a button's own
    plate, which `parsers.building` has to separate by colour instead.
    """
    digits = ""
    for digit, distance in _row_glyphs(mask, floor):
        if tolerance is not None and distance > tolerance:
            return None
        digits += digit
    return int(digits) if digits else None


def _read_row(
    image: Image.Image, box: tuple[int, int, int, int], tolerance: int | None = None
) -> int | None:
    """One storage bar's number, read off the bar the game paints it on.

    The tighter saturation ceiling belongs to those bars rather than to rows in
    general; see `STOCK_INK_SATURATION` for what it is holding back.
    """
    return digits_from(_ink_mask(image.crop(box), saturation=STOCK_INK_SATURATION), tolerance)


def _read_loot_row(image: Image.Image, box: tuple[int, int, int, int]) -> int | None:
    """One row of the loot panel, with the village showing through it dropped.

    The village only shows through to the right of the digits, so that is the one
    end a poor match can be dropped from. Dropping it there rather than failing
    the row is the opposite of `_read_row` and deliberately so: `read_scout`
    returning None means "no opponent on screen", so one speckle of village would
    leave the loop waiting out a search it had already paid for.

    A poor match with digits still to its right is different in kind: it is a
    digit this frame cannot read, and dropping it silently divides the number by
    ten. Measured on one opponent's gold row, the second 7 of 1 047 758 swung
    between 14 and 42 bits off its template from frame to frame, so four readings
    in ten came back as 104 758 — which a 500k threshold skips outright. The row
    fails instead, and the caller reads the next frame.
    """
    glyphs = list(_row_glyphs(_ink_mask(image.crop(box), LOOT_INK_BRIGHTNESS)))
    while glyphs and glyphs[-1][1] > LOOT_DIGIT_TOLERANCE:
        glyphs.pop()
    if any(distance > LOOT_DIGIT_TOLERANCE for _, distance in glyphs):
        return None
    digits = "".join(digit for digit, _ in glyphs)
    return int(digits) if digits else None


def _orange_ratio(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    data = image.crop(box).tobytes()
    orange = sum(
        data[i] > 190 and 110 < data[i + 1] < 205 and data[i + 2] < 110
        for i in range(0, len(data), 3)
    )
    return orange / (len(data) // 3)


def skip_offered(png: bytes) -> bool:
    """Whether 下一個 is on screen, which takes none of the loot digits to answer.

    `read_scout` says None both for 正在搜尋對手 and for an opponent whose loot
    panel this frame cannot read, and those want opposite things from a caller:
    the first is worth waiting out and the second is worth leaving. This is the
    only part of that screen that separates them, because it is read off one
    saturated orange rather than off the digits — the same test `can_skip`
    already uses, asked without needing a whole `ScoutView` to exist first.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    return _orange_ratio(image, NEXT_BUTTON_BOX) >= BUTTON_ORANGE


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


def _rejoined(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Pieces a dark seam cut one card into, put back together; see `CARD_SPAN`."""
    joined: list[tuple[int, int]] = []
    for left, right in spans:
        if (
            joined
            and joined[-1][1] - joined[-1][0] < CARD_SPAN[0]
            and right - left < CARD_SPAN[0]
            and CARD_SPAN[0] <= right - joined[-1][0] <= CARD_SPAN[1]
        ):
            joined[-1] = (joined[-1][0], right)
        else:
            joined.append((left, right))
    return joined


def card_groups(png: bytes) -> list[list[int]]:
    """Card centres in the battle row, split into the groups the game lays them out in.

    How many cards fall in each group depends on the army, so callers are meant
    to read the first group as the main troops and the rest as one-off drops,
    rather than trying to name which group is which.

    Only valid on a full row. A spent card greys out below the detection floor
    and the row fragments, at which point the battlefield visible past its ends
    reads as a card too.

    A card whose own artwork is dark enough to break the strip is put back
    together before anything is measured, because a piece narrow enough to be
    dropped takes the whole card with it; `CARD_SPAN` is what that costs and how
    it is judged.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    strip = image.crop((0, CARD_TOP, image.width, CARD_BOTTOM)).convert("L")
    columns = strip.resize((image.width, 1), Image.Resampling.BILINEAR).tobytes()
    pieces: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(image.width + 1):
        lit = x < image.width and columns[x] > CARD_LIT_BRIGHTNESS
        if lit and start is None:
            start = x
        elif not lit and start is not None:
            pieces.append((start, x))
            start = None
    spans = [span for span in _rejoined(pieces) if span[1] - span[0] >= CARD_MIN_WIDTH]
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

    The `x`'s width was the only check here, and it is not enough: what follows
    it was matched against the templates with no tolerance at all, so any scrap
    of card art left standing became a digit. Measured, a four-pixel sliver off
    the `x` matched a 1 at 31 bits and turned a card of twelve into one of a
    hundred and twenty-one.
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
        digit = min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
        if (TEMPLATES[digit] ^ signature).bit_count() > COUNT_DIGIT_TOLERANCE:
            return None
        digits += digit
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


def split_numbers(mask: list[list[bool]], tolerance: int) -> list[int]:
    """The numbers on a row, cut wherever a glyph is too poor a match to be one.

    What separates the two numbers of `305/305` or `1/5` is not a gap but a
    character that is not a digit at all, and it is found by how badly it
    matches rather than by being recognised: measured, every real digit on either
    of those rows lands within 18 of its template while the slash between them
    reads 25 on the army screen and 40 on the builder counter.
    """
    numbers: list[str] = [""]
    for left, right in _glyph_columns(mask):
        signature = _signature(mask, left, right)
        if signature is None:
            continue
        digit = min(TEMPLATES, key=lambda d: (TEMPLATES[d] ^ signature).bit_count())
        if (TEMPLATES[digit] ^ signature).bit_count() > tolerance:
            numbers.append("")
            continue
        numbers[-1] += digit
    return [int(value) for value in numbers if value]


def army_strength(png: bytes) -> tuple[int, int] | None:
    """Trained and total army size off the 我的軍隊 screen, or None if not on it.

    Checked before the attack is confirmed, because the search fee is charged
    after that and an army still being trained is not worth paying it for.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    mask = _ink_mask(image.crop(ARMY_BOX), ARMY_INK_BRIGHTNESS)
    found = split_numbers(mask, ARMY_DIGIT_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]


def freeze_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these spell cards hold freeze, the one spell worth holding back.

    Both halves of cyan are asked for; see `FREEZE_BLUE` for what the green one
    alone lets through.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    frozen: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - SPELL_ART_HALF_WIDTH,
            SPELL_ART_TOP,
            centre + SPELL_ART_HALF_WIDTH,
            SPELL_ART_BOTTOM,
        )).tobytes()
        pixels = len(data) // 3
        green = sum(data[i + 1] for i in range(0, len(data), 3)) / pixels
        blue = sum(data[i + 2] for i in range(0, len(data), 3)) / pixels
        if green > FREEZE_GREEN and blue > FREEZE_BLUE:
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
