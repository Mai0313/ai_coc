"""Crossing between the game's two villages, which is a boat and nothing else.

There is no menu for this. The only way over is a boat moored at one corner of
each map, so the crossing is a tap at a place on the ground — and a place on the
ground is exactly what this project refuses to remember, because the camera
moves and what is under a coordinate changes with it.

**What makes a remembered spot safe here is that the camera clamps.** The game
will not pan past the edge of its own map, so swiping hard at one corner ends at
the same view whatever the camera was doing before, and from that view each boat
sits at one fixed pixel. That holds on the home village; the builder base's park
ends anywhere along a band about 55 px wide depending on where the camera started
(see `CART_SPOTS`), which its boat is big enough to absorb and its loot cart is
not. **How many swipes that takes is not a constant**, which
this file assumed for a long time and paid for in every crossing: see
`park_camera`, which now swipes until the picture stops moving and says whether
it got there.

**Nothing here recognises the boat itself.** It is a sprite the game dresses up
for events, and the scenery decides where it is moored: one bought scenery puts
it on a tower nowhere near the spots measured on the jungle. What the home
village's boat does have is the marker the game floats over it, which is drawn
the same on every scenery and sails when tapped, so that is looked for first
(`boat_marker`). The builder base's sits under the button column, and that
village has no sceneries to move its boat, so it is tapped at its measured
spots. Either way `parsers.world` is what says whether the tap worked. A tap
that landed on the water instead simply leaves the world unchanged and the next
candidate spot is tried. That is the pattern `hero --at` already uses on a
building that two taps in a row select alternately.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import Pinch, Crossing, CartReport
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import ZOOM_PINCHES
from ai_coc.parsers.home import boat_marker, plate_panel_open
from ai_coc.parsers.field import view_shift
from ai_coc.parsers.scout import (
    in_battle,
    battle_over,
    card_groups,
    loading_screen,
    loot_cart_load,
    loot_cart_open,
    loot_cart_ready,
    read_builder_stock,
)
from ai_coc.parsers.world import current_world

if TYPE_CHECKING:
    from ai_coc.models import World, DisplayTarget
    from ai_coc.adapters.adb import AdbController

logger = logging.getLogger(__name__)

# Keyed by the village being sailed **to**. Each drag starts mid-screen so
# neither end of it lands on the game's own button columns, and points away from
# the corner being revealed, because it is the map moving under a fixed finger.
CROSSINGS: dict[World, Crossing] = {
    # Leaving the home village: on the jungle scenery the spots were measured
    # on, the boat is on the water off the bottom-left shore. The candidates
    # stay up and left of the measured spot on purpose — the clan capital's own
    # vessel is moored below and to the right of it there, and tapping that one
    # opens a village this project has no business in.
    "night": Crossing(
        start=(500, 600),
        drift=(700, -350),
        spots=((400, 625), (382, 610), (416, 634)),
        marked=True,
    ),
    # Leaving the builder base: the boat is at the pier off the top-right. The
    # candidates stay left of the measured spot, because the game's right-hand
    # button column starts at x 1490 and overlaps the boat's stern, and its
    # marker with it.
    "day": Crossing(
        start=(1100, 300),
        drift=(-700, 350),
        spots=((1440, 545), (1415, 555), (1432, 522)),
        marked=False,
    ),
}
# **A fixed count was the bug.** The count stood in for the clamp: swipe enough
# times and the camera must be against the map edge, so every coordinate
# measured from that view is valid again. Measured live, a home village parked
# once still moved (131, -65) when parked a second time — and that 130 px is
# exactly how far below the crossing's candidate spots the boat was sitting. The
# whole failure is silent: the taps land on water, and the run spends three
# minutes finding that out, measured live at 80, 87 and 18 seconds for the three
# spots before it reported no boat.
#
# **What `SWIPES = 5` was really short of was the pinch, not four more swipes.**
# That reading of "about ten" was taken at whatever camera the park was handed,
# and the zoom is what decides how far the map is able to travel at all — see
# `park_camera`, which pinches out before it counts anything now.
#
# So the swipes are counted against the picture instead. `PARK_SWIPES` is only a
# ceiling, and reaching it is a failure worth reporting rather than a park worth
# trusting.
#
# **The walk is one swipe and the rest of this number is slack, deliberately.**
# Measured at the far zoom, the home village crosses its whole range on the
# first swipe and the builder base is already there, so three would cover both.
# What the rest are for is a reading that came back non-zero on a camera that
# had already stopped: `view_shift` answers in steps of about 65 px and the
# builder base animates, and measured live an already-parked home village spent
# three swipes where two would have done. Every such reading resets `still`, and
# **a spurious False is expensive** — `cross` returns without tapping, and
# `ai_coc world --go` reports a crossing that never happened. What ten buys
# depends on where the bad readings fall: seven in a row still leave a clean
# pair at the end, while five alternating with zeros leave no adjacent pair at
# all.
#
# **A frame caught mid-zoom is not one of those, and it fails the other way.**
# The opening frame is taken one `PINCH_SETTLE` after the pinch, so it can
# compare two scales rather than two positions — and measured, a pure scale
# change of up to 10% reads (0, 0) rather than non-zero. It spends no slack; it
# risks a premature *arrived*, which is what `PARK_STILL` is for.
PARK_SWIPES = 10
# Two readings rather than one, because a single swipe the game swallowed looks
# exactly like a camera that has arrived.
PARK_STILL = 2
SWIPE_MS = 500
SWIPE_SETTLE = 1.2

# **A pinch does not put the camera anywhere in particular, and everything that
# taps the map was built as though it did.** The far zoom was described here as
# centring the village to within 20 px, on the reasoning that the map clamps the
# camera at its own edges; measured, that is not what a pinch does at all. Shove
# the camera off centre and pinch, and `view_shift` across the pinch reads
# (0, 0) — it widens the view and leaves the middle exactly where it was pushed.
# So every coordinate aimed at the map — the sweep grid, a remembered `--at`,
# anything a session writes down — was only ever valid until something moved the
# camera, which is silent and constant.
#
# What *is* reproducible is the clamp itself. Swiping into a map corner runs the
# camera up against the edge and it stops, and the frames it ends on align at
# `view_shift` (0, 0) with a mean absolute pixel difference of about 2 on the
# home village — water and flags, nothing else. The whole village is on screen
# there, the diamond spanning about x 200-1400 by y 60-780.
#
# **How many swipes that takes was written down here as two and four, and both
# were wrong by half** — see `PARK_SWIPES` for what the walk really measures.
# That pixel difference is not a usable stop signal either: the builder base
# animates enough to read 12.3 while clamped against 20.8 for a real 65 px move,
# where the home village reads 2.1. `view_shift` separates the two on both.
#
# So the camera is parked rather than centred, and each village is pushed into
# its own corner with its own crossing's swipe. **Which village it is on decides
# both**, because the two maps clamp in opposite corners: a park that always
# pushed the home village's way would leave the builder base walking away from
# its own clamp, which is the one thing this is for.


def park_camera(
    adb: AdbController, display: DisplayTarget, world: World, *, corner: bool = False
) -> bool:
    """Run this village's camera into its own map corner, then drag the village to the middle.

    **The corner is where the park starts, not where it ends**, unless `corner`
    asks for it. Pushed into its corner the home village of a bought scenery
    kept its wall block and collector row off the top of the screen or under
    the storage bars, and the pinch back to the free scale moved it by up to
    100 px from one park to the next, so nothing it found stayed where it was
    found. Dragged to the middle at the far zoom first and pinched in about the
    middle after, the pinch can only change the size: measured over four parks
    from scrambled cameras, the village landed within about 30 px and 2% of
    itself, with 13 to 17 collector markers on screen against 0 to 2 in the
    corner. A free scenery and the builder base are already at the measured
    scale, so they get their own drag and no pinch: the jungle's corner showed
    1 collector marker and the same drag brought 15. The crossing is the one
    caller that asks for the corner, since the builder base's boat is under the
    storage bars once its village is centred.

    **Answers whether it got there**, which a fixed number of swipes could not.
    This said there was nothing to check against — the game reports no camera
    position, and no reader here can say where a village sits on its own frame —
    and that was true of the *position*. It is not true of the *movement*:
    `view_shift` already slides one frame over the other along a drag, and a
    camera that has stopped moving is a camera at the clamp. So the swipes are
    counted against the picture rather than against a constant, and False means
    every coordinate the caller was about to spend is invalid.

    **False is worth acting on rather than pressing past.** A crossing whose
    camera never arrived taps water three times over about three minutes; the
    loot cart taps grass. Both then report that the thing they were looking for is
    missing, which sends the next reader after a boat that never moved.

    **The caller has to know which village this is, and that it is a village.**
    A swipe with a card selected deploys troops along its path rather than
    panning, so a park sent into a battle puts the army out along a line nobody
    chose; and the two maps clamp in opposite corners, so the wrong village's
    push walks away from the clamp instead of into it. Neither can be settled
    from in here without a capture the callers have already taken, which is why
    `world` is a parameter rather than something this reads for itself.

    **`view_shift` answers in steps of a fifteenth of the drag**, about 65 px
    here, so (0, 0) means "moved less than half a step" rather than "did not
    move at all". That is the resolution this rests on: measured, a camera still
    walking reports 65, 131 or 261 and a clamped one reports 0, so what a real
    clamp has to survive is one reading it could have earned by creeping 30 px.
    Two consecutive readings are what make that unlikely rather than one.

    **The pinch belongs to this function, and what it really buys is something
    for the reader to look at.** `view_shift` compares a fixed box in the middle
    of the screen, and **what breaks it is an empty box rather than a long
    move**: with nothing in it to compare, every offset scores the same. That
    used to be resolved by the tie-break rather than reported, and the tie-break
    picked by the sign of the drag — so the same nothing read as (0, 0) on the
    home village, which means *arrived*, and as a long step on the builder base,
    which means never stops. It answers None now and this walk carries on, which
    is the right move for both. This said the failure was distance, which does
    not hold up: over synthetic pans a move past the box's reach saturates at a
    wrong non-zero, which only costs another swipe.

    So the far zoom is what keeps that box on the village. Measured two pinches
    in, the camera runs off the map into the dark border, the box lands on
    nothing, and a real 221 px move came back (0, 0) — it was still walking
    after fourteen swipes. At the far limit the box is over the village and the
    whole walk is short: the home village's range along its crossing is
    **352 px**, one swipe crosses all of it and thirteen more move nothing, while
    the builder base has its boat and loot cart on screen from the start and
    pans less than one `view_shift` step along its crossing — so it reads as
    parked wherever along a band about 55 px wide the camera happened to start
    (see `CART_SPOTS`). **Five unchecked swipes were the old answer
    and bought neither end.**

    **The one cover this has to shut itself is the plate panel**, and it is not
    a full-screen one. It floats over the middle of the map, so every village
    test the callers have answers through it and `uncovered` hands it straight
    back — while it fills 60% of the box this reader compares, which pins the
    answer at (0, 0) whatever the camera does: measured, true moves of 65, 131
    and 261 px all read as no move at all. No caller clears it, and nothing else
    can, since the button toggles and a run that died with one open leaves it
    up. So this presses it shut before it reads anything, and stands down rather
    than parking if it will not go. **That makes this the one tap this function
    takes**, which its swipes' own safety argument does not cover — it is at
    y 48 on a frame every caller has already read as a village, so it cannot
    land in a battle.

    A full-screen panel is still not handled here and does not need to be: it
    fails every village test, so no caller reaches this with one.
    """
    png = adb.screenshot(display)
    # **Before the pinch, to save the 2.6 s a park that is going to stand down
    # would otherwise spend on it** — not because the pinch lands on the panel.
    # It does not: a zoom out starts its fingers at x 300 and x 1300, outside
    # all four measured panel boxes.
    #
    # **Two taps rather than three, and the parity is the point.** A detection
    # that will not clear is answered an even number of times, so a false
    # positive on village grass ends with the screen as it was found rather than
    # with a panel this opened standing over it — which would be worse than what
    # is being fixed, since a `False` from here ends a whole `walls` or
    # `collect` run.
    for _ in range(PANEL_TAPS):
        button = plate_panel_open(png, world)
        if button is None:
            break
        logger.info("A plate panel is over the village; pressing it shut before parking")
        adb.tap(*button, display)
        time.sleep(UNCOVER_SETTLE)
        png = adb.screenshot(display)
    else:
        logger.warning("The plate panel would not shut; the camera cannot be read through it")
        return False
    adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, display)
    if (after := _walk(adb, display, world)) is None:
        return False
    if world == "day" and boat_marker(after) is not None:
        if not corner:
            _to_middle(adb, display, CENTRE_DRAGS["free"])
        return True
    if not corner:
        _centre(adb, display, world)
        return True
    if world == "day" and not _free_scale(adb, display):
        # The pinch did not bring the marker back, so this was no bought
        # scenery's far zoom: the far park is what everything was measured on.
        adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, display)
        return _walk(adb, display, world) is not None
    return True


def _centre(adb: AdbController, display: DisplayTarget, world: World) -> None:
    """Drag the village from its far corner to the middle, and pinch a bought scenery in about it.

    **A home village with no boat marker at the far corner is taken for a
    bought scenery without pinching to confirm it.** The confirmation pinched
    in at the corner, zoomed back out and walked again before the drag, which
    the user watched as the camera settling three times over a park that had
    landed the first time. What it guarded is a free scenery whose marker
    something covered for that one frame, which then gets pinched to twice its
    scale until the next park; the corner park the crossing takes still
    confirms, since its measured spots are worth nothing at the wrong scale.
    """
    if world == "night":
        _to_middle(adb, display, CENTRE_DRAGS["night"])
        return
    logger.info("The far zoom drew no boat marker; centring a bought scenery and zooming in")
    _to_middle(adb, display, CENTRE_DRAGS["bought"])
    adb.zoom("in", 1, COC_PACKAGE, display, CENTRED_PINCH)


def _to_middle(
    adb: AdbController, display: DisplayTarget, drag: tuple[tuple[int, int], tuple[int, int]]
) -> None:
    """One slow drag from the corner, which lands the village 1:1 where it was aimed."""
    adb.swipe(*drag, CENTRE_MS, display)
    time.sleep(SWIPE_SETTLE)


def _walk(adb: AdbController, display: DisplayTarget, world: World) -> bytes | None:
    """Swipe into this village's corner until the picture stops; the frame it stopped on, or None."""
    # Keyed by the village being sailed to, so the push away from the village
    # being stood on is the other one's.
    crossing = CROSSINGS["night" if world == "day" else "day"]
    landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
    before = adb.screenshot(display)
    still = 0
    for swipe in range(1, PARK_SWIPES + 1):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)
        after = adb.screenshot(display)
        moved = view_shift(before, after, crossing.drift)
        before = after
        # None as well as a real move: a pair the reader could not make out is
        # not a camera that has stopped, and the walk going on is what this
        # caller wants out of both. `view_shift`'s own warning names which.
        if moved != (0, 0):
            still = 0
            continue
        still += 1
        if still >= PARK_STILL:
            # At INFO, because how far this village really walks is the number
            # nobody could see while it was a constant.
            logger.info("The %s camera stopped moving after %d swipe(s)", world, swipe)
            return after
    logger.warning(
        "The %s camera was still moving after %d swipes; every remembered coordinate is off",
        world,
        PARK_SWIPES,
    )
    return None


