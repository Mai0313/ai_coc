"""Read the clan chat's donation requests, and the panel that opening one shows.

Same rule as everywhere else here: none of this looks at a troop. Every troop is
repainted at each level and the roster differs between town halls, so what is
read is the UI around them — the green button on a request card, the plate the
panel is drawn on, and whether a card is in colour or in greyscale.
"""

from __future__ import annotations

import logging

from ai_coc.parsers.frame import SCREEN_SIZE, open_frame
from ai_coc.parsers.regions import Patch, mask, patches

logger = logging.getLogger(__name__)

# 增援 sits on a request card in the chat panel, which scrolls, so it is found
# rather than remembered. Green picks it out on its own: measured over a live
# chat, the button is the only green patch in the panel above 1000 px and the
# next largest is 203 — a green word inside somebody's message. The input row
# along the bottom is left out because 友誼戰 down there is the same green and
# is not a donation.
CHAT_PANEL = (0, 80, 610, 810)
BUTTON_GREEN = ((0, 200), (150, 255), (0, 180))
MIN_BUTTON_AREA = 1000
# The chat has to be open before green means anything, because with it closed
# this box is the left half of a village and a village is mostly grass — swept
# over a home screen, that answers a 149 px "button" made of lawn. The panel
# itself is a flat dark sheet: measured, it fills 0.93 to 0.97 of this box, a
# village fills 0.17, and the dimmed screen behind the idle-disconnect dialog —
# the nearest thing to a false positive — fills 0.66. The floor is what keeps
# black from counting: a frame with nothing drawn on it yet is flat and dark by
# every measure, and the chat's own sheet runs around 70.
CHAT_DIM = (25, 130)
CHAT_FLAT = 34
CHAT_RATIO = 0.85
# Green is not enough on its own either. A friendly-challenge card carries 偵察
# in the same green and at the same size — measured, 5154 px against 增援's 4754,
# so "the biggest green" picks the wrong one — and tapping it opens somebody's
# village instead. What the challenge also carries is 進攻 in red, on the same
# row: measured, that button spans y 706-757 against 偵察's 705-757, while a
# request has no red on its row at all. So a green button with a red one beside
# it is a challenge and is left alone.
ATTACK_RED = ((170, 255), (0, 110), (0, 110))

# **The donation panel floats.** It is drawn against the request card that opened
# it, so its top moves with wherever the chat had scrolled to: measured over two
# live runs it started at y 166 once and y 109 the other, while coming out 721 px
# tall both times. Nothing below is a fixed y for that reason — everything is an
# offset from the top, found here first.
#
# This column runs down the panel's right margin and crosses no card, so the run
# of plate on it is the panel's own extent. Its height is what says a panel is
# what this is: on every other screen recorded the column has no such run at all.
PANEL_COLUMN = 1470
PANEL_HEIGHT = (700, 740)
PANEL_PALE = 195
PANEL_FLAT = 30

# The troop cards: two rows of seven, and the row scrolls further right. Nothing
# here scrolls it, because nothing needs to — the cards are laid out in unlock
# order, so the leftmost card the village can give is also the cheapest.
CARD_LEFT, CARD_PITCH, CARD_COLUMNS = 645, 114, 7
CARD_WIDTH = 100
# From the panel's top, not from the screen's.
CARD_ROWS = ((49, 179), (194, 324))
# A card the village cannot give is drawn in greyscale, and that is not a matter
# of degree: measured across a full panel, every card that could be given reads
# 52 to 101 of mean saturation and every one that could not reads 0.2.
CARD_SATURATION = 20


