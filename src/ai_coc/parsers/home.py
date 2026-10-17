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

from typing import TYPE_CHECKING
from statistics import median

from PIL import Image

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

    from ai_coc.models import PlateRole

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

# The marker over the builder base's loot cart while it holds elixir: a khaki
# plate, the day collectors' dimmed by the night, pointing down at the cart. In
# the band the parked camera has put it in: measured on five parks from
# different starts, the middle of the drop on it sat at x 1059-1078, y 105-120,
# and on the park that missed with every fixed spot at (1096, 71), so the band
# runs from the top of the frame. An empty cart has no marker.
#
# **Found by its plate, never by the icon on it**, because the icon changes
# under a still camera: a drop for a while, then two crossed axes. A burst of a
# parked cart holding 656 538 read the axes for 4 s, the drop for at least 23 s,
# then the axes for all of a 96 s burst, so the drop alone was missed on most
# visits (#415). The plate reads (128, 128, 114) at its top edge down to
# (180, 175, 125) at its point on both icons, 39-40 across by 43-45 down at a
# fill of 0.46-0.55. The axes' blades can cut it in two: the lower part, 40x28
# or 40x30 at 0.48-0.50, ends on the same point, which is why the point is what
# is returned; the upper part, 40x14 or 40x15 at 0.35-0.415, is turned away by
# its height, since its fill comes within 0.005 of the floor. Swept over the
# committed frames and 934 recorded on 2026-10-16, the nearest anything else of
# this size came was a home village plate at 0.37-0.38 (40x26, 38x24), so the
# fill floor sits about 0.04 from each side, and nothing on a builder base. The
# width keeps out the open cart sheet's plank, 220 across at 1.00, and the gold
# bar's coin, 22x32, which the open build panel shifts into the band's corner.
#
# **A full cart's plate is orange instead**, (148, 66, 35) down to (202, 53, 22),
# as a home collector's is over a full storage, and in that colour a home
# village shield rim comes to 37x31 at 0.44, past every limit above. So on it
# the drop is what is read: the orange plate carried the drop on all 90 frames of
# a 140 s burst of a full cart, and on #415's one frame of another. The drop's
# middle sits 27 px above the plate's point on every frame that has both. It is
# the night's dimmed drop, red 81 to 173 where `MARKERS` wants 196 and up, 20-22
# across by 26-27 down at a fill of 0.61; the nearest thing in its colour is the
# cart's own bottles beside it, 17x8 and 11x11.
CART_MARKER_BAND = (980, 0, 1200, 200)
CART_MARKER_PLATE = ((125, 185), (120, 180), (90, 135))
CART_MARKER_ACROSS = (36, 44)
CART_MARKER_DOWN = (24, 48)
CART_MARKER_FILL = 0.42
CART_DROP = ((80, 180), (0, 125), (145, 245))
CART_DROP_ACROSS = (17, 25)
CART_DROP_DOWN = (23, 30)
CART_DROP_FILL = 0.45
CART_DROP_ABOVE_POINT = 27

# The marker the game floats over the builder base's clock tower (時光鐘樓) while
# its free boost is ready, a khaki plate with a clock face on it. It goes while
# the tower is selected and comes back when it is not, and there is none while
# the boost cools down, so it is the ready signal as well as the tower's place.
# The face is what is found, held to two things around it, each of which alone
# matched something else. On the two parked frames that carry it (2026-10-17)
# the face measured 23x21 at a fill of 0.56, the khaki plate took 0.27 of the
# box 14 px around it, and the face's brown octagon rim 0.15 of the box 3 px
# around it. Swept over the 163 frames then on disk: the plate alone also took
# the crossed swords on `welcome_back` (0.30) and a clan chat portrait (0.18),
# neither with any rim (under 0.03); the rim alone took the small clock the
# builder base draws on a boosted building's timer (0.14), with no plate around
# it (under 0.07). The tower's own pale stone came 12-16 across at 0.26-0.42.
CLOCK_FACE = ((215, 250), (195, 245), (215, 252))
CLOCK_FACE_ACROSS = (18, 30)
CLOCK_FACE_DOWN = (16, 28)
CLOCK_FACE_FILL = 0.45
CLOCK_PLATE = ((160, 205), (165, 210), (110, 180))
CLOCK_PLATE_REACH = 14
CLOCK_PLATE_SHARE = 0.15
CLOCK_RIM = ((110, 180), (65, 125), (35, 95))
CLOCK_RIM_REACH = 3
CLOCK_RIM_SHARE = 0.08
# Where the tower stands under the face: tapped this far below it, it was selected.
CLOCK_TOWER_BELOW = 78