# **A scenery bought from the shop lets the camera out about twice as far as the
# free ones, over a map much larger than the village, and at that far zoom the
# game draws no markers at all** — not the boat's, not a collector's — so
# `collect` found nothing and the crossing tapped scenery, while every
# coordinate here was measured at a free scenery's far park. Measured on one
# bought scenery against the jungle on the same village: the free far zoom is
# 2.04 to 2.09 times the bought one (the village matched across the two, and
# the distance between two labels over it), and from the bought park one
# gesture of 150 to 309 about (780, 368) put the village at the jungle park's
# size and pixels, its clan castle label on the same pixel.
BOUGHT_PINCH = Pinch(near=150, far=309, centre=(780, 368))
# **The drag from each far corner that puts the village in the middle of the
# screen**, measured on 2026-10-07 on one bought scenery, the jungle and the
# builder base: the usable middle is (800, 490), between the plate row and the
# bottom, and the drags put the far views' village middles there — about
# (880, 300) on the bought scenery's far zoom and (970, 955) on the builder
# base's. The jungle's is the bought one's at the free scale, read off where its
# clan castle label lands on both. Slow enough to land 1:1, measured: a 1500 ms
# drag moved the picture exactly its own length with no fling after it. Then the
# bought scenery is pinched back to the free scale about that middle, which is
# what keeps a pinch that varies from moving the village (see `park_camera`).
CENTRE_DRAGS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "bought": ((840, 305), (760, 495)),
    "free": ((830, 315), (770, 485)),
    "night": ((885, 632), (715, 167)),
}
CENTRE_MS = 1500
CENTRED_PINCH = Pinch(near=BOUGHT_PINCH.near, far=BOUGHT_PINCH.far, centre=(800, 490))


