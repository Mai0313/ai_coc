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

from typing import TYPE_CHECKING
import logging
import itertools

from PIL import Image

from ai_coc.models import World, WallMenu, GameDialog, SheetOffer, SheetResource, UpgradeButton
from ai_coc.parsers.frame import open_frame
from ai_coc.parsers.scout import read_stock
from ai_coc.parsers.glyphs import digits_from
from ai_coc.parsers.regions import mask, patches

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# The button row the game opens under whatever is selected. Its buttons are laid
# out from the middle of the screen outwards, so not one of them sits at a fixed
# x: a batch with no walls left to add loses its 新增城牆+10 button, and every
# button after it moves a place along. Measured across both layouts the spacing
# does not move, so one located button locates the rest.
BUTTON_PITCH = 176
BUTTON_ROW_Y = 700

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
# plate on its own: white ink is bright *and* neutral where the plate is yellow,
# and red ink runs (255, 137, 128), which no part of the plate comes near. Red is
# the game saying the village cannot afford this, and it is read rather than
# skipped: the loop does its own arithmetic, and a price it cannot read at all is
# one it cannot size a batch against.
PRICE_BOX = (-66, 634, 52, 666)
# White ink fades into its dark outline through greys whose three channels stay
# within 5 of each other, while the plate, wherever it is brighter than the floor,
# is 14 to 19 apart. These were 245 and no spread, pure white only, which shaved
# those greys off, and a price of seven or eight figures is drawn small enough for
# the shaving to change the digit. A wall at 8 500 000 read 8 900 000 on both
# buttons, which then agreed and were believed. On a batch of 25 500 000 the white
# 5s came out 27 bits from "9" against 32 from "5", the gold button read
# 29 900 000 beside the red elixir one's 25 500 000, and the menu was not taken
# for a wall at all. Swept
# over 335 frames, the committed ones and every recorded run then on disk, these
# change only that run's white prices, each to the right number, and the worst
# glyph any reading keeps drops from 27 bits to 23; a floor of 150 lets a hero
# hall glyph reach 30, 210 lets a 15 300 000 reach 28, and a spread of 6 lets an
# unaffordable wall price reach 28.
INK_WHITE = 180
INK_WHITE_SPREAD = 10
# **A grey only counts as ink beside a pure white one.** The button's plate
# carries a neutral sheen, measured at 210 to 239 with a spread of 5 to 9 under
# the digits, which passes the two lines above, and on a batch of 24 000 000 it
# ran up under the 2 and the 4 and joined them into one span too wide to read:
# the elixir price came back as nothing and the menu was not taken for a wall.
# A digit's grey is its own edge fading out, so it sits within this reach of the
# pure white core; the sheen has no core and never reaches 245.
INK_WHITE_CORE = 245
# One pixel, not two. Right after 新增城牆 is tapped the whole row is lit, the
# plate above the digits goes nearly neutral, and at two pixels its top edge was
# taken into a 6 of 16 000 000: the glyph read 37 bits off against 25 a moment
# later, and the elixir price stopped reading. Swept over 908 frames, one pixel
# changes only those readings, each to the right number.
INK_RIM_REACH = 1
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
# own 10 400 000 came back as 14, and the old upgrade loop could not see a
# single eight-figure upgrade while picking the dearest one it could find.
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
# 立即完成, which finishes a running upgrade with gems, sits on a green plate.
# Measured at the same corner, green over the larger of red and blue reads 86 on
# it, against at most 18 on every other button and 40 on the village past the
# end of a row.
PLATE_GREEN = 60

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
    # `ink` rather than `mask`, which is the name this module now imports from
    # `parsers.regions`; the same collision in `parsers.home` reads the same way.
    ink: list[list[bool]] = []
    grey: list[list[bool]] = []
    core: set[tuple[int, int]] = set()
    for y in range(height):
        row: list[bool] = []
        greys: list[bool] = []
        for x in range(width):
            offset = (y * width + x) * 3
            red, green, blue = data[offset], data[offset + 1], data[offset + 2]
            low, high = min(red, green, blue), max(red, green, blue)
            white = low > INK_WHITE and high - low < INK_WHITE_SPREAD
            if white and low > INK_WHITE_CORE:
                core.add((x, y))
            greys.append(white)
            row.append(
                red > INK_RED_LEVEL
                and red - green > INK_RED_MARGIN
                and red - blue > INK_RED_MARGIN
            )
        ink.append(row)
        grey.append(greys)
    reach = range(-INK_RIM_REACH, INK_RIM_REACH + 1)
    for x, y in core:
        for dy in reach:
            for dx in reach:
                if 0 <= y + dy < height and 0 <= x + dx < width and grey[y + dy][x + dx]:
                    ink[y + dy][x + dx] = True
    return ink


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


