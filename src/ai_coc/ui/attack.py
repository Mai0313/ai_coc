"""One pass of 攻擊 → 偵察 → 進攻或跳過 → 回營.

Deliberately outside the Gemini agent loop. The scout screen expires after 30
seconds and takes the 下一個 button with it, which a vision call per candidate
does not reliably fit inside, and every opponent that is skipped anyway would
still have been paid for. Screen reading is `parsers.scout` instead.
"""

from __future__ import annotations

import time
import logging
from pathlib import Path
from functools import partial
from collections.abc import Callable, Iterable

from pydantic import BaseModel, PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    HeroOrder,
    LootOffer,
    ScoutView,
    AttackPlan,
    StockLimits,
    AttackReport,
    DisplayTarget,
    LootThresholds,
)
from ai_coc.prompts import PROMPTS
from ai_coc.ui.world import cross
from ai_coc.constants import COC_PACKAGE
from ai_coc.ui.runner import restart_game
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import AdbController
from ai_coc.parsers.field import view_shift, army_centre
from ai_coc.parsers.scout import (
    card_count,
    live_cards,
    read_scout,
    read_stock,
    battle_over,
    card_groups,
    field_units,
    card_drained,
    freeze_cards,
    skip_offered,
    army_strength,
    counted_cards,
    attack_menu_open,
    idle_disconnected,
)
from ai_coc.parsers.world import current_world
from ai_coc.parsers.boundary import DEPLOY_BOUND, fitted_line, village_box

logger = logging.getLogger(__name__)

# Everything the loop does on a clock: when it is due, what to call it in the
# log, and what to do. A tuple rather than a model because the payload is a bound
# call rather than data, and it never leaves this module.
Moves = list[tuple[float, str, Callable[[], None]]]

# Every coordinate is the 1600x900 layout, the one `_apply_agent_action` assumes.
HOME_ATTACK = (105, 830)
FIND_MATCH = (272, 665)
ARMY_ATTACK = (1411, 803)
NEXT_TARGET = (1450, 630)
END_BATTLE = (118, 670)
RETURN_HOME = (798, 768)

# Card positions come from the frame rather than a constant, because the row
# depends on the army. Cards are then emptied in passes: `live_cards` reports
# which ones are done, so the passes stop as soon as the cards are empty rather
# than running a tap count guessed up front.
#
# A pass used to drop four per card and there used to be ten of them, which read
# as cautious and was the opposite. A pass costs a settle and a capture whatever
# it carries, so four-at-a-time meant a twelve-giant card took three passes and
# nine seconds of overhead to put down nine seconds' worth of troops; measured,
# the whole troop deployment took 37 seconds and still left a card holding
# something.
#
# **A pass now walks the line twice, so one pass empties the card.** At one
# circuit it was the card that decided: measured on a live row of x9 dragons and
# x16 balloons, the first pass emptied the dragons and left four balloons, which
# went down in a second pass **1.4 seconds later** — a settle, a capture and a
# decode after the rest of the army. Four balloons arriving on ground the push
# has already left are four balloons on their own, and balloons are slow.
#
# **Which way this cuts depends on the army, so both directions are measured.**
# A tap costs `TAP_GAP` plus its own `input` exec, about 0.062 s, and a pass
# boundary costs 1.4 s whatever it carries. On that dragon-and-balloon row the
# old shape spent 1.66 s + 1.4 s + 0.85 s = 3.91 s reaching the last troop and
# the new one spends 3.15 s, so it is **0.76 s faster** as well as unbroken. On
# an army of four cards none holding more than twelve — the hog-rider and giant
# rows this file's other comments describe — the first circuit already emptied
# every card, so the second is 0.74 s per card of tapping into nothing and the
# deployment runs about 3 s longer.
#
# **That trade is now only paid by a card whose corner will not read.** A
# counted card says what it holds, so `_spread_troops` taps that many plus one,
# and this figure is both what a card the artwork swallowed falls back to and
# the ceiling on what a count is allowed to ask for. Measured on a row of x9,
# x3 and x2, that is 34 taps rather than 75. Which corners read was measured
# too: of those three, the two on blue plates came back 3 and 2, and the pale
# one merged its `x` into the background — one 25 px span matching no
# character — which is why the fallback has to stay. `DEPLOY_PASSES` is behind
# all of it for a card bigger than two circuits.
#
# An over-tap on a card that has just emptied costs nothing beyond its own
# 0.062 s: the selection clears with the card, so the taps that follow land on
# ground with nothing selected.
CARD_ROW_Y = 800
DROPS_PER_PASS = 24
DEPLOY_PASSES = 6
# How many points along the line each tap of a pass moves. Coprime with
# LINE_POINTS, so a pass still visits every point exactly once and a full card
# lands exactly where it used to — what changes is that consecutive taps are
# five points apart rather than neighbours.
#
# It used to be one, and a card holding fewer troops than a pass has taps then
# emptied into a huddle at whichever point it started on. Three baby dragons
# went down 41 px apart that way, and a baby dragon rages only while no other
# air troop is within about three and a half tiles — 84 px across at this
# camera — so all three sat in each other's way and not one of them ever raged.
# At this stride they land 208 px apart. Two headhunters had the same problem
# and the same fix; a full card of twelve hog riders is unaffected either way.
DROP_STRIDE = 5

# Lines just outside the deployment boundary on each flank, used when there is
# no plan or the line it drew crosses the village. A drop inside the boundary is
# refused, and a named side maps onto a line already known to be outside it.
DEPLOY_LINES = {
    "top_left": ((600, 110), (230, 380)),
    "top_right": ((1000, 110), (1370, 380)),
    "bottom_left": ((230, 430), (600, 660)),
    "bottom_right": ((1370, 430), (1000, 660)),
}
DEPLOY_START, DEPLOY_END = DEPLOY_LINES["top_left"]
LINE_POINTS = 12

# Where the spells go when no plan says otherwise. Rage covers 5 tiles, and the
# 44x44 map spans about 1040x605 px here, so a tile is roughly 24x12 and the
# footprint is a 240x120 ellipse rather than a circle: the board is isometric, so
# vertical spacing is half the horizontal. Two overlapping rages waste one, hence
# this pitch. It runs from the drop flank towards the middle, because rage
# belongs where the troops are about to walk, not where they land.
RAGE_PATH = (
    (520, 300),
    (760, 300),
    (520, 420),
    (760, 420),
    (1000, 300),
    (1000, 420),
    (640, 180),
    (880, 180),
)
# Freeze is the opposite: it is held back until the troops are deep enough to be
# under fire, and goes where they are rather than where they were headed.
FREEZE_TARGET = (800, 420)
# One bottle's own footprint, which is what tells two of them apart. It is the
# pitch RAGE_PATH is already laid out on; naming it is what lets a planned point
# be measured against the same ellipse.
RAGE_SPAN = (240, 120)

# What waits on the clock when nothing named a moment for it. Every timing on an
# attack belongs to the plan now, and `plans/flat.json` carries these same three
# — so these are reached only by a card the plan did not name, which is what a
# flat plan leaves every hero, and by a `_deploy` running without a plan at all.
#
# They are neutral rather than good. A queen wants her cloak inside a second or
# two, a warden's tome wants the push to be under fire first, and no single
# number is right for both; a card given this one fires somewhere between the
# two and gets in the way of neither. That being unsatisfying is the point of
# asking the planner instead.
FALLBACK_ABILITY = 20
FALLBACK_RAGE = 15
FALLBACK_FREEZE = 30

# How far apart the two captures that locate the army are taken. It is the one
# number `parsers.field`'s own thresholds are tied to, since both the moving
# pixels and the ambient shimmer scale with it.
MOTION_GAP = 1.5

# Between selecting a one-off card and placing what it holds. It was 0.6 on the
# reasoning that a hero gets one attempt where a troop card is tapped repeatedly,
# so a swallowed tap costs the whole hero. Measured against a live battle, a
# single burst that selected and placed the siege machine and all four heroes at
# 0.15 put every one of the five on the field.
SINGLE_DROP_DELAY = 0.15
# Selecting a spell opens a radius indicator that has to be up before a
# placement lands: measured live, 0.6 s goes through where the 0.1 a troop is
# happy with does not. Only the selection is slow, though — the card stays
# selected while it still holds something, so the rest of a card's spells follow
# as a fast burst.
SPELL_SELECT_DELAY = 0.6
SPELL_PLACE_GAP = 0.15

# A card takes a moment to show that it lost something. Measured live, a corner
# read by the first capture taken after the tap — which lands 0.86 s later,
# because a capture is 0.7 s of that on its own — had already repainted.
DROP_SETTLE = 0.5
# A hero's health bar is the slower of the two signals, and the expensive one to
# read early: a hero wrongly called refused has its card tapped again, which is
# its ability. This is the delay the five-in-one-burst measurement was taken at.
HERO_SETTLE = 1.5

