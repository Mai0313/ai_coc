"""Read the row of buttons the game opens under a selected building.

**Nothing here looks at the building.** Every level repaints it, every building
is its own artwork, and the same goes for troops and heroes elsewhere in the
game — so a reader taught to recognise the thing selected would need re-teaching
at each level and again after any update that retextures one. What is read
instead is the UI the game paints on top of the village: the resource icons on
the button row and the price written beside them, which are the same pixels
whatever happens to be selected.

So this says what the menu *offers*, never what it belongs to. `upgrade_buttons`
answers where 升級 is and what it costs; that is enough to upgrade anything,
because the price is what the decision rests on and the name is not.

Walls are the one exception, and they are told apart by something no other
building can do rather than by their artwork: being upgradeable with gold *or*
elixir. Everything else takes a single resource, so a row carrying an elixir drop
with a gold coin one pitch to its left, priced the same in each, is a wall menu
and nothing else is.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
import logging
import itertools

from PIL import Image

from ai_coc.models import WallMenu, GameDialog, UpgradeButton
from ai_coc.parsers.home import _mask, _patches
from ai_coc.parsers.scout import read_stock, digits_from

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

# Where the game writes what is selected, as 城牆(15級) or 英雄殿堂(9級).
#
# **It is the one thing on this screen that says which building this is**, and
# nothing here can read it: it is Chinese, it differs in every village, and a
# reader taught the names would need re-teaching whenever the game adds one. So
# this is a crop rather than a parse — the strip goes to a model that reads it.
#
# Fixed, and centred on the screen middle rather than floating over the building
# it names: measured across nine live menus and both committed ones, every label
# sat inside this band whatever was selected and wherever it stood on the map.
# The band is 4% of the frame's area, which is the whole reason a per-candidate
# call is affordable at all.
NAME_BAND = (350, 570, 1250, 635)
# The row is centred here, which is what puts every possible button middle on
# a half-pitch grid: an odd number of buttons puts one on the middle itself and
# an even number straddles it. Measured, a real button lands within a pixel of
# that grid and a patch of village between two buttons does not.
BUTTON_MIDDLE = 800
BUTTON_GRID_TOLERANCE = 8
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
# of its template, red and white alike. It was defined and never passed, which is
# why a row this reader only half resolved came back as a shorter number rather
# than as nothing: 10 400 000 was reported as 14. A truncated price is the
# dangerous kind of wrong, because one that keeps seven of its eight digits still
# clears `MIN_PRICE` and is spent against. Swept over 548 recorded frames,
# applying it changes eight readings and every one is a stray 1 becoming None.
PRICE_TOLERANCE = 30
# How short a digit may be here, which is lower than the loot panel's floor
# because **a price is shrunk to fit its button**. Measured across the recorded
# menus, five figures are drawn 16 px tall, seven 13 to 14, and eight 12 — of
# which the digits with no ascender measure 11. At `MIN_GLYPH_ROWS` those drop
# out one by one and whatever survives is reported as the price: the 英雄殿堂's
# own 10 400 000 came back as 14, and `ai_coc upgrade` could not see a single
# eight-figure upgrade while picking the dearest one it could find.
PRICE_ROWS = 10
# Nothing in the village upgrades for single figures, so a price that small is a
# number read off some other button — a count, a level badge — rather than a
# cost. The cheapest real upgrade in the game is a level-1 wall at 5000.
MIN_PRICE = 100

# **A button on a blue plate spends gems or a magic item, never the storages**,
# and must never be tapped. There are two: 加速所有同類項目 on an ordinary menu
# and the wall ring on a wall's. Both carry an icon that reads as a resource —
# the speed-up's potion answers the elixir test outright — so without this a
# sweep reports "an upgrade costing 1 elixir" and a loop taps a magic item away.
# Measured at this corner of the plate, a blue one reads blue-minus-red of 199
# to 204 and every ordinary one reads -20 to -29.
PLATE_BOX = (-58, 622, -38, 642)
PLATE_BLUE = 50

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
    return digits_from(
        _price_mask(image.crop((centre + left, top, centre + right, bottom))),
        PRICE_TOLERANCE,
        floor=PRICE_ROWS,
    )


def _on_gem_plate(image: Image.Image, centre: int) -> bool:
    """Whether the button centred here is one of the blue, gem-priced ones."""
    left, top, right, bottom = PLATE_BOX
    data = image.crop((centre + left, top, centre + right, bottom)).tobytes()
    count = len(data) // 3
    red = sum(data[i] for i in range(0, len(data), 3)) / count
    blue = sum(data[i + 2] for i in range(0, len(data), 3)) / count
    return blue - red > PLATE_BLUE


def _on_grid(centre: int) -> bool:
    """Whether a button could really sit here, given how the row is laid out.

    The row is centred on the screen and spaced a pitch apart, so an odd number
    of buttons puts one in the middle and an even number straddles it — which
    leaves every possible button middle on a half-pitch grid. A resource icon
    found anywhere else is the village showing through between two buttons, and
    the price read beside it would be whatever the grass spelled.
    """
    step = BUTTON_PITCH // 2
    offset = (centre - BUTTON_MIDDLE) % step
    return min(offset, step - offset) <= BUTTON_GRID_TOLERANCE


def upgrade_buttons(png: bytes) -> list[UpgradeButton]:
    """Every 升級 button on the menu this frame is showing, cheapest first.

    A resource icon is not enough on its own to call something an upgrade: 收集
    on a collector's menu carries a gold coin too, and so does the wall ring.
    What separates them is the price written beside it, so a button whose price
    does not read is not reported — which also means a frame with no menu at all
    comes back empty rather than wrong.

    Dark elixir is deliberately not among them. Its icon is nearly black and a
    village is full of dark pixels, so it cannot be found the way the other two
    are; what it would buy is heroes, which take days rather than the moments
    this is written around.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"建築選單座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    found: list[UpgradeButton] = []
    for resource, test in (("gold", _is_gold), ("elixir", _is_elixir)):
        for icon in _icon_centres(image, test):
            centre = icon - ICON_OFFSET
            if not _on_grid(centre) or _on_gem_plate(image, centre):
                continue
            price = _price(image, centre)
            if price is not None and price >= MIN_PRICE:
                found.append(
                    UpgradeButton(resource=resource, point=(centre, BUTTON_ROW_Y), price=price)
                )
    return sorted(found, key=lambda button: button.price)


