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

Dark elixir's is nearly black, so it cannot be, and **the plate it sits on is
not one colour**: measured on two frames of the same village with the same
collectors in the same places, the dark bubbles are drawn on an orange plate in
one and on the pale plate every other bubble uses in the other. So one of those
plates is written down and the other is learned from the frame — gold and elixir
are found whatever their plate is painted, which makes their plate this frame's
answer. `_dark_bubbles` carries the measurement and says why a miss here is
allowed to be quiet.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
import logging
from statistics import median

from ai_coc.models import Patch, BuildQueue, ShieldState, ResourceBubble
from ai_coc.parsers.frame import open_frame
from ai_coc.parsers.world import ROW_LEFT_SPLIT, SHIELD_BADGE_LEFT, info_badges, current_world

# The digit reader and the mask it wants; nothing here is worth a second copy of
# either. It used to be reached for through `parsers.scout`, by private name,
# because that is where it happened to have been written.
from ai_coc.parsers.glyphs import (
    nearest,
    ink_mask,
    signature,
    ink_patches,
    glyph_columns,
    split_numbers,
)
from ai_coc.parsers.regions import mask, patches

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PIL import Image

    from ai_coc.models import World, PlateRole

logger = logging.getLogger(__name__)

# Where a marker can be. Outside this is the game's own furniture — the button
# columns down either side and the row along the bottom — and a marker cannot be
# under it because the furniture is drawn on top. The storage bars in the top
# right are the same, and they carry the very icons being looked for.
VILLAGE_AREA = (110, 80, 1480, 780)
STORAGE_BARS = (1260, 0, 1600, 330)

# What each marker is made of: the channel ranges its own pixels fall in, and the
# size the patch of them comes out at. Measured on live frames before and after
# the camera was dragged — the coin segments to 21x16, the drop to 18x25 and the
# orange plate to 38x41, each to within a pixel or two every time. The three
# differ because they are three different shapes, not because of the camera.
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
    ("dark", ((201, 255), (56, 129), (0, 74)), (35, 41), (37, 45), 0.38),
)

# The dark marker's other plate, and how it is recognised without being written
# down. The icon itself is what is found — nearly black, and measured at 24 to
# 25 across by 27 to 28 down on both frames that carry one — and what separates
# it from the twelve other dark shapes a village offers is the plate it is
# sitting on. See `_dark_bubbles` for why that colour is learned per frame
# instead of listed above.
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
PLATE_MATCH = 28
# Swept over every committed frame that has a marker to learn a plate from: the
# thirteen real dark bubbles score 0.570 to 0.935 and the highest thing that is
# not one scores 0.192 — a grey boulder in dark foliage on
# `home_marker_over_bars.png`, whose pale stone is close to the pale plate and
# whose shadow is the icon-shaped patch beside it. So the line goes midway
# across that gap. **A first pass put it at 0.20**, which cleared that boulder
# by 0.008 while the comment beside it claimed a gap of 0.39, because it was
# measured on the two frames the change was written from rather than on all of
# them. Obstacles are ordinary village furniture, so that floor is pinned by a
# test now.
PLATE_SHARE = 0.38
# Two readings of one bubble, since the orange plate and the icon inside it are
# found by different passes and their middles sit a few pixels apart.
DARK_APART = 30

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

# 1/5 beside the builder's head, in the same white the storage bars use. The
# slash between the two numbers is not a digit and that is how it is found:
# swept over every recorded frame carrying this counter, a digit lands within 14
# of its template and the slash between 38 and 41, so anything over the line
# separates one number from the other.
#
# **The box was cut from a `1/5`, and `1` is the one digit narrow enough to fit
# it.** The number is centred, so a wide leading digit hangs off the left: a
# leading `0` inks from x 792 and the old edge at 795 took three columns off it,
# putting it 23 bits from its template against 5 with them restored. That is
# either side of the tolerance, so a village at `0/6` read as no counter at all.
# The leading digit is at its floor for any edge from 792 down to 774, and at
# 772 the counter plate's own frame (inked at 771 to 773) arrives as a fourth
# glyph; 783 is the middle of that range and leaves the 9 px of margin the right
# edge already had.
#
# **Written as an offset from the plate's own badge, because the row moves.** It
# is laid out from the middle outwards and holds one plate more on the home
# village, so the same plate sits at a different x in the two — and even within
# the builder base the badges were measured at both 628/830 and 644/846. What
# does not move is the plate around its own badge: this one offset reads `0/2`
# and `0/6` on the home village and `0/1` and `0/3` on the builder base.
PLATE_DIGITS = (64, 33, 141, 66)