# A village whose boundary reaches past the chosen line refuses every drop, and
# layouts vary far more than one line can allow for. So the line is tested with a
# single troop and pushed further from the middle until the game accepts it,
# staying inside the playfield the UI leaves free.
SCREEN_CENTRE = (800, 400)
PUSH_STEP = 70
DEPLOY_ATTEMPTS = 5
# A tile is about 24x12 px here, so two drops closer together than this are the
# same drop. It is what tells a push that moved the spot from one that was
# clamped straight back onto it: measured live, four pushes in a row of a point
# already on the map's edge came back within two pixels of each other.
SPOT_APART = 20
# A spell card that would not cast is offered the run once more and no further:
# past that it is a card the game is refusing, not a tap it happened to swallow.
SPELL_ATTEMPTS = 2
PLAYFIELD = (30, 105, 1570, 700)
# The 放棄 button, measured at x 12-205 and y 636-700 on the battle screen. It
# is the one piece of UI the playfield would otherwise reach over, and a drop
# pushed onto it is not a wasted troop but a button press: a hero pushed out
# three times landed at (49, 650), and the 結束戰鬥？ dialog it opened read as
# the battle being over, so every freeze and every hero ability was skipped and
# the report claimed the loot had never moved. That battle was winning at the
# time. Only this corner is excluded — trimming the whole bottom edge instead
# left the two lower flanks with no room to push past a village's boundary,
# which loses the army just as completely.
ABANDON_BUTTON = (215, 630)
# A planned line has to run past the village, not merely start and end clear of
# it. `push_out` moves a point away from SCREEN_CENTRE, so a point near the
# centre has almost no direction to be pushed in and a line drawn straight
# across the village is refused at every probe, which loses the whole army.
# The four preset flanks all sit about 410 px out at their midpoint; this keeps
# a planned line in the same band and falls back to a flank when it is not.
MIN_LINE_RADIUS = 300

# The camera is measured rather than assumed, and only moved when it is really
# off. Measured across nine battles the game opens every attack with the village
# already within 35 px of the middle, so this normally reads once and does
# nothing; the tolerance keeps it that way and leaves the drag for a camera that
# has genuinely been left somewhere else.
CAMERA_TOLERANCE = 60
CAMERA_ATTEMPTS = 2
# Putting the camera back at the far zoom before every battle, and how much of a
# change in the frame counts as evidence it had drifted there. Two pinches
# because `view` measured one as covering the whole range and a second as doing
# nothing, so this is that plus a spare; the whole thing costs about three
# seconds against a battle of three minutes.
#
# How short the village has to read before the pinch to say the camera had
# drifted. Swept over a day of live rounds, every healthy one measured 500 to
# 572 px tall and the single round that deployed nothing measured 411 — a
# village grown too big for the screen and clipped at top and bottom. 470 sits
# between them with room on both sides. This is only ever reported, never acted
# on: the pinch has already happened.
ZOOM_PINCHES = 2
ZOOM_CLIPPED = 470
CAMERA_GRIP = (800, 400)
CAMERA_DRAG_MS = 350
CAMERA_SETTLE = 1.5

# How much clear ground a flank needs between the village's own red line and the
# edge of the playfield. `fitted_line` already sits 30 px outside the stroke and
# a refusal is answered by pushing 70 px further, so this is the line plus one
# push and nothing spare.
#
# It is why the camera has to move at all. The card row takes the bottom of the
# screen, which leaves the playfield 595 px tall, and a village fills nearly all
# of it: measured over nine battles the red line spans 530 to 575 of those. So a
# lower flank has under 30 px of ground to work with, which is how one recorded
# battle spent three pushes clamped against y 700 and another deployed nothing at
# all. Dragging the village up hands that side the room, at the cost of the side
# nobody is attacking.
FLANK_ROOM = 110

# How long the planner gets before the loop stops waiting and plays the flat
# plan instead. It is the scout countdown, because the call starts at the top of
# one — measured on a recorded run, the opponent is read at 20:21:02 and the
# countdown ends at 20:21:31 — so a reply that arrives after it has already
# missed the window it was for. Thirty also clears every call measured so far,
# thirteen of them running 11.2 to 25.6 s, so it cuts none of them off. Without
# it one call took 180.7 s and then failed validation, by which point the
# three-minute battle it was planning was over and the army was spent on nothing.
PLAN_TIMEOUT = 30

# How many frames in a row may show an opponent whose loot will not read before
# the loop stops waiting on it and asks for a different one. Measured over 72
# groups of consecutive scout frames across four batches: of the 70 that
# eventually read, not one took more than **2** consecutive misses to do it,
# while both of the two that never read ran to 17. This sits in that gap.
#
# Being wrong in the two directions does not cost the same. Waiting is what the
# loop used to do, and the scout countdown expires while it waits: measured live
# on one ten-round batch, two rounds stood on an unreadable panel until the game
# force-started the battle with the whole army still in its cards, and each then
# spent two more rounds standing down with 畫面不在主村 before the result screen
# could be cleared. About four and a half minutes and a search fee, twice, for a
# battle that ended 戰敗 at 0% with nothing deployed. Asking for another opponent
# costs 1400 gold.
UNREADABLE_SKIPS = 5

# The scout countdown is 30 seconds; this polls a second at a time and leaves
# room for a slow frame rather than sitting through a whole battle.
COUNTDOWN_ATTEMPTS = 45
# Consecutive unreadable frames that end the wait early. One is a moment of
# animation over the panel; three in a row is a screen this cannot read.
UNREADABLE_ATTEMPTS = 3

BATTLE_TIMEOUT = 240
# Two things routinely cover the home village between runs: a building panel
# left open by a stray tap, which back closes, and the idle-disconnect dialog,
# which a loop that waits minutes for barracks will certainly meet. That one is
# answered by restarting the game rather than by tapping its 重新登入遊戲 button:
# the button reloads the game anyway, so the two cost the same seconds, and the
# tap also costs a hard-coded position belonging to this emulator's resolution
# alone. `ui.runner` owns the restart.
# A battle that pays out a reward covers the village with it, and the card tears
# itself open over about fifteen seconds before its 繼續 button appears. Nothing
# can be read off those frames — measured over one payout, `battle_over`,
# `attack_menu_open`, `idle_disconnected` and `read_stock` all answer no on every
# one of them — and the 攻擊 tap lands on the popup instead of the corner. Three
# attempts ran out a beat before the button arrived, so the whole round stood
# down with 畫面不在主村 and waited a minute for the next one to walk the same
# path. Five covers the animation with room for the card that follows it; once
# the button is up `_leave_result` already knows what to do with it, because a
# reward dismisses from where the result screen's 回營 sits.
HOME_ATTEMPTS = 5
HOME_RETRY_DELAY = 3
# The result screen animates its stars in before its button answers, so leaving
# it is a poll rather than a tap.
RESULT_ATTEMPTS = 4
RESULT_RETRY_DELAY = 3
# The army screen comes up before the search fee is charged, so a half-trained
# army can still back out for free rather than paying to attack with nothing.
MIN_ARMY_RATIO = 0.9


def clear_of_controls(point: tuple[int, int]) -> tuple[int, int]:
    """Pull a drop onto the ground the UI leaves free.

    Two things are in the way. The card row takes the bottom of the screen, and
    a tap down there is not a drop at all — it selects a card. That is how a
    plan answering y_pct 80 sent a hero to y 720 and spent all five of its
    attempts tapping its own army bar, and neither the plan's own line nor a
    flank fitted to the village boundary was being held inside the playfield.

    The 放棄 button is the other, in the corner the lower-left flank pushes
    towards; a drop landing on it opens 結束戰鬥？, which then reads as the
    battle being over. That one is dodged by lifting rather than by moving
    aside: a point ends up there because it is heading away from the village,
    and shifting it right would send it back towards the boundary it is clearing.
    """
    left, top, right, bottom = PLAYFIELD
    x = min(max(point[0], left), right)
    y = min(max(point[1], top), bottom)
    return (x, min(y, ABANDON_BUTTON[1])) if x < ABANDON_BUTTON[0] else (x, y)


def deploy_line(count: int, *anchors: tuple[int, int]) -> list[tuple[int, int]]:
    """Evenly spaced positions along a flank, walking through every anchor given.

    Two anchors is a straight chord; three or more bend it, which is what a flank
    fitted to a village's own boundary needs, since a chord across a diamond cuts
    back inside it. Every point is cleared of the button, not only the anchors:
    the span between two of them can cross that corner while both sit clear of it.
    """
    marks = list(anchors) or [DEPLOY_START, DEPLOY_END]
    if len(marks) == 1:
        marks = marks * 2
    legs = len(marks) - 1
    step = max(count - 1, 1)
    points: list[tuple[int, int]] = []
    for i in range(count):
        # Which leg this point falls on, and how far along that leg it sits.
        travelled = i / step * legs
        leg = min(int(travelled), legs - 1)
        (x0, y0), (x1, y1) = marks[leg], marks[leg + 1]
        offset = travelled - leg
        points.append(
            clear_of_controls((round(x0 + (x1 - x0) * offset), round(y0 + (y1 - y0) * offset)))
        )
    return points


def push_out(
    point: tuple[int, int], steps: int, centre: tuple[int, int] = SCREEN_CENTRE
) -> tuple[int, int]:
    """Move a drop further from the village, kept on the map, the screen and off the UI.

    The map is a diamond, so clamping to the playfield rectangle alone pushed
    drops into corners that are not on the map at all: a hero refused four times
    ended up at (30, 175), where the map only spans x 596 to 1004, and the game
    had nothing to accept. `DEPLOY_BOUND.clamp` is what keeps a push heading
    outwards from leaving the board.

    `centre` is where the village is sitting, which is the screen middle until
    the camera is dragged to free up a flank hidden behind the card row.
    """
    dx, dy = point[0] - centre[0], point[1] - centre[1]
    span = max((dx * dx + dy * dy) ** 0.5, 1.0)
    return clear_of_controls(
        DEPLOY_BOUND.model_copy(update={"centre": centre}).clamp((
            round(point[0] + dx / span * PUSH_STEP * steps),
            round(point[1] + dy / span * PUSH_STEP * steps),
        ))
    )


