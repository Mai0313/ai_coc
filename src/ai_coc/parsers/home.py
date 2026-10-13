"""Read the home village's own overlay: what is waiting to be collected, and who is free.

Like `parsers.building`, none of this looks at the village underneath. A
collector is repainted at every level and sits wherever the player put it, so
what is read is the marker the game floats above it — a rounded speech bubble
carrying the resource's own icon, drawn in the same pixels whatever it is
standing on and wherever that happens to be.

The markers are found by colour and then held to a size, which is what separates
them from the village. Two of the three are the icon itself, because a gold coin
and an elixir drop are both far more saturated than anything the ground is
painted in.

Dark elixir's is nearly black, so it cannot be, and is found by the plate it
sits on instead. **The plate says whether there is anything to take**: a marker
on the orange plate is over a collector whose storage is full, which a tap
cannot empty, so only the pale plate counts (`FULL_PLATE_LEAD`).
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
from statistics import median

from ai_coc.models import Patch, ShieldState, ResourceBubble
from ai_coc.parsers.frame import open_frame
from ai_coc.parsers.scout import bar_ink
from ai_coc.parsers.world import DAY_SHIELD_PLUS, plus_button

# The digit reader and the mask it wants; nothing here is worth a second copy of
# either. It used to be reached for through `parsers.scout`, by private name,
# because that is where it happened to have been written.
from ai_coc.parsers.glyphs import (
    NOT_A_GLYPH,
    nearest,
    ink_mask,
    signature,
    glyph_columns,
    split_numbers,
)
from ai_coc.parsers.regions import mask, patches

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PIL import Image

    from ai_coc.models import World, PlateRole

# Where a marker can be. Outside this is the game's own furniture — the button
# columns down either side and the row along the bottom — and a marker cannot be
# under it because the furniture is drawn on top. The storage bars in the top
# right are the same, and they carry the very icons being looked for.
VILLAGE_AREA = (110, 80, 1480, 780)
# The bars down to the shield's, whose + buys a shield and whose bar opens the
# shop's shield page.
STORAGE_BARS = (1240, 0, 1600, 330)

# What each marker is made of: the channel ranges its own pixels fall in, and the
# size the patch of them comes out at. Measured on live frames before and after
# the camera was dragged — the coin segments to 21x16 and the drop to 18x25, each
# to within a pixel or two every time.
#
# **The size is doing most of the work.** Colour alone answers thirteen patches
# of gold and nine of magenta on a single village frame — storage bars, the shop
# button, spell factories, a burning building — and every one of them is either
# far larger or far smaller than a marker. Held to a size, what is left is
# exactly the markers on screen.
#
# `fill` is the tiebreak for the two that come closest: a marker's icon is a
# solid shape and fills half its own box or more, where the village patches that
# survive the size test are outlines and speckle at 0.32 and under.
MARKERS = (
    ("gold", ((236, 255), (181, 234), (0, 69)), (18, 24), (13, 20), 0.40),
    ("elixir", ((196, 255), (0, 114), (176, 255)), (16, 22), (21, 28), 0.50),
)
# **A full storage's markers sit on an orange plate**, the rest on a pale one,
# and a tap on the orange kind collects nothing. Told apart by how far the
# plate's red leads its green: measured as the median of the ring round each
# gold and elixir icon on every committed frame, the orange plate leads by 121
# to 158 and the pale one by -8 to 6. The 224 frames recorded on 2026-10-13
# took the orange down to 88, and the most anything else there led by was 49, a
# gold patch on the builder base's battle screen. A ring the frame blacked out
# leads by 0 and is no evidence of a full storage.
FULL_PLATE_LEAD = 60

# The dark marker, recognised by its icon and its plate. The icon itself is
# nearly black, measured at 24 to 25 across by 27 to 28 down, and what separates
# it from the twelve other dark shapes a village offers is the pale plate it is
# sitting on, written down (`PALE_PLATE`) and learned from the frame's own gold
# and elixir markers besides, since the game paints it per bubble.
#
# The ring is sampled tight, because the plate is only a few pixels wider than
# the icon: at 4 px and beyond it lands on the village behind the bubble, which
# measured takes the gap between a marker and the noise from 0.42-vs-0.03 down
# to nothing separable. The learning pass is allowed further out because it is
# sampling a plate it already knows is there.
DARK_ICON = ((0, 60), (0, 60), (0, 60))
DARK_ICON_ACROSS = (20, 30)
DARK_ICON_DOWN = (22, 34)
DARK_ICON_FILL = 0.30
PLATE_RING_PADS = (2, 3)
PLATE_LEARN_PADS = (3, 5)
# The written plate, as the tight ring reads it. Round every dark marker found it
# read (194 to 202, 199 to 208, 152 to 162) in the open and as low as
# (170, 170, 132) and (180, 180, 124) under the shade along the top of the
# screen, and this sits between the two: one end alone loses the other.
PALE_PLATE = (192, 198, 146)
PLATE_MATCH = 28
# Swept over every committed frame and the 224 recorded on 2026-10-13, every real
# dark bubble scores 0.426 or more against the written and learned plates, and
# the highest thing that is not one scores 0.333 — a dragon on
# `battle_in_progress.png` — so the line goes midway across that gap. Next come
# troops in other battles at 0.266, and on a village a grey boulder in dark
# foliage on `home_marker_over_bars.png` at 0.192. **A first pass put it at
# 0.20**, which cleared that boulder by 0.008 while the comment beside it claimed
# a gap of 0.39, because it was measured on the two frames the change was
# written from rather than on all of them. Obstacles are ordinary village
# furniture, so that floor is pinned by a test now.
PLATE_SHARE = 0.38

# The marker the game floats over the boat to the builder base: a pale plate
# with a ship on it and a strip of water along its bottom. The boat itself is
# scenery, moored wherever the scenery puts it, while this is drawn the same on
# every one. The water strip is found first and the plate above it confirms it.
# Measured: the strip reads (140, 229, 254) on a dark stone scenery and
# (136, 222, 246) on the jungle one, and the plate (215, 220, 185) and
# (212, 217, 182). The strip comes out 34 to 50 px across and 12 to 18 down,
# growing as the camera zooms in, and the plate covers 0.15 of the square above
# it on every one of 21 frames that carry it, against at most 0.02 for any other
# patch of that blue on every committed frame and a night of recorded ones.
BOAT_WATER = ((115, 165), (205, 255), (230, 255))
BOAT_PLATE = ((195, 230), (200, 235), (160, 200))
BOAT_ACROSS = (30, 56)
BOAT_DOWN = (9, 22)
BOAT_PLATE_SHARE = 0.08

# The two counters in the bottom right, idle over total: the builders' and the
# laboratory's. The 2026-10 overhaul moved them off the plate row along the top
# to just above the 建造 button, at the same place on both villages, so a box
# per role serves both. Measured on 0/6 and 0/2 at home and 0/3 and 0/1 on the
# builder base: the digits ink 1426-1482 and 1505-1559 over rows 681-702, and
# the portraits above them reach down to 679, which is where the boxes start.
COUNTER_BOXES: dict[PlateRole, tuple[int, int, int, int]] = {
    "builder": (1418, 680, 1492, 708),
    "lab": (1497, 680, 1572, 708),
}
# The slash between the two numbers is not a digit, and that is how it is found:
# it lands 35 to 39 from its nearest template on all four counters, while the
# digits on them land at 23 at worst (the builder base's lone 1). The line goes
# midway. The digits are larger than the old plates' were, which is why this is
# not the old plates' 19: at 19 that 1 dropped out and 0/1 read as 0 alone.
COUNTER_TOLERANCE = 29

# 建造 opens one panel for both counters, full height on the right: running
# rows, then 建議升級 and 其他升級, with a tab per counter along its foot. It
# opens on whatever tab was last open, so a reader taps the tab it wants.
BUILD_BUTTON = (1495, 800)
PANEL_TABS: dict[tuple[World, PlateRole], tuple[int, int]] = {
    ("day", "builder"): (1222, 850),
    ("day", "lab"): (1330, 850),
    ("night", "builder"): (1277, 850),
    ("night", "lab"): (1492, 850),
}
# **`back` does not shut it**, measured on the builder base: the panel stayed up
# and only the camera moved. Its own red X does.
PANEL_CLOSE = (1572, 30)
# The X is a red plate, which no other screen draws there: it fills 0.36 of this
# box on every panel recorded and under 0.1 on every other frame, committed or
# recorded. The title bar beside it is the second reading, a flat grey-brown
# around (128, 113, 98) that a village never paints over the whole box.
PANEL_X_BOX = (1554, 12, 1592, 50)
PANEL_X_RED = 0.25
PANEL_TITLE_BOX = (1250, 8, 1330, 22)
PANEL_TITLE = (128, 113, 98)
PANEL_TITLE_SPREAD = 14
PANEL_TITLE_SHARE = 0.8

# Each running row is itself a progress bar the width of the panel: a green fill
# from the left over a grey-brown track, with the name on the left and the
# countdown on the right. **The fill and the track are the only thing that says
# which rows those are**: the 建議升級 and 其他升級 rows under them are light
# brown and carry a time as well, and the section headers between are dark
# brown. Read in a band clear of the name and the countdown on every panel
# recorded, along the top half of each row, where the track is (101, 82, 71)
# give or take and the fill at its brightest; the bottom halves are both darker.
# Rows are 48 px apart and about 38 tall, so a top found within `ROW_GAP` of the
# last is the same row again.
ROW_BAND = (1300, 1460)
ROW_COVERAGE = 0.8
ROW_GAP = 40
PANEL_TOP, PANEL_BOTTOM = 60, 800

# The countdown, right-aligned against the clock on each running row, measured
# from the row's top: the text runs to x 1561 and the clock starts at 1571. The
# box reaches above and below the text, for the reason `TIME_TEXT_ROWS` gives.
TIME_BOX = (1420, 2, 1566, 32)
# **The panel writes smaller than the old one did**, about 12 rows for a digit
# and less for 小時, so glyphs are measured from six rows rather than the usual
# twelve. A digit is believed at 39 from its template and at least 3 px wide:
# measured over every countdown on the recorded panels, digits land at 34 at
# worst (a 1, the narrowest and least stable), while the pieces 小時, 分鐘 and
# 天 fall into come back at 44 and over — except 小's centre stroke, 1 px wide,
# which reads 30 from `0`. The line goes midway.
TIME_FLOOR = 6
TIME_DIGIT_TOLERANCE = 39
TIME_DIGIT_WIDTH = 3
# **A row an apprentice or a goblin works on carries their portrait right
# against its countdown**, and cut to the text's own height the apprentice's
# ring came out as a piece the height of a digit, 37 from a 1, 2 px before the
# real first digit: 1天 would have read as 11天. Seen whole, every portrait
# piece runs 15 rows or more (the goblin's face 15, the lab assistant's 16, the
# apprentice's ring 25), while every digit and unit runs 12 at most, so a span
# taller than this is a portrait and is left out of the reading altogether.
TIME_TEXT_ROWS = 13
# How close something text-sized that is not a digit can sit before the first
# number and still be something else. A digit missed there would be dropped,
# and 16小時 would read as 6: the gap between two digits is 1 or 2 px.
TIME_LEAD_GAP = 4
# The unit after the first number is read whole, as one shape from the end of
# that number to the start of the next, because the characters fall apart at
# this size: 小 into three strokes, 時 and 分 into two each. 天 is one character
# and comes out 9 or 10 px wide against 21 or 22 for the two-character units;
# 小時 and 分鐘 land 0 to 2 bits from their own template over eleven rows and 26
# to 28 from each other's.
DAY_WIDTH = 14
UNIT_WORDS = {
    3600: 3991578210008329590722701825776025600,
    60: 10645515507765239062307640030384884224,
}
# **天 is held to its shape as well as its width**, because 秒 is one character
# too, about as wide, and 45秒 taken for 天 is 45 days. It renders five ways on
# the recorded panels, in two families 41 to 47 bits apart, so each is kept.
DAY_SHAPES = (
    65398079761088050085476826099556345972481,
    65398079761088050085476844184319308596737,
    1393795308048637152504369991213948321664259,
    1393795308048617384187137361284480929105409,
    1393795266469657850937188490023723944119555,
)
UNIT_TOLERANCE = 13
UNIT_LADDER = (86400, 3600, 60, 1)

# The shield is a bar of its own now, under the gems bar, writing 無 or a
# countdown right-aligned beside the shield icon. The box stops short of the
# green + on its left, which buys a shield with gems.
SHIELD_BOX = (1375, 284, 1525, 312)
# 無 against a countdown, by how far the writing reaches rather than by what it
# says: 無 inks 1502-1517 and falls into three or four column spans depending on
# what shows through the bar, while a countdown is at least a digit and a unit,
# 30 px or more at this size.
SHIELD_NONE_WIDTH = 24


def _in_storage_bars(point: tuple[int, int]) -> bool:
    left, top, right, bottom = STORAGE_BARS
    return left <= point[0] <= right and top <= point[1] <= bottom


def _ring(
    village: bytes, size: tuple[int, int], patch: Patch, pads: tuple[int, ...]
) -> list[tuple[int, int, int]]:
    """The pixels just outside a patch, which for an icon in a bubble is its plate.

    Raw bytes rather than `getpixel`, three per RGB pixel and typed as integers,
    which is how every other reader in this package walks an image.
    """
    width, height = size
    out: list[tuple[int, int, int]] = []
    for pad in pads:
        edges = [
            (x, y)
            for x in range(patch.left - pad, patch.right + pad + 1, 2)
            for y in (patch.top - pad, patch.bottom + pad)
        ]
        edges += [
            (x, y)
            for y in range(patch.top - pad, patch.bottom + pad + 1, 2)
            for x in (patch.left - pad, patch.right + pad)
        ]
        for x, y in edges:
            if 0 <= x < width and 0 <= y < height:
                at = (y * width + x) * 3
                out.append((village[at], village[at + 1], village[at + 2]))
    return out


def _plate(village: bytes, size: tuple[int, int], patch: Patch) -> tuple[int, int, int] | None:
    """The plate one marker's icon sits on, as the median of a ring around it."""
    ring = _ring(village, size, patch, PLATE_LEARN_PADS)
    if not ring:
        return None
    red, green, blue = (round(median(pixel[band] for pixel in ring)) for band in range(3))
    return red, green, blue