def plate_box(centre: int) -> tuple[int, int, int, int]:
    """Where a plate writes its count, given where its own badge sits."""
    return centre + PLATE_DIGITS[0], PLATE_DIGITS[1], centre + PLATE_DIGITS[2], PLATE_DIGITS[3]


BUILDER_BOX = plate_box(719)

# The shield plate writes a countdown rather than a count, so it needs a box of
# its own: wider on the left, where 6小時 2分鐘 runs further than 0/6 does, and
# stopping short of the green + on its right, which is a gem purchase and the one
# thing on this row nothing here should be reading, let alone reaching.
# The + starts about 145 past the badge. **The left edge is where the text can
# start, not where the icon ends**: the plate centres its countdown, so a longer
# one starts further left and is drawn over the icon's right side. Measured on
# the plates on record, the first digit starts anywhere from about 32 to 40 past
# the badge, and at 42 this cut 3小時 12分鐘's 3 in half and read 12 minutes. 35
# is past the icon's tip at the digits' height. A first digit starting left of
# it is left as a sliver in the box or not in it at all, and `shield_state` says
# what each of those reads as.
SHIELD_DIGITS = (35, 33, 145, 66)
# The icon's silver rim rises above the text at its top right, and a digit that
# starts over the icon fuses with it into a shape no template matches — 3 came
# out nearest to 9. The rim is cleared from the box's first nine rows up to 52
# past the badge, which is everything above the digits' tops: at eight rows the
# 3 still read 26 against a tolerance of 25, at nine it reads 18.
SHIELD_RIM = (52, 9)
# Below the digits' tops the rim is a patch of its own, and it only shows when a
# shorter countdown sits further right and leaves it bare between the first
# digit and 小; column spans then fuse all three into a shape nothing matches.
# Over the 23 home village frames of one recorded night, 3小時 26分, 45分 and 53分
# among them, and the committed frames with the plate, every patch this clears
# was 1 to 4 px wide and 1 to 12 rows tall. A 1 is 7 wide and only 5 on most of
# its rows, so the width alone leaves one pixel; what keeps a 1 is its height,
# 18 rows or more like every digit, and a digit the box cut into a sliver keeps
# the height of the digit. Taking either would read 12小時 as 2小時.
SHIELD_RIM_WIDTH = 5
SHIELD_RIM_ROWS = 15
# How tall a leading fragment can be and still be a leftover of that rim rather
# than part of a character. Every rim leftover on the plates on record is one row
# tall; a digit stands 18 to 24, and a sliver of one that the box cut stays that
# tall, which the width it is left with would not tell apart from the rim.
SHIELD_SCRAP_ROWS = 10
# 無 against a countdown, by how many glyphs are written rather than by matching
# the character: swept over every committed frame with the plate on it, 無 comes
# out as one, once `_clear_rim` has taken the stub of rim it was counted with,
# and a countdown as six or more.
SHIELD_GLYPHS = 4


def shield_box(centre: int) -> tuple[int, int, int, int]:
    """Where the shield plate writes its countdown, given where its badge sits."""
    return (
        centre + SHIELD_DIGITS[0],
        SHIELD_DIGITS[1],
        centre + SHIELD_DIGITS[2],
        SHIELD_DIGITS[3],
    )


