"""Read the row of buttons the game opens under a selected building.

**Nothing here looks at the building.** Every level repaints it, every building
is its own artwork, and the same goes for troops and heroes elsewhere in the
game — so a reader taught to recognise the thing selected would need re-teaching
at each level and again after any update that retextures one. What is read
instead is the UI the game paints on top of the village: the resource icons on
the button row and the price written beside them, which are the same pixels
whatever happens to be selected.

So this says what a button *offers*, never what it belongs to: the price and
the resource are what a press rests on, and the name is not.

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

from ai_coc.models import World, GameDialog, SheetOffer, GoblinOffer, PanelWallMenu, SheetResource
from ai_coc.parsers.home import build_panel_open
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

# The row is centred here, which is what puts every possible button middle on
# a half-pitch grid: an odd number of buttons puts one on the middle itself and
# an even number straddles it. Measured, a real button lands within a pixel of
# that grid and a patch of village between two buttons does not.
BUTTON_MIDDLE = 800
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
# skipped: `confirm` answers `cannot_afford` for it, where a price it could not
# read at all would be no wall menu.
#
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
# reader takes it for "an upgrade costing 1 elixir" and a tap spends a magic item.
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


def _icon_centres(
    image: Image.Image, test: Callable[[int, int, int], bool], rows: tuple[int, int]
) -> list[int]:
    """Where along the button row an icon this test recognises sits."""
    band = image.crop((0, rows[0], image.width, rows[1]))
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


def _on_gem_plate(
    image: Image.Image, centre: int, box: tuple[int, int, int, int] = PLATE_BOX
) -> bool:
    """Whether the button centred here is one of the blue, gem-priced ones."""
    left, top, right, bottom = box
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
# Measured on fourteen home sheets and eight builder base ones, two heroes' and
# two of a research's among them.
#
# **The same green, in the same place, is 立即完成 on a running upgrade's
# sheet**, priced in gems: a tap on a running hero's or pet's row opens
# 「正在將X升至N級」 instead. The icon is what tells them apart, so an offer is
# only a resource icon that clears its own floor. On the home sheets the icon
# box holds 0.249 coin on both gold sheets, 0.204 drop on the elixir one and
# 0.34 on the four dark ones, against at most 0.009 of any of them on the gem
# sheet; each floor sits about halfway, and a box that clears two floors is no
# offer either.
#
# Each layout is where its button is looked for, then its price box and its
# icon box: the home one's span is `upgrade_sheet`'s own, the builder base's the
# middle, where its one button stands. **A builder base hero gets the home
# layout's shape instead**, its button bottom right under the same X as the
# other builder base sheets: on the Battle Machine's, measured 2026-10-16, the
# button spans x 1102-1354 and y 743-844, the white price x 1118-1274, the drop
# x 1294-1338 and y 764-817. **The hero's is tried first**, since the grass
# under its portrait reaches into the middle span (4382 px of that green there,
# against the 5000 a button needs), while its own span holds 2 px of it at most
# on every other builder base sheet.
#
# **A builder base research sheet puts that button 36 px left and 11 px up**:
# on the Drop Ship's, measured 2026-10-19, it spans x 1074-1318 and y 732-832,
# so the hero's boxes cut the leading 3 off (440 000 read, 28 off) and held
# 0.09 of the drop. It is a layout of its own, the hero's boxes moved by that
# much, told from the hero's by where the button's right edge lands (1318
# against 1354 and 1355 on the two hero sheets). The boxes stay fixed rather
# than following the button: the shrunk digits read differently a pixel apart,
# and moved 1 px right and 4 px down with the Battle Machine's 2 400 000 button
# they read 2 400 080 or came back 30 off. The last entry is that right edge,
# None where one layout is the only one in its span.
SHEET_LAYOUTS: dict[
    World,
    tuple[
        tuple[
            tuple[int, int, int, int],
            tuple[int, int, int, int],
            tuple[int, int, int, int],
            int | None,
        ],
        ...,
    ],
] = {
    "day": ((SHEET_SPAN, (1000, 765, 1190, 803), (1190, 770, 1245, 830), None),),
    "night": (
        ((1050, 700, 1400, 880), (1110, 760, 1290, 810), (1288, 752, 1350, 825), 1354),
        ((1050, 700, 1400, 880), (1074, 749, 1254, 799), (1252, 741, 1314, 814), 1318),
        ((600, 700, 1010, 880), (668, 760, 872, 810), (876, 752, 948, 822), None),
    ),
}
# How far a button's right edge may land from its layout's and still be that layout.
SHEET_EDGE_SLACK = 8
SHEET_ICON_FLOORS: dict[SheetResource, float] = {"gold": 0.12, "elixir": 0.1, "dark": 0.17}
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
# **The builder base's shrunk digits land further from their templates, and
# the distance does not tell a right reading from a wrong one there.** At its
# 0.7 scale the builder base sheets read right with digits up to 30 off (two
# frames of the Drop Ship's 3 440 000 on 2026-10-19 came 27 and 30, a pixel
# apart); swept over scales 0.6 to 0.85 the right readings ran up to 35 and the
# wrong ones (2 400 800, 2 240 080, 2 240 008) came 30 to 32. So the tolerance
# only keeps out a glyph that is no digit at all, and what keeps a misread from
# spending is `confirm` pressing only on exactly the price the agent passed.
SHEET_DIGIT_TOLERANCE: dict[World, int] = {"day": 26, "night": 35}
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
# The builder base's research sheet draws its X 20 px left of the others' (x
# 1285-1348, y 33-90), so the builder base box is the part both cover: it reads
# 0.292 on the four building sheets, 0.35 and 0.356 on the heroes', 0.333 on the
# research's, and 0.2 at most anywhere else (a loot cart mid-animation), where
# the box the X used to be read in caught the research's at 0.2003. The floor
# sits between.
SHEET_X_BOX: dict[World, tuple[int, int, int, int]] = {
    "day": (1383, 40, 1445, 100),
    "night": (1305, 33, 1348, 88),
}
SHEET_X_RED = 0.25
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
# takes no tap: across the whole screen, or left of the build panel while it is
# open, since its running rows are green bars too and have a refusal of their
# own. Measured, that offer's button is 14915 px, a sheet's green stat boxes
# about 10500, the hero hall's card buttons about 5500 (one of them right of
# the panel's edge), and every village and panel frame committed has none
# bigger than 769 (the + by the gems, which `on_hud` takes).
GREEN_BUTTON_AREA = (0, 0, 1170, 900)
GREEN_BUTTON_PATCH = 2500
# **A paler lime button is one too** (#457). The clock tower's boost sheet draws
# its 加速 in a lime this green misses, (108, 187, 31) to (224, 249, 137)
# (`BOOST_SHEET_GREEN`), so on it a tap was refused only where the menu row's
# plate test happened to read the sheet as a gem plate, and its top half and
# right edge took one. Home village grass comes in that lime too, in patches of
# over 100 000 px, so what tells a button is its solid fill. Measured over the
# whole of every committed frame: the boost's button 30 046 px at 0.85, and the
# other lime buttons (the wall dialogs' 確定, `star_bonus`, `night_menu`,
# `sheet_running_gems`) 20 189 to 29 706 px at 0.79 to 0.85, every one already
# refused earlier as a dialog or a sheet; the gem offer on
# `night_builders_busy` came 0.73, refused as a bright green; grass 0.41 at most
# on a village, 0.49 under the hero upgrade sheet. The size floor only keeps
# small lime bits out: 回營 on a battle result sits just under it, 14 142 px.
LIME_BUTTON_PATCH = 15000
LIME_BUTTON_FILL = 0.75
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


def _sheet_button(
    image: Image.Image, world: World
) -> (
    tuple[tuple[int, int], tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]
    | None
):
    """The middle of the biggest green patch where one of that village's sheets keeps its
    button, with that sheet's price box and icon box.

    A button whose right edge matches no layout in its span still counts, with
    no boxes: it is a sheet nobody recorded, which `sheet_open` must still see
    so a plain `tap` stays off its button, and which `sheet_offer` reads no
    price off.
    """
    unmatched: tuple[int, int] | None = None
    for (left, top, right, bottom), price_box, icon_box, edge in SHEET_LAYOUTS[world]:
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
        if biggest is None:
            continue
        point = (biggest.middle[0] + left, biggest.middle[1] + top)
        if edge is None or abs(biggest.right + left - edge) <= SHEET_EDGE_SLACK:
            return point, price_box, icon_box
        unmatched = unmatched or point
    return None if unmatched is None else (unmatched, None, None)


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
    if (button := _sheet_button(image, world)) is None:
        return None
    point, price_box, icon_box = button
    # No boxes is a button no layout knows. Otherwise the button the price is
    # read off, and not the magic item's beside it: only a green patch that
    # sits under the price is the button this reads.
    if price_box is None or icon_box is None or not price_box[0] <= point[0] <= icon_box[2]:
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
    price = digits_from(ink, SHEET_DIGIT_TOLERANCE[world], floor=PRICE_ROWS)
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


# 嘿，首領, what 確認 raises instead of a sheet closing when every builder is
# busy and an event's goblin builder is free: the goblin on the left, a pale
# panel with its X top right, and one green button priced in gems, which hires
# it for that upgrade. Measured once, 2026-10-27, at 15 gems over the home
# village's build panel. Three readings, because each alone has a near miss
# over the committed frames: its X box reads 0.354 red against 0.343 on
# `loading_screen_badge_noise`, the panel box 1.000 pale against 0.983 on
# `hud_settings`, and the button box 0.378 green against 0.426 on the builder
# base research sheet; no frame but this one passes two of them. The builder
# base's 所有建築工人都在忙碌中 offer, which finishes a running job for gems,
# keeps its X and button elsewhere and passes none.
GOBLIN_X_BOX = (1405, 170, 1458, 218)
GOBLIN_PANEL_BOX = (760, 240, 1440, 290)
GOBLIN_BUTTON_BOX = (1000, 500, 1210, 590)
GOBLIN_X_RED = 0.25
GOBLIN_PANEL_PALE = 0.9
GOBLIN_BUTTON_GREEN = 0.25
# The price, white on the button left of its gem. Shrunk to 0.6 the 15 read 2
# and 11 off its templates; it read right at every scale from 0.5 to 0.8.
GOBLIN_PRICE_BOX = (995, 505, 1125, 580)
GOBLIN_PRICE_SCALE = 0.6


def _share(
    image: Image.Image, box: tuple[int, int, int, int], test: Callable[[int, int, int], bool]
) -> float:
    """How much of a box passes a pixel test."""
    data = image.crop(box).tobytes()
    return sum(test(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 3)) / (
        len(data) // 3
    )


def _goblin_prompt(image: Image.Image) -> bool:
    return (
        _share(image, GOBLIN_X_BOX, lambda r, g, b: r >= 180 and g <= 80 and b <= 80)
        >= GOBLIN_X_RED
        and _share(
            image,
            GOBLIN_PANEL_BOX,
            lambda r, g, b: min(r, g, b) > 200 and max(r, g, b) - min(r, g, b) < 20,
        )
        >= GOBLIN_PANEL_PALE
        and _share(image, GOBLIN_BUTTON_BOX, lambda r, g, b: g > 150 and g - r > 45 and g - b > 60)
        >= GOBLIN_BUTTON_GREEN
    )


def goblin_prompt(png: bytes) -> bool:
    """Whether the prompt to hire the goblin builder is up, whether or not its price reads.

    `tap` asks this rather than `goblin_offer`: its gem button is a green that
    `green_buttons` misses, and a price that does not read must not open it.
    """
    return _goblin_prompt(open_frame(png))


def goblin_offer(png: bytes) -> GoblinOffer | None:
    """How many gems the prompt to hire the goblin builder asks, or None where it is not up."""
    image = open_frame(png)
    if not _goblin_prompt(image):
        return None
    band = image.crop(GOBLIN_PRICE_BOX)
    band = band.resize(
        (round(band.width * GOBLIN_PRICE_SCALE), round(band.height * GOBLIN_PRICE_SCALE)),
        Image.Resampling.BILINEAR,
    )
    width, height = band.size
    pixels = band.tobytes()
    ink = [
        [
            min(pixels[(y * width + x) * 3 : (y * width + x) * 3 + 3]) > SHEET_INK_WHITE
            for x in range(width)
        ]
        for y in range(height)
    ]
    gems = digits_from(ink, SHEET_DIGIT_TOLERANCE["day"])
    if not gems:
        return None
    left, top, right, bottom = GOBLIN_BUTTON_BOX
    return GoblinOffer(gems=gems, point=((left + right) // 2, (top + bottom) // 2))


# **A wall picked off the build panel opens a menu of its own beside it**, in
# two rows: 資訊, 整排選取 and 升級更多 (移除城牆 and 新增城牆 once it is a
# batch) above, and the two price buttons below, gold then elixir. The rows are
# laid out on a half-pitch grid around the middle of the screen left of the
# panel, so the geometry of the row under a selected building (`BUTTON_MIDDLE`,
# `BUTTON_PITCH`) does not fit it. Measured on the builder base, a single wall and a batch of two:
# the icons span x 545-560 and 721-732 across y 731-746 and the buttons are
# middled at x 497 and 671, 174 apart around 585.
PANEL_WALL_ICON_BAND = (728, 750)
PANEL_WALL_ICON_OFFSET = 55
PANEL_WALL_PRICE_BOX = (-58, 722, 46, 756)
PANEL_WALL_MIDDLE = 585
PANEL_WALL_PITCH = 174
PANEL_WALL_ROW_Y = 790
# **The menu can also stand in one row**, all five buttons side by side, with
# the price buttons 80 px higher on the same grid: on 2026-10-16 the drop sat at
# y 652-661 against 732-741 on a single wall (x 933) and a batch (x 846). Every
# two-row menu recorded had an extra button in the bottom right corner, a box
# with a red 1, and the one-row menus did not. The heights below are added to
# every y above, the two-row one first.
PANEL_WALL_RISES = (0, -80)
# **`tap`'s plate test reads this menu on its own grid and rows** (#439): the
# ordinary row's grid sits about 40 px off these buttons, and in one row its
# corners land on the night's ground above them, which read blue minus red 51
# and refused 新增城牆. At these corners every button of both layouts reads -30
# to -79, the ground 15 to 51, a blue plate 199 to 204. The two-row layout's
# upper row (資訊 or 移除城牆 and the rest) stands 155 px above the price
# buttons, its buttons ending by y 705 where the lower row's start at 720.
PANEL_WALL_TOP_RISE = -155
PANEL_WALL_ROW_SPLIT = 712
PANEL_MENU_BAND = (560, 860)
# Both real buttons sat 1 px off that grid on every menu recorded; the icons the
# village throws up in the same band sat 10 to 36 px off it.
PANEL_WALL_GRID_TOLERANCE = 5
# Beside the price, blue minus red reads -34 to -46 on every button recorded,
# against 199 to 204 on the old row's blue plates, which a magic item pays for.
PANEL_WALL_PLATE_BOX = (-62, 760, -42, 780)
# The red a price is written in when the village cannot pay: the gold of a batch
# of two, 1 600 000 against 845 208 held, filled 0.19 of its price box, and no
# white price recorded carries any.
PANEL_WALL_RED_SHARE = 0.01


def _panel_wall_button(
    image: Image.Image, centre: int, resource: SheetResource, rise: int
) -> SheetOffer | None:
    """What the wall's price button centred here asks, or None where it does not read."""
    left, top, right, bottom = PANEL_WALL_PRICE_BOX
    band = image.crop((centre + left, top + rise, centre + right, bottom + rise))
    price = digits_from(_price_mask(band), PRICE_TOLERANCE, floor=PRICE_ROWS)
    if price is None or price < MIN_PRICE:
        return None
    pixels = band.tobytes()
    reds = sum(
        1
        for at in range(0, len(pixels), 3)
        if pixels[at] > INK_RED_LEVEL
        and pixels[at] - pixels[at + 1] > INK_RED_MARGIN
        and pixels[at] - pixels[at + 2] > INK_RED_MARGIN
    )
    return SheetOffer(
        price=price,
        resource=resource,
        point=(centre, PANEL_WALL_ROW_Y + rise),
        affordable=reds / (band.width * band.height) < PANEL_WALL_RED_SHARE,
    )