def _free_scale(adb: AdbController, display: DisplayTarget) -> bool:
    """Zoom a park with no boat marker in to where a free scenery stops; whether the marker came back.

    A free scenery's home park has the boat's marker on screen, and a bought
    one's far zoom draws none, so its absence is what calls for this. Its coming
    back is what confirms it: on a free scenery whose marker something covered,
    this pinch carries the boat off the left edge, where none reads either. The
    builder base has no scenery to buy.
    """
    logger.info("The far zoom drew no boat marker; zooming in to where a free scenery stops")
    adb.zoom("in", 1, COC_PACKAGE, display, BOUGHT_PINCH)
    if boat_marker(adb.screenshot(display)) is not None:
        return True
    logger.warning("The boat's marker did not come back; going back to the far zoom")
    return False


# The crossing plays an animation and reloads the other village. Measured, one
# tap was answered four seconds later; this polls rather than sleeping the worst
# case, since a crossing that already happened costs nothing to notice early.
SAIL_POLLS = 8
SAIL_GAP = 1.5

# How many times to press `back` at whatever is covering the village. A building
# panel goes in one, and the ceiling is there because a game still loading takes
# none: `back` cannot hurry that, so more presses would only be more waiting.
# How many times the plate panel is pressed at before this gives up. Even, so a
# detection that will not clear leaves the screen as it was found.
PANEL_TAPS = 2
UNCOVER_TRIES = 3
UNCOVER_SETTLE = 1.5