# Which plate is which, **keyed by where it sits and never by its place in the
# row**. `info_badges` returns only the badges it can see, and `parsers/world.py`'s
# sweep records rows a gem shower left as `[516, 933]` or `[933]` — so on
# `world_day_shield_only.png` the first badge is the *shield*, and a reader
# taking index 0 for the laboratory would open the wrong plate. `ROW_LEFT_SPLIT`
# and `SHIELD_BADGE_LEFT` are `current_world`'s own thresholds, reused here so
# the two cannot disagree about the same row.
#
# The builder base needs one line of its own: its badges sit at 628-644 and
# 830-846, so the line goes midway between them with 90 px of room either side.
NIGHT_ROW_SPLIT = 740
# Midway between the worst digit that resolves confidently and the first reading
# that must not be believed. Two frames of one `0/5` counter read their `5` at 24
# against a `9` at 25, once each way round, so that pair is a coin flip: 24 is
# where a wrong answer starts, not where a right one ends. Nothing between 14 and
# 23 changes any recorded frame, so the line goes in the middle of that gap
# rather than at either edge of it.
BUILDER_TOLERANCE = 19

# Tapping a plate opens the panel listing what it is counting, and tapping it
# again closes it — the button toggles rather than opens, which is why the
# runner reads the panel before deciding it failed. Measured from the badge, the
# same way the digit box is and for the same reason: the row moves.
PLATE_BUTTON = (26, 48)


def plate_button(centre: int) -> tuple[int, int]:
    """Where to tap to open one plate's panel, given where its badge sits."""
    return centre + PLATE_BUTTON[0], PLATE_BUTTON[1]


# Each running upgrade gets a progress bar with its remaining time drawn over
# it, and **the bar is the only thing that says which rows those are**. The panel
# carries two more sections under them, 建議升級 and 其他升級, whose rows carry a
# price in the same place in the same white — so a reader going by the text alone
# would report a gold cost as a countdown. Nothing below the running rows has a
# bar. Nothing reads the names beside them either: they are Chinese, they are
# different in every village, and none of them is needed to answer when a
# builder comes free.
#
# Measured on live panels the bars share one column and one pair of colours: the
# unfilled track is a flat (47, 47, 47) and the filled part a bright green, and
# together they cover the column edge to edge. Rows sit 48 px apart, so anything
# within BAR_GAP of a bar already found is the same bar found again a row down.
BAR_TRACK, BAR_TRACK_SPREAD = 47, 12
BAR_COVERAGE = 0.8
BAR_GAP = 20
PANEL_TOP, PANEL_BOTTOM = 150, 700

# **A table rather than a formula, because the panel floats and its offset from
# its own plate does not hold.** Every plate opens the same panel — 升級中 over
# 建議升級 over 其他升級, one green bar per running row — and measured live all
# four sit at the same y (first row 212-216, then every 48 px) and carry a bar
# 113 to 116 px wide. What differs is x, and not by a constant: from each
# plate's badge the bar starts at +158, +172, +147 and +168, a 25 px spread
# against a 115 px bar, so a derived box would cut the countdown off.
#
# Widening one band to cover all four was measured and thrown away: the green
# test matches grass, and over the committed fixtures a 490 px band picks up
# 110 px runs on `shop_skins.png` and both wall dialogs. Four narrow bands hold
# `BAR_COVERAGE` meaningful — a real bar fills 0.97 of its own band, where in a
# 490 px one it could never reach 0.8 at all.
PANEL_BANDS = {
    ("day", "lab"): (672, 790),
    ("day", "builder"): (889, 1008),
    ("night", "lab"): (789, 907),
    ("night", "builder"): (1012, 1129),
}

# The time sits in the band directly above its own bar, right-aligned — and
# **reaches past the bar at both ends**: measured on `builder_panel.png` the ink
# runs 884 to 1006 against a bar of 889 to 1006, so a box cut to the bar clips
# the leading glyph and the reading loses a place.
TIME_PAD = (-32, 7)
TIME_HEIGHT = 26