def panel_wall_menu(png: bytes) -> PanelWallMenu | None:
    """The wall menu beside the open build panel, or None where none is up.

    Two readings, as everywhere a button is pressed on one: the panel has to be
    open, and the elixir drop has to stand on that menu's grid with a gold coin
    one pitch to its left, both priced alike, which no other menu can show.
    """
    if not build_panel_open(png):
        return None
    image = open_frame(png).crop(GREEN_BUTTON_AREA)
    for rise in PANEL_WALL_RISES:
        if (menu := _panel_wall_row(image, rise)) is not None:
            return menu
    return None


def _panel_wall_row(image: Image.Image, rise: int) -> PanelWallMenu | None:
    """The wall menu's price buttons at one of the heights it stands at, or None."""
    step = PANEL_WALL_PITCH // 2
    icons = (PANEL_WALL_ICON_BAND[0] + rise, PANEL_WALL_ICON_BAND[1] + rise)
    left, top, right, bottom = PANEL_WALL_PLATE_BOX
    plate = (left, top + rise, right, bottom + rise)
    coins = [icon - PANEL_WALL_ICON_OFFSET for icon in _icon_centres(image, _is_gold, icons)]
    for drop in _icon_centres(image, _is_elixir, icons):
        elixir = drop - PANEL_WALL_ICON_OFFSET
        offset = (elixir - PANEL_WALL_MIDDLE) % step
        if min(offset, step - offset) > PANEL_WALL_GRID_TOLERANCE:
            continue
        gold = elixir - PANEL_WALL_PITCH
        if not any(abs(coin - gold) <= ICON_TOLERANCE for coin in coins):
            continue
        if any(_on_gem_plate(image, middle, plate) for middle in (gold, elixir)):
            continue
        offers = (
            _panel_wall_button(image, gold, "gold", rise),
            _panel_wall_button(image, elixir, "elixir", rise),
        )
        if offers[0] is None or offers[1] is None or offers[0].price != offers[1].price:
            continue
        return PanelWallMenu(gold=offers[0], elixir=offers[1])
    return None


