"""Read the 英雄殿堂 screen: which hero sits on each card, and what raising one costs.

**Nothing here looks at a hero.** Every level repaints one, every hero is its own
artwork, and a new one arrives with each update — so a reader taught to
recognise 飛龍公爵 by his face would need re-teaching the moment his next level
lands. What is read instead is the UI the game paints on top: the coloured
banner across the top of each card, the green 升級 button along its bottom, and
the price written on that button.

The banner is the one identifying mark that is not artwork. The game gives each
hero a flat theme colour and paints the whole card frame in it, so it is the same
pixels at level 1 as at level 100 and the same wherever the row has been
scrolled to. It is not free of assumptions — an update that recolours a hero
moves it — but it fails loudly rather than silently, because a colour that
matches nothing comes back as `unknown` and every caller refuses to spend on
one of those.

The row shows five of the six cards at a time, so a card is only reported when
its whole banner is on screen: a partly scrolled one shows 64 to 71 px of banner
against 250 to 262 for a whole one, and reading a price off a card whose middle
is off screen would read whatever the neighbour's button spelled.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal
import logging
import itertools

from PIL import Image

from ai_coc.models import HeroCard, HeroKind
from ai_coc.parsers.scout import digits_from
from ai_coc.parsers.building import (
    ICON_OFFSET,
    BUTTON_PITCH,
    BUTTON_ROW_Y,
    _is_gold,
    _on_grid,
    _is_elixir,
    _icon_centres,
    _on_gem_plate,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

SCREEN_SIZE = (1600, 900)

# The band across each card's banner that carries nothing but its theme colour.
# The hero's weapon badge stops above it and the name is written below it, so
# what is left is flat colour the whole width of the card.
BANNER_BAND = (256, 264)
# A banner pixel: saturated, and neither the wooden panel behind the row nor the
# white text on it. Measured, every banner reads 40 to 113 in its brightest
# channel with 33 to 113 between brightest and dimmest, where the panel behind
# the cards is a flat brown of 20 or less apart.
BANNER_SPREAD = 40
BANNER_CEILING = 200
BANNER_FLOOR = 35
# How wide a banner runs when the whole card is on screen. Measured across three
# captures, a whole card spans 250 to 262 px and a card the row has scrolled
# half out of view spans 64 to 71.
MIN_BANNER_WIDTH = 250

# Each hero's own theme colour, averaged over `BANNER_BAND`. Measured on three
# captures at two scroll positions, every hero came back within 2 of these.
# The closest pair is 野蠻人之王 and 飛龍公爵 at 29 apart, which is what
# `TINT_TOLERANCE` is set well under: a card is matched to its nearest colour
# rather than to the first one within reach, so the tolerance is only there to
# reject a colour that is no hero at all — a stretch of village grass reads
# (115, 147, 48), which is 137 from the nearest hero.
HERO_TINTS: dict[HeroKind, tuple[int, int, int]] = {
    "king": (113, 11, 33),
    "queen": (11, 113, 33),
    "minion_prince": (44, 0, 39),
    "warden": (90, 11, 113),
    "champion": (11, 33, 113),
    "duke": (89, 0, 46),
}
TINT_TOLERANCE = 14

# The green 升級 button along the bottom of a card, and the price on it. The
# button's own plate is a mid green — measured (147, 214, 73) — and the price is
# painted pure white over it, so brightness alone separates the two. The 升級
# text sits above the price and is small enough that its anti-aliased strokes
# never reach white, which is what keeps this band to the number.
BUTTON_Y = 648
PRICE_BOX = (-95, 640, 82, 662)
PRICE_INK = 235
# Swept over the recorded cards every digit landed within 22 of its template.
PRICE_TOLERANCE = 30
# Whether that button can actually be pressed, which the price does not say.
# **A hero capped by the hall's own level keeps its price and loses its
# button**: the game greys the plate, leaves the number on it, and answers a tap
# with a red line naming the hall level it wants first — Chinese, so nothing
# here reads it. Measured on one screen holding both, a live plate runs `green - max(red,
# blue)` from 29 to 44 while a greyed one is a perfect grey at 0, so the two do
# not overlap and the line goes between them.
BUTTON_LIVE = 15
PLATE_BAND = (640, 660)
PLATE_SPAN = 60
# Nothing in the Hero Hall costs single figures, and a price that small is ink
# read off something else. The cheapest hero level in the game is 4000 dark.
MIN_PRICE = 100

# Where the button carries its resource icon, measured from the card's middle.
# Two resources are what a hero can cost — 大守護者 takes elixir and every other
# hero takes dark — so the elixir test settles it and dark is what is left. Dark
# is not tested for directly because its drop is nearly black, and so is half a
# village; inside a located button, though, the question is only which of two.
ICON_SPAN = (92, 111)
ICON_BAND = (638, 668)

# **A third icon appears in exactly that box, and it spends gems.** A hero
# already being upgraded has 立即完成 where its 升級 was — same green plate, same
# place, a number beside it — and that number is a gem count. Read as a price it
# is a cheap one, so it passed the affordability check and the button was live,
# which left `hero --upgrade` one tap away from finishing an upgrade with gems.
# Measured live it read `warden 268 dark upgradable=True` against a village
# holding 11 359 gems.
#
# What separates them is that the gem is the only green one: swept over both
# committed halls and the live frame, `green - max(red, blue)` runs -120 for the
# elixir drop and -7 to -9 for every dark one, against +48 for the gem. The line
# goes between rather than beside either, and this is the same shape as the
# building menu's own gem guard — the one button in that row that must never be
# tapped is found and excluded rather than avoided by luck.
GEM_GREEN = 20

# Where the row's own scroll arrows sit. They are the only way to reach the
# sixth card, and they stay in the same place because they are the panel's
# furniture rather than anything to do with the cards. An arrow the row has run
# out of is not drawn at all, and what stands there instead is a card — so
# whether one is on screen has to be read before it is tapped, or the tap
# selects a hero.
SCROLL_RIGHT = (1537, 428)
SCROLL_LEFT = (65, 428)

# How many places right of a priced button to keep looking for the way in, and
# what a button's own plate reads there.
#
# **The entrance is not a fixed number of places along.** The row gains and
# loses buttons with the building's state: the same 英雄殿堂 menu came up five
# buttons wide with a 強化 running — 資訊, 加速, 強化英雄, 升級, 英雄殿堂, the
# hall one place right of 升級 — and four buttons wide without it, which slid
# every button half a pitch and left 強化英雄 in that place instead. A reader
# that only looked one place along found the hall on the first layout and a
# gem-plated button it must not tap on the second, so it reported nothing and
# the sweep walked past the hall it had just opened.
#
# The plate is what separates a button from the village showing between two of
# them. Measured across both layouts, 升級 reads a minimum channel of 185 and
# the hall's own button 144 to 146, against 12 to 74 for the village.
HALL_STEPS = 4
PLATE_BOX = (-70, 630, 70, 700)
PLATE_PALE = 120
ARROW_BOX = (14, 22)
# Measured, an arrow lights 0.16 to 0.19 of its box in near-white and the card
# that stands there when the row has run out lights none of it at all.
ARROW_PALE = 170
ARROW_SHARE = 0.05


def _plated(image: Image.Image, centre: int) -> bool:
    """Whether a button really sits here, rather than the village between two."""
    left, top, right, bottom = PLATE_BOX
    data = image.crop((centre + left, top, centre + right, bottom)).tobytes()
    count = len(data) // 3
    channels = (sum(data[i + c] for i in range(0, len(data), 3)) / count for c in range(3))
    return min(channels) > PLATE_PALE


def hall_buttons(png: bytes) -> list[tuple[int, int]]:
    """Where this building menu has a button to the right of a priced one.

    The way into the hall is a button on the 英雄殿堂's own menu, and no reader
    here can recognise it: it is a gold crown, which is artwork. What can be
    located is the 升級 button beside it, and the row is laid out from the middle
    outwards a fixed pitch apart, so one located button locates its neighbours.
    How many places along it is varies with the row, which is what `HALL_STEPS`
    is about.

    That neighbour is found from the resource icon alone rather than through
    `upgrade_buttons`, which also wants the price to read. Here the price is not
    needed for anything — what a button is gets settled by tapping it and
    reading the screen that came up — so wanting one would only be a second way
    to miss a menu that is really there.

    So this says where to try, not what is there. Plenty of buildings have a
    button in that place — a barracks has 訓練, a laboratory has 研究 — and each
    of them opens a screen that is backed out of for the cost of a capture.

    The one place that must never be tapped is a blue plate, which spends gems
    or a magic item, and those are dropped here rather than left to the caller.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"建築選單座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    found: list[tuple[int, int]] = []
    for test in (_is_gold, _is_elixir):
        for icon in _icon_centres(image, test):
            priced = icon - ICON_OFFSET
            if not _on_grid(priced):
                continue
            for step in range(1, HALL_STEPS + 1):
                centre = priced + BUTTON_PITCH * step
                if centre + PLATE_BOX[2] >= image.width:
                    break
                if _on_gem_plate(image, centre) or not _plated(image, centre):
                    continue
                if (centre, BUTTON_ROW_Y) not in found:
                    found.append((centre, BUTTON_ROW_Y))
    return found