def _on_finish_plate(image: Image.Image, centre: int) -> bool:
    """Whether the button centred here is 立即完成, on its green plate."""
    left, top, right, bottom = PLATE_BOX
    data = image.crop((centre + left, top, centre + right, bottom)).tobytes()
    count = len(data) // 3
    red, green, blue = (sum(data[i + c] for i in range(0, len(data), 3)) / count for c in range(3))
    return green - max(red, blue) > PLATE_GREEN


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
    image = open_frame(png)
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
    image = open_frame(png)
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
    image = open_frame(png)
    left, top, right, bottom = SHEET_SPAN
    biggest = max(
        (
            patch
            for patch in patches(
                mask(image.crop(SHEET_SPAN), SHEET_GREEN), right - left, bottom - top
            )
            if patch.count >= SHEET_BUTTON
        ),
        key=lambda patch: patch.count,
        default=None,
    )
    if biggest is None:
        return None
    return biggest.middle[0] + left, biggest.middle[1] + top


# What the sheet's green 確認 asks for, which is what `confirm` holds against
# the price the agent read off the panel row before it taps.
#
# **Each village draws its own sheet.** The home village's 「將X升至N級？」
# puts 確認 bottom right, beside a magic item's 確認 and the upgrade time, the
# same for heroes, buildings, research and pets on every one recorded. The
# builder base's 「是否提升至N級？」 is a larger window with one button in the
# middle, its price drawn bigger, its gold a square and its elixir violet.
# Measured on fourteen home sheets and four builder base ones.
#
# **The same green, in the same place, is 立即完成 on a running upgrade's
# sheet**, priced in gems: a tap on a running hero's or pet's row opens
# 「正在將X升至N級」 instead. The icon is what tells them apart, so an offer is
# only a resource icon that clears its own floor. On the home sheets the icon
# box holds 0.249 coin on both gold sheets, 0.204 drop on the elixir one and
# 0.34 on the four dark ones, against at most 0.009 of any of them on the gem
# sheet; each floor sits about halfway, and a box that clears two floors is no
# offer either.
SHEET_PRICE_BOX: dict[World, tuple[int, int, int, int]] = {
    "day": (1000, 765, 1190, 803),
    "night": (668, 760, 872, 810),
}
SHEET_ICON_BOX: dict[World, tuple[int, int, int, int]] = {
    "day": (1190, 770, 1245, 830),
    "night": (876, 752, 948, 822),
}
SHEET_ICON_FLOORS: dict[SheetResource, float] = {"gold": 0.12, "elixir": 0.1, "dark": 0.17}
# Where the button is looked for on each sheet: the home one is `upgrade_sheet`'s
# own span, the builder base's is the middle, where its one button stands.
SHEET_BUTTON_SPAN: dict[World, tuple[int, int, int, int]] = {
    "day": SHEET_SPAN,
    "night": (600, 700, 1010, 880),
}
# The builder base draws its price too large for the digit templates' widths:
# shrunk to this, its four sheets read 2 560 000, 2 000 000, 192 000 and 4 000
# with no digit worse than 23.
SHEET_PRICE_SCALE: dict[World, float] = {"day": 1.0, "night": 0.7}
# The price is drawn yellow while an event discounts it (every home sheet
# recorded, with the old price struck through below the box), white otherwise
# (every builder base sheet), and red, like a menu's, when the village cannot
# pay. The worst digit on the seven discounted home prices landed 20 from its
# template; the gem sheet's white 500 came back 35 to 40 off, so it does not
# read as a price either. **Yellow is the home sheet's alone**: the builder
# base's lime button carries a highlight that passes for it right across the
# price box, so there only white and red count.
SHEET_INK_YELLOW = (225, 200, 150)
SHEET_INK_WHITE = 225
SHEET_DIGIT_TOLERANCE = 26
# The sheet's red X, the one part of it a plain `tap` may press: on the home
# sheets it spans about x 1383-1445 and y 40-100, centred on (1413, 70), and on
# the builder base's x 1301-1377 and y 23-92.
SHEET_CLOSE_BOX: dict[World, tuple[int, int, int, int]] = {
    "day": (1378, 35, 1450, 105),
    "night": (1296, 18, 1382, 97),
}
# **`upgrade_sheet` alone is no test for a sheet being up**: it is a green patch
# on a frame whose storages do not read, which 40 of the committed frames are —
# battles, the army screen, the old panels, settings, the shop. It was only ever
# asked right after 升級 was tapped. The X is what this window has and they do
# not: measured, the home box is 0.432 red on all fourteen home sheets, the gem
# sheet included, and on a troop's info window that carries no green button;
# 0.164 on the profile window's settings tab, and 0.03 at most anywhere else.
# The builder base box reads 0.302 on its four.
SHEET_X_BOX: dict[World, tuple[int, int, int, int]] = {
    "day": (1383, 40, 1445, 100),
    "night": (1305, 27, 1373, 88),
}
SHEET_X_RED = 0.2
# **Red is the game saying the village cannot pay, and tapping it anyway opens
# the gem shop for the shortfall**, so a red price is read but never offered as
# affordable. On every discounted or white sheet recorded this box holds no red
# at all (a home sheet's struck-through old price and its red line sit below
# it), and the builder base's red 2 560 000 fills well over a hundredth of it.
SHEET_RED_SHARE = 0.01
# A plain `tap` on a building's menu row may not land on the two kinds of
# button there that are never paid from the storages: the blue plate
# (加速所有同類項目, the wall ring) and 立即完成's green one. The row is laid out
# on a half-pitch grid around the middle, so the button under a point is the
# grid position nearest it, this far across either way, inside this band.
MENU_BAND = (610, 770)
MENU_HALF_WIDTH = 80
# **A menu row carrying a green button takes no plain tap anywhere along it.**
# A running row tapped on the builder base's build panel opens no sheet: it
# selects the building, whose row then reads 資訊, 取消 and 立即完成 for gems,
# and 取消 throws the running upgrade away. So the whole band is refused
# whenever a bright green plate stands in it. Measured, the biggest such patch
# here is 4159 px on that row, 1401 or more on every frame carrying a green
# button in this band (hero hall cards, the night menu's 升級, 確定 on the
# welcome report, the shield sheet's 繼續), and 159 at most on every village
# and build panel frame committed.
MENU_ROW_BOX = (250, 600, 1170, 800)
MENU_ROW_GREEN = ((0, 185), (200, 255), (0, 110))
MENU_ROW_PATCH = 800
# **Nothing a plain tap needs is a big green button**, and every way to spend
# gems by mistake is one: 確認 itself (which `confirm` presses), 立即完成, and
# 所有建築工人都在忙碌中's offer to finish a job for gems, which the game raises
# over the panel when 確認 is pressed with every builder busy and which is
# neither a sheet nor a two-button dialog. So any bright green patch this big
# left of the build panel takes no tap. Measured, that offer's button is 14915
# px, a sheet's green stat boxes about 10500, and every village and panel frame
# committed has none bigger than 740 (the + by the gems, which `on_hud` takes).
GREEN_BUTTON_AREA = (0, 0, 1170, 900)
GREEN_BUTTON_PATCH = 2500
# Each village's resource icons, as the colour of a pixel on them. The home
# village's coin and drop are the menus' own tests; its dark drop is the only
# near-black on the button. The builder base's gold is a square and its elixir
# violet rather than magenta, and it has no dark elixir.
SHEET_ICONS: dict[World, tuple[tuple[SheetResource, Callable[[int, int, int], bool]], ...]] = {
    "day": (
        ("gold", lambda red, green, blue: red > 200 and 150 < green < red and blue < 90),
        ("elixir", _is_elixir),
        (
            "dark",
            lambda red, green, blue: max(red, green, blue) < 90 and green <= max(red, blue) + 5,
        ),
    ),
    "night": (
        ("gold", lambda red, green, blue: red > 220 and green > 150 and blue < 60),
        ("elixir", lambda red, green, blue: blue > 200 and green < 100 and 90 < red < 210),
    ),
}