def green_buttons(png: bytes) -> list[tuple[int, int, int, int]]:
    """The box round every big bright green button, left of the build panel while it is open."""
    image = open_frame(png)
    if build_panel_open(png):
        image = image.crop(GREEN_BUTTON_AREA)
    bright = [
        (patch.left, patch.top, patch.right, patch.bottom)
        for patch in patches(mask(image, MENU_ROW_GREEN), *image.size)
        if patch.count >= GREEN_BUTTON_PATCH
    ]
    lime = [
        (patch.left, patch.top, patch.right, patch.bottom)
        for patch in patches(mask(image, BOOST_SHEET_GREEN), *image.size)
        if patch.count >= LIME_BUTTON_PATCH
        and patch.count / ((patch.right - patch.left + 1) * (patch.bottom - patch.top + 1))
        >= LIME_BUTTON_FILL
    ]
    return bright + lime


# **A building or a wall left selected keeps its button row along the bottom of
# the village**, and a swipe that starts on one of those buttons does not move
# the camera: measured on the builder base, a park's centring drag began on a
# selected wall's 升級 and left the village in the map corner while `view`
# reported it parked (#429). One that starts on the selected building itself may
# drag it somewhere else, which nobody has measured and nobody should. The row's
# buttons are beige plates about 152 by 143 px: each holds 12 196 to 17 315 px
# of this colour on the four village frames with something selected, two of
# each village, and the biggest patch on the ten other village frames
# committed is 487, the bars along the bottom edge. Asked only of a frame
# already known to be a village: the report the game opens on after a raid
# (`welcome_back`) is one beige plate of 84 140.
SELECTION_ROW_BOX = (250, 600, 1400, 800)
SELECTION_PLATE = ((180, 230), (165, 210), (135, 190))
SELECTION_PLATE_PATCH = 8000