# The loot cart, moored on the grass beside the builder base's own boat and so
# reached by the same clamped view. **It is where that village's elixir goes**:
# measured over one attack, the gold landed in the storages directly while the
# elixir went into the cart and stayed there, so a run nobody empties by hand
# farms half of what it wins.
#
# **Tapping it opens a panel rather than collecting**, which is the opposite of
# the home village's collector markers and cost the first attempt everything it
# went for: the tap landed, the 聖水車 sheet came up, and the storage bars
# afterwards said nothing had been paid. What has to be pressed is its own 收集,
# and then the sheet closed again, because it covers the middle of the village.
# The first one this opened was holding 300 000 of what was then a 1 000 000
# ceiling; `night_cart_locked.png` writes 135 843 / 1 600 000, so it grows.
#
# **The cart does not sit in one place on the parked view**, so the spots are
# spread evenly across the band it has been seen in, 28 px apart against a cart
# about 70 px wide. Over 29 trips on LDPlayer, (1240, 610) opened the sheet on
# 18 of the 21 up to 23:45 on 2026-09-27 and (1262, 624) on the other three;
# from 23:59 on it was (1262, 624) on six of eight and nothing on two, while
# (1218, 596) opened nothing on any of the 29. **Where the park ends depends on
# where the camera started**, and every end reads as parked: parks from an
# already parked camera put the cart's body at (1295-1300, 612-618), four more
# pushes after one moved the picture by nothing and two more by 7 px, and yet a
# park started from a camera shoved the other way stopped with the cart at
# (1245, 622) — where the first spot used to find it — and (1268, 615) opened
# the sheet there. Finding the cart on the frame instead is no easier: its
# elixir bubble is dimmed at night to a khaki plate and a (110, 30, 200) drop,
# which none of `collect_bubbles`' marker colours take.
#
# **Those were measured in the corner, and the park now drags the village to the
# middle**, so the spots below are those three carried by the builder base's
# centring drag of (-170, -465): (1296, 617), (1268, 615) and (1240, 610). The
# drag lands 1:1, so they keep the spread the band needed. On the centred view
# the cart's marker read at (1110, 95) and (1105, 90) on two parks, and a tap at
# (1105, 130) opened the sheet.
CART_SPOTS = ((1126, 152), (1098, 150), (1070, 145))
CART_COLLECT = (1176, 760)
CART_CLOSE = (1338, 89)
CART_SETTLE = 1.5
# **The sheet animates the first time it opens after a battle, and every trip
# here comes straight after one.** Measured on a burst from the tap: the held
# number is still counting up at 0.9 s (1 238 535 against a settled 1 256 000),
# a column of elixir drops covers it and the plank beside the bar from 1.4 s to
# 1.9 s (0.76 of plank at 1.9 s), and it holds still from 2.4 s on. Read at
# `CART_SETTLE` instead, one live trip reported `not_found` over a cart holding
# 1 198 000 and landed its next two spots on the sheet's own 重播 buttons — the
# drops, by every reading of that trip, having taken the plank under
# `CART_PLANK`. The amount does not lengthen it: a first open after four
# defences, about 634 000 at once, had finished counting by 1.5 s. A reopened
# sheet does not animate at all.
CART_OPENING = 3.0