def can_scroll(png: bytes, arrow: tuple[int, int]) -> bool:
    """Whether the row still has an arrow at this end to be tapped."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    across, down = ARROW_BOX
    box = image.crop((arrow[0] - across, arrow[1] - down, arrow[0] + across, arrow[1] + down))
    data = box.tobytes()
    pale = sum(
        min(data[offset], data[offset + 1], data[offset + 2]) > ARROW_PALE
        for offset in range(0, len(data), 3)
    )
    return pale / (len(data) // 3) > ARROW_SHARE


def _banners(image: Image.Image) -> Iterator[tuple[int, tuple[int, int, int]]]:
    """Where each whole card's banner sits, and the colour it is painted."""
    top, bottom = BANNER_BAND
    band = image.crop((0, top, image.width, bottom))
    width, height = band.size
    data = band.tobytes()

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        offset = (y * width + x) * 3
        return data[offset], data[offset + 1], data[offset + 2]

    lit = [
        x
        for x in range(width)
        if all(
            BANNER_FLOOR < max(pixel(x, y)) < BANNER_CEILING
            and max(pixel(x, y)) - min(pixel(x, y)) > BANNER_SPREAD
            for y in range(height)
        )
    ]
    for _, group in itertools.groupby(enumerate(lit), lambda pair: pair[1] - pair[0]):
        block = [value for _, value in group]
        if len(block) < MIN_BANNER_WIDTH:
            continue
        pixels = [pixel(x, y) for x in block for y in range(height)]
        red, green, blue = (sum(p[channel] for p in pixels) // len(pixels) for channel in range(3))
        yield (block[0] + block[-1]) // 2, (red, green, blue)


def _hero(tint: tuple[int, int, int]) -> HeroKind | None:
    """Whose card carries this colour, or None when it is nobody's.

    A colour that matches nobody is not reported as an unknown hero, it is
    dropped — because far more often than a hero the game has just added, it is
    some other screen. The 我的軍隊 screen puts its own heroes on tall coloured
    plates, and a run that took those for hall cards walked into it, called it
    the Hero Hall, and stopped sweeping for the real one.
    """
    hero, distance = min(
        (
            (name, sum((tint[channel] - known[channel]) ** 2 for channel in range(3)) ** 0.5)
            for name, known in HERO_TINTS.items()
        ),
        key=lambda pair: pair[1],
    )
    if distance > TINT_TOLERANCE:
        logger.debug(
            "A banner of %s matched no hero; nearest was %s at %.0f", tint, hero, distance
        )
        return None
    return hero


def _price(image: Image.Image, centre: int) -> int | None:
    """What this card's 升級 button asks for, or None where it does not read."""
    left, top, right, bottom = PRICE_BOX
    band = image.crop((centre + left, top, centre + right, bottom))
    width, height = band.size
    data = band.tobytes()
    mask = [
        [min(data[offset], data[offset + 1], data[offset + 2]) > PRICE_INK for offset in row]
        for row in (range(y * width * 3, (y + 1) * width * 3, 3) for y in range(height))
    ]
    price = digits_from(mask, PRICE_TOLERANCE)
    return price if price is not None and price >= MIN_PRICE else None


def _live_button(image: Image.Image, centre: int) -> bool:
    """Whether this card's 升級 button is green rather than greyed out.

    Read from the plate rather than from the price, because the price is there
    either way. A hero the hall's own level has capped shows the number it would
    cost with the button dead underneath it, which is indistinguishable from an
    affordable one to anything that only reads digits.
    """
    top, bottom = PLATE_BAND
    plate = image.crop((centre - PLATE_SPAN, top, centre + PLATE_SPAN, bottom))
    data = plate.tobytes()
    count = len(data) // 3
    red, green, blue = (sum(data[i + c] for i in range(0, len(data), 3)) / count for c in range(3))
    return green - max(red, blue) > BUTTON_LIVE


def _resource(image: Image.Image, centre: int) -> Literal["elixir", "dark"] | None:
    """What this button spends, or None where it is not a resource at all.

    None is 立即完成's gem, and it is the answer that matters: see `GEM_GREEN`
    for the live reading where that button came back as 268 dark on a live plate.
    A hero cannot be raised with gems, so a button asking for them is not an
    upgrade button and the caller is meant to treat the card as having no price.
    """
    left, right = ICON_SPAN
    top, bottom = ICON_BAND
    icon = image.crop((centre + left, top, centre + right, bottom))
    data = icon.tobytes()
    pixels = len(data) // 3
    channels = (sum(data[offset + c] for offset in range(0, len(data), 3)) for c in range(3))
    red, green, blue = (total / pixels for total in channels)
    if green - max(red, blue) > GEM_GREEN:
        return None
    lit = sum(
        _is_elixir(data[offset], data[offset + 1], data[offset + 2])
        for offset in range(0, len(data), 3)
    )
    # Measured, an elixir drop lights 0.42 of this box and a dark one 0.00.
    return "elixir" if lit / pixels > 0.2 else "dark"


def hero_cards(png: bytes) -> list[HeroCard]:
    """Every whole card the 英雄殿堂 is showing, left to right.

    A card with no readable price comes back with `price` unset rather than
    being left out, because the two questions a caller asks are different ones:
    whether this screen is the Hero Hall at all, which the cards answer, and
    whether a hero can be raised right now, which the price answers. A hero
    already being upgraded has a countdown where its button was, so it is on the
    screen and not on offer — and a run that treated that as "no Hero Hall here"
    would walk back out of the screen it was looking for.

    An empty list is therefore also the answer to "this is not the hall", which
    is what every caller here uses it for: a hero the colours do not know is
    dropped rather than reported, so a screen that merely has coloured plates on
    it comes back with nothing.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"英雄殿堂座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    cards: list[HeroCard] = []
    for centre, tint in _banners(image):
        hero = _hero(tint)
        if hero is None:
            continue
        # The resource decides whether there is a price at all: a gem button is
        # not an upgrade button, so the card reads as a hero already being
        # raised — which is what it is — rather than as a cheap one on offer.
        resource = _resource(image, centre)
        price = None if resource is None else _price(image, centre)
        cards.append(
            HeroCard(
                hero=hero,
                point=(centre, BUTTON_Y),
                price=price,
                resource=None if price is None else resource,
                upgradable=price is not None and _live_button(image, centre),
            )
        )
    return cards