def selection_row(png: bytes) -> bool:
    """Whether a selected building's or wall's button row stands along the bottom of the village."""
    image = open_frame(png).crop(SELECTION_ROW_BOX)
    return any(
        patch.count >= SELECTION_PLATE_PATCH
        for patch in patches(mask(image, SELECTION_PLATE), *image.size)
    )


# The clock tower's free boost (免費加速, met 2026-10-17) on the tower's own
# button row: the button carrying the tower's clock icon, with nothing printed
# along its top, where every button that costs something carries its price.
# The boost out of turn costs gems, so a price of any kind, or a blue or green
# plate, keeps this from answering. On the one frame of the row the icon's white
# face measured 52x48 at a fill of 0.56 with its brown rim taking 0.10 of the
# box 4 px around it; swept over the committed frames, the largest white patch
# on any other button came 38x5 or 21x15. The price strip held 949 px of price
# ink on 升級 (960 000) and none on 資訊 or the boost, whose icon starts 2 px
# below it.
BOOST_ICON_BAND = (250, 640, 1400, 705)
BOOST_FACE = ((235, 255), (220, 255), (235, 255))
BOOST_FACE_ACROSS = (40, 64)
BOOST_FACE_DOWN = (38, 58)
BOOST_FACE_FILL = 0.45
BOOST_RIM = ((130, 185), (90, 135), (55, 95))
BOOST_RIM_REACH = 4
BOOST_RIM_SHARE = 0.05
BOOST_PRICE_STRIP = (-70, 630, 70, 653)
BOOST_PRICE_INK = 20
BOOST_BUTTON_Y = 700