# A reward chest the game raises over the home village after some battles,
# full screen (a 普通 one, met 2026-10-16): `back` does nothing to it, so an
# attack series ended `no_attack_menu` five rounds running beneath it. Every tap
# on the chest spends one of the hammers on the strip under it, white unused and
# black once used, and the last one opens it. Read by its unused hammers: a white
# head 35x27 at 0.57 fill with an orange handle 15x22 at about 0.2 straight
# below it, the heads at x 669-881 a pitch of 70-73 apart, y 728. Swept over
# 254 frames, the committed ones and those recorded on 2026-10-16, nothing else
# in the strip pairs the two: the 4x speed button's white comes 37x40 with no
# handle, the loading screen's badge 22x19 with none.
CHEST_STRIP = (640, 722, 940, 788)
CHEST_HEAD = ((186, 255), (186, 255), (186, 255))
CHEST_HEAD_ACROSS = (30, 40)
CHEST_HEAD_DOWN = (24, 32)
CHEST_HEAD_FILL = 0.45
CHEST_HANDLE = ((150, 230), (70, 135), (20, 80))
CHEST_HANDLE_ACROSS = (11, 19)
CHEST_HANDLE_DOWN = (18, 26)
CHEST_HANDLE_FILL = 0.12
# How far a handle may sit from straight under its head, either way: measured 3
# left and 2 up into the head's bottom row on every hammer.
CHEST_HANDLE_REACH = 8

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
# opens on whatever tab was last open.
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
# How far apart running rows sit and how tall each is, for `running_rows`.
RUNNING_STEP = 60
RUNNING_HEIGHT = 44

# How short a piece of a countdown may be and still be measured. Set for the
# build panel's countdowns, written about 12 rows a digit and less for 小時;
# their reader is gone, and the shield bar's countdown, the one left, writes
# its digits about 22 rows tall, so this floor only drops speckle there.
TIME_FLOOR = 6
UNIT_LADDER = (86400, 3600, 60, 1)

# The shield is a bar of its own now, under the gems bar, writing 無 or a
# countdown right-aligned beside the shield icon. The box starts just right of
# the dark border of the green + on its left, which buys a shield with gems: a
# two-digit hour reaches past x 1375, where 5小時 36分鐘 started at 1383.
SHIELD_BOX = (1371, 284, 1525, 312)
# 無 against a countdown, by how far the writing reaches rather than by what it
# says: 無 inks 1502-1517 and falls into three or four column spans depending on
# what shows through the bar, while a countdown is at least a digit and a unit,
# 30 px or more at this size.
SHIELD_NONE_WIDTH = 24
# **A countdown on the bar is written in the storage bars' font**, about twice
# the panel's size. Its digits read off the shared templates the way the bars'
# own do (18 at worst on the frames recorded with a shield up, and at least 7 px
# wide where the units' strokes are 2 to 7), and its units do not: their strokes
# are too thin for the 10x14 cell, where 小時 and 分鐘 came out one bit apart. So
# a unit is compared as an 18x10 cell where any ink counts, which measured 86
# bits between 小時 and 分鐘 and 0 between four frames of the same word. 天 and 秒 have
# not been seen on the bar yet, so a countdown in them reads as none at all.
SHIELD_TEXT_ROWS = 22
SHIELD_DIGIT_TOLERANCE = 26
SHIELD_DIGIT_WIDTH = 6
SHIELD_UNIT_CELL = (18, 10)
SHIELD_UNIT_INK = 40
SHIELD_UNIT_WORDS = {
    3600: 49211869528294647620196602586854557406177924202389505,
    60: 25070008390483987176172767018809866025469748488181828,
}
SHIELD_UNIT_TOLERANCE = 30


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