def push_line(
    anchors: tuple[tuple[int, int], ...], steps: int, centre: tuple[int, int] = SCREEN_CENTRE
) -> list[tuple[int, int]]:
    """Every anchor of a flank moved the same distance further from the village."""
    return [push_out(anchor, steps, centre) for anchor in anchors]


def planned_line(plan: AttackPlan | None) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The line a plan drew, or None when it is not one the loop can work with.

    A line whose midpoint sits on the village is the failure case: every probe
    along it is refused and `push_out` has no room to move it, so the caller
    needs to know to use a named flank instead rather than lose the army.
    """
    if plan is None or plan.deploy_start is None or plan.deploy_end is None:
        return None
    start, end = plan.deploy_start.pixels(), plan.deploy_end.pixels()
    dx = (start[0] + end[0]) / 2 - SCREEN_CENTRE[0]
    dy = (start[1] + end[1]) / 2 - SCREEN_CENTRE[1]
    radius = (dx * dx + dy * dy) ** 0.5
    if radius < MIN_LINE_RADIUS:
        logger.info(
            "The planned line runs %.0f px from the middle, so it crosses the village; "
            "falling back to the %s flank",
            radius,
            plan.deploy_from,
        )
        return None
    return start, end


def deploy_candidates(plan: AttackPlan | None) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Every line worth trying, best first: what the plan drew, its flank, then the rest.

    One flank is not enough. `push_out` runs out of room on a village whose
    boundary reaches the screen edge, because the presets already hug the
    playfield, and giving up there spends the whole battle with the army still
    in the cards. A village only ever blocks the sides it grew towards, so the
    remaining flanks are what turns that into a fought battle.
    """
    sides = sorted(DEPLOY_LINES, key=lambda side: side != (plan.deploy_from if plan else ""))
    line = planned_line(plan)
    return ([line] if line else []) + [DEPLOY_LINES[side] for side in sides]


def single_spots(
    line: list[tuple[int, int]], centre: tuple[int, int] = SCREEN_CENTRE
) -> list[tuple[int, int]]:
    """Where to try a one-off drop, best first: the middle of the line, then out, then along.

    `push_out` stops moving a point once it reaches the edge of `DEPLOY_BOUND`,
    because everything past that is clamped back onto the same place. Measured
    live, a siege machine refused at (266, 199) was pushed four more times and
    came back to within two pixels of itself every time, so it spent nine seconds
    finding out what the first attempt had already said and the unit was lost.

    Whatever the pushes cannot reach is tried along the line instead, which is
    ground the troops are about to be spread over anyway.
    """
    middle = len(line) // 2
    spots: list[tuple[int, int]] = []
    for spot in (
        *(push_out(line[middle], step, centre) for step in range(DEPLOY_ATTEMPTS)),
        *(line[(middle + offset) % len(line)] for offset in (2, -2, 4, -4)),
    ):
        if all(((spot[0] - x) ** 2 + (spot[1] - y) ** 2) ** 0.5 >= SPOT_APART for x, y in spots):
            spots.append(spot)
    return spots[:DEPLOY_ATTEMPTS]