def _sheet_ink(world: World, red: int, green: int, blue: int) -> bool:
    """A pixel of the price on that village's sheet button, in any colour it is drawn in."""
    bright, warm, cool = SHEET_INK_YELLOW
    return (
        (world == "day" and red > bright and green > warm and blue < cool)
        or min(red, green, blue) > SHEET_INK_WHITE
        or (red > INK_RED_LEVEL and red - green > INK_RED_MARGIN and red - blue > INK_RED_MARGIN)
    )


def _sheet_icon(world: World, red: int, green: int, blue: int) -> SheetResource | None:
    """Which resource icon this pixel of the button could belong to, on that village's sheet."""
    return next(
        (resource for resource, test in SHEET_ICONS[world] if test(red, green, blue)), None
    )


def _sheet_button(image: Image.Image, world: World) -> tuple[int, int] | None:
    """The middle of the biggest green patch where that village's sheet keeps its button."""
    left, top, right, bottom = SHEET_BUTTON_SPAN[world]
    biggest = max(
        (
            patch
            for patch in patches(
                mask(image.crop((left, top, right, bottom)), SHEET_GREEN),
                right - left,
                bottom - top,
            )
            if patch.count >= SHEET_BUTTON
        ),
        key=lambda patch: patch.count,
        default=None,
    )
    return None if biggest is None else (biggest.middle[0] + left, biggest.middle[1] + top)


