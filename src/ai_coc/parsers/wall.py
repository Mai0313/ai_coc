"""Read the menu the game opens under a selected building, for the walls alone.

**Nothing here looks at a wall.** Every one of its levels repaints the wall
itself, every building is its own artwork, and the same goes for troops and
heroes elsewhere in the game — so a reader taught to recognise the thing selected
would need re-teaching at each level and again after any update that retextures
one. What is read instead is the UI the game paints on top of the village: the
resource icons on the button row and the price written beside them, which are
the same pixels whatever happens to be selected.

That leaves the walls identifiable by something no other building can do: being
upgradeable with gold *or* elixir. Everything else takes one resource, so a
button row carrying an elixir drop with a gold coin one pitch to its left, and
the same price written beside each, is a wall menu and nothing else is.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
import logging
import itertools

from PIL import Image

from ai_coc.models import WallMenu, GameDialog
from ai_coc.parsers.scout import digits_from

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)

# The button row the game opens under whatever is selected. Its buttons are laid
# out from the middle of the screen outwards, so not one of them sits at a fixed
# x: a batch with no walls left to add loses its 新增城牆+10 button, and every
# button after it moves a place along. Measured across both layouts the spacing
# does not move, so one located button locates the rest.
BUTTON_PITCH = 176
BUTTON_ROW_Y = 700
# Where a button carries its resource icon: this band across the row, and this
# far to the right of the button's own middle.
ICON_BAND = (636, 664)
ICON_OFFSET = 64
# A resource icon measures about 17 px across, so anything narrower is a speckle
# of the village showing through between two buttons.
MIN_ICON_WIDTH = 10
# How far off the pitch a coin may sit and still be the drop's own neighbour.
ICON_TOLERANCE = 8

# The price sits between a button's left edge and its icon. The plate behind it
# is a pale khaki — measured (237, 238, 220) — and the digits are either pure
# white or a flat red, so neither brightness nor saturation separates ink from
# plate on its own: white ink is 255 in all three channels where the plate never
# reaches 245 in its blue, and red ink runs (255, 137, 128), which no part of the
# plate comes near. Red is the game saying the village cannot afford this, and it
# is read rather than skipped: the loop does its own arithmetic, and a price it
# cannot read at all is one it cannot size a batch against.
PRICE_BOX = (-66, 634, 52, 666)
INK_WHITE = 245
INK_RED_LEVEL = 200
INK_RED_MARGIN = 80
# Swept over the recorded menus, every digit that read correctly landed within 24
# of its template, red and white alike.
PRICE_TOLERANCE = 30

# The confirmation the game raises before it spends anything, which is both the
# last chance to back out and the only signal that a tap on 升級 landed at all.
# Its panel is a flat near-white sheet over the middle of the screen: measured,
# this box reads 0.95 of it against 0.02 of a village and 0.17 of the army screen.
DIALOG_BOX = (500, 230, 1100, 330)
DIALOG_PALE = 0.6
DIALOG_PANEL = 195
DIALOG_FLAT = 22
# 確定 is the green button and 取消 the orange one, so hue alone tells them apart
# inside a panel that holds nothing else. The dialog is wider than the box above
# and runs down to about y 690 whatever it says, so the search covers the whole
# of it and stops short of the button row below. Measured, the two buttons land
# in the same place on every dialog the game raises: 確定 spans x 825-1122 and
# 取消 x 476-771, on the 升級城牆 confirmation and on 確定退出遊戲嗎 alike.
BUTTON_SPAN = (420, 1180)
BUTTON_BAND = (330, 720)
CONFIRM_GREEN = 200
CONFIRM_OVER_RED = 40
CONFIRM_OVER_BLUE = 80
CANCEL_RED = 200
CANCEL_GREEN = 90
CANCEL_OVER_GREEN = 60
CANCEL_OVER_BLUE = 120


def _is_gold(red: int, green: int, blue: int) -> bool:
    """The gold coin. Not enough on its own to find a button by: the wall ring on
    the next button along is gold too and answers this as readily, and that is
    the one button that must never be tapped.
    """
    return red > 200 and green > 150 and blue < 110


def _is_elixir(red: int, green: int, blue: int) -> bool:
    """The elixir drop, the one magenta on the row and so the anchor for the rest."""
    return red > 170 and blue > 170 and green < 130


def _icon_centres(image: Image.Image, test: Callable[[int, int, int], bool]) -> list[int]:
    """Where along the button row an icon this test recognises sits."""
    band = image.crop((0, ICON_BAND[0], image.width, ICON_BAND[1]))
    width, height = band.size
    data = band.tobytes()
    lit = [
        x
        for x in range(width)
        if any(test(*data[(y * width + x) * 3 : (y * width + x) * 3 + 3]) for y in range(height))
    ]
    centres = []
    for _, group in itertools.groupby(enumerate(lit), lambda pair: pair[1] - pair[0]):
        block = [value for _, value in group]
        if len(block) >= MIN_ICON_WIDTH:
            centres.append((block[0] + block[-1]) // 2)
    return centres


def _price_mask(band: Image.Image) -> list[list[bool]]:
    """One price reduced to its digits, told apart from the plate they sit on."""
    width, height = band.size
    data = band.tobytes()
    mask: list[list[bool]] = []
    for y in range(height):
        row: list[bool] = []
        for offset in range(y * width * 3, (y + 1) * width * 3, 3):
            red, green, blue = data[offset], data[offset + 1], data[offset + 2]
            row.append(
                min(red, green, blue) > INK_WHITE
                or (
                    red > INK_RED_LEVEL
                    and red - green > INK_RED_MARGIN
                    and red - blue > INK_RED_MARGIN
                )
            )
        mask.append(row)
    return mask


def _price(image: Image.Image, centre: int) -> int | None:
    """What the button centred here asks for, or None where it does not read."""
    left, top, right, bottom = PRICE_BOX
    return digits_from(_price_mask(image.crop((centre + left, top, centre + right, bottom))))


def wall_menu(png: bytes) -> WallMenu | None:
    """The wall menu on this frame, or None where the screen is not showing one.

    None covers every way of not being on a wall: no menu at all, a menu for
    something that takes a single resource, and a wall whose price this frame
    could not read. That they are one answer is deliberate — each of them leaves
    the loop with nothing it can safely tap, and a caller acting on the
    difference would be acting on a guess about a button it never located.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"城牆選單座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    coins = _icon_centres(image, _is_gold)
    for drop in _icon_centres(image, _is_elixir):
        if not any(abs(drop - BUTTON_PITCH - coin) <= ICON_TOLERANCE for coin in coins):
            continue
        elixir = drop - ICON_OFFSET
        gold = elixir - BUTTON_PITCH
        asked = [_price(image, gold), _price(image, elixir)]
        # Both have to read, and read the same. A wall is charged the same number
        # whichever resource pays, so two different numbers mean one of them was
        # misread — and a misread price is how a batch gets sized against money
        # the village does not have.
        if asked[0] is None or asked[0] != asked[1]:
            logger.debug("Row at %d carried both icons but priced %s", elixir, asked)
            continue
        return WallMenu(
            gold=(gold, BUTTON_ROW_Y),
            elixir=(elixir, BUTTON_ROW_Y),
            add=(gold - BUTTON_PITCH, BUTTON_ROW_Y),
            price=asked[0],
        )
    return None