def free_boost(png: bytes) -> tuple[int, int] | None:
    """The middle of the clock tower's free boost button, or None where there is none to press."""
    image = open_frame(png)
    band = image.crop(BOOST_ICON_BAND)
    width, height = band.size
    for patch in patches(mask(band, BOOST_FACE), width, height):
        if not patch.sized(BOOST_FACE_ACROSS, BOOST_FACE_DOWN, BOOST_FACE_FILL):
            continue
        rim = mask(
            band.crop((
                max(0, patch.left - BOOST_RIM_REACH),
                max(0, patch.top - BOOST_RIM_REACH),
                min(width, patch.right + BOOST_RIM_REACH + 1),
                min(height, patch.bottom + BOOST_RIM_REACH + 1),
            )),
            BOOST_RIM,
        )
        if rim.count(255) / len(rim) < BOOST_RIM_SHARE:
            continue
        step = BUTTON_PITCH // 2
        x = BOOST_ICON_BAND[0] + patch.middle[0]
        centre = BUTTON_MIDDLE + round((x - BUTTON_MIDDLE) / step) * step
        if _on_gem_plate(image, centre) or _on_finish_plate(image, centre):
            return None
        left, top, right, bottom = BOOST_PRICE_STRIP
        ink = _price_mask(image.crop((centre + left, top, centre + right, bottom)))
        return None if sum(map(sum, ink)) >= BOOST_PRICE_INK else (centre, BOOST_BUTTON_Y)
    return None