def sheet_open(png: bytes) -> World | None:
    """Which village's upgrade sheet is up, by its red X and its green button, or None."""
    if read_stock(png) is not None:
        return None
    image = open_frame(png)
    for world, box in SHEET_X_BOX.items():
        data = image.crop(box).tobytes()
        reds = sum(
            1
            for at in range(0, len(data), 3)
            if data[at] >= 180 and data[at + 1] <= 80 and data[at + 2] <= 80
        )
        if reds / (len(data) // 3) >= SHEET_X_RED and _sheet_button(image, world) is not None:
            return world
    return None


def sheet_offer(png: bytes) -> SheetOffer | None:
    """What the upgrade sheet's 確認 costs and in which resource, or None.

    None for no sheet, for a gem price (立即完成), for a button whose icon is
    no resource or more than one, and for a price that does not read: anything
    that is not plainly a resource-priced 確認 is something `confirm` must not
    tap.
    """
    world = sheet_open(png)
    if world is None:
        return None
    image = open_frame(png)
    point = _sheet_button(image, world)
    price_box, icon_box = SHEET_PRICE_BOX[world], SHEET_ICON_BOX[world]
    # The button the price is read off, and not the magic item's beside it:
    # only a green patch that sits under the price is the button this reads.
    if point is None or not price_box[0] <= point[0] <= icon_box[2]:
        return None
    data = image.crop(icon_box).tobytes()
    count = len(data) // 3
    found = [_sheet_icon(world, data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 3)]
    resources = [
        resource
        for resource, floor in SHEET_ICON_FLOORS.items()
        if found.count(resource) / count >= floor
    ]
    if len(resources) != 1:
        return None
    band = image.crop(price_box)
    scale = SHEET_PRICE_SCALE[world]
    if scale != 1.0:
        band = band.resize(
            (round(band.width * scale), round(band.height * scale)), Image.Resampling.BILINEAR
        )
    width, height = band.size
    pixels = band.tobytes()
    ink = [
        [
            _sheet_ink(world, *pixels[(y * width + x) * 3 : (y * width + x) * 3 + 3])
            for x in range(width)
        ]
        for y in range(height)
    ]
    price = digits_from(ink, SHEET_DIGIT_TOLERANCE, floor=PRICE_ROWS)
    if price is None or price < MIN_PRICE:
        return None
    reds = sum(
        1
        for at in range(0, len(pixels), 3)
        if pixels[at] > INK_RED_LEVEL
        and pixels[at] - pixels[at + 1] > INK_RED_MARGIN
        and pixels[at] - pixels[at + 2] > INK_RED_MARGIN
    )
    return SheetOffer(
        price=price,
        resource=resources[0],
        point=point,
        affordable=reds / (width * height) < SHEET_RED_SHARE,
    )


def green_buttons(png: bytes) -> list[tuple[int, int, int, int]]:
    """The box round every big bright green button left of the build panel."""
    image = open_frame(png).crop(GREEN_BUTTON_AREA)
    return [
        (patch.left, patch.top, patch.right, patch.bottom)
        for patch in patches(mask(image, MENU_ROW_GREEN), *image.size)
        if patch.count >= GREEN_BUTTON_PATCH
    ]


def green_button_row(png: bytes) -> bool:
    """Whether a bright green button stands in the band a building's menu row uses."""
    image = open_frame(png).crop(MENU_ROW_BOX)
    width, height = image.size
    return any(
        patch.count >= MENU_ROW_PATCH
        for patch in patches(mask(image, MENU_ROW_GREEN), width, height)
    )


def menu_spends_gems(png: bytes, point: tuple[int, int]) -> bool:
    """Whether this point is on a menu button paid in gems or a magic item.

    Asked of any frame, and answered from the plate alone: a village with no
    menu open has grass where the plate would be, which neither test takes
    (measured 40 at most against the green floor's 60).
    """
    x, y = point
    if not MENU_BAND[0] <= y <= MENU_BAND[1]:
        return False
    step = BUTTON_PITCH // 2
    centre = BUTTON_MIDDLE + round((x - BUTTON_MIDDLE) / step) * step
    # Every grid position a button this wide could be standing on, not only the
    # nearest: more than half a step from a button's middle the nearest one is
    # the gap beside it, and the plate sampled there was the neighbour's.
    near = [
        middle
        for middle in (centre - step, centre, centre + step)
        if abs(x - middle) <= MENU_HALF_WIDTH
    ]
    image = open_frame(png)
    return any(_on_gem_plate(image, middle) or _on_finish_plate(image, middle) for middle in near)