# How far around the bars to crop when handing the 升級中 block to a model. The
# names run to the left of every bar and the panel's own left edge moves with the
# longest of them, so this reaches further than any measured panel does rather
# than trying to find it: the narrowest gap between a bar and its panel edge was
# about 226 px. Whatever village comes with it is not a problem for a model
# reading a label, where a name cut in half would be.
NAMES_LEFT = 300
NAMES_ABOVE = 40
# How far past a bar's right end a 建議升級 price reaches: on the four committed
# panels the last digit ends within a few pixels of the bar's own right edge.
PRICE_RIGHT = 20
# Midway between the worst digit that resolves and the nearest thing that is not
# one. This said the gap was 16 against 49 and it is not: swept over 15 rows
# across five panels, a digit lands within **19** while 小 comes back **exactly
# 30** from the template for `0` — so at the old line of 30 the `<=` took it for
# one, and 6小時 was read as 60小時.
#
# **It only misses on a live panel, which is why a fixture could not show it.**
# The panel is translucent, so the village behind it decides whether 小's two
# short outer strokes reach the ink mask; on `builder_panel.png`, shot over
# flat grass, they do and the character reads 83 to 86 from any digit, while
# over a wall's dense lattice only the middle stroke survives and what is left
# is a bare vertical bar. Every unit that does resolve lands within 15, so one
# line still separates both questions.
#
# Measured against 15 countdowns whose values were read off the frames by eye:
# at 30, 8 right and **6 wrong**; at 25, 14 right and none wrong, with all four
# of the committed fixture's rows unchanged. A wrong countdown is the dangerous
# kind, because 1天16小時 arriving as 7天16小時 is a number a caller believes.
TIME_DIGIT_TOLERANCE = 25

# 天, 小時 and 分鐘, as the seconds one of each is worth. Only the **first**
# character of a unit is matched, which is what keeps this to three templates:
# 小 and 分 each lead a two-character unit, and the second unit on a row is
# always the next step down the ladder rather than something to be read.
#
# Measured across every recorded panel, one unit that resolves at all lands
# within 15 of its own template while the nearest other unit is 49 away, so
# `TIME_DIGIT_TOLERANCE` separates these as well as it separates the digits.
# **That tolerance moved to 25 and this went with it**: the figures here used to
# say every unit missed a digit by 49 or more, which is what the block above
# measured and disproved — over a live panel 小 lands exactly 30 from `0`.
UNIT_TEMPLATES = {
    86400: 694176028518715082074175994823591921588995,
    3600: 1362459995062295920913326796806252659743,
    60: 98001493513730352222404855677119927554332,
}
UNIT_LADDER = (86400, 3600, 60, 1)


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


def _plate_colours(
    village: bytes, size: tuple[int, int], lit: list[Patch]
) -> list[tuple[int, int, int]]:
    """Every plate colour this frame is drawing, one reading per marker already found.

    **One list rather than one colour, because the game paints this per bubble
    and not per frame.** Measured on `home_builders_busy.png`, seven gold
    bubbles sit on an orange plate at (221, 92, 33) while seven elixir bubbles
    on the same frame sit on the pale one at (184, 189, 131). Pooled into a
    single median those two average to (213, 131, 66), which is near neither, and
    a dark icon on the real orange then scores 0.38 against it where it scores
    0.75 against the colour it is actually on. The composition that loses a
    marker outright is a frame whose lit bubbles are mostly one plate and whose
    dark bubble is on the other — which is the bug this whole reader exists to
    fix, reintroduced by averaging.

    Empty when no gold or elixir marker is on screen to learn from, which is an
    ordinary answer and not an error — see `_dark_bubbles`.
    """
    found: list[tuple[int, int, int]] = []
    for patch in lit:
        ring = _ring(village, size, patch, PLATE_LEARN_PADS)
        if not ring:
            continue
        red, green, blue = (round(median(pixel[band] for pixel in ring)) for band in range(3))
        found.append((red, green, blue))
    return found