def cart_marker(png: bytes) -> tuple[int, int] | None:
    """Where the loot cart's marker points, the bottom of its plate, or None where none is drawn."""
    left, top = CART_MARKER_BAND[:2]
    band = open_frame(png).crop(CART_MARKER_BAND)
    for patch in patches(mask(band, CART_MARKER_PLATE), *band.size):
        if patch.sized(CART_MARKER_ACROSS, CART_MARKER_DOWN, CART_MARKER_FILL):
            return left + patch.middle[0], top + patch.bottom
    for patch in patches(mask(band, CART_DROP), *band.size):
        if patch.sized(CART_DROP_ACROSS, CART_DROP_DOWN, CART_DROP_FILL):
            return left + patch.middle[0], top + patch.middle[1] + CART_DROP_ABOVE_POINT
    return None


def clock_tower_marker(png: bytes) -> tuple[int, int] | None:
    """The middle of the clock face over a clock tower whose free boost is ready, or None."""
    left, top = VILLAGE_AREA[:2]
    image = open_frame(png).crop(VILLAGE_AREA)
    width, height = image.size

    def share(patch: Patch, reach: int, colour: tuple[tuple[int, int], ...]) -> float:
        around = mask(
            image.crop((
                max(0, patch.left - reach),
                max(0, patch.top - reach),
                min(width, patch.right + reach + 1),
                min(height, patch.bottom + reach + 1),
            )),
            colour,
        )
        return around.count(255) / len(around)

    for patch in patches(mask(image, CLOCK_FACE), width, height):
        if (
            patch.sized(CLOCK_FACE_ACROSS, CLOCK_FACE_DOWN, CLOCK_FACE_FILL)
            and share(patch, CLOCK_PLATE_REACH, CLOCK_PLATE) >= CLOCK_PLATE_SHARE
            and share(patch, CLOCK_RIM_REACH, CLOCK_RIM) >= CLOCK_RIM_SHARE
        ):
            return left + patch.middle[0], top + patch.middle[1]
    return None


def reward_chest(png: bytes) -> int:
    """How many unused hammers a reward chest has left; 0 where no chest is up."""
    strip = open_frame(png).crop(CHEST_STRIP)
    heads = [
        patch
        for patch in patches(mask(strip, CHEST_HEAD), *strip.size)
        if patch.sized(CHEST_HEAD_ACROSS, CHEST_HEAD_DOWN, CHEST_HEAD_FILL)
    ]
    handles = [
        patch
        for patch in patches(mask(strip, CHEST_HANDLE), *strip.size)
        if patch.sized(CHEST_HANDLE_ACROSS, CHEST_HANDLE_DOWN, CHEST_HANDLE_FILL)
    ]
    return sum(
        any(
            abs(handle.left - head.left) <= CHEST_HANDLE_REACH
            and abs(handle.top - head.bottom) <= CHEST_HANDLE_REACH
            for handle in handles
        )
        for head in heads
    )


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
    """Idle builders over total, or None where this screen is not showing them."""
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


def running_rows(png: bytes) -> list[tuple[int, int]]:
    """The spans of the open build panel's running rows, top to bottom, or none.

    **A running row is the one row a tap must not land on**: on the home
    village a hero's or a pet's opens 立即完成 for gems where 確認 would be,
    and on the builder base a building's selects it with 取消 and 立即完成
    beside each other. They are always the panel's first rows and 48 px
    apart, so only the unbroken run from the top counts: further down,
    measured on the builder base, a 其他升級 row's plate matched the track
    colour too, at y 616.
    """
    image = open_frame(png)
    if not build_panel_open(png):
        return []
    spans: list[tuple[int, int]] = []
    for top in _row_tops(image):
        if top > (spans[-1][0] if spans else PANEL_TOP) + RUNNING_STEP:
            break
        spans.append((top, top + RUNNING_HEIGHT))
    return spans


