"""Crossing between the game's two villages, which is a boat and nothing else.

There is no menu for this. The only way over is a boat moored at one corner of
each map, so the crossing is a tap at a place on the ground — and a place on the
ground is exactly what this project refuses to remember, because the camera
moves and what is under a coordinate changes with it.

**What makes a remembered spot safe here is that the camera clamps.** The game
will not pan past the edge of its own map, so swiping hard at one corner ends at
the same view whatever the camera was doing before, and from that view each boat
sits at one fixed pixel. **How many swipes that takes is not a constant**, which
this file assumed for a long time and paid for in every crossing: see
`park_camera`, which now swipes until the picture stops moving and says whether
it got there.

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
from ai_coc.parsers.field import view_shift
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
# **A fixed count was the bug, and five was half of what this account needs.**
# The count stood in for the clamp: swipe enough times and the camera must be
# against the map edge, so every coordinate measured from that view is valid
# again. Measured live, a home village parked once still moved (131, -65) when
# parked a second time, and took about ten swipes before it stopped — and that
# 130 px is exactly how far below the crossing's candidate spots the boat was
# sitting. The builder base needed four more from where a crossing leaves it.
# Both were twice what this allowed, and the whole failure is silent: the taps
# land on water, and the run spends three minutes finding that out — measured
# live, three spots took 80, 87 and 18 seconds before it reported no boat.
#
# So the swipes are counted against the picture instead. `PARK_SWIPES` is only a
# ceiling — twice the worst walk measured — and reaching it is a failure worth
# reporting rather than a park worth trusting.
PARK_SWIPES = 16
# Two readings rather than one, because a single swipe the game swallowed looks
# exactly like a camera that has arrived.
PARK_STILL = 2
# **The first few swipes are not checked, and that is what makes the check
# trustworthy.** `view_shift` slides a 700 px box that starts 450 px from either
# screen edge, so it cannot see a move larger than about that: measured against
# synthetic pans of a committed frame, 455 px reads correctly, 560 comes back
# short, and **700 — a single swipe's full travel — reads (0, 0)**, which is
# exactly the answer that means "arrived". A camera far from its clamp would
# therefore stop on the very move proving it is still walking. Swiping blind
# first spends that distance where nothing is being asked, and what is left is
# inside what the reader can see: every live park measured here moved 261 px or
# less per swipe once the first few were behind it.
PARK_BLIND = 5
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


def park_camera(adb: AdbController, display: DisplayTarget, world: World) -> bool:
    """Run this village's camera into its own map corner, where it stops.

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
    """
    # Keyed by the village being sailed to, so the push away from the village
    # being stood on is the other one's.
    crossing = CROSSINGS["night" if world == "day" else "day"]
    landing = (crossing.start[0] + crossing.drift[0], crossing.start[1] + crossing.drift[1])
    for _ in range(PARK_BLIND):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)
    before = adb.screenshot(display)
    still = 0
    for swipe in range(PARK_BLIND + 1, PARK_SWIPES + 1):
        adb.swipe(crossing.start, landing, SWIPE_MS, display)
        time.sleep(SWIPE_SETTLE)
        after = adb.screenshot(display)
        moved = view_shift(before, after, crossing.drift)
        before = after
        if moved != (0, 0):
            still = 0
            continue
        still += 1
        if still >= PARK_STILL:
            # At INFO, because how far this village really walks is the number
            # nobody could see while it was a constant.
            logger.info("The %s camera stopped moving after %d swipe(s)", world, swipe)
            return True
    logger.warning(
        "The %s camera was still moving after %d swipes; every remembered coordinate is off",
        world,
        PARK_SWIPES,
    )
    return False


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


def collect_cart(adb: AdbController, display: DisplayTarget) -> int | None:
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
    try:
        # The cart is moored beside this village's own boat, so the view that
        # finds it is the parked one — the same push `park_camera` makes, which
        # is why this asks for it rather than repeating the swipes. Inside the
        # `try` so a park that never arrived still leaves the zoom where every
        # other coordinate here expects it.
        if not park_camera(adb, display, "night"):
            # **None rather than 0**, because the two are opposite news and the
            # caller writes a sentence from them: 0 is a cart that was found and
            # had nothing in it, and this is a cart nobody went looking for.
            # Reported as the first, it reads as an empty cart on a village
            # whose elixir is all still sitting in one.
            logger.warning("The camera never parked; the cart's spots are somewhere else entirely")
            return None
        before = read_builder_stock(adb.screenshot(display))
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
    try:
        # `park_camera` keyed on the village being stood on pushes toward the
        # one being sailed to, which is this crossing's own drag — the two agree
        # by construction, and asking for it is what makes the boat's spot mean
        # anything. Without the clamp those spots are water: measured, a camera
        # 130 px short put every one of them above the boat, and the three taps
        # spent about three minutes between them before the run reported that
        # there was no boat.
        if not park_camera(adb, display, here):
            logger.warning("The camera never parked, so the boat is not where it is remembered")
            return here
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