def _dark_bubbles(image: Image.Image, lit: list[Patch]) -> Iterator[Patch]:
    """Dark elixir markers, found by their icon and the plate it is sitting on.

    **The plate is not one colour, which is what this exists for.** The dark
    marker's own icon is nearly black, so unlike gold and elixir it cannot be
    found by the icon alone — the village is full of dark shapes — and the
    reader keyed on the plate instead, in the one orange `MARKERS` carries.
    Measured on two frames of this same village, taken weeks apart with the same
    collectors in the same places, the three dark bubbles are drawn on an orange
    plate in one and on the pale plate every other bubble uses in the other. Two
    frames cannot say what the game means by that — full against merely worth
    collecting is the obvious guess and is only a guess — but they are enough to
    say the one written-down colour finds one of the two states and misses the
    other, which is every dark marker on this account today.

    So the pale one is learned from the frame rather than written down: gold and
    elixir are found by their own icons, whatever their plate is painted, and
    their plate is this frame's answer. Measured over both frames, a dark icon
    on that plate matches 0.42 to 0.51 of its own ring against at most 0.03 for
    the twelve other dark shapes the village offers, so the line has room.

    **Nothing here fails loudly, and that is deliberate.** A frame with no gold
    or elixir marker on it has nothing to learn from and this yields nothing;
    so does a plate in some third colour. What that costs is a collector left
    standing until the next pass, which is a few minutes of one collector's
    production — far less than a reader that guesses would cost.
    """
    size = image.size
    village = image.tobytes()
    plates = _plate_colours(village, size, lit)
    candidates = [
        patch
        for patch in patches(mask(image, DARK_ICON), *size)
        if patch.sized(DARK_ICON_ACROSS, DARK_ICON_DOWN, DARK_ICON_FILL)
    ]
    if not plates:
        # **Asked after the scan and not before it**, because the frame with
        # nothing to learn from is overwhelmingly the frame with no markers on
        # it at all: a battle, a card row, a loading screen, or the plain
        # village every successful pass ends on. Warned before the scan this
        # fired on 74 of 84 committed frames, and a line that fires on
        # everything teaches a reader to skip the one that will matter.
        if candidates:
            logger.warning(
                "%d dark icon(s) on this frame and no gold or elixir marker to learn a plate "
                "from, so any that are markers are left for the next pass",
                len(candidates),
            )
        return
    for patch in candidates:
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
    """Every collector marker standing on this frame, in reading order.

    A marker is what the game puts above a collector that has something waiting,
    and tapping one collects it — no menu, no confirmation. It disappears once
    collected, so a second read is what says whether a tap landed.
    """
    image = open_frame(png)
    left, top, right, bottom = VILLAGE_AREA
    village = image.crop(VILLAGE_AREA)
    width, height = right - left, bottom - top
    found: list[ResourceBubble] = []
    lit: list[Patch] = []
    for resource, ranges, across, down, fill in MARKERS:
        for patch in patches(mask(village, ranges), width, height):
            if not patch.sized(across, down, fill):
                continue
            middle = (patch.middle[0] + left, patch.middle[1] + top)
            if _in_storage_bars(middle):
                continue
            if resource != "dark":
                lit.append(patch)
            found.append(ResourceBubble(resource=resource, point=middle))
    # After the loop, because it is the gold and elixir markers above that say
    # what a plate looks like on this frame.
    taken = {bubble.point for bubble in found if bubble.resource == "dark"}
    for patch in _dark_bubbles(village, lit):
        middle = (patch.middle[0] + left, patch.middle[1] + top)
        if _in_storage_bars(middle) or any(
            abs(middle[0] - x) < DARK_APART and abs(middle[1] - y) < DARK_APART for x, y in taken
        ):
            continue
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


def _bar_tops(image: Image.Image, band: tuple[int, int]) -> list[int]:
    """The top row of each progress bar in one panel's own column.

    Every fourth pixel is enough to tell a bar from anything else: it has to run
    the whole column, and nothing else in the panel does.
    """
    width = band[1] - band[0]
    data = image.crop((band[0], PANEL_TOP, band[1], PANEL_BOTTOM)).tobytes()
    wanted = len(range(0, width, 4)) * BAR_COVERAGE
    tops: list[int] = []
    for row in range(PANEL_BOTTOM - PANEL_TOP):
        base = row * width * 3
        covered = 0
        for offset in range(base, base + width * 3, 12):
            red, green, blue = data[offset], data[offset + 1], data[offset + 2]
            track = all(abs(value - BAR_TRACK) < BAR_TRACK_SPREAD for value in (red, green, blue))
            if track or (green > 140 and green - red > 40 and green - blue > 60):
                covered += 1
        y = PANEL_TOP + row
        if covered >= wanted and (not tops or y - tops[-1] > BAR_GAP):
            tops.append(y)
    return tops