def _locked_cart(sheet: bytes) -> CartReport:
    """Which of the two a greyed 收集 means, off the amount written beside it.

    The button cannot say: a cart this loop emptied minutes ago is nothing to
    act on, and one holding loot behind a full 聖水 storage is a village banking
    elixir in its cart until that fills too. Read off the frame
    `loot_cart_open` accepted rather than the re-read taken to confirm the
    button, which is gated on nothing.
    """
    load = loot_cart_load(sheet)
    if load is None:
        logger.warning("The cart is open, its 收集 greyed, and the line would not read")
        return CartReport(outcome="locked")
    held, capacity = load
    logger.warning("The cart is open, its 收集 greyed, and it is holding %d of %d", held, capacity)
    return CartReport(
        outcome="locked_holding" if held else "locked_empty", held=held, capacity=capacity
    )


def collect_cart(adb: AdbController, display: DisplayTarget) -> CartReport:
    """Empty the builder base's loot cart, and say what really happened there.

    Judged on the storage bar rather than on the cart, for the reason `collect`
    judges its markers that way: a full storage takes none of what it is handed,
    and a tap the game swallowed leaves the cart looking exactly like one that
    has just been emptied. Both readings are taken with the sheet down, since it
    covers the bars while it is up.

    **A locked 收集 is the state this could not see, and it is the ordinary one
    on a farmed village.** The game greys that button — measured live, a flat
    (178, 178, 178) with both storages exactly at capacity and 135 843 elixir
    waiting — and the sheet used to be recognised by the button's green, so a
    locked cart read as no cart at all: every candidate spot was reported as a
    miss, the trip answered 0, and **the sheet was left standing over the
    village** for whatever ran next to tap into. `loot_cart_open` reads the
    sheet's own plank now and `loot_cart_ready` reads the button.

    **The sheet is closed on every path that recognised it**, which is the half
    of that failure nobody would have noticed from the report. A sheet the
    reader missed is a different matter and gets the crossing's own answer: the
    spots are places on the map, so a miss opens whatever building was standing
    there, and `uncovered` presses that away before this gives up — measured,
    `night_cart_locked.png` reads as no village at all, so a sheet left standing
    was itself a way to send the next `collect` to the wrong village.

    **`read_builder_stock`, because this only ever runs on the builder base.**
    `read_stock` reads a third row that village does not have: what sits at that
    y is its gems bar, and 10 152 gems come back as `dark=10152`, the gem count
    read as dark elixir. Two costs, both real. It has to resolve at all for
    `read_stock` to answer anything, so a gems row that will not read loses the
    whole trip to the cart — the taps are spent, the elixir is collected, and
    this reports 0. And it logged `Village holds … dark=410005` in the middle of
    a builder base run, which is the one line a reader uses to tell the two
    villages apart in a log.
    """
    if current_world(adb.screenshot(display)) != "night":
        # **Its own outcome and not `not_found`**, because nothing was tapped
        # here: the caller read the world before it called, so arriving is the
        # two readings disagreeing — a gem shower over the badge row does it,
        # which this project has recorded three times — and a sentence about
        # three candidate spots would be describing taps that never happened.
        logger.info("The loot cart is the builder base's; there is none here")
        return CartReport(outcome="wrong_world")
    # The cart is moored beside this village's own boat, so the view that finds
    # it is the parked one — the same push `park_camera` makes, which is why
    # this asks for it rather than repeating the swipes. **Nothing puts the zoom
    # back afterwards any more**, and nothing needs to: the park pinches out
    # before it swipes, this never leaves the village it started on, and neither
    # a tap nor a swipe changes the scale. The pinch that used to sit in a
    # `finally` here was written for a park that ran at whatever camera it was
    # handed, and it explained itself as undoing the swiping — which never
    # touched the zoom in the first place.
    if not park_camera(adb, display, "night"):
        # **Its own outcome rather than an empty cart**, because the two are
        # opposite news: `empty` is a cart that was found and had nothing in it,
        # and this is a cart nobody went looking for. Reported as the first, it
        # reads as an empty cart on a village whose elixir is all still in one.
        logger.warning("The camera never parked; the cart's spots are somewhere else entirely")
        return CartReport(outcome="not_parked")
    before = read_builder_stock(adb.screenshot(display))
    for spot in CART_SPOTS:
        adb.tap(spot[0], spot[1], display)
        time.sleep(CART_OPENING)
        sheet = adb.screenshot(display)
        if loot_cart_open(sheet):
            break
        logger.info("The tap at %s did not open the cart; trying the next spot", spot)
    else:
        logger.warning("None of the %d candidate spots found the cart", len(CART_SPOTS))
        # **Every one of those taps was a place on the map**, so a miss opened
        # whichever building was standing there — and a full-screen panel left
        # behind is what `commands.collect` then reads as no village at all,
        # which sends a builder base run home on a boat nobody asked for.
        # `uncovered` is the same step the crossing takes and is safe for the
        # same reason: never on a village, never on a battle.
        uncovered(adb, display)
        return CartReport(outcome="not_found")
    # **Read again before calling it locked**, because that answer reads as a
    # conclusion about the village rather than about this frame: the sheet is
    # still painting its button on the capture that recognised it, and this is
    # the path that is giving up anyway, so a second look costs one capture on a
    # trip that has already spent four.
    ready = loot_cart_ready(sheet) or loot_cart_ready(adb.screenshot(display))
    if ready:
        adb.tap(*CART_COLLECT, display)
        time.sleep(CART_SETTLE)
    # **Outside the `if`**, because the sheet covers the middle of the village
    # whether or not anything was collectable, and the run that found that out
    # left it there for the next command to tap into.
    adb.tap(*CART_CLOSE, display)
    time.sleep(CART_SETTLE)
    if not ready:
        return _locked_cart(sheet)
    after = read_builder_stock(adb.screenshot(display))
    if before is None or after is None:
        logger.warning("The storage bars would not read either side of the cart")
        return CartReport(outcome="unreadable")
    gained = after.elixir - before.elixir
    logger.info("The loot cart paid %d elixir", gained)
    # **Greater than zero rather than truthy**, because the storage can also go
    # down between the two readings — the builder base loses loot when somebody
    # raids it, which is what that village's whole defence reward is about — and
    # a negative difference reported as `collected` prints 收到聖水 -100000 and
    # counts a marker for it.
    return CartReport(outcome="collected" if gained > 0 else "empty", elixir=gained)