def _dark_bubbles(image: Image.Image, plates: list[tuple[int, int, int]]) -> Iterator[Patch]:
    """Dark elixir markers worth tapping: the nearly black icon, on a pale plate.

    `plates` is the pale plate as this frame's gold and elixir markers draw it,
    with `PALE_PLATE` among them for a frame that has none to learn from — one
    whose gold and elixir storages are full draws every one of those orange.
    **One reading per bubble rather than one per frame**, because the game
    paints the plate per bubble. A plate in some third colour yields nothing,
    quietly: that costs a collector left standing until the next pass, far less
    than a reader that guesses would cost.
    """
    size = image.size
    village = image.tobytes()
    for patch in patches(mask(image, DARK_ICON), *size):
        if not patch.sized(DARK_ICON_ACROSS, DARK_ICON_DOWN, DARK_ICON_FILL):
            continue
        ring = _ring(village, size, patch, PLATE_RING_PADS)
        if not ring:
            continue
        for plate in plates:
            near = sum(
                all(abs(pixel[band] - plate[band]) <= PLATE_MATCH for band in range(3))
                for pixel in ring
            )
            if near / len(ring) >= PLATE_SHARE:
                yield patch
                break


def collect_bubbles(png: bytes) -> list[ResourceBubble]:
    """Every collector marker worth tapping on this frame, in reading order.

    A marker is what the game puts above a collector that has something waiting,
    and tapping one collects it — no menu, no confirmation. It disappears once
    collected, so a second read is what says whether a tap landed. One on the
    orange plate is left out: its storage is full and the tap takes nothing.
    """
    image = open_frame(png)
    left, top, right, bottom = VILLAGE_AREA
    village = image.crop(VILLAGE_AREA)
    width, height = right - left, bottom - top
    data = village.tobytes()
    found: list[ResourceBubble] = []
    plates = [PALE_PLATE]
    for resource, ranges, across, down, fill in MARKERS:
        for patch in patches(mask(village, ranges), width, height):
            if not patch.sized(across, down, fill):
                continue
            middle = (patch.middle[0] + left, patch.middle[1] + top)
            if _in_storage_bars(middle):
                continue
            plate = _plate(data, (width, height), patch)
            if plate is not None and plate[0] - plate[1] >= FULL_PLATE_LEAD:
                continue
            if plate is not None:
                plates.append(plate)
            found.append(ResourceBubble(resource=resource, point=middle))
    for patch in _dark_bubbles(village, plates):
        middle = (patch.middle[0] + left, patch.middle[1] + top)
        if not _in_storage_bars(middle):
            found.append(ResourceBubble(resource="dark", point=middle))
    return sorted(found, key=lambda bubble: (bubble.point[1], bubble.point[0]))