def _remaining(image: Image.Image, bar_top: int, band: tuple[int, int]) -> int | None:
    """The seconds written above one progress bar, or None where they will not read.

    A row reads as a number, a unit, and usually a second number in the next unit
    down — 9小時 23分鐘, or 1天 17小時. The second unit is never matched, because
    the game writes them in descending order and adjacent, so knowing the first
    settles it.
    """
    box = (band[0] + TIME_PAD[0], bar_top - TIME_HEIGHT, band[1] + TIME_PAD[1], bar_top)
    return _seconds_from(ink_mask(image.crop(box)))


def _seconds_from(ink: list[list[bool]]) -> int | None:
    """One countdown as seconds, from a mask of the row it is written on.

    Shared with the shield plate, which writes the same 6小時 2分鐘 in the same
    font from the same templates — the only thing that differs is which box it
    was cropped from.
    """
    numbers: list[int] = []
    digits = ""
    scale: int | None = None
    # Speckle is left in: half of what this row has to read is Chinese, and 小
    # is three short strokes that the digit reader's filter takes for noise.
    for left, right in glyph_columns(ink, speckle=False):
        pattern = signature(ink, left, right)
        if pattern is None:
            continue
        digit, distance = nearest(pattern)
        if distance <= TIME_DIGIT_TOLERANCE:
            digits += digit
            continue
        if digits:
            numbers.append(int(digits))
            digits = ""
        if scale is not None or len(numbers) != 1:
            continue
        # The first character after the first number is the one unit worth
        # matching; everything after it follows from the ladder.
        unit = min(
            UNIT_TEMPLATES, key=lambda seconds: (UNIT_TEMPLATES[seconds] ^ pattern).bit_count()
        )
        if (UNIT_TEMPLATES[unit] ^ pattern).bit_count() <= TIME_DIGIT_TOLERANCE:
            scale = unit
    if digits:
        numbers.append(int(digits))
    if scale is None or not numbers:
        return None
    below = UNIT_LADDER[UNIT_LADDER.index(scale) + 1]
    return numbers[0] * scale + (numbers[1] * below if len(numbers) > 1 else 0)


def panel_rows(png: bytes, world: World, role: PlateRole) -> list[int | None] | None:
    """Each running row's countdown, **in the order the panel draws them**.

    None for the whole list means that panel is not on screen at all, which is
    what a caller that tapped a button that toggles needs to be told apart from
    a plate with nothing running behind it. None for one row is a countdown that
    would not resolve; the row is kept, because something is in it and dropping
    it would report the plate as emptier than it is.

    The order is what separates this from `panel_jobs`: the names beside these
    rows are read left to right by a model, so anything matching one to a row
    needs them as drawn rather than sorted.
    """
    band = PANEL_BANDS.get((world, role))
    if band is None:
        return None
    image = open_frame(png)
    tops = _bar_tops(image, band)
    if not tops:
        return None
    return [_remaining(image, top, band) for top in tops]


def jobs_strip(png: bytes, world: World, role: PlateRole) -> bytes | None:
    """The 升級中 block and the 建議升級 block under it as one PNG, for a model to read.

    Cropped rather than read, the same bargain `parsers.building.name_strip`
    makes: what is being raised is written in Chinese, no parser here reads any,
    and a strip is what a cheap model is actually for.

    **One crop for the whole block rather than one per row**, because the rows
    are contiguous and a call each would be one per running upgrade where this
    is one per panel. The left edge is generous on purpose — the panel's own
    width is not measured and varies with the longest name on it — so some
    village comes with it, which a model reading a label has no trouble with.
    """
    band = PANEL_BANDS.get((world, role))
    if band is None:
        return None
    image = open_frame(png)
    tops = _bar_tops(image, band)
    if not tops:
        return None
    # **Down to the bottom of the panel**, so the suggestions come along with
    # the running rows; their prices end a few pixels right of the bars. The
    # 其他升級 rows under them only show in full once the panel is scrolled, so
    # they are left to the model to skip rather than read, until the panel is
    # walked row by row to locate a building.
    box = (band[0] - NAMES_LEFT, tops[0] - NAMES_ABOVE, band[1] + PRICE_RIGHT, PANEL_BOTTOM)
    out = io.BytesIO()
    image.crop(box).save(out, format="PNG")
    return out.getvalue()