def _is_confirm(red: int, green: int, blue: int) -> bool:
    """確定, the green button."""
    return (
        green > CONFIRM_GREEN
        and green - red > CONFIRM_OVER_RED
        and green - blue > CONFIRM_OVER_BLUE
    )


def _is_cancel(red: int, green: int, blue: int) -> bool:
    """取消, the orange one."""
    return (
        red > CANCEL_RED
        and green > CANCEL_GREEN
        and red - green > CANCEL_OVER_GREEN
        and red - blue > CANCEL_OVER_BLUE
    )


def _button_middle(image: Image.Image, test: Callable[[int, int, int], bool]) -> tuple[int, int]:
    """The middle of the one button on the dialog painted in this hue."""
    left, right = BUTTON_SPAN
    top, bottom = BUTTON_BAND
    band = image.crop((left, top, right, bottom))
    width = band.width
    data = band.tobytes()
    xs: list[int] = []
    ys: list[int] = []
    for offset in range(0, len(data), 3):
        if test(data[offset], data[offset + 1], data[offset + 2]):
            pixel = offset // 3
            xs.append(pixel % width)
            ys.append(pixel // width)
    return left + (min(xs) + max(xs)) // 2, top + (min(ys) + max(ys)) // 2


def game_dialog(png: bytes) -> GameDialog | None:
    """The yes/no dialog on this frame, with both buttons, or None when none is up.

    The panel is checked for before either button is looked for, because these
    hues are only unambiguous inside it: a village is mostly grass, and a caller
    tapping whatever green it found would be tapping the village.

    What comes back says nothing about *which* question is being asked, because
    nothing here can tell: 升級城牆 and 確定退出遊戲嗎 are the same panel with the
    same buttons in the same pixels. Only the caller knows what it just did, and
    so only the caller can know which button it means.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    panel = image.crop(DIALOG_BOX).tobytes()
    flat = sum(
        min(panel[i], panel[i + 1], panel[i + 2]) > DIALOG_PANEL
        and max(panel[i], panel[i + 1], panel[i + 2]) - min(panel[i], panel[i + 1], panel[i + 2])
        < DIALOG_FLAT
        for i in range(0, len(panel), 3)
    )
    if flat / (len(panel) // 3) < DIALOG_PALE:
        return None
    try:
        return GameDialog(
            confirm=_button_middle(image, _is_confirm), cancel=_button_middle(image, _is_cancel)
        )
    except ValueError:
        # `min` over an empty list: the panel is up but one of the two buttons
        # is not on it. Answering None rather than half a dialog keeps a caller
        # from reaching for the button that was found because the other was not.
        logger.warning("A dialog panel is up but its buttons were not both found")
        return None