def uncovered(adb: AdbController, display: DisplayTarget) -> World | None:
    """The village under whatever is over it, pressed away; None if none appeared.

    **A tap that misses the boat does not miss the ground.** Every spot this
    module aims at is a place on the map, so a tap that was a pixel out opens
    whatever building is standing there — and a building panel swallows the
    swipes and taps that come after it, which is how one missed spot used to
    cost the whole crossing and the two remaining candidates with it. Measured
    live: a `collect` run left an 8級聖水收集器 panel up, and `ai_coc world
    --go night` then reported 畫面還停在不明的畫面 twice in a row without
    moving anything.

    **A battle is never pressed at**, and that is the whole reason this filters
    rather than pressing at anything `current_world` will not name. Its callers
    hand it whatever is on screen: `commands.world` goes straight into `cross`
    on an unreadable frame — so a game left mid-battle by a killed run, which
    is a state this project has written down, would take three presses aimed
    at 放棄. Every
    other place that presses `back` filters first — `GameRunner._home` on its
    dialogs, and the attack loop by routing through here rather than pressing
    itself — and this is not the one to make an exception of. A battle answers
    None, which every caller already handles as "nothing to work from".

    **But a card row is not a battle, and taking it for one turned every panel
    the loop tapped open into a deadlock.** `card_groups` answers wherever the
    bottom of the frame holds card-shaped patches, which the game's own panels
    do: the 探礦者 sheet reads three cards off its builder portraits, and the
    shop's 外觀 page reads a row off the skins on sale. Both were reported here
    as battles, so `back` was never pressed and nothing else clears them —
    measured, ten rounds and ten minutes on the first, and a run that spent
    twenty frames inside the shop on the second. `in_battle` is the feature that
    was missing: the 放棄 plate is on screen for the whole of a battle and on
    none of those, so the pair is what a battle is now, and either one alone is
    not. The panels then get their `back` like any other cover.

    Otherwise `back` is safe for the reason `_home` gives: on a clear village it
    raises 確定退出遊戲嗎, so it is only ever pressed on a frame that is **not**
    one. A village that reads is handed straight back untouched.
    """
    for _ in range(UNCOVER_TRIES):
        png = adb.screenshot(display)
        if (here := current_world(png)) is not None:
            return here
        # Nothing on the loading screen answers a press, so the presses were
        # only ever noise in the log — and the log is what a session reads to
        # tell a server that will not answer from a game on the wrong screen.
        if loading_screen(png):
            logger.info(
                "The game is on its loading screen; there is nothing here to press back at"
            )
            return None
        if (card_groups(png) and in_battle(png)) or battle_over(png):
            logger.info("A battle is on screen; there is nothing here to press back at")
            return None
        logger.info("Something is over the village; pressing back to get at it")
        adb.back(display)
        time.sleep(UNCOVER_SETTLE)
    return current_world(adb.screenshot(display))