def panel_jobs(png: bytes, world: World, role: PlateRole) -> BuildQueue | None:
    """What one plate's panel says is running, soonest first.

    A row whose time will not read is counted but left out of the times rather
    than guessed at, which is why both numbers are reported: the two disagreeing
    is worth seeing rather than hiding.
    """
    rows = panel_rows(png, world, role)
    if rows is None:
        return None
    return BuildQueue(running=len(rows), remaining=sorted(s for s in rows if s is not None))


def builder_jobs(png: bytes) -> BuildQueue | None:
    """The home village's builder panel, which is what every caller here means."""
    return panel_jobs(png, "day", "builder")


def plate_badges(png: bytes) -> dict[PlateRole, int]:
    """Where each plate's badge sits, by what that plate is rather than by its place.

    **Never by index.** `info_badges` returns only the badges it can see, and a
    gem shower drifting over the row leaves frames like `[516, 933]` or `[933]`
    — on the second of those the first badge is the shield, so a reader taking
    index 0 for the laboratory would open the wrong plate. The thresholds are
    `current_world`'s own, so the two cannot disagree about one row.
    """
    world = current_world(png)
    found: dict[PlateRole, int] = {}
    for left, right in info_badges(png):
        centre = (left + right) // 2
        if centre >= SHIELD_BADGE_LEFT:
            found["shield"] = centre
        elif world is not None:
            split = ROW_LEFT_SPLIT if world == "day" else NIGHT_ROW_SPLIT
            found["lab" if centre < split else "builder"] = centre
    return found


def plate_panel_open(png: bytes, world: World) -> tuple[int, int] | None:
    """Where to press to shut the plate panel over this village, or None for none.

    **The panel is a village by every test the loops have**, which is what makes
    it worth a reader of its own: it floats over the middle of the map rather
    than filling the screen, so `current_world` reads the badge row above it and
    `read_stock` reads the bars beside it, and `uncovered` hands a frame that
    reads as a village straight back untouched. Nothing else clears it — the
    button toggles, so a run that opened one and died leaves it up for whatever
    comes next.

    What it does cover is the middle, which is where `view_shift` looks: measured
    on `day_builder_panel.png` it fills 60% of `ALIGN_BOX`, and a static overlay
    that large pins the reader to (0, 0) whatever the camera does — true moves of
    65, 131 and 261 px all read as no move at all.

    Found by its running rows rather than by its own plate, reusing `panel_rows`
    at no new threshold: over the 18 committed frames `current_world` names a
    village on, the four with a panel are the four this answers for and the
    other fourteen answer None. **An idle panel is invisible to it**, since a
    plate with nothing running has no bars to find, and there is nothing else in
    the band to go by — the sheet's own dark reads 0.00 to 0.20 of bar where a
    real bar reads 0.83 to 0.97.
    """
    badges = plate_badges(png)
    for role in ("lab", "builder"):
        centre = badges.get(role)
        if centre is not None and panel_rows(png, world, role) is not None:
            return plate_button(centre)
    return None


def _rows(ink: list[list[bool]], span: tuple[int, int]) -> int:
    """How many rows one column span's ink runs over, top to bottom."""
    rows = [y for y, row in enumerate(ink) if any(row[span[0] : span[1]])]
    return rows[-1] - rows[0] + 1 if rows else 0


def _clear_rim(ink: list[list[bool]]) -> None:
    """Clear, in place, the stretch of the icon's rim a short countdown leaves bare."""
    reach = SHIELD_RIM[0] - SHIELD_DIGITS[0]
    for cells in list(ink_patches(ink)):
        xs, ys = [x for _, x in cells], [y for y, _ in cells]
        if (
            max(xs) < reach
            and max(xs) - min(xs) + 1 < SHIELD_RIM_WIDTH
            and max(ys) - min(ys) + 1 < SHIELD_RIM_ROWS
        ):
            for y, x in cells:
                ink[y][x] = False


