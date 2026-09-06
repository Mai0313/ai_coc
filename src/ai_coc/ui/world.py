"""Crossing between the game's two villages, which is a boat and nothing else.

There is no menu for this. The only way over is a boat moored at one corner of
each map, so the crossing is a tap at a place on the ground — and a place on the
ground is exactly what this project refuses to remember, because the camera
moves and what is under a coordinate changes with it.

**What makes a remembered spot safe here is that the camera clamps.** The game
will not pan past the edge of its own map, so swiping hard at one corner ends at
the same view whatever the camera was doing before; measured with `view_shift`, the
home village stops moving after the second swipe and the builder base after the
fourth, and every swipe after that reports (0, 0). From that view each boat sits
at one fixed pixel.

**Nothing here recognises the boat.** It is a sprite and the game dresses it up
for events, so it is tapped rather than looked for, and `parsers.world` is what
says whether the tap worked. A tap that landed on the water instead simply
leaves the world unchanged and the next candidate spot is tried. That is the
pattern `hero --at` already uses on a building that two taps in a row select
alternately.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import Crossing
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import ZOOM_PINCHES
from ai_coc.parsers.scout import (
    in_battle,
    battle_over,
    card_groups,
    loading_screen,
    loot_cart_open,
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
    # Leaving the home village: the boat is on the water off the bottom-left
    # shore. The candidates stay up and left of the measured spot on purpose —
    # the clan capital's own vessel is moored below and to the right of it, and
    # tapping that one opens a village this project has no business in.
    "night": Crossing(
        start=(500, 600), drift=(700, -350), spots=((400, 625), (382, 610), (416, 634))
    ),
    # Leaving the builder base: the boat is at the pier off the top-right. The
    # candidates stay left of the measured spot, because the game's right-hand
    # button column starts at x 1490 and overlaps the boat's stern.
    "day": Crossing(
        start=(1100, 300), drift=(-700, 350), spots=((1440, 545), (1415, 555), (1432, 522))
    ),
}
# The measured clamp plus slack: over-swiping a clamped camera costs a second and
# does nothing at all, while under-swiping leaves the boat off screen and the
# whole crossing fails.
SWIPES = 5
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
# camera up against the edge and it stops: measured over three runs from three
# scattered starting positions, the view stopped moving within two swipes
# (shifts of (196, -98) then (0, 0); (0, 0) twice; (65, -33) then (0, 0)) and the
# three frames it ended on aligned at `view_shift` (0, 0) with a mean absolute
# pixel difference of 1.95 to 1.99 — water and flags, nothing else. The whole
# village is on screen there, the diamond spanning about x 200-1400 by y 60-780.
#
# So the camera is parked rather than centred, and each village is pushed into
# its own corner with its own crossing's swipe. **Which village it is on decides
# both**: the two maps clamp in opposite corners, and the builder base needs four
# swipes to the home village's two, which is what `SWIPES` was already sized for.
# A park that always pushed the home village's way would leave the builder base
# short of any clamp, at a position that is neither measured nor reproducible —
# which is the one thing this is for.


def park_camera(adb: AdbController, display: DisplayTarget, world: World) -> None:
    """Run this village's camera into its own map corner, where it stops.

    Nothing is read here and nothing is checked, because there is nothing to
    check against: the game reports no camera position and no reader in this
    project can say where a village sits on its own frame. What makes it safe to
    run blind is the same property that makes it useful — a swipe at an already
    clamped camera does nothing at all.

    **The caller has to know which village this is, and that it is a village.**
    A swipe with a card selected deploys troops along its path rather than
    panning, so a park sent into a battle puts the army out along a line nobody
    chose; and the two maps clamp in opposite corners, so the wrong village's
    push walks away from the clamp instead of into it. Neither can be settled
    from in here without a capture the callers have already taken, which is why
    `world` is a parameter rather than something this reads for itself.
    """
    # Keyed by the village being sailed to, so the push away from the village
    # being stood on is the other one's.
    crossing = CROSSINGS["night" if world == "day" else "day"]
    landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
    for _ in range(SWIPES):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)


# The crossing plays an animation and reloads the other village. Measured, one
# tap was answered four seconds later; this polls rather than sleeping the worst
# case, since a crossing that already happened costs nothing to notice early.
SAIL_POLLS = 8
SAIL_GAP = 1.5

# How many times to press `back` at whatever is covering the village. A building
# panel goes in one, and the ceiling is there because a game still loading takes
# none: `back` cannot hurry that, so more presses would only be more waiting.
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
# The first one this opened was holding 300 000 of a 1 000 000 ceiling.
CART_SPOTS = ((1240, 610), (1218, 596), (1262, 624))
CART_COLLECT = (1176, 760)
CART_CLOSE = (1338, 89)
CART_SETTLE = 1.5


def collect_cart(adb: AdbController, display: DisplayTarget) -> int:
    """Empty the builder base's loot cart; answers the elixir it actually paid.

    Judged on the storage bar rather than on the cart, for the reason `collect`
    judges its markers that way: a full storage takes none of what it is handed,
    and a tap the game swallowed leaves the cart looking exactly like one that
    has just been emptied. Both readings are taken with the sheet down, since it
    covers the bars while it is up.

    **`read_builder_stock`, because this only ever runs on the builder base.**
    `read_stock` reads a third row that village does not have: what sits at that
    y is its gems bar, and 10 152 gems come back as `dark=410152` with the green
    `+` read as a leading 4. Two costs, both real. It has to resolve at all for
    `read_stock` to answer anything, so a gems row that will not read loses the
    whole trip to the cart — the taps are spent, the elixir is collected, and
    this reports 0. And it logged `Village holds … dark=410005` in the middle of
    a builder base run, which is the one line a reader uses to tell the two
    villages apart in a log.
    """
    if current_world(adb.screenshot(display)) != "night":
        logger.info("The loot cart is the builder base's; there is none here")
        return 0
    crossing = CROSSINGS["day"]
    landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
    for _ in range(SWIPES):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)
    before = read_builder_stock(adb.screenshot(display))
    try:
        for spot in CART_SPOTS:
            adb.tap(spot[0], spot[1], display)
            time.sleep(CART_SETTLE)
            if loot_cart_open(adb.screenshot(display)):
                adb.tap(*CART_COLLECT, display)
                time.sleep(CART_SETTLE)
                adb.tap(*CART_CLOSE, display)
                time.sleep(CART_SETTLE)
                break
            logger.info("The tap at %s did not open the cart; trying the next spot", spot)
        else:
            logger.warning("None of the %d candidate spots found the cart", len(CART_SPOTS))
            return 0
        after = read_builder_stock(adb.screenshot(display))
    finally:
        # The swiping above leaves the camera at a map corner whether or not the
        # cart was found, and every coordinate in this project was measured at
        # the far zoom. Zooming out puts the scale back; the corner it is already
        # in is where `park_camera` would have put it anyway, so there is nothing
        # to undo — this used to say the pinch centred the village, and it does
        # not move the camera at all.
        adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, display)
    if before is None or after is None:
        logger.warning("The storage bars would not read either side of the cart")
        return 0
    gained = after.elixir - before.elixir
    logger.info("The loot cart paid %d elixir", gained)
    return gained


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
    on an unreadable frame, and so does `_pick_world` when a run named a
    village — so a game left mid-battle by a killed run, which is a state this
    project has written down, would take three presses aimed at 放棄. Every
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
    landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
    for _ in range(SWIPES):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)
    try:
        # What the village last read as, so a crossing with nothing to try
        # answers where it started rather than nothing at all.
        cleared = here
        for spot in crossing.spots:
            adb.tap(spot[0], spot[1], display)
            for _ in range(SAIL_POLLS):
                time.sleep(SAIL_GAP)
                if (arrived := current_world(adb.screenshot(display))) == want:
                    logger.info("Landed on the %s village from the boat at %s", want, spot)
                    return arrived
            logger.info("The tap at %s did not sail; trying the next spot", spot)
            # A tap that opened a building instead of the boat leaves its panel
            # over the map, and every candidate after it lands on that panel
            # rather than on the ground. Clearing it is what makes the remaining
            # spots worth trying at all.
            if (cleared := uncovered(adb, display)) is None:
                logger.warning("The village never came back; giving up on the crossing")
                return None
        logger.warning("None of the %d candidate spots found the boat", len(crossing.spots))
        # What the last spot's own check already read, rather than a capture
        # asking the same question again: one of those is 0.6-0.8 s here.
        return cleared
    finally:
        # On both paths, for the reason `collect_cart` gives: the swiping parks
        # the camera at a corner whether or not the boat was found.
        adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, display)