def spaced(points: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """The points, minus every one landing inside a bottle already placed.

    `prompts/attack_plan.md` gives the planner the footprint and tells it two
    rages must not overlap, and the planner does not comply: measured on one
    five-point reply, four of its ten pairs sat inside one another and only two
    points came through here. The board is isometric, which is what makes the
    spacing impossible to eyeball — the closest of those four, (768, 495) against
    (800, 378), is 121 px apart, clear of the ellipse's 240 px axis and just
    inside its 120 px one. That is not a wording problem, since a model reading a
    screenshot cannot measure the distance between two points it has itself just
    invented, so the geometry is settled here instead of asked for.

    It is worth settling because an overlapping bottle is a whole spell's worth
    of nothing: rage does not stack, so the second one over the same ground buys
    the cargo one footprint of effect rather than two. Whatever this drops is
    made back up from RAGE_PATH by the caller, which is spaced on this same
    pitch by construction.
    """
    kept: list[tuple[int, int]] = []
    for x, y in points:
        if all(
            ((x - px) / RAGE_SPAN[0]) ** 2 + ((y - py) / RAGE_SPAN[1]) ** 2 >= 1 for px, py in kept
        ):
            kept.append((x, y))
    return kept


def drop_points(
    line: list[tuple[int, int]], seed: int, taps: int = DROPS_PER_PASS
) -> list[tuple[int, int]]:
    """One card's worth of drops, spread over the whole line rather than bunched.

    `seed` rotates the starting point per card and per pass, so a card holding
    a single troop does not put it on the same spot every other card started on.

    `taps` is what the card actually holds where its corner can be read, and the
    full pass where it cannot; see `_spread_troops`.

    A pass covers the whole line either way; what `DROP_STRIDE` decides is the
    order, and the order is what a card holding fewer troops than the pass has
    taps gets judged on — it runs out partway through, so the ones it did put
    down are wherever the first few taps went.

    **A pass is longer than the line, so its later circuits land between the
    points of the first rather than back on them.** The stride cannot help with
    that: it decides the order within a circuit, and circuit two walks the same
    twelve points in the same order as circuit one. Without the offset a card of
    sixteen would stack its last four on the exact pixels its first four went
    to — which is what the stride exists to prevent, applied to a card big
    enough to come round again.
    """
    spots: list[tuple[int, int]] = []
    for i in range(taps):
        at = (seed + i * DROP_STRIDE) % len(line)
        x, y = line[at]
        # How far between this point and its neighbour, by which circuit this
        # is: the second lands halfway along, a third a third of the way.
        #
        # The neighbour is the previous point at the far end, because the line
        # is a segment and not a ring: wrapping to `line[0]` there aims the
        # offset at the other end of the flank, which put one drop 229 px away
        # from the point it was meant to sit beside — five gaps, and outside the
        # boundary the line was fitted to.
        laps = i // len(line)
        if laps:
            nx, ny = line[at + 1] if at + 1 < len(line) else line[at - 1]
            x += round((nx - x) / (laps + 1))
            y += round((ny - y) / (laps + 1))
        spots.append((x, y))
    return spots


PLAN_PROMPT = PROMPTS["attack_plan"]


class AttackRunner(BaseModel):
    """Drives one attack from the home village and back.

    Screen reading never involves Gemini; only the tactical choice does, and only
    once an opponent has already passed the loot thresholds, so a skipped
    opponent costs nothing. Without `ai`, or if the call fails, the fixed flank
    and spell grid are used instead.
    """

    adb: AdbController
    display: DisplayTarget
    thresholds: LootThresholds
    # Read once per run off the home village, before the search fee is charged.
    stock: StockLimits = StockLimits()
    max_skips: int = 20
    ai: GeminiClient | None = None
    # A plan settled before the run, which skips the Gemini call entirely. This is
    # what `--plan-in` fills, and it is how a hand-written tactic is replayed
    # exactly: the loop plays what it is given rather than asking for its own.
    plan: AttackPlan | None = None
    # Checked between opponents only. A battle already under way is played out:
    # abandoning one mid-deploy would leave the army on the field and the game
    # on a screen the next run does not know how to get home from.
    should_stop: Callable[[], bool] = lambda: False
    # Where to keep every frame the loop reads, for a run being studied afterwards.
    frame_dir: Path | None = None

    _captures: int = PrivateAttr(default=0)
    _seen: LootOffer | None = PrivateAttr(default=None)
    # Whether the last frame `_scout` gave up on still had 下一個 on it, which
    # decides how the round is left: 結束戰鬥 is only on the screen while that
    # button is, and walking out of 正在搜尋對手 instead would pay for a battle
    # nothing is deployed in.
    _offered: bool = PrivateAttr(default=False)
    # Opponents `_scout` swapped out because their loot would not read. They are
    # skips like any other — a search fee each — so they belong in the report
    # rather than only in the log, which is the number a run is judged on.
    _swapped: int = PrivateAttr(default=0)
    # Whatever `_plan` settled on, kept so a run can be written down and replayed.
    _played: AttackPlan | None = PrivateAttr(default=None)
    # How far the village has moved on screen since the attack opened, which is
    # only ever the deliberate drag that frees up a flank. Every coordinate the
    # loop holds — the preset flanks, the plan's line and its spell points — is
    # drawn for a village in the middle of the screen, so once the camera moves
    # they are all read through this.
    _panned: tuple[int, int] = PrivateAttr(default=(0, 0))

    @property
    def played(self) -> AttackPlan | None:
        """The plan this run actually used, once one has been settled on."""
        return self._played

    @property
    def _middle(self) -> tuple[int, int]:
        """Where the village is sitting now, which is what a drop is pushed away from."""
        return (SCREEN_CENTRE[0] + self._panned[0], SCREEN_CENTRE[1] + self._panned[1])

    def _onscreen(self, points: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
        """Points drawn for a centred village, read against wherever the camera is now."""
        return tuple((x + self._panned[0], y + self._panned[1]) for x, y in points)

    def _spots(self, orders: list[HeroOrder], cards: list[int]) -> dict[int, tuple[int, int]]:
        """Where the plan wants each of these cards dropped, keyed by card.

        Left to right, one order per card, so a list shorter than the row simply
        leaves the cards past its end to the shared spot `_drop_singles` falls
        back to.

        Every point goes through the same corrections the drop line does: the
        camera may have been dragged clear of the card row since the plan drew
        this against a centred village, and `push_out` at zero steps is what
        pulls it onto the map without otherwise moving it. That last one matters
        because the map is a diamond while the prompt hands the planner a
        rectangle — `x_pct` 2 to 98 by `y_pct` 12 to 77 — whose corners are off
        the board entirely. `clear_of_controls` alone holds a point inside that
        rectangle and no further, which is how a hero refused four times ended
        up at (30, 175) with the map spanning x 596 to 1004.
        """
        return {
            card: push_out(point, 0, self._middle)
            for card, point in zip(
                cards, self._onscreen(tuple(order.drop.pixels() for order in orders)), strict=False
            )
        }

    def _tap(self, point: tuple[int, int]) -> None:
        self.adb.tap(point[0], point[1], self.display)

    def _frame(self, label: str) -> bytes:
        """One capture, kept on disk when the run is being recorded.

        Every screenshot the loop reads comes through here, so a recorded run is
        the whole battle in the order the loop saw it, each frame named for what
        it was being asked. Afterwards that is the only thing separating a frame
        the parser misread from a tap that never landed.
        """
        png = self.adb.screenshot(self.display)
        if self.frame_dir is not None:
            self._captures += 1
            (self.frame_dir / f"{self._captures:04d}_{label}.png").write_bytes(png)
        return png

    def _battle_ended(self, label: str) -> bool:
        """Whether the result screen is up, and the loot on the way past.

        A loot panel that will not read is not a battle that has ended, and this
        used to treat the two as one thing. Measured on a live battle sitting at
        69% with two stars and every rage still in its card, the gold row of
        485 715 resolved on no frame at all — the battlefield shows through the
        panel, its 48 arrives as a single 32 px span, and no cut through that
        reads as two digits — so the wait declared the battle over, left through
        a 回營 button that was not there, and the next round opened onto a battle
        still being fought. Five rounds of one recorded run went that way, each
        standing down with 畫面不在主村 while the battle behind it played itself
        out unattended.

        The green 回營 button needs no digits, which is the whole reason to ask
        it instead. The loot is still read on the way past, because whether
        anything was ever deployed is judged on the last reading anyone took —
        including the ones the abilities took, on a battle short enough that this
        poll never saw the panel at all.
        """
        png = self._frame(label)
        view = read_scout(png)
        if view is not None:
            self._seen = view.loot
        return battle_over(png)

    def _open_attack_menu(self) -> bytes | None:
        """Get to the attack menu, clearing whatever is covering the village.

        The home village frame comes back with it. That is the one screen the
        storage bars are on, and the frame is already being taken here to check
        for the idle dialog, so reading the storages costs no extra capture.
        """
        for _ in range(HOME_ATTEMPTS):
            home = self._frame("home")
            if idle_disconnected(home):
                logger.info("Idle-disconnect dialog is up; restarting the game")
                self.display = restart_game(self.adb, self.display)
                continue
            if battle_over(home):
                logger.info("The last battle's result screen is still up; leaving it")
                self._leave_result()
                continue
            # The game reopens on whichever village it was closed on, and this
            # loop is the home village's. Without this it fails safe but
            # expensively and says the wrong thing: 攻擊 in the corner opens the
            # builder base's own dialog, `attack_menu_open` does not recognise
            # it, and the run spends every attempt here before reporting
            # 畫面不在主村 — which reads as a game that is stuck rather than one
            # that is simply in the other village.
            if current_world(home) == "night":
                logger.warning("The game is on the builder base; sailing home before attacking")
                cross(self.adb, self.display, "day")
                continue
            self._tap(HOME_ATTACK)
            time.sleep(2)
            if attack_menu_open(self._frame("attack-menu")):
                return home
            # Never `back` here: on the home village that is 確定退出遊戲嗎, one
            # tap away from closing the game. A building panel left open does
            # not cover the 攻擊 button in the corner anyway, so the tap above
            # only needs the panel to swallow one press and then retries.
            time.sleep(HOME_RETRY_DELAY)
        return None

    def _leave_result(self) -> None:
        """Tap 回營 until the result screen has actually gone.

        One tap was not enough and cost four runs in a row. The loot panel
        vanishes as the result screen starts animating in, so the tap fired the
        moment `_battle_view` reads nothing lands before the button is alive; the
        village then stayed covered and every following run stood down with
        畫面不在主村 without ever attacking.
        """
        for _ in range(RESULT_ATTEMPTS):
            if not battle_over(self._frame("result")):
                return
            self._tap(RETURN_HOME)
            time.sleep(RESULT_RETRY_DELAY)
        logger.warning("The result screen will not close; the next run has nowhere to start")

    def _plan(self, frame: bytes, rage_count: int, freeze_count: int) -> AttackPlan | None:
        """The plan for this opponent: the one handed in, the AI's, or the flat default.

        Falling back to a written-out plan rather than to constants is what makes
        the default readable and editable, and it is the same tactic the loop
        used to hold in `DEPLOY_LINES` and `RAGE_PATH`.
        """
        if self.plan is not None:
            logger.info("Playing the plan handed in: %s", self.plan.reason or "no reason given")
            self._played = self.plan
            return self.plan
        if self.ai is None:
            self._played = plans.flat()
            return self._played
        try:
            plan = self.ai.generate_structured(
                PLAN_PROMPT.format(rage_count=rage_count, freeze_count=freeze_count),
                AttackPlan,
                frame,
                PLAN_TIMEOUT,
            )
        except Exception:
            logger.warning("Attack planning failed; falling back to the flat plan", exc_info=True)
            self._played = plans.flat()
            return self._played
        # **Its timings are kept, and they used to be thrown away.** The reason
        # given was that a still frame says nothing about how long this machine
        # takes to put an army down — true, and beside the point, because what
        # the numbers are really about is how long the army takes to *walk*.
        # That is a property of the village on the frame: how far the drop line
        # is from the first wall, how deep the defences sit, whether the push
        # has to cross open ground. The table they fell back to was a set of
        # constants somebody chose without seeing any village at all, so the
        # exchange was a guess made with the layout in hand for one made
        # without it.
        self._played = plan
        logger.info(
            "Plan: from %s, line %s to %s, %d rage point(s) at %ds, %d freeze point(s) at %ds, "
            "heroes=%s (%s)",
            plan.deploy_from,
            plan.deploy_start,
            plan.deploy_end,
            len(plan.rage_points),
            plan.rage_after,
            len(plan.freeze_points),
            plan.freeze_after,
            [(order.kind, order.ability_after) for order in plan.heroes],
            plan.reason,
        )
        return plan

    def _scout(self, timeout: float = 30) -> tuple[ScoutView, bytes] | None:
        """Poll until an opponent is on screen; 正在搜尋對手 reads as nothing at all.

        The frame comes back with the view because `card_groups` only holds on a
        full card row, and this is the last moment one is guaranteed.

        **An opponent whose loot will not read is not an empty search**, and
        waiting on one is the expensive way to find that out: the countdown is
        running the whole time, and when it ends the game starts the battle
        whether anything was deployed or not. `skip_offered` is what tells the
        two apart, since the 下一個 button needs none of the digits, and the
        answer to the second is to ask for another opponent rather than to stand
        there. Each skip leaves the rest of the window to the one that follows.

        Running out of window is a different screen from the one this used to
        give up on, and `_offered` is what says so. The timeout used to land on a
        countdown that had already expired; now it can land on an opponent that
        arrived seconds ago with most of its own still to run, so the caller has
        to leave through 結束戰鬥 rather than walk away — which is only safe
        while 下一個 is up, and that is exactly what this last read answers.
        """
        deadline = time.monotonic() + timeout
        unread = 0
        while time.monotonic() < deadline:
            png = self._frame("scout")
            view = read_scout(png)
            if view:
                return view, png
            self._offered = skip_offered(png)
            unread = unread + 1 if self._offered else 0
            if unread >= UNREADABLE_SKIPS:
                logger.warning(
                    "An opponent is on screen but %d frames running would not read its loot; "
                    "asking for another rather than letting the countdown start the battle",
                    unread,
                )
                self._tap(NEXT_TARGET)
                self._swapped += 1
                unread = 0
            time.sleep(1)
        return None

    def _usable_line(self, troops: list[int], anchors: tuple[tuple[int, int], ...]) -> int | None:
        """How far out the flank has to be pushed before the game accepts drops on it.

        Troops are dropped as probes, because a village whose boundary reaches
        past the line swallows them. Both ends go in as well as the middle: a
        flank can sit outside the boundary at its centre and inside it at the
        tips, which would silently lose every troop aimed there. A probe that
        takes nothing pushes the line further out; None means even the playfield
        edge was inside the boundary.

        The card is what says whether the probe landed, and which card is picked
        fresh each time: three troops a probe over five pushes and four flanks is
        more than a small army holds, and a card that has run dry reads exactly
        like a flank the village has grown over. Reading the game's red warning
        instead never worked either — the tap is as often swallowed without one,
        so this returned 0 on the first try every time and the flank was never
        actually tested.

        The step count is what comes back rather than the line, because everyone
        downstream keeps pushing from it and a line cannot say how far out it
        already is.
        """
        # Nothing happens between attempts, so the frame that judged the last one
        # is the one to judge the next against. A whole capture per attempt is
        # worth saving here: a village that refuses every flank spends twenty of
        # them, and the battle only runs three minutes.
        shot = self._frame("before-probe")
        for attempt in range(DEPLOY_ATTEMPTS):
            card = next(iter(live_cards(shot, troops)), None)
            if card is None:
                logger.warning("Every troop card is spent before a flank was settled")
                return None
            line = deploy_line(LINE_POINTS, *push_line(anchors, attempt, self._middle))
            probes = [line[0], line[len(line) // 2], line[-1]]
            self.adb.tap_many([(card, CARD_ROW_Y), *probes], self.display)
            time.sleep(DROP_SETTLE)
            before, shot = shot, self._frame("probe")
            if card_drained(before, shot, [card]):
                logger.info("Deploying along %s, pushed out %d step(s)", anchors, attempt)
                return attempt
            logger.info("Nothing landed along %s; pushing the flank out", anchors)
        logger.info("The %s line takes nothing at any push; trying the next flank", anchors)
        return None

    def _spread_troops(
        self, troops: list[int], anchors: tuple[tuple[int, int], ...], pushed: int
    ) -> list[tuple[int, int]]:
        """Empty the troop cards along the flank; returns the line ending up in use.

        Probing the ends is not enough on its own: the village is a diamond, so
        the ground around it is not convex and a line whose ends are both clear
        can still cut through it. A pass that empties nothing at all is a line
        the village has grown over, so the flank is pushed out for the passes
        that follow.

        A pass is judged on the cards, not on the screen. The red warning was
        what drove this before, and it pushed the flank out twice in every one
        of six recorded runs — never for a refusal, always because the last pass
        over-taps a card that has just emptied and the game answers 請選擇其他
        兵種 in the same red as the boundary warning.

        `pushed` carries on from where `_usable_line` left off. Restarting it at
        zero made the first push land back on the line already in use, so a real
        refusal was answered by moving nothing at all.
        """
        remaining = list(troops)
        line = deploy_line(LINE_POINTS, *push_line(anchors, pushed, self._middle))
        shot = self._frame("before-pass")
        for index in range(DEPLOY_PASSES):
            # **A counted card says how many taps it wants, in its own corner.**
            # Tapping that many plus one empties it, where a fixed pass spends
            # whatever is left of its circuits on ground with nothing selected:
            # measured on a row of x9, x3 and x2 against a 24-tap pass, 58 of
            # its 72 drops land on nothing, and reading the two that will read
            # takes the row from 75 taps to 34 — about 2.5 s of a ten-second
            # deployment.
            #
            # The reading comes off the newest frame there is, so a second pass
            # asks what the first one left rather than what the card started
            # with. `None` is a corner the artwork swallowed — measured, the
            # pale card of those three merged its `x` into the background and
            # came back with one 25 px span matching nothing — and that falls
            # back to the full pass, which is what the fixed count was for.
            #
            # **The pass is still the ceiling, because this reader fails high.**
            # A four-pixel sliver of card art past the last digit matches a `1`
            # inside tolerance on some frames and not others: across three
            # committed fixtures the same x12 card reads 12, None and 121. Taken
            # at face value the last of those is 122 drops in one burst, about
            # 7.6 s, more than double what reading the count saves and with
            # every hero waiting behind it. Clamped, a genuinely larger card
            # loses nothing — it takes another pass, exactly as it used to.
            held = {x: card_count(shot, x) for x in remaining}
            for card, x in enumerate(remaining):
                count = held[x]
                drops = drop_points(
                    line,
                    index * len(remaining) + card,
                    min(count + 1, DROPS_PER_PASS) if count else DROPS_PER_PASS,
                )
                self.adb.tap_many([(x, CARD_ROW_Y), *drops], self.display)
            time.sleep(DROP_SETTLE)
            before, shot = shot, self._frame("pass")
            if not card_drained(before, shot, remaining) and pushed + 1 < DEPLOY_ATTEMPTS:
                pushed += 1
                logger.info("The whole pass landed nothing; flank pushed out to %d", pushed)
                line = deploy_line(LINE_POINTS, *push_line(anchors, pushed, self._middle))
            remaining = live_cards(shot, remaining)
            logger.info("%d troop card(s) still hold something", len(remaining))
            if not remaining:
                break
        return line

    def _landed(
        self, before: bytes, after: bytes, cards: list[int]
    ) -> tuple[list[int], list[int]]:
        """Which of these one-off cards landed, and which of those drew a health bar.

        The card is the evidence and the screen is not. A hero keeps its card
        once it is down — the card becomes the ability button — so what says it
        landed is the health bar the game draws over it; a card that greys out
        instead is what the second test is for.

        This used to answer the red warning banner, which cannot say it: a tap
        inside the boundary is as often swallowed in silence, so every hero came
        back "landed" whether it went down or not. One recorded run reported
        four of four while the fourth never left its card.

        **The bar says a unit is on the field, not that the unit is a hero.**
        It was read as the second for a while, because a leading card is a hero
        on any army carrying no siege machine and nothing else on the row tells
        the two apart — neither shows an `xN`, and both sit in the same group.
        Measured on a recorded run, the game draws a bar over a siege machine
        too, so the caller counts the plan's own heroes instead; see `_deploy`.
        """
        on_field = field_units(after, cards)
        # Both readings are taken once. Asking `live_cards` inside the
        # comprehension decoded the whole screenshot again for every card it
        # walked, which on a row of four heroes is four PNG decodes nobody wanted.
        was_live, still_live = live_cards(before, cards), live_cards(after, cards)
        emptied = [card for card in was_live if card not in still_live]
        return [card for card in cards if card in on_field or card in emptied], on_field

    def _drop_singles(
        self,
        cards: list[int],
        line: list[tuple[int, int]],
        what: str,
        wanted: dict[int, tuple[int, int]] | None = None,
    ) -> tuple[list[int], list[int]]:
        """Every one-off card onto its own spot at once, retried as a group where refused.

        Hands back what landed and, of that, which cards the game drew a health
        bar over; `_landed` is where both are read.

        They used to go down one at a time, each paying a capture to frame the
        drop, a settle and another capture to judge it — three and a half seconds
        a card, so four heroes spent seventeen seconds arriving while the troops
        they were meant to follow were already under fire. Measured live, one
        burst carrying the siege machine and all four heroes put every one of
        them on the field, so the whole row goes in one shell round-trip and only
        the cards the game did not take are offered another spot.

        **`wanted` is what lets heroes do different jobs in one attack.** They
        all used to go on the same spot, which can only express "everyone
        follows the troops" — where a village usually wants one or two walking
        the outside to clear the stray buildings that pull an army off course
        and the rest going in behind the push. It goes **ahead of** the shared
        ladder rather than replacing its first rung: a point the game refused
        once is not worth insisting on, but the rung it would have displaced is
        the midpoint `_usable_line` has already probed and proved the game
        accepts, which is the one spot with evidence behind it. Overwriting it
        cost a named hero both that spot and one of its retries.

        Getting the verdict wrong is not free, which is what the settle is for: a
        second tap on a hero already on the field is its ability, so a drop
        wrongly called refused burns the cloak or the tome and leaves the
        schedule tapping a card that has nothing left to give.
        """
        landed: list[int] = []
        onfield: list[int] = []
        pending = list(cards)
        shared = single_spots(line, self._middle)
        # Where every card goes on each attempt in turn. A card the plan did not
        # name falls straight through to the shared spot of that round.
        rounds = [dict.fromkeys(cards, spot) for spot in shared]
        if wanted:
            rounds.insert(0, {card: wanted.get(card, shared[0]) for card in cards})
        # Where the last card actually went, which is not the spot the loop
        # happens to be holding when it stops: it breaks at the top of the next
        # iteration, so by then it has already moved past the one that worked.
        worked = line[len(line) // 2]
        for aim in rounds:
            if not pending:
                break
            before = self._frame("before-drop")
            self.adb.tap_many(
                [tap for card in pending for tap in ((card, CARD_ROW_Y), aim[card])],
                self.display,
                gap=SINGLE_DROP_DELAY,
            )
            time.sleep(HERO_SETTLE)
            down, bars = self._landed(before, self._frame("dropped"), pending)
            landed += down
            onfield += bars
            pending = [card for card in pending if card not in down]
            if down:
                # One of the spots that worked rather than the shared one, since
                # on the first attempt each card may have gone somewhere of its
                # own. It is a sample for the log, not the whole answer.
                worked = aim[down[0]]
            if pending:
                # Each card's own spot rather than one shared name for them all:
                # on the round the plan aimed, they went to different places, and
                # a recorded run read afterwards would otherwise point at a
                # coordinate the card was never sent to.
                logger.info(
                    "%d %s card(s) took nothing: %s",
                    len(pending),
                    what,
                    ", ".join(f"{card} at {aim[card]}" for card in pending),
                )
        for card in pending:
            logger.warning("The %s card at %d never landed; its unit stays put", what, card)
        logger.info("%d of %d %s card(s) landed at %s", len(landed), len(cards), what, worked)
        return landed, onfield

    def _wait_for_battle(self) -> bytes | None:
        """Hold until the scout countdown ends, and hand back the first battle frame.

        The 下一個 button going away is the countdown ending, which is what
        `can_skip` reads. The frame comes back because the boundary is only drawn
        once the battle is under way, so this is the first moment it can be read
        and the caller would otherwise have to pay for another capture.

        A panel that stops reading ends the wait rather than extending it. It is
        the countdown *still running* that is worth waiting out; an unreadable
        screen says nothing either way, and sitting on it would spend a quarter
        of the battle to learn nothing, where deploying at least might land.
        """
        unreadable = 0
        for _ in range(COUNTDOWN_ATTEMPTS):
            png = self._frame("waiting")
            view = read_scout(png)
            if view is not None and not view.can_skip:
                return png
            unreadable = 0 if view else unreadable + 1
            if unreadable >= UNREADABLE_ATTEMPTS:
                logger.warning("The loot panel will not read; deploying without waiting further")
                return None
            time.sleep(1)
        logger.warning("The scout countdown never ended; deploying without a battle frame")
        return None

    def _pan(self, frame: bytes, drift: tuple[int, int]) -> bytes:
        """Drag the battle camera, and write down how far it really went.

        Every coordinate the loop holds is drawn for a village in the middle of
        the screen, so a camera that moves has to be recorded or all of them
        point somewhere else. `_panned` is that record and `_onscreen` is how
        everything reads it.
        """
        self.adb.swipe(
            CAMERA_GRIP,
            clear_of_controls((CAMERA_GRIP[0] + drift[0], CAMERA_GRIP[1] + drift[1])),
            CAMERA_DRAG_MS,
            self.display,
        )
        time.sleep(CAMERA_SETTLE)
        moved = self._frame("camera")
        shift = view_shift(frame, moved, drift)
        self._panned = (self._panned[0] + shift[0], self._panned[1] + shift[1])
        logger.info(
            "Dragged the camera by %s; the village moved %s, now %s off where the battle opened",
            drift,
            shift,
            self._panned,
        )
        return moved

    def _settle_zoom(self, frame: bytes) -> bytes:
        """Put the camera back at the far limit, and say whether it had left.

        **Asked for rather than checked**, because there is nothing to check
        against: the game reports no zoom level, and every attempt to read one
        off a frame here was defeated by something else moving in it — the
        deployment boundary shrinks as buildings fall, the village ground gets
        covered by whatever panel the game feels like opening. What is known is
        that zooming out past the far limit does nothing at all, so the way to
        be there is to ask, every round, without knowing where you were.

        The report afterwards is the other half, and it goes by **the height of
        the deployment boundary before the pinch**. That is the one measurement
        of this that survived contact with a live run:

        - **Not the width.** Every round faces a different opponent, and their
          villages are their own widths — swept over one day the widths ran 909
          to 1560 while nothing was wrong.
        - **Not the frame's byte length.** Tried first, and it cried wolf on its
          second live round: 30 KB apart with the village measuring 549 and 559,
          which is two seconds of animation rather than a changed camera.
        - **The height.** A village too big for the screen is clipped top and
          bottom, so it reads short whatever the opponent's layout. Every
          healthy round of that day fell in 500 to 572; the one round that
          deployed nothing came back 411.

        An unreadable frame reports nothing rather than guessing. The pinch has
        already happened by then, so the correction never depends on it.
        """
        before = village_box(frame)
        self.adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, self.display)
        if before is not None and before[3] - before[1] < ZOOM_CLIPPED:
            logger.warning(
                "The camera was not at the far zoom: the village measured %d tall before the "
                "pinch, against %d for one that fits on screen",
                before[3] - before[1],
                ZOOM_CLIPPED,
            )
        return self._frame("zoomed")

    def _settle_camera(self, frame: bytes) -> bytes:
        """Put the village in the middle of the screen, and hand back what it looks like.

        Everything downstream reads the camera without being able to check it:
        `push_out` moves a drop away from the village, the preset flanks are
        screen coordinates, and the spell grid is spaced off the middle. The game
        does open every attack centred — measured across nine battles it was never
        more than 35 px out — but that is the sort of fact that is true until it
        is not, and a camera left anywhere else puts the whole army somewhere
        nobody asked for. This runs before the plan is drawn so the planner is
        looking at the same screen the drops will land on, which is also what
        keeps `_panned` to one meaning: the flanks and the plan are both drawn
        against a centred village, so one offset moves both.
        """
        for _ in range(CAMERA_ATTEMPTS):
            box = village_box(frame)
            if box is None:
                logger.info("The village will not measure; the camera is left where it is")
                return frame
            middle = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            drift = (SCREEN_CENTRE[0] - middle[0], SCREEN_CENTRE[1] - middle[1])
            if max(abs(drift[0]), abs(drift[1])) <= CAMERA_TOLERANCE:
                # The span goes in the log as well as the middle. Nothing here
                # can tell a zoomed camera from a village that is simply bigger,
                # because both make the box wider — but a run of battles whose
                # spans all move together is what a changed zoom would look like,
                # and that is only visible if the number was written down.
                logger.info(
                    "Village sits at %s, %s off the middle, spanning %dx%d",
                    middle,
                    drift,
                    box[2] - box[0],
                    box[3] - box[1],
                )
                return frame
            logger.info("Village sits at %s; dragging the camera by %s", middle, drift)
            # Not `_pan`: this drag ends with the village back in the middle, so
            # there is nothing for `_panned` to record. Recording it would send
            # every preset flank off by the drag, since a preset is drawn for a
            # centred village and the village is centred again by the time this
            # returns.
            self.adb.swipe(
                CAMERA_GRIP,
                clear_of_controls((CAMERA_GRIP[0] + drift[0], CAMERA_GRIP[1] + drift[1])),
                CAMERA_DRAG_MS,
                self.display,
            )
            time.sleep(CAMERA_SETTLE)
            frame = self._frame("camera")
        return frame

    def _clear_flank(
        self, frame: bytes | None, preset: tuple[tuple[int, int], ...]
    ) -> bytes | None:
        """Drag the village clear of the card row so this flank has ground to drop on.

        Which way comes from the flank about to be tried rather than from the
        plan's own `deploy_from`, because the loop falls through to other flanks
        when one is refused and the camera has to follow whichever is really
        being used.

        How much comes from where the village's own red line already reaches, so
        a village small enough to leave the flank room is left alone: dragging a
        camera that is fine only takes the room off the other side.
        """
        if frame is None:
            return None
        box = village_box(frame)
        if box is None:
            logger.info("The village will not measure; the camera stays where it is")
            return frame
        below = sum(y for _, y in preset) / len(preset) > SCREEN_CENTRE[1]
        wanted = (
            min(0, PLAYFIELD[3] - box[3] - FLANK_ROOM)
            if below
            else max(0, PLAYFIELD[1] - box[1] + FLANK_ROOM)
        )
        if not wanted:
            return frame
        logger.info("The %s flank has %d px too little ground", preset, abs(wanted))
        return self._pan(frame, (0, wanted))

    def _deploy(self, frame: bytes) -> None:
        """Spread the main troops along one flank; everything else drops once, mid-line."""
        # Zoom before centring, and not the other way round: centring measures
        # the village against the screen, so it has to be looking at the view
        # every one of those numbers was taken at.
        frame = self._settle_camera(self._settle_zoom(frame))
        groups = card_groups(frame)
        if not groups:
            logger.warning("No cards found on the battle row; nothing to deploy")
            return
        # The first group is the troops the attack is built on. Of what follows,
        # spells are held back for the village itself; `xN` is what separates
        # them, since spells carry a count and heroes and the siege machine do
        # not. The game then always orders what is left as siege machine first
        # and heroes after, so the leader is simply the first of them. Taking it
        # from the group boundary instead put four heroes in the vanguard: they
        # went down ahead of the troops with nothing covering them, and since
        # only the followers are scheduled, not one of their abilities was ever
        # fired.
        troops = groups[0]
        rest = [x for group in groups[1:] for x in group]
        spells = counted_cards(frame, rest)
        singles = [x for x in rest if x not in spells]
        vanguard = singles[:1]
        followers = singles[1:]
        logger.info(
            "%d troop card(s), %d leading, %d following, %d spell(s)",
            len(troops),
            len(vanguard),
            len(followers),
            len(spells),
        )
        freezes = freeze_cards(frame, spells)
        rages = [x for x in spells if x not in freezes]
        rage_count = sum(card_count(frame, x) or len(RAGE_PATH) for x in rages)
        # One apiece where the count is unreadable: there is no fixed freeze grid
        # to fall back on the way rage has RAGE_PATH, and asking for eight points
        # for a single bottle would only spend the battle tapping empty ground.
        freeze_count = sum(card_count(frame, x) or 1 for x in freezes)
        plan = self._plan(frame, rage_count, freeze_count)
        # Nothing can be placed while the scout countdown is still running, and a
        # tap the game ignores raises nothing at all, so probing then drains no
        # card and every flank in turn reads as one the village has grown over.
        # With Gemini in the loop the planning call happens to outlast the
        # countdown, which is what has been hiding this; without a key `_plan`
        # returns at once and the run would probe into the countdown every time.
        battle = self._wait_for_battle()
        for preset in deploy_candidates(plan):
            # The camera moves first, because a lower flank on a village that
            # fills the playfield has nothing to drop on until it does, and
            # everything after this reads its coordinates through where it ended.
            battle = self._clear_flank(battle, preset)
            flank = self._onscreen(preset)
            # The boundary the game draws beats a flank drawn for a village that
            # does not exist, and fitting to it is what saves probing outwards one
            # refused troop at a time. It is still probed once before it is used.
            anchors = (
                fitted_line(battle, flank[0], flank[1], centre=self._middle) if battle else None
            ) or flank
            pushed = self._usable_line(troops, anchors)
            if pushed is not None:
                break
        else:
            logger.warning("Every flank was refused; the boundary reaches past the playfield")
            return
        line = deploy_line(LINE_POINTS, *push_line(anchors, pushed, self._middle))
        planned = self._onscreen(
            tuple(point.pixels() for point in plan.rage_points) if plan else ()
        )
        # A plan can name fewer spots than the army carries rages, and `_cast`
        # cycles back over its targets — which would stack two rages on one spot
        # and waste one. The fixed grid fills the tail so each gets its own, and
        # `spaced` is what makes "its own" true: the planner's points overlap
        # each other as often as not, and two bottles on one footprint are one
        # bottle's worth of effect.
        #
        # Cut to the bottles actually carried, because the tail is otherwise
        # dead weight that the aiming below would average into its idea of where
        # the plan was pointing. The one tap of slack `_cast` adds then wraps
        # back onto the first spot rather than spending a real bottle on a
        # fallback point nobody chose.
        rage_path = tuple(spaced(planned + self._onscreen(RAGE_PATH)))[: max(rage_count, 1)]
        # Every freeze used to stack on one spot, which is one spell's worth of
        # effect for the whole cargo. A plan names one per bottle instead.
        freeze_targets = self._onscreen(
            tuple(point.pixels() for point in plan.freeze_points) if plan else ()
        ) or self._onscreen((FREEZE_TARGET,))
        # The army goes down as fast as the game will take it: siege machine
        # first to open the path, then the troops along the flank, then the
        # heroes straight behind them. Every second one of them spends in its
        # card is a second the ones already out are taking fire alone, which is
        # what a whole minute of arriving used to cost.
        #
        # Both spells wait on the clock instead. Rage used to be cast the moment
        # the troop cards emptied, and that was right while emptying them took
        # half a minute: the troops were at the wall by then. It stops being
        # right once they are all down in five seconds, because a rage lasts 18
        # and would expire on troops still walking. So it goes where the freeze
        # already went, on a delay from the attack opening.
        #
        # Two clocks, because the two kinds of number mean different things. A
        # hero's ability is timed from that hero landing, which is what a queen's
        # cloak is worth. A spell is timed from the attack opening, because that
        # is how both are judged on screen and hanging them off the heroes would
        # move them by however long the army happened to take to go down.
        rage_after = plan.rage_after if plan else FALLBACK_RAGE
        freeze_after = plan.freeze_after if plan else FALLBACK_FREEZE
        opened = time.monotonic()
        pending: Moves = []
        if rages:
            pending.append((
                opened + rage_after,
                f"{len(rages)} rage card(s)",
                partial(self._rage, rages, rage_path, frame),
            ))
        if freezes:
            pending.append((
                opened + freeze_after,
                f"{len(freezes)} freeze card(s)",
                partial(self._cast, freezes, freeze_targets, frame),
            ))
        # Nothing on the clock is allowed to interrupt this. The schedule used to
        # be offered a turn between one card and the next, because putting the
        # army down took about as long as the freeze was meant to wait; now that
        # it takes ten seconds the only battles where a spell comes due mid-
        # deployment are the ones where the deployment is going badly, and those
        # are exactly the battles that need finishing rather than interrupting.
        # Measured live on a flank half inside the boundary: rage fired 21 s in
        # while a troop card was still draining and the heroes landed at 41 s,
        # where finishing first would have had them down at about 31 s.
        named, aimed = self._named_heroes(plan, singles)
        lead_down, led = self._drop_singles(
            vanguard, line, "leading", self._spots(named[:1] if aimed else [], vanguard)
        )
        opener = time.monotonic()
        line = self._spread_troops(troops, anchors, pushed)
        # Only the heroes that actually went down get an ability. A hero still in
        # its card answers an ability tap by deploying instead, with nothing
        # around it and no ability fired. The line is the one the troops ended up
        # on rather than the one they started on: the flank moves while they go
        # down, and a hero sent to the old midpoint is sent somewhere refused.
        down, _ = self._drop_singles(
            followers, line, "hero", self._spots(named[1:] if aimed else named, followers)
        )
        landed = time.monotonic()
        # The same answer the drops above were aimed with, and the health bar
        # only where there was no plan to count.
        leads = aimed if aimed is not None else bool(led)
        row = (vanguard if leads else []) + followers
        # Each hero's ability runs from its own hero landing, which for the
        # leader is a whole troop deployment earlier than for the rest. Only
        # what really went down is in here: an ability tap on a hero still in
        # its card deploys it instead, with nothing around it. A leader refused
        # at every spot still holds its slot above, because the plan named a
        # hero for it either way and dropping it would shift all the rest.
        arrived = dict.fromkeys(lead_down if leads else [], opener) | {
            x: landed for x in followers if x in down
        }
        # A card the plan did not name still gets an ability, on the neutral
        # delay rather than none: a hero that never fires is a hero half spent.
        delays = [order.ability_after for order in named]
        delays += [FALLBACK_ABILITY] * (len(row) - len(delays))
        self._run_schedule(
            opened,
            [
                *pending,
                *(
                    (
                        arrived[x] + delays[i],
                        f"ability on the card at {x}",
                        partial(self._tap, (x, CARD_ROW_Y)),
                    )
                    for i, x in enumerate(row)
                    if x in arrived
                ),
            ],
        )

    def _named_heroes(
        self, plan: AttackPlan | None, singles: list[int]
    ) -> tuple[list[HeroOrder], bool | None]:
        """The plan's hero orders, and whether the first of them holds the leading card.

        Settled **before** anything is dropped, because it is what says where
        each hero goes as well as when it fires. It can be: the arithmetic needs
        only the two counts. The one reading not available this early is the
        health bar, and that is reached only when the plan named nobody — where
        there are no points to place either, so None is the honest answer.
        """
        named = list(plan.heroes) if plan else []
        # **The leading card is a hero on any army that carries no siege
        # machine**, and it is the one card nothing on the row can tell apart:
        # neither it nor a hero shows an `xN`, and the two sit in the same
        # group. So the game's own ordering picks it — siege machine first —
        # and the plan's own count corrects that ordering where it is wrong: as
        # many heroes named as there are one-off cards is an army carrying no
        # siege machine. Without it that hero got no ability at all and every
        # hero after it was read a slot off the card it names, so a queen's
        # cloak went to whoever stood next to her.
        #
        # **The health bar cannot answer this, though it used to be asked
        # first.** The game draws one over a siege machine as readily as over a
        # hero: measured across all three rounds of a recorded run, the 攻城戰車
        # landed and `field_units` read its card as a hero on the very next
        # frame. That put a slot in front of every real hero, so the queen went
        # out on the king's twenty seconds where her cloak wants one. She was
        # the whole cost on that army, whose other three all want about twenty
        # anyway; one carrying a warden or a champion pays on every card.
        # The bar stays as the answer when there is no plan to count, where
        # every card takes the same delay anyway and a tap on a spent siege
        # card costs nothing.
        #
        # **The count is a better bet than the bar, not a sound one.** A plan
        # naming one hero too few against an army carrying no siege machine is
        # the same arithmetic as a plan naming them all against an army that
        # does, so this reads it as the second and the leading hero loses its
        # ability — which is what the bar happened to get right. It is still
        # the better way round: the bar is wrong on every battle this army
        # fights, since it carries a siege machine and the game draws a bar
        # over it, while the count is wrong only when the planner miscounts.
        #
        # A count matching neither arithmetic is past being a bet: the plan
        # cannot be trusted to name the cards in order either. It is said out
        # loud rather than guessed at quietly, because whichever way it falls
        # both the drop points and the ability timings are a slot out from here
        # on and nothing downstream notices.
        if named and len(named) not in (len(singles), len(singles) - 1):
            logger.warning(
                "The plan names %d hero(es) against %d one-off card(s); neither their "
                "drop points nor their ability timings will line up with the row",
                len(named),
                len(singles),
            )
        return named, (len(named) == len(singles) if named else None)

    def _run_schedule(self, opened: float, moves: Moves) -> None:
        """Everything that waits on the clock, in time order, each at its own moment.

        One list rather than the abilities and then the freeze, because ordering
        by position in the code made the freeze wait out the slowest hero on the
        field: with a champion at 45 seconds it landed a minute and a half into a
        three-minute battle, long after the defences it was meant to stop had
        done their work. Ability timing is per hero rather than per card slot for
        the same reason it always was — an upgrading hero has no card, so every
        slot after it shifts.

        Some card slots overlap the result screen's 回營 button, so nothing here
        runs unless the battle is genuinely still on.
        """
        for move in sorted(moves, key=lambda move: move[0]):
            remaining = move[0] - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if not self._play(opened, move):
                return

    def _play(self, opened: float, move: tuple[float, str, Callable[[], None]]) -> bool:
        """One scheduled move, or False once the battle is over and there is no point.

        Some card slots overlap the result screen's 回營 button, so nothing is
        tapped unless the battle is genuinely still on.
        """
        _, what, act = move
        if self._battle_ended("scheduled"):
            logger.info("Battle ended with the %s still to come", what)
            return False
        act()
        logger.info("Played the %s, %.0fs into the attack", what, time.monotonic() - opened)
        return True

    def _rage(self, cards: list[int], targets: tuple[tuple[int, int], ...], frame: bytes) -> None:
        """Cast rage over the army rather than over the ground it started from.

        The plan draws its rage points while the scout screen is still up, which
        is a good half minute before the bottles land, and the army does not
        wait there. Measured over a recorded battle, the fighting moved from
        (610, 305) five seconds in to (934, 456) a minute later, so the points
        are a whole footprint behind by the time they are used — which is what a
        run of frames showed: rage rings sitting on empty grass with the troops
        already at the next wall.

        The plan still decides the *shape*, because the spacing is what keeps two
        bottles from overlapping and the spread is what covers a group that
        arrived along a line. Only where that shape sits comes off the screen.
        """
        self._cast(cards, self._onto_army(targets), frame)

    def _onto_army(self, targets: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
        """The same points slid across so their middle sits on the fighting.

        Unreadable leaves them where the plan put them: a bad shift is worse
        than a stale one, since the plan at least aimed at the village.
        """
        before = self._frame("before-front")
        time.sleep(MOTION_GAP)
        centre = army_centre(before, self._frame("front"))
        if centre is None:
            return targets
        drift = (
            round(centre[0] - sum(x for x, _ in targets) / len(targets)),
            round(centre[1] - sum(y for _, y in targets) / len(targets)),
        )
        logger.info("The fighting is at %s, %s from the planned rage; moving them", centre, drift)
        return tuple(clear_of_controls((x + drift[0], y + drift[1])) for x, y in targets)

    def _cast(self, cards: list[int], targets: tuple[tuple[int, int], ...], frame: bytes) -> None:
        """Empty every spell card over `targets`, and say so when one would not go.

        One slow selection per card, then that card's placements as a burst,
        since the card stays selected while it still holds something. How many to
        tap comes from the card's own `xN` plus one for slack, falling back to
        the whole target list when the artwork makes the count unreadable.

        A cargo that never leaves the card is the quiet half of the same bug the
        heroes had: nothing downstream notices, and the report says the attack
        went in. So the cards are asked, and the ones still holding something are
        offered the run once more — measured live, a spell placed straight after
        another was the one that got swallowed, so a second selection is usually
        all it wants. They are asked together rather than one at a time, because
        a capture and a settle apiece was most of what a cast cost.

        **A card is finished when it is empty, not when something left it.** That
        distinction used to be missing: the test was whether the `xN` corner had
        repainted, and it repaints on the first bottle to go, so a card that cast
        four of its five read as a success. Measured over three recorded
        battles, the rage card came off the row at x1 on two of them — a whole
        bottle carried home each time, which is the one thing a spell must never
        do. `live_cards` answers the question that was meant all along, since a
        spent card goes fully greyscale and a card with one bottle left does not.
        """
        pending = list(cards)
        for _attempt in range(SPELL_ATTEMPTS):
            if not pending:
                return
            for index, x in enumerate(pending):
                count = card_count(frame, x)
                cast_count = count + 1 if count else len(targets)
                cells = [targets[(index + i) % len(targets)] for i in range(cast_count)]
                self._tap((x, CARD_ROW_Y))
                time.sleep(SPELL_SELECT_DELAY)
                self.adb.tap_many(cells, self.display, gap=SPELL_PLACE_GAP)
                logger.info("Spell card at %d held %s, tapped %d", x, count, cast_count)
            time.sleep(DROP_SETTLE)
            # One reading for the whole row rather than one per card: `live_cards`
            # decodes the frame it is handed, so asking it per card decodes it
            # per card.
            pending = list(live_cards(self._frame("cast"), pending))
            if pending:
                logger.info("%d spell card(s) held on to their bottles", len(pending))
        for x in pending:
            logger.warning("The spell card at %d never cast; its bottles stay in it", x)

    def _wait_out_battle(self, opening: LootOffer) -> bool:
        """Sit through the battle and leave through 回營; False if no loot ever moved.

        Loot that never drops is how a deployment nothing came of shows up, and
        it costs nothing extra because the panel is already being polled. The
        verdict comes from the last reading anyone took rather than only from
        this loop's own, because remaining loot only ever falls: a battle short
        enough that the first poll ten seconds in already finds the result screen
        is a battle that went *well*, and judging it on nothing was how a village
        taken to 100% got reported as one the army never reached.

        **False is not the alarm on its own; `_seen` is what separates the two.**
        A panel read on every poll and never moving is the deployment that came
        to nothing. A panel that never read at all says only that — measured over
        one twelve-round run, two rounds polled a village whose loot panel would
        not resolve on any frame, and both of them had in fact taken their
        opponent for 800k and 1.1M. Reported as the same thing, the check that
        exists to catch an army that never landed cried wolf on one round in six,
        which is worth less than no check at all.
        """
        deadline = time.monotonic() + BATTLE_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(10)
            # The result screen is the first one with no loot panel on it.
            if self._battle_ended("battle"):
                break
        self._leave_result()
        return self._seen is not None and self._seen != opening

    def _outcome(self, reason: str, took: bool) -> str:
        """How a battle that was actually fought is reported.

        Three answers rather than two, and the third is the whole point: see
        `_wait_out_battle` for why a panel nobody could read is not the same
        thing as an army that never landed.
        """
        if took:
            return f"{reason}，已進攻並回營"
        if self._seen is None:
            logger.warning("Nothing ever read the loot panel; this round cannot be judged")
            return f"{reason}，已進攻並回營，但整場都讀不到戰利品面板，成果無從判斷"
        logger.warning("The whole battle passed without any loot moving")
        return f"{reason}，但整場戰利品沒有變化，部隊可能沒有成功部署"

    def run(self) -> AttackReport:
        logger.info("Attack run starts, thresholds=%s", self.thresholds.model_dump())
        # The same runner plays round after round, and the loot it last saw is
        # what `_wait_out_battle` judges the battle on. Carried over, a battle
        # short enough that nothing ever read its panel would be judged against
        # the previous opponent's remaining loot and reported as a success.
        self._seen = None
        # A new battle opens on its own camera, so whatever the last one was
        # dragged to has nothing to do with this one.
        self._panned = (0, 0)
        # Both belong to one round's own search, and both are reported on it.
        self._offered = False
        self._swapped = 0
        # And so does the tactic, which the run directory files under this
        # round's own number. Rounds that never reach the planner are ordinary —
        # no opponent above the thresholds, an army under `MIN_ARMY_RATIO`, the
        # attack menu not opening — and carried over, each of them would be
        # filed holding the previous round's plan. That is worse than no file:
        # a `--plan-in` or flat-fallback series writes identical plans round
        # after round, so nothing downstream can tell a stale copy from a real
        # one.
        self._played = None
        home = self._open_attack_menu()
        if home is None:
            logger.warning("The attack menu did not open; the game is not on the home village")
            return AttackReport(message="畫面不在主村，沒有開啟攻擊選單就停手")
        # Before the search fee, like the army check below: a village with every
        # storage full has nowhere to put what this run would win. **Every**, not
        # any — a battle brings home three resources, so one of them being at the
        # ceiling is no reason to stop paying a fee the other two still earn out.
        # An unreadable frame stops nothing, because a village that cannot be
        # read is not evidence of a full one.
        stock = read_stock(home)
        if stock and (full := self.stock.full(stock)):
            logger.info(
                "Storage limit reached (%s); farming stops with gold=%d elixir=%d dark=%d",
                "/".join(full),
                stock.gold,
                stock.elixir,
                stock.dark,
            )
            self.adb.back(self.display)
            return AttackReport(
                stock_full=True,
                message=f"{'、'.join(full)}已達停止門檻"
                f"（金幣 {stock.gold}／聖水 {stock.elixir}／黑水 {stock.dark}），停止刷資源",
            )
        self._tap(FIND_MATCH)
        time.sleep(2)
        strength = army_strength(self._frame("army"))
        if strength and strength[0] < strength[1] * MIN_ARMY_RATIO:
            logger.info("Army is only %d/%d; backing out before the search fee", *strength)
            self.adb.back(self.display)
            return AttackReport(message=f"兵力只有 {strength[0]}/{strength[1]}，等練好再打")
        self._tap(ARMY_ATTACK)
        skipped = 0
        while True:
            scouted = self._scout()
            if scouted is None:
                # An opponent still offering 下一個 has a countdown of its own
                # running, and leaving it to expire is what starts a battle the
                # army sits out. 結束戰鬥 is on that screen for exactly this.
                if self._offered:
                    logger.info("Leaving through 結束戰鬥 rather than letting the countdown run")
                    self._tap(END_BATTLE)
                return AttackReport(
                    skipped=skipped + self._swapped, message="等不到對手畫面，已放棄這一輪搜尋"
                )
            view, frame = scouted
            forced = not view.can_skip
            # Leaving is only safe on an opponent that can still be skipped: 結束
            # 戰鬥 is on that screen, and the search fee is already spent, so
            # walking out of 正在搜尋對手 would pay for a battle nothing is
            # deployed in and lose the shield with it. A forced battle is played.
            stopping = self.should_stop() and not forced
            if stopping:
                logger.info("Stop pressed; leaving the search after %d skip(s)", skipped)
            if not stopping and (forced or self.thresholds.accepts(view.loot)):
                reason = "倒數結束被強制開戰" if forced else "戰利品達標"
                logger.info("Attacking after %d skips (%s)", skipped, reason)
                self._deploy(frame)
                took = self._wait_out_battle(view.loot)
                return AttackReport(
                    skipped=skipped + self._swapped,
                    attacked=view.loot,
                    message=self._outcome(reason, took),
                )
            if stopping or skipped >= self.max_skips:
                self._tap(END_BATTLE)
                return AttackReport(
                    skipped=skipped + self._swapped,
                    message="已停止，未開打就離開搜尋"
                    if stopping
                    else f"連續跳過 {skipped} 個對手都未達門檻，已結束搜尋",
                )
            skipped += 1
            self._tap(NEXT_TARGET)