def chat_open(png: bytes) -> bool:
    """Whether the clan chat panel is drawn over the left of the screen."""
    data = open_frame(png).crop(CHAT_PANEL).tobytes()
    floor, ceiling = CHAT_DIM
    sheet = sum(
        floor < max(data[i], data[i + 1], data[i + 2]) < ceiling
        and max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2])
        < CHAT_FLAT
        for i in range(0, len(data), 3)
    )
    return sheet / (len(data) // 3) >= CHAT_RATIO


def reinforce_button(png: bytes) -> tuple[int, int] | None:
    """Where 增援 is on the topmost request in the chat, or None if nobody is asking.

    None also covers the chat not being open, and deliberately so: both mean
    there is nothing here to tap.
    """
    if not chat_open(png):
        return None
    left, top, right, bottom = CHAT_PANEL
    width, height = right - left, bottom - top
    panel = open_frame(png).crop(CHAT_PANEL)

    def buttons(ranges: tuple[tuple[int, int], ...]) -> list[Patch]:
        found = patches(mask(panel, ranges), width, height)
        return [patch for patch in found if patch.count >= MIN_BUTTON_AREA]

    reds = buttons(ATTACK_RED)
    for green in sorted(buttons(BUTTON_GREEN), key=lambda patch: -patch.count):
        if any(red.top <= green.bottom and red.bottom >= green.top for red in reds):
            logger.debug("Green button on a row with a red one; that is 偵察, not 增援")
            continue
        return green.middle[0] + left, green.middle[1] + top
    return None


def panel_top(png: bytes) -> int | None:
    """Where the 增援資源 panel starts down the screen, or None if none is up."""
    height = SCREEN_SIZE[1]
    column = open_frame(png).crop((PANEL_COLUMN, 0, PANEL_COLUMN + 1, height)).tobytes()
    found: int | None = None
    start: int | None = None
    for y in range(height + 1):
        offset = y * 3
        plate = y < height and (
            min(column[offset], column[offset + 1], column[offset + 2]) > PANEL_PALE
            and max(column[offset], column[offset + 1], column[offset + 2])
            - min(column[offset], column[offset + 1], column[offset + 2])
            < PANEL_FLAT
        )
        if plate and start is None:
            start = y
        elif not plate and start is not None:
            if PANEL_HEIGHT[0] <= y - start <= PANEL_HEIGHT[1]:
                found = start
            start = None
    return found


def donation_panel(png: bytes) -> bool:
    """Whether the 增援資源 panel is the thing on screen."""
    return panel_top(png) is not None


def donatable_cards(png: bytes) -> list[tuple[int, int]]:
    """Where to tap for each troop the village could give, cheapest first.

    Only the cards in colour. A greyed card is one the village cannot give —
    none trained, or the request already full of them — and tapping it does
    nothing at all, which is indistinguishable from a donation that failed.
    """
    origin = panel_top(png)
    if origin is None:
        return []
    image = open_frame(png)
    found: list[tuple[int, int]] = []
    for offset_top, offset_bottom in CARD_ROWS:
        top, bottom = origin + offset_top, origin + offset_bottom
        for column in range(CARD_COLUMNS):
            left = CARD_LEFT + column * CARD_PITCH
            data = image.crop((left, top, left + CARD_WIDTH, bottom)).tobytes()
            spread = sum(
                max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2])
                for i in range(0, len(data), 3)
            ) / (len(data) // 3)
            if spread >= CARD_SATURATION:
                found.append((left + CARD_WIDTH // 2, (top + bottom) // 2))
    return found


# The panel's own area, for telling one frame from the next. A donation does not
# reliably change the cards — a card holding twenty barbarians gives five away
# and still reads five — so what says one landed is the panel as a whole: the
# 已增援 row along the bottom gains an icon, and the counter at the top moves.
# Reading that counter directly is not on: it is written in near-black on the
# plate, and in that font the slash matches a digit template as closely as the
# digits do (measured 27, against 25 and 35 for the real ones), so nothing can
# cut one number from the other.
PANEL_SPAN = (620, 1500)
PANEL_TALL = 721


def panel_difference(before: bytes, after: bytes) -> int:
    """How many bytes of the donation panel differ between two frames."""
    origin = panel_top(after)
    if origin is None:
        return 0
    box = (PANEL_SPAN[0], origin, PANEL_SPAN[1], origin + PANEL_TALL)
    first = open_frame(before).crop(box).tobytes()
    second = open_frame(after).crop(box).tobytes()
    return sum(one != two for one, two in zip(first, second, strict=True))
