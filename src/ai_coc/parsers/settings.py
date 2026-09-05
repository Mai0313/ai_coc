"""The game's own settings windows, which is where the village export lives.

Clash of Clans can write the whole village out as JSON, behind 設定 → 更多設定 →
scroll to the bottom → 複製. Every step is a fixed coordinate, so what these
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

# The settings window, read as two features because the button alone is not
# enough. Its 更多設定 plate measures 0.422 green, but a battle result screen puts
# 0.262 of its own 回營 button in the same box and an event reward page 0.205 —
# swept over every committed frame, which is how those two turned up. What no
# other screen has is the SUPERCELL ID band across the top: 0.806 blue here
# against at most 0.047 on all 66 of them, so the line goes nowhere near either
# reading. The more-settings page reaches 0.30 there, being a list of blue rows,
# and is ruled out by the button instead.
MORE_BUTTON_BOX = (700, 750, 890, 790)
MORE_BUTTON_GREEN = 0.20
BANNER_BOX = (400, 160, 1200, 230)
BANNER_BLUE = 0.50

# The back arrow, which only the more-settings page has: 0.412 there against
# 0.000 on the settings window it replaces and 0.041 on a village.
BACK_ARROW_BOX = (295, 85, 385, 125)
BACK_ARROW_GREEN = 0.20

# The export row, read as two features because neither separates it alone. The
# white word JSON measures 0.280 when the list is at the bottom, 0.094 at the
# top of the same page — but 0.938 on the settings window, whose cream panel
# fills that box. The 複製 plate beside it measures 0.452 against 0.091 and
# 0.000 for those two. Together they answer only on the row itself.
JSON_LABEL_BOX = (378, 542, 446, 574)
JSON_LABEL_WHITE = 0.18
COPY_BUTTON_BOX = (1080, 545, 1185, 578)
COPY_BUTTON_GREEN = 0.30
COPY_BUTTON = (1131, 560)


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