def shield_state(png: bytes, centre: int) -> ShieldState | None:
    """What the shield plate says, or None where this frame will not resolve it.

    The plate writes either a countdown — 6小時 2分鐘, in the same font and from
    the same templates as a panel row — or 無 when nothing is protecting the
    village. **Those are told apart by how much is written there rather than by
    reading any of it**: swept over every committed frame carrying the plate, 無
    comes out as one glyph and a countdown as six or more, so the line goes
    between at four. Nothing here matches the character itself, which would be
    one more Chinese template to keep.

    None is the honest third answer and it is not rare: the plate is translucent,
    so the village behind it decides whether the strokes resolve — measured, two
    of three frames read their countdown and the third did not.

    **A countdown whose first character is not a digit is None, not the rest of
    it.** The reader skips what it cannot match, so a first number it could not
    read left the second one standing as the whole countdown: 1小時 48分 came
    back as 48 minutes, and a plate whose hours a gem shower covered as 14. A
    shield reported hours shorter than it is reads as one about to lapse, which
    is the wrong direction to be wrong in.

    Known and open (#260): a countdown with two-digit hours, which is what a shield
    from a raid starts at, runs further left still, and nothing on record says
    where. If its leading 1 falls wholly left of the box the plate reads ten
    hours short, as it always did; a sliver of it in the box is caught above.
    Also still None on the night `SHIELD_RIM_WIDTH` was measured on: five frames
    whose first digit will not match for other reasons, four of them 4小時 and
    one of those a 4 fused with a line drawn under it, and one whose 小 fell
    apart into two spans, so the unit after the hours never matched.
    """
    ink = ink_mask(open_frame(png).crop(shield_box(centre)))
    for row in ink[: SHIELD_RIM[1]]:
        for x in range(SHIELD_RIM[0] - SHIELD_DIGITS[0]):
            row[x] = False
    _clear_rim(ink)
    columns = glyph_columns(ink, speckle=False)
    if len(columns) < SHIELD_GLYPHS:
        return ShieldState()
    first = next((span for span in columns if _rows(ink, span) > SHIELD_SCRAP_ROWS), None)
    if first is not None:
        pattern = signature(ink, *first)
        if pattern is None or nearest(pattern)[1] > TIME_DIGIT_TOLERANCE:
            return None
    seconds = _seconds_from(ink)
    return None if seconds is None else ShieldState(remaining=seconds)


def plate_count(png: bytes, centre: int) -> tuple[int, int] | None:
    """Idle over total on the plate whose badge is here, or None where it will not read.

    None is the same answer `free_builders` has always given and wants the same
    treatment: the plate is translucent, so what the camera has behind it decides
    whether the digits resolve — measured, one committed frame reads its `0/5`
    as `0` alone because the 5 lands 24 from its template against a tolerance of
    19. A count nobody can read is worth less than nothing if it is guessed at.
    """
    found = split_numbers(ink_mask(open_frame(png).crop(plate_box(centre))), BUILDER_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]


def free_builders(png: bytes) -> tuple[int, int] | None:
    """Idle builders over total, or None where this screen is not showing them.

    The game counts the ones standing around, not the ones at work, so zero here
    means every builder is busy and nothing new can be started.

    **None costs each caller something different, and the cheap-looking one is
    the expensive one.** `UpkeepRunner.upgrade` and `HeroRunner` both stand the
    run down on it, which is loud and harmless. `WallRunner` does the opposite:
    its check is `counted is not None and counted[0] == 0`, so an unread counter
    silently never fires the "every builder is busy" branch and the run walks the
    rest of the village at forty seconds a wall, buying nothing. That is why the
    box above is sized to the widest number the counter can hold rather than to
    the one it happened to be showing when it was cut.
    """
    found = split_numbers(ink_mask(open_frame(png).crop(BUILDER_BOX)), BUILDER_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]