def _time_runs(
    ink: list[list[bool]], text_rows: int, tolerance: int, width: int
) -> list[list[int | str]]:
    """A countdown row as runs of digits and of everything else, each with its span.

    A run of digits carries them as its first item and any other run an empty
    one. A piece taller than `text_rows` in front of the number is left out by its height.
    """
    runs: list[list[int | str]] = []
    for left, right in glyph_columns(ink, speckle=False):
        rows = [y for y, row in enumerate(ink) if any(row[left:right])]
        if rows[-1] - rows[0] + 1 > text_rows:
            continue
        pattern = signature(ink, left, right, TIME_FLOOR)
        digit, distance = nearest(pattern) if pattern is not None else ("", NOT_A_GLYPH)
        kind = digit if distance <= tolerance and right - left >= width else ""
        if runs and bool(runs[-1][0]) == bool(kind):
            runs[-1][0] = f"{runs[-1][0]}{kind}"
            runs[-1][2] = right
        else:
            runs.append([kind, left, right])
    return runs


def _unit_bits(ink: list[list[bool]], left: int, right: int) -> int:
    """One unit word on the shield bar as a `SHIELD_UNIT_CELL` pattern, any ink counting."""
    rows = [y for y, row in enumerate(ink) if any(row[left:right])]
    word = Image.frombytes(
        "L",
        (right - left, rows[-1] + 1 - rows[0]),
        bytes(
            255 if ink[y][x] else 0
            for y in range(rows[0], rows[-1] + 1)
            for x in range(left, right)
        ),
    )
    bits = 0
    for value in word.resize(SHIELD_UNIT_CELL, Image.Resampling.BOX).tobytes():
        bits = bits << 1 | int(value > SHIELD_UNIT_INK)
    return bits


def _shield_countdown(ink: list[list[bool]]) -> int | None:
    """The shield bar's countdown as seconds, or None where it does not read."""
    runs = _time_runs(ink, SHIELD_TEXT_ROWS, SHIELD_DIGIT_TOLERANCE, SHIELD_DIGIT_WIDTH)
    if len(runs) < 2 or not runs[0][0] or runs[1][0]:
        return None
    pattern = _unit_bits(ink, int(runs[1][1]), int(runs[1][2]))
    scale = min(
        SHIELD_UNIT_WORDS, key=lambda unit: (SHIELD_UNIT_WORDS[unit] ^ pattern).bit_count()
    )
    if (SHIELD_UNIT_WORDS[scale] ^ pattern).bit_count() > SHIELD_UNIT_TOLERANCE:
        return None
    numbers = [int(str(run[0])) for run in runs if run[0]]
    below = UNIT_LADDER[UNIT_LADDER.index(scale) + 1]
    return numbers[0] * scale + (numbers[1] * below if len(numbers) > 1 else 0)


def shield_state(png: bytes) -> ShieldState | None:
    """What the shield bar says, or None where this frame will not resolve it.

    The bar writes either a countdown or 無 when nothing is protecting the
    village. **Those are told apart by how far the writing reaches rather than
    by reading any of it** (`SHIELD_NONE_WIDTH`). The bar is translucent, so
    the ink is a storage bar's, which leaves out what the village puts behind
    it. A frame without the bar's own + is not showing the bar at all, and
    answers None rather than 無.

    A shield that is up answers its countdown (`_shield_countdown`), and one
    whose countdown does not read answers None.
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
    remaining = _shield_countdown(ink)
    return None if remaining is None else ShieldState(remaining=remaining)