# Pressing the free boost raises a sheet of its own (2026-10-17): a dark title
# bar, a red X in its corner and one lime 加速 button, a green paler than the
# one `green_buttons` looks for, which misses it. `commands.boost` presses that
# button only because its own press raised the sheet, from the unpriced boost
# under the ready marker, the way `confirm` answers a dialog it raised itself.
# Nothing on the sheet itself can tell a gem: the game's gem icon is the same
# yellow-green as this lime, measured (139, 200, 67) to (214, 250, 121) on
# `sheet_running_gems`. On the one frame of it the button measured 298x118 at a
# fill of 0.85 and the X 77x71 at 2188 px; swept over the committed frames, the
# nearest thing in that lime came 295x95 at 0.65 (grass on `night_builders_busy`).
BOOST_SHEET_BUTTON_BOX = (640, 540, 970, 690)
BOOST_SHEET_GREEN = ((100, 240), (180, 255), (15, 140))
BOOST_SHEET_BUTTON_ACROSS = (260, 330)
BOOST_SHEET_BUTTON_DOWN = (100, 135)
BOOST_SHEET_BUTTON_FILL = 0.75
BOOST_SHEET_X_BOX = (1100, 170, 1215, 270)
BOOST_SHEET_RED = ((200, 255), (0, 90), (0, 90))
BOOST_SHEET_X_PATCH = 1500