def wall_menu(png: bytes) -> WallMenu | None:
    """The wall menu on this frame, or None where the screen is not showing one.

    A wall is the pair of buttons a pitch apart asking the same number in both
    resources. Everything else in the village takes one resource, so no other
    menu can produce that pair.

    None covers every way of not being on a wall: no menu at all, a menu for
    something that takes a single resource, and a wall whose price this frame
    could not read. That they are one answer is deliberate — each of them leaves
    the loop with nothing it can safely tap, and a caller acting on the
    difference would be acting on a guess about a button it never located.
    """
    buttons = upgrade_buttons(png)
    coins = [button for button in buttons if button.resource == "gold"]
    for drop in (button for button in buttons if button.resource == "elixir"):
        for coin in coins:
            # A misread price is how a batch gets sized against money the village
            # does not have, so the two have to agree as well as line up. They
            # line up to within a pixel rather than exactly, because each middle
            # is derived from where its own icon happened to segment.
            if abs(drop.point[0] - coin.point[0] - BUTTON_PITCH) > ICON_TOLERANCE:
                continue
            if drop.price != coin.price:
                logger.debug("Buttons a pitch apart priced %d and %d", coin.price, drop.price)
                continue
            # Everything is measured off the drop: it is the one icon on the row
            # nothing else can be confused with, where the coin has the wall ring
            # two places along answering the same colour test.
            elixir, gold = drop.point[0], drop.point[0] - BUTTON_PITCH
            return WallMenu(
                gold=(gold, BUTTON_ROW_Y),
                elixir=(elixir, BUTTON_ROW_Y),
                add=(gold - BUTTON_PITCH, BUTTON_ROW_Y),
                price=drop.price,
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


# A building's upgrade does not confirm in the little dialog a wall's does. It
# opens a full-screen sheet — 將金礦升至10級？ — showing what the level buys, with
# a green 確認 carrying the price along the bottom.
#
# The sheet covers the storage bars, and that is what tells it from a village:
# its own green is otherwise indistinguishable from grass. Measured, a village
# frame answers a 474x214 patch of lawn to this same test and reads its storages
# fine, while the sheet answers a 251x99 button and reads no storages at all.
SHEET_SPAN = (850, 620, 1400, 880)
SHEET_GREEN = ((0, 210), (150, 255), (0, 190))
SHEET_BUTTON = 5000


def upgrade_sheet(png: bytes) -> tuple[int, int] | None:
    """Where 確認 sits on the full-screen upgrade sheet, or None if none is up."""
    if read_stock(png) is not None:
        return None
    image = Image.open(io.BytesIO(png)).convert("RGB")
    left, top, right, bottom = SHEET_SPAN
    biggest = max(
        (
            patch
            for patch in _patches(
                _mask(image.crop(SHEET_SPAN), SHEET_GREEN), right - left, bottom - top
            )
            if patch.count >= SHEET_BUTTON
        ),
        key=lambda patch: patch.count,
        default=None,
    )
    if biggest is None:
        return None
    return biggest.middle[0] + left, biggest.middle[1] + top


def name_strip(png: bytes) -> bytes:
    """Just the band the selected building's name is written in, as a PNG.

    Cropped rather than read: see `NAME_BAND`. A frame with nothing selected
    still answers, because whether a name is there is the reader's question and
    not this one's.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"建築名稱座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    out = io.BytesIO()
    image.crop(NAME_BAND).save(out, format="PNG")
    return out.getvalue()