def boat_marker(png: bytes) -> tuple[int, int] | None:
    """The middle of the boat's marker, which sails when tapped; None where none is drawn.

    **A scenery bought from the shop zooms out further than the free ones, and
    past that point the game draws no markers at all** — measured on one, the
    far zoom hid this one and every collector's, and one pinch back in brought
    them back. So None on a home village's park is how `ui.world.park_camera`
    tells such a scenery apart, and it zooms in to where a free one stops.
    """
    image = open_frame(png).crop(VILLAGE_AREA)
    left, top = VILLAGE_AREA[:2]
    width, height = image.size
    for patch in patches(mask(image, BOAT_WATER), width, height):
        across = patch.right - patch.left + 1
        if not patch.sized(BOAT_ACROSS, BOAT_DOWN, 0):
            continue
        plate = mask(
            image.crop((patch.left, max(0, patch.top - across), patch.right + 1, patch.top)),
            BOAT_PLATE,
        )
        if plate and plate.count(255) / len(plate) >= BOAT_PLATE_SHARE:
            return left + patch.left + across // 2, top + patch.top - across // 2
    return None


def plate_count(png: bytes, role: PlateRole) -> tuple[int, int] | None:
    """Idle over total on one counter in the bottom right, or None where it will not read.

    The game counts the ones standing around, not the ones at work, so zero here
    means every builder, or every research slot, is busy. A count nobody can
    read is worth less than nothing if it is guessed at, so anything but two
    numbers is None.
    """
    box = COUNTER_BOXES[role]
    found = split_numbers(ink_mask(open_frame(png).crop(box)), COUNTER_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]