def _boat_spots(
    adb: AdbController, display: DisplayTarget, here: World, crossing: Crossing
) -> tuple[tuple[int, int], ...] | None:
    """Where to tap for the boat once the camera is parked; None when it never parked.

    The marker first, on the centred park every other caller uses, and then in
    the corner, which the measured spots were taken on. The builder base goes
    straight to its corner: centred, its boat is under the storage bars.
    """
    if crossing.marked:
        if not park_camera(adb, display, here):
            return None
        if (spot := boat_marker(adb.screenshot(display))) is not None:
            logger.info("The boat's marker is at %s", spot)
            return (spot,)
        logger.info("No marker over the boat on the centred view; parking in the corner")
    if not park_camera(adb, display, here, corner=True):
        return None
    if not crossing.marked:
        return crossing.spots
    if (spot := boat_marker(adb.screenshot(display))) is not None:
        logger.info("The boat's marker is at %s", spot)
        return (spot,)
    logger.info("No marker over the boat; trying its measured spots")
    return crossing.spots


def cross(adb: AdbController, display: DisplayTarget, want: World) -> World | None:
    """Sail to `want`, and answer which village the game was left on.

    Already being there is the ordinary case and costs one capture: no swipe, no
    tap, nothing moved. None means no village could be read even after whatever
    was over it was pressed away, which is a game that is loading or lost rather
    than one in the wrong village.
    """
    here = uncovered(adb, display)
    if here == want:
        return here
    if here is None:
        logger.warning("No village is on screen; there is nothing to sail from")
        return None
    logger.info("On the %s village and the %s one was asked for; sailing", here, want)
    crossing = CROSSINGS[want]
    # **Nothing here settles the landed village's camera, and nothing needs
    # to.** A pinch used to sit in a `finally` for it, on the reasoning that the
    # village the boat arrives on comes up at whatever camera the game gives it
    # — true, and beside the point, because every caller that goes on to tap the
    # map pinches again before it does. The attack loop pinches per battle,
    # the home village's loops on the first village they read, and
    # `commands.world` taps no map coordinate at all.
    #
    # `park_camera` keyed on the village being stood on pushes toward the one
    # being sailed to, which is this crossing's own drag — the two agree by
    # construction, and asking for it is what makes the boat's spot mean
    # anything. Without the clamp those spots are water: measured, a camera
    # 130 px short put every one of them above the boat, and the three taps
    # spent about three minutes between them before the run reported that there
    # was no boat.
    if (spots := _boat_spots(adb, display, here, crossing)) is None:
        logger.warning("The camera never parked, so the boat is not where it is remembered")
        return here
    # What the village last read as, so a crossing with nothing to try answers
    # where it started rather than nothing at all.
    cleared = here
    for spot in spots:
        adb.tap(spot[0], spot[1], display)
        for _ in range(SAIL_POLLS):
            time.sleep(SAIL_GAP)
            if (arrived := current_world(adb.screenshot(display))) == want:
                logger.info("Landed on the %s village from the boat at %s", want, spot)
                return arrived
        logger.info("The tap at %s did not sail; trying the next spot", spot)
        # A tap that opened a building instead of the boat leaves its panel over
        # the map, and every candidate after it lands on that panel rather than
        # on the ground. Clearing it is what makes the remaining spots worth
        # trying at all.
        if (cleared := uncovered(adb, display)) is None:
            logger.warning("The village never came back; giving up on the crossing")
            return None
    logger.warning("None of the %d candidate spots found the boat", len(spots))
    # What the last spot's own check already read, rather than a capture asking
    # the same question again: one of those is 0.6-0.8 s here.
    return cleared
