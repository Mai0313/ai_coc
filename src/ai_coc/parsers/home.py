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

from typing import TYPE_CHECKING
import logging
from statistics import median

from ai_coc.models import Patch, BuildQueue, ResourceBubble
from ai_coc.parsers.frame import open_frame

# The digit reader and the mask it wants; nothing here is worth a second copy of
# either. It used to be reached for through `parsers.scout`, by private name,
# because that is where it happened to have been written.
from ai_coc.parsers.glyphs import nearest, ink_mask, signature, glyph_columns, split_numbers
from ai_coc.parsers.regions import mask, patches

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PIL import Image

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
BUILDER_BOX = (783, 33, 860, 66)
# Midway between the worst digit that resolves confidently and the first reading
# that must not be believed. Two frames of one `0/5` counter read their `5` at 24
# against a `9` at 25, once each way round, so that pair is a coin flip: 24 is
# where a wrong answer starts, not where a right one ends. Nothing between 14 and
# 23 changes any recorded frame, so the line goes in the middle of that gap
# rather than at either edge of it.
BUILDER_TOLERANCE = 19

# Tapping that counter opens the panel listing every upgrade the village has
# running, and tapping it again closes it — the button toggles rather than
# opens, which is why the runner reads the panel before deciding it failed.
BUILDER_BUTTON = (745, 48)

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
BAR_LEFT, BAR_RIGHT = 870, 1010
BAR_TRACK, BAR_TRACK_SPREAD = 47, 12
BAR_COVERAGE = 0.8
BAR_GAP = 20
PANEL_TOP, PANEL_BOTTOM = 150, 700

# The time sits in the band directly above its own bar, right-aligned.
TIME_BOX = (860, 1015)
TIME_HEIGHT = 26
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
# Measured across every recorded panel, one unit's own readings land within 16
# bits of each other while the nearest other unit is 49 away and the nearest
# digit 54, so the same 30 the digits use separates these too.
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


def _bar_tops(image: Image.Image) -> list[int]:
    """The top row of each progress bar in the panel's own column.

    Every fourth pixel is enough to tell a bar from anything else: it has to run
    the whole column, and nothing else in the panel does.
    """
    width = BAR_RIGHT - BAR_LEFT
    data = image.crop((BAR_LEFT, PANEL_TOP, BAR_RIGHT, PANEL_BOTTOM)).tobytes()
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


def _remaining(image: Image.Image, bar_top: int) -> int | None:
    """The seconds written above one progress bar, or None where they will not read.

    A row reads as a number, a unit, and usually a second number in the next unit
    down — 9小時 23分鐘, or 1天 17小時. The second unit is never matched, because
    the game writes them in descending order and adjacent, so knowing the first
    settles it.
    """
    box = (TIME_BOX[0], bar_top - TIME_HEIGHT, TIME_BOX[1], bar_top)
    ink = ink_mask(image.crop(box))
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


def builder_jobs(png: bytes) -> BuildQueue | None:
    """What the builder panel says is running, soonest first.

    None means the panel is not on screen at all, which is what a caller that
    tapped a button that toggles needs to be told apart from a village with
    nothing being built.

    A row whose time will not read is counted but left out of the times rather
    than guessed at, which is why both numbers are reported: the two disagreeing
    is worth seeing rather than hiding.
    """
    image = open_frame(png)
    tops = _bar_tops(image)
    if not tops:
        return None
    found = [_remaining(image, top) for top in tops]
    return BuildQueue(running=len(tops), remaining=sorted(s for s in found if s is not None))


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