def free_builders(png: bytes) -> tuple[int, int] | None:
    """Idle builders over total, or None where this screen is not showing them.

    **None costs each caller something different, and the cheap-looking one is
    the expensive one.** `UpkeepRunner.upgrade` and `HeroRunner` both stand the
    run down on it, which is loud and harmless. `WallRunner` does the opposite:
    its check is `counted is not None and counted[0] == 0`, so an unread counter
    silently never fires the "every builder is busy" branch and the run walks the
    rest of the village at forty seconds a wall, buying nothing.
    """
    return plate_count(png, "builder")


def build_panel_open(png: bytes) -> bool:
    """Whether the panel 建造 opens is up, by its red X and the title bar beside it.

    **It is not a village by any test the loops have**: it shifts the corner
    column left, so `current_world` reads nothing, and it covers the right of
    the map. `back` does not shut it, so whatever finds it presses
    `PANEL_CLOSE`.
    """
    image = open_frame(png)
    red = image.crop(PANEL_X_BOX).tobytes()
    reds = sum(
        1
        for at in range(0, len(red), 3)
        if red[at] >= 180 and red[at + 1] <= 80 and red[at + 2] <= 80
    )
    title = image.crop(PANEL_TITLE_BOX).tobytes()
    flat = sum(
        1
        for at in range(0, len(title), 3)
        if all(
            abs(title[at + channel] - PANEL_TITLE[channel]) <= PANEL_TITLE_SPREAD
            for channel in range(3)
        )
    )
    return reds / (len(red) // 3) >= PANEL_X_RED and flat / (len(title) // 3) >= PANEL_TITLE_SHARE


def _row_tops(image: Image.Image) -> list[int]:
    """The top of each running row, read off its bar along `ROW_BAND`."""
    width = ROW_BAND[1] - ROW_BAND[0]
    data = image.crop((ROW_BAND[0], PANEL_TOP, ROW_BAND[1], PANEL_BOTTOM)).tobytes()
    wanted = len(range(0, width, 4)) * ROW_COVERAGE
    tops: list[int] = []
    for row in range(PANEL_BOTTOM - PANEL_TOP):
        base = row * width * 3
        covered = 0
        for at in range(base, base + width * 3, 12):
            red, green, blue = data[at], data[at + 1], data[at + 2]
            track = 90 <= red <= 112 and 74 <= green <= 92 and 60 <= blue <= 78
            fill = green >= 150 and green - red >= 40 and green - blue >= 80
            covered += track or fill
        y = PANEL_TOP + row
        if covered >= wanted and (not tops or y - tops[-1] > ROW_GAP):
            tops.append(y)
    return tops


def _time_runs(ink: list[list[bool]]) -> list[list[int | str]]:
    """A countdown row as runs of digits and of everything else, each with its span.

    A run of digits carries them as its first item and any other run an empty
    one. A portrait in front of the number is left out by its height.
    """
    runs: list[list[int | str]] = []
    for left, right in glyph_columns(ink, speckle=False):
        rows = [y for y, row in enumerate(ink) if any(row[left:right])]
        if rows[-1] - rows[0] + 1 > TIME_TEXT_ROWS:
            continue
        pattern = signature(ink, left, right, TIME_FLOOR)
        digit, distance = nearest(pattern) if pattern is not None else ("", NOT_A_GLYPH)
        kind = (
            digit if distance <= TIME_DIGIT_TOLERANCE and right - left >= TIME_DIGIT_WIDTH else ""
        )
        if runs and bool(runs[-1][0]) == bool(kind):
            runs[-1][0] = f"{runs[-1][0]}{kind}"
            runs[-1][2] = right
        else:
            runs.append([kind, left, right])
    return runs


def _countdown(ink: list[list[bool]]) -> int | None:
    """One countdown as seconds: a number, a unit, and usually a number in the unit below.

    The unit after the first number is the only one read, because the game
    writes them in descending order and adjacent, so knowing the first settles
    the second.
    """
    runs = _time_runs(ink)
    while runs and not runs[0][0]:
        if len(runs) > 1 and int(runs[1][1]) - int(runs[0][2]) < TIME_LEAD_GAP:
            return None
        runs.pop(0)
    if len(runs) < 2:
        return None
    left, right = int(runs[1][1]), int(runs[1][2])
    pattern = signature(ink, left, right, TIME_FLOOR)
    if pattern is None:
        return None
    if right - left <= DAY_WIDTH:
        if min((shape ^ pattern).bit_count() for shape in DAY_SHAPES) > UNIT_TOLERANCE:
            return None
        scale = 86400
    else:
        scale = min(UNIT_WORDS, key=lambda unit: (UNIT_WORDS[unit] ^ pattern).bit_count())
        if (UNIT_WORDS[scale] ^ pattern).bit_count() > UNIT_TOLERANCE:
            return None
    numbers = [int(str(run[0])) for run in runs if run[0]]
    below = UNIT_LADDER[UNIT_LADDER.index(scale) + 1]
    return numbers[0] * scale + (numbers[1] * below if len(numbers) > 1 else 0)


def panel_rows(png: bytes) -> list[int | None] | None:
    """Each running row's countdown, **in the order the panel draws them**.

    None for the whole list means the panel is not on screen at all; an empty
    list is a panel with nothing running on its tab, which the old plate panel
    could not tell apart from one that never opened. None for one row is a
    countdown that would not resolve; the row is kept, because something is in
    it and dropping it would report the tab as emptier than it is.

    These are the panel's own countdowns, at normal speed. `status` reads the
    export instead, which works through whatever speed-ups are running.
    """
    if not build_panel_open(png):
        return None
    image = open_frame(png)
    left, above, right, below = TIME_BOX
    return [
        _countdown(ink_mask(image.crop((left, top + above, right, top + below))))
        for top in _row_tops(image)
    ]


def jobs_strip(png: bytes) -> bytes | None:
    """The panel from its first row down, as one PNG for a model to read.

    Cropped rather than read, the same bargain `parsers.building.name_strip`
    makes: what is being raised is written in Chinese, no parser here reads any,
    and a strip is what a cheap model is actually for. **Down to the bottom of
    the panel**, so the 建議升級 rows come along with the running ones; the
    其他升級 rows under them only show in full once the panel is scrolled, so
    they are left to the model to skip.
    """
    if not build_panel_open(png):
        return None
    out = io.BytesIO()
    open_frame(png).crop((PANEL_X_BOX[0] - 384, PANEL_TOP, 1600, PANEL_BOTTOM)).save(
        out, format="PNG"
    )
    return out.getvalue()


def shield_state(png: bytes) -> ShieldState | None:
    """What the shield bar says, or None where this frame will not resolve it.

    The bar writes either a countdown or 無 when nothing is protecting the
    village. **Those are told apart by how far the writing reaches rather than
    by reading any of it** (`SHIELD_NONE_WIDTH`). The bar is translucent, so
    the ink is a storage bar's, which leaves out what the village puts behind
    it. A frame without the bar's own + is not showing the bar at all, and
    answers None rather than 無.

    A shield that is up answers None, the way a countdown that would not
    resolve always has: its time is not read, on purpose. The user has said
    more than once that it does not matter, since loot is cheap and a few
    battles win back what a raid takes (2026-10-10, 2026-10-13).
    """
    image = open_frame(png)
    if not plus_button(image, DAY_SHIELD_PLUS):
        return None
    ink = bar_ink(image.crop(SHIELD_BOX))
    spans = glyph_columns(ink, speckle=False)
    if not spans:
        return None
    if spans[-1][1] - spans[0][0] <= SHIELD_NONE_WIDTH:
        return ShieldState()
    return None
