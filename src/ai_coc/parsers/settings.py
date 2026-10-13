"""The game's own settings windows, which is where the village export lives.

Clash of Clans can write the whole village out as JSON, behind the avatar →
設定 tab → 更多設定 → scroll to the bottom → 複製. Since the 2026-10 overhaul
settings is a tab of the profile window rather than a page behind a gear. Every
step is a fixed coordinate, so what these
readers are for is refusing to tap when the screen is not the one the
coordinate was measured on.

That matters most on the scrolled page. The top of 更多設定 carries a red
刪除帳號 button; it sits well above the copy button and scrolling only pushes it
further up, so it is never in danger — but a scroll that did not take leaves the
copy coordinate on the 更高幀數 toggle, which flips a game setting and reports
nothing. Reading the row first is what makes a missed scroll a refusal instead.

Like every reader here these look at the UI the game paints and never at the
thing underneath: a button plate, and the word JSON, which is latin text the
game leaves untranslated in every language it ships.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .frame import open_frame

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL import Image

# The red X every page of the profile window closes on. It fills 0.35 of this
# box on all of them; two sheets from before the overhaul drew theirs in the
# same place at 0.33, and nothing else recorded reaches 0.03. An X there is a
# window to close, whichever window it is.
WINDOW_X_BOX = (1375, 48, 1422, 95)
WINDOW_X_RED = 0.2

# The settings page, read as two features because the button alone is not
# enough. Its 更多設定 plate measures 0.40 green, but battle result screens put
# 0.59 to 0.80 of their own 回營 button in the same box and the shield sheet
# 0.69. What no other screen has is the SUPERCELL ID band across the top: 0.82
# blue here against at most 0.36 (a loading screen) everywhere else. Measured
# on 2026-10-13 over every frame recorded that day and every committed one.
MORE_BUTTON_BOX = (700, 775, 905, 815)
MORE_BUTTON_GREEN = 0.20
BANNER_BOX = (400, 130, 1200, 210)
BANNER_BLUE = 0.50

# The back arrow in the window's top left, which only the more-settings page
# has: 0.75 there against 0.01 at most anywhere else.
BACK_ARROW_BOX = (100, 45, 145, 110)
BACK_ARROW_GREEN = 0.20

# The export row, read as two features because neither separates it alone. The
# white word JSON measures 0.39 when the list is at the bottom — but 0.93 on the
# settings page and 1.0 on the loot cart's sheet, whose pale panels fill that
# box. The 複製 plate beside it measures 0.46 against at most 0.75 elsewhere (the
# builder base's attack menu, which has no JSON). Together they answer only on
# the row itself.
JSON_LABEL_BOX = (362, 557, 428, 584)
JSON_LABEL_WHITE = 0.18
COPY_BUTTON_BOX = (1080, 555, 1250, 590)
COPY_BUTTON_GREEN = 0.30
COPY_BUTTON = (1163, 571)


def _ratio(image: Image.Image, box: tuple[int, int, int, int], lit: Callable[..., bool]) -> float:
    data = image.crop(box).tobytes()
    hits = sum(lit(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 3))
    return hits / (len(data) // 3)


def _green(r: int, g: int, b: int) -> bool:
    return g > 150 and g - max(r, b) > 45


def _white(r: int, g: int, b: int) -> bool:
    return min(r, g, b) > 200


def _blue(r: int, g: int, b: int) -> bool:
    return b > 120 and b - max(r, g) > 40


def _red(r: int, g: int, b: int) -> bool:
    return r >= 180 and g <= 80 and b <= 80


def window_open(png: bytes) -> bool:
    """Whether a window with its red X in the profile window's place is up."""
    return _ratio(open_frame(png), WINDOW_X_BOX, _red) >= WINDOW_X_RED


def settings_open(png: bytes) -> bool:
    """Whether the 設定 window is up, by its 更多設定 button and its account band."""
    image = open_frame(png)
    return (
        _ratio(image, MORE_BUTTON_BOX, _green) >= MORE_BUTTON_GREEN
        and _ratio(image, BANNER_BOX, _blue) >= BANNER_BLUE
    )


def more_settings_open(png: bytes) -> bool:
    """Whether the 更多設定 page is up, by the back arrow only it carries.

    True at any scroll position, which is what a caller swiping the list needs:
    it says the swipes are landing on the right page, and `export_row` says
    whether they have gone far enough.
    """
    return _ratio(open_frame(png), BACK_ARROW_BOX, _green) >= BACK_ARROW_GREEN


def export_row(png: bytes) -> tuple[int, int] | None:
    """Where the 複製 button is on the JSON export row, or None if it is not there.

    None covers both "not scrolled far enough" and "this is not that page at
    all", which are the same instruction to a caller: do not tap.
    """
    image = open_frame(png)
    if _ratio(image, JSON_LABEL_BOX, _white) < JSON_LABEL_WHITE:
        return None
    if _ratio(image, COPY_BUTTON_BOX, _green) < COPY_BUTTON_GREEN:
        return None
    return COPY_BUTTON