def boost_sheet(png: bytes) -> tuple[int, int] | None:
    """The middle of the free boost sheet's 加速 button, or None where that sheet is not up."""
    image = open_frame(png)
    x_box = image.crop(BOOST_SHEET_X_BOX)
    if not any(
        patch.count >= BOOST_SHEET_X_PATCH
        for patch in patches(mask(x_box, BOOST_SHEET_RED), *x_box.size)
    ):
        return None
    band = image.crop(BOOST_SHEET_BUTTON_BOX)
    for patch in patches(mask(band, BOOST_SHEET_GREEN), *band.size):
        if patch.sized(
            BOOST_SHEET_BUTTON_ACROSS, BOOST_SHEET_BUTTON_DOWN, BOOST_SHEET_BUTTON_FILL
        ):
            left, top = BOOST_SHEET_BUTTON_BOX[:2]
            return left + patch.middle[0], top + patch.middle[1]
    return None


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
    (measured 40 at most against the green floor's 60). With the wall menu up
    beside the build panel, it reads that menu's own grid and rows instead.
    """
    if (wall := panel_wall_menu(png)) is not None:
        return _panel_menu_spends_gems(png, point, wall.gold.point[1] - PANEL_WALL_ROW_Y)
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


def _panel_menu_spends_gems(png: bytes, point: tuple[int, int], rise: int) -> bool:
    """`menu_spends_gems` on the wall menu beside the panel, on that menu's own grid and rows."""
    x, y = point
    if not PANEL_MENU_BAND[0] <= y <= PANEL_MENU_BAND[1]:
        return False
    if rise == 0 and y < PANEL_WALL_ROW_SPLIT:
        rise = PANEL_WALL_TOP_RISE
    left, top, right, bottom = PANEL_WALL_PLATE_BOX
    plate = (left, top + rise, right, bottom + rise)
    step = PANEL_WALL_PITCH // 2
    centre = PANEL_WALL_MIDDLE + round((x - PANEL_WALL_MIDDLE) / step) * step
    near = [m for m in (centre - step, centre, centre + step) if abs(x - m) <= MENU_HALF_WIDTH]
    image = open_frame(png)
    return any(_on_gem_plate(image, middle, plate) for middle in near)
