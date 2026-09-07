"""One pass of 攻擊 → 偵察 → 進攻或跳過 → 回營.

Deliberately outside the Gemini agent loop. The scout screen expires after 30
seconds and takes the 下一個 button with it, which a vision call per candidate
does not reliably fit inside, and every opponent that is skipped anyway would
still have been paid for. Screen reading is `parsers.scout` instead.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from pydantic import PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    World,
    BattleRow,
    LootOffer,
    NightPlan,
    ScoutView,
    AttackPlan,
    AttackStep,
    ScreenPoint,
    AttackReport,
    LootThresholds,
    StorageCapacity,
)
from ai_coc.prompts import PROMPTS
from ai_coc.ui.world import cross, uncovered
from ai_coc.constants import COC_PACKAGE
from ai_coc.ui.runner import ScreenRunner, restart_game
from ai_coc.adapters.adb import ZOOM_PINCHES
from ai_coc.parsers.field import view_shift
from ai_coc.parsers.scout import (
    in_battle,
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
    loading_screen,
    selected_cards,
    attack_menu_open,
    storage_capacity,
    idle_disconnected,
    night_attack_menu,
    read_builder_stock,
    searching_opponent,
)
from ai_coc.parsers.world import current_world
from ai_coc.parsers.boundary import (
    PLAYFIELD,
    DEPLOY_BOUND,
    VILLAGE_CENTRE,
    fitted_line,
    village_box,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

# Every coordinate is the 1600x900 layout, the one `_apply_agent_action` assumes.
HOME_ATTACK = (105, 830)
FIND_MATCH = (272, 665)
ARMY_ATTACK = (1411, 803)
NEXT_TARGET = (1450, 630)
END_BATTLE = (118, 670)
RETURN_HOME = (798, 768)

# Where to tap to drop a storage bar's 最大儲存量 tooltip open, one per bar down
# the corner. The x sits inside the bar and well right of the builder base's
# gems row, whose green + spans 1345 to 1385 and opens the shop — the one thing
# on any of these rows that a stray tap must not reach. The y is each bar's own
# middle, the same rows `read_stock` reads its digits from.
#
# The tooltip takes a moment to animate in and is a toggle rather than a popup,
# so a capture taken too early reads the frame before it opened and costs the
# whole row: `CAPACITY_TRIES` is what pays for that, and for a tooltip somebody
# left open before the run started.
STOCK_BAR_X = 1400
STOCK_BAR_Y = (52, 136, 219)
TOOLTIP_SETTLE = 1.0
CAPACITY_TRIES = 2

# The builder base's own way into a battle, which is two buttons where the home
# village's is three: its 攻擊 sits in the same corner and opens 開始進攻, whose
# 立即尋找 goes straight to the matchmaker. There is no scout screen at all —
# no loot to read, nothing to skip, and no search fee to weigh, because the
# opponent is whoever the matchmaker pairs you with.
NIGHT_FIND = (1187, 592)
SEARCH_CANCEL = (798, 786)

# **The builder base matches you against a live player**, so the wait is for
# somebody else to be looking too rather than for a server to answer. Measured
# here, one search took five and a half minutes; the player says that is rare.
# So a search that drags is cancelled and started again rather than sat out —
# a fresh one gets a fresh pass over whoever is queueing now — and the state is
# read throughout, because a stop that takes minutes to show reads as one that
# did nothing.
SEARCH_PATIENCE = 150
SEARCH_ATTEMPTS = 4
SEARCH_POLL = 3.0

# How often the machine is offered its ability, and how often the loop looks at
# the battle while doing so. **The builder base's machine recharges instead of
# firing once**, so there is no moment to schedule and no per-hero clock to
# plan: the card is simply tapped for the whole battle and the game takes the
# taps that are ready. A tap on a card whose unit is not out selects the card
# and does nothing else, so this is safe on a machine the boundary refused as
# well.
#
# Measured over three recorded nights, about thirty battles: the ability is
# ready the moment the machine lands, one tap fires it, the bar the game draws
# over the card then refills over about 14 s, and a tap during the refill
# changes nothing — the bar grew monotonically through a tap every 3.5 s on
# every battle. So the tap is cheap to repeat (one shell round trip, about
# 50 ms) and only the capture is worth pacing: a frame every `ABILITY_POLL` to
# see whether the stage is over, a tap every `ABILITY_TAP` in between, which
# takes the gap between the ability being ready and being fired from up to a
# whole poll to under a second.
ABILITY_POLL = 3.0
ABILITY_TAP = 1.0

# How many times one attack can put an army down. The second is the stage the
# game opens after a first attack takes the whole base, sending what survived
# against the opponent's other, smaller one. Nothing here predicts it — the loop
# asks whether it is back on a village after each stage, so it plays whatever it
# is offered rather than modelling when the offer comes.
NIGHT_PHASES = 2

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

# One bottle's own footprint, which is what tells two of them apart. Rage covers
# 5 tiles, and the 44x44 map spans about 1040x605 px here, so a tile is roughly
# 24x12 and the footprint is a 240x120 ellipse rather than a circle: the board is
# isometric, so vertical spacing is half the horizontal. Two overlapping rages
# waste one, which is the pitch `plans/flat.json` lays its grid out at, and
# naming it is what lets a planned point be measured against the same ellipse.
RAGE_SPAN = (240, 120)
# How many bottles a rage card is taken to hold when its own `xN` corner will
# not read. It is what the flat plan's grid carries, so a card the artwork
# swallowed is asked for as many points as the fallback tactic can place.
RAGE_BOTTLES = 8
# How far past the edge of a footprint a crowded bottle gets pushed. Exactly to
# the edge is what the geometry asks for, but the answer is rounded back to a
# whole pixel, and half a pixel of that is enough to leave the point a hair
# inside — which pushes it again, to the same place, until the attempts run out
# and it is dropped after all. A hundredth of a footprint is 2 px across and 1 px
# down: far too little to matter on the ground, and more than rounding can undo.
NUDGE_CLEARANCE = 1.01
# One push can walk a point into the next bottle along, so it is offered again.
# The planner's own too-tight 2x2 opening into a proper one takes three.
NUDGE_ATTEMPTS = 8
# And how far the whole walk is allowed to carry it from where it was asked for,
# in footprints. Each push moves up to one, so without a ceiling eight of them
# compound: swept over 20 000 random point sets the worst case travelled 1531 px,
# which is the width of the screen and precisely the failure this replaced —
# a bottle covering ground nobody chose. One footprint is the honest limit,
# because past that it is no longer the planner's point at all, and a point that
# cannot be placed inside it is dropped rather than walked out there. The real
# plans need a fraction of it: replayed over 24 recorded rounds the furthest any
# point moved was 0.32 of a footprint.
NUDGE_REACH = 1.0

# How long a pause has to be before the loop will spend a frame inside it. A
# capture and its decode are about 0.9 s, so a shorter wait than this would be
# overrun by the very check it was hiding — and a check that pushes the next
# step late is the thing steps exist to avoid. Everything that needs a frame
# waits for a pause this long: the one reading of what the burst landed, and
# whether there is still a battle to play into. Some card slots sit exactly
# where the result screen draws 回營, so a tactic still tapping after a battle
# has ended is one walking itself out of the village.
CHECK_BUDGET = 2

# Between selecting a one-off card and placing what it holds, and it is now
# nothing. It was 0.6, then 0.15 on a live burst that put the siege machine and
# all four heroes on the field, and now 0 for the reason `TAP_GAP` is: the
# spacing the sleeps were guarding against turned out not to matter, measured on
# the builder base where five cards emptied on exactly four taps with no gap at
# all. What makes it safe here in particular is that `_drop_singles` reads the
# cards afterwards and offers another spot to whatever the game did not take, so
# a swallowed tap costs a retry rather than a hero.
SINGLE_DROP_DELAY = 0.0
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
# layouts vary far more than one line can allow for. So the line is pushed
# further from the middle when a whole pass lands nothing, staying inside the
# playfield the UI leaves free. The middle is the map's own, which is where the
# camera puts the village at the start of every battle.
SCREEN_CENTRE = VILLAGE_CENTRE
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
# How short the village has to read before the pre-battle pinch to say the
# camera had drifted off the far zoom. Swept over a day of live rounds, every
# healthy one measured 500 to 572 px tall and the single round that deployed
# nothing measured 411 — a village grown too big for the screen and clipped at
# top and bottom. 470 sits between them with room on both sides. This is only
# ever reported, never acted on: the pinch has already happened.
#
# **It is the home village's number and it is only asked there.** The builder
# base is a smaller map, so at the same far zoom it reads shorter than this on
# every frame; see `_settle_zoom` for the sweep and for why a second constant
# would not work either.
ZOOM_CLIPPED = 470
# A camera drag starts from the middle of the screen, so neither end of it
# lands on the game's own button columns.
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
# path. Five covers the animation with room for the card that follows it.
#
# **Which of the two ways out that card takes is no longer certain, and both
# work.** It used to be `_leave_result`, because a reward dismisses from where
# 回營 sits and `battle_over` answered True for it under the old 0.15 by
# construction. Nothing here re-measured its 繼續 button against the 0.25 that
# replaced it, and there is no fixture of one to measure — so it either still
# reads as a result screen and leaves that way, or it reads as nothing and
# `uncovered` presses `back` at it like any other popup. Worth settling with one
# capture of a payout if a run is ever seen stuck on one.
HOME_ATTEMPTS = 5
HOME_RETRY_DELAY = 3
# How the loading screen is waited out. Nothing on it answers a tap, and the
# two outages measured ran 25 to 40 minutes with the game coming back on its
# own; restarting it, or the emulator, shortened neither. So the wait is long,
# and it is polled at a pace that keeps a whole outage to a few hundred
# captures taken straight off the emulator rather than through `_frame`, since
# a recorded run has nothing to learn from that many copies of the same bar.
SERVER_POLL = 10.0
SERVER_POLLS = 270
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


def merged(steps: list[AttackStep]) -> list[AttackStep]:
    """Neighbouring spell steps of one kind, folded into the single cast they mean.

    The prompt asks for one step carrying every bottle's point and the planner
    does not comply: measured on its first live reply it wrote `rage → rage →
    rage → rage`, one step per bottle. That is not free the way a list is.
    Every step selects the card again and reads it back afterwards, about 3.4 s
    apiece, so those four spread from 11 s to 21 s into the battle — and a rage
    lasts 18, meaning the first had nearly expired before the last went down.

    Only *neighbours* are folded, because two casts with a `wait` between them
    are a tactic asking for exactly that: rage now, rage again when the push
    reaches the next ring. It is the ones with nothing in between that cannot
    have meant to be spread out, since nothing separates them but the loop's own
    cost.
    """
    out: list[AttackStep] = []
    for step in steps:
        if out and step.act == out[-1].act and step.act in ("rage", "freeze"):
            out[-1] = out[-1].model_copy(update={"at": [*out[-1].at, *step.at]})
            continue
        out.append(step)
    return out


def _reads(step: AttackStep) -> str:
    """One step as a few words, for the line the log prints a whole tactic on."""
    if step.act == "wait":
        return f"wait {step.seconds}s"
    who = f" {step.who}" if step.act in ("hero", "ability") and step.who != "unknown" else ""
    where = f" x{len(step.at)}" if len(step.at) > 1 else ""
    return f"{step.act}{who}{where}"


def planned_line(
    plan: AttackPlan | NightPlan | None,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
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


def deploy_candidates(
    plan: AttackPlan | NightPlan | None,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
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
    """The points, each moved off whatever bottle is already standing on it.

    `prompts/attack_plan.md` gives the planner the footprint and tells it two
    rages must not overlap, and the planner does not comply. The board is
    isometric, which is what makes the spacing impossible to eyeball: on one
    five-point reply four of the ten pairs sat inside one another, the closest
    being (768, 495) against (800, 378) — 121 px apart, clear of the ellipse's
    240 px axis and just inside its 120 px one. That is not a wording problem,
    since a model reading a screenshot cannot measure the distance between two
    points it has itself just invented, so the geometry is settled here instead
    of asked for.

    **It is settled by moving the point, not by dropping it.** Dropping was the
    first answer, and the caller made the loss back up from a fixed grid laid
    across the whole village. Measured over 24 live rounds the planner drew
    its 2x2 at 13% by 10% of the screen where the prompt asks for 15% by 13%,
    which puts the horizontal neighbour at 0.75 of a footprint and the vertical
    one at 0.56: the block collapsed to its own diagonal on **24 rounds out of
    24**, losing 48 of the 96 points asked for, and the grid filled the gap with
    bottles at (1000, 300) behind a village being attacked from the top left.
    An overlapping bottle wastes itself; one on the far side of the map wastes
    itself *and* leaves the ground the planner chose uncovered. Pushed out to
    the edge of the footprint it is standing on, that same tight block opens
    into the one the prompt asked for, over the ground the planner picked.
    """
    kept: list[tuple[int, int]] = []
    for point in points:
        clear = _clear_of(point, kept)
        if clear is not None:
            kept.append(clear)
    return kept


def _clear_of(point: tuple[int, int], kept: list[tuple[int, int]]) -> tuple[int, int] | None:
    """`point` moved out of every footprint already placed, or None if it will not go.

    The push is measured in footprints rather than pixels, where the ellipse is
    the unit circle and "clear" is simply a distance of 1, so leaving one is a
    single step along the line away from it. It repeats because a step can walk
    into the next bottle along, and because `clear_of_controls` can put the point
    straight back where it came from — a bottle pushed towards the card row has
    nowhere to go, and that is the case which runs out of attempts and is dropped
    after all, exactly as every crowded point used to be.

    `NUDGE_REACH` is what stops those repeats compounding into a walk across the
    village, which is the same wasted bottle this was written to prevent.
    """
    # The walk is measured from here rather than from `point`, because pulling a
    # point onto the playfield is not something this did to it: a plan point the
    # camera pan has carried under the card row arrives already 110 px out, and
    # charging that to the budget would drop it for a push it never took — while
    # the same clamped point with no neighbour beside it is returned unchecked.
    # Identical displacement, opposite verdicts, decided by whether a clash
    # happened to be nearby.
    spot = start = clear_of_controls(point)
    for _ in range(NUDGE_ATTEMPTS):
        clash = next(
            (
                (px, py)
                for px, py in kept
                if ((spot[0] - px) / RAGE_SPAN[0]) ** 2 + ((spot[1] - py) / RAGE_SPAN[1]) ** 2 < 1
            ),
            None,
        )
        if clash is None:
            return spot
        reach = (
            ((spot[0] - clash[0]) / RAGE_SPAN[0]) ** 2 + ((spot[1] - clash[1]) / RAGE_SPAN[1]) ** 2
        ) ** 0.5
        # Exactly on top of one already placed: there is no direction to push in,
        # and picking one would be inventing ground the planner never named.
        if not reach:
            return None
        step = NUDGE_CLEARANCE / reach
        spot = clear_of_controls((
            round(clash[0] + (spot[0] - clash[0]) * step),
            round(clash[1] + (spot[1] - clash[1]) * step),
        ))
        gone = (
            ((spot[0] - start[0]) / RAGE_SPAN[0]) ** 2 + ((spot[1] - start[1]) / RAGE_SPAN[1]) ** 2
        ) ** 0.5
        if gone > NUDGE_REACH:
            return None
    return None


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
# The builder base gets its own, because most of the home village's prompt is
# about things that do not exist there: spells, their two clocks, and each
# hero's own ability moment. What is left is the drop line and where the
# machine goes, and a prompt that spends four paragraphs on absent mechanics is
# a prompt that invites answers about them.
NIGHT_PROMPT = PROMPTS["night_plan"]


class AttackRunner(ScreenRunner):
    """Drives one attack from the home village and back.

    Screen reading never involves Gemini; only the tactical choice does, and only
    once an opponent has already passed the loot thresholds, so a skipped
    opponent costs nothing. Without `ai`, or if the call fails, the fixed flank
    and spell grid are used instead.

    **A `ScreenRunner` rather than a `GameRunner`**, which is where the screen,
    the tap and the recorded capture come from. It is not the other one because
    it cannot use `_home`: that method means the home village and answers a
    covered screen with `back`, where this loop has a result screen to leave, a
    matchmaker to sit through, a loading screen to wait out and possibly a boat
    to catch — see `_open_attack_menu`, which is its own answer to the same
    question.

    `should_stop` is checked between opponents only. A battle already under way
    is played out: abandoning one mid-deploy would leave the army on the field
    and the game on a screen the next run does not know how to get home from.
    """

    # Which village to play. The two are different games under one loop: the
    # home village scouts opponents, weighs their loot against thresholds and
    # pays a search fee, while the builder base is matched against a live player
    # with nothing to skip and nothing to weigh. What they share is everything
    # after the battle opens — the boundary, the drop line, the probing, the
    # card row — which is why this is a field rather than a second runner.
    world: World = "day"
    thresholds: LootThresholds
    # How full every storage has to be before the run stands itself down, as a
    # share of what that storage holds; 0 never stands one down. One number for
    # both villages, because the ceilings themselves are read off the game.
    stop_at: int = 0
    max_skips: int = 20
    # A plan settled before the run, which skips the Gemini call entirely. This is
    # what `--plan-in` fills, and it is how a hand-written tactic is replayed
    # exactly: the loop plays what it is given rather than asking for its own.
    plan: AttackPlan | None = None

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
    _played: AttackPlan | NightPlan | None = PrivateAttr(default=None)
    # How far the village has moved on screen since the attack opened, which is
    # only ever the deliberate drag that frees up a flank. Every coordinate the
    # loop holds — the preset flanks, the plan's line and its spell points — is
    # drawn for a village in the middle of the screen, so once the camera moves
    # they are all read through this.
    _panned: tuple[int, int] = PrivateAttr(default=(0, 0))
    # Where each one-off card was sent by the tactic being played, and which
    # hero cards it has not reached yet. Both belong to one run of
    # `_play_tactic` and are reset by it: they are here rather than threaded
    # through `_act` because they are what the steps accumulate, and a step
    # signature carrying its own bookkeeping is one nobody can read.
    _sending: dict[int, tuple[int, int]] = PrivateAttr(default_factory=dict)
    _unsent: list[int] = PrivateAttr(default_factory=list)
    # Cards the game really put something on the field for, which is a different
    # question from `_sending` and outlives it: that one is emptied by every
    # reading, and an `ability` step after one still has to know which heroes
    # are out there to fire.
    _onfield: list[int] = PrivateAttr(default_factory=list)
    # Whether anything at all left a card this battle, which is a different
    # question from `_onfield` and the one `_outcome` needs: that list is the
    # one-off cards alone, so an army whose heroes are all being upgraded fills
    # nothing into it however many troops went out. Set wherever the loop
    # actually watched a card give something up, never inferred from a tap.
    _deployed: bool = PrivateAttr(default=False)
    # Which card each named hero was sent to, so its own `ability` step can find
    # it again. The row itself says nothing about who is on which card.
    _named: dict[str, int] = PrivateAttr(default_factory=dict)
    # The last frame `_battle_ended` looked at, and the line the tactic is being
    # played along. Both are here so the battle poll can spend the frame it was
    # taking anyway on a second question: is anything still sitting in a card.
    _last: bytes = PrivateAttr(default=b"")
    _line: list[tuple[int, int]] = PrivateAttr(default_factory=list)
    # What this village's storages hold when full, read off their own tooltips
    # the first time a round reaches the village and kept for the rest of the
    # run. A storage only grows when a builder finishes upgrading one, which is
    # days apart, against six taps and three captures to ask again every round.
    _capacity: StorageCapacity | None = PrivateAttr(default=None)
    # Why the last `_open_attack_menu` gave up, when the reason is worth more
    # than 畫面不在主村: a loading screen that outlasted `SERVER_POLLS`, or a
    # stop that landed during that wait. Empty is the ordinary failure.
    _stuck: str = PrivateAttr(default="")

    @property
    def played(self) -> AttackPlan | NightPlan | None:
        """The plan this run actually used, once one has been settled on."""
        return self._played

    @property
    def _ceiling(self) -> StorageCapacity:
        """The ceilings this run read, or none at all before it has read them.

        An empty one watches nothing, so a round that never reached the village
        to read its bars never stands the run down either — which is the same way
        an unreadable storage row is already treated.
        """
        return self._capacity or StorageCapacity()

    @property
    def _middle(self) -> tuple[int, int]:
        """Where the village is sitting now, which is what a drop is pushed away from."""
        return (SCREEN_CENTRE[0] + self._panned[0], SCREEN_CENTRE[1] + self._panned[1])

    def _onscreen(self, points: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
        """Points drawn for a centred village, read against wherever the camera is now."""
        return tuple((x + self._panned[0], y + self._panned[1]) for x, y in points)

    def _spots(
        self, points: Sequence[ScreenPoint], cards: list[int]
    ) -> dict[int, tuple[int, int]]:
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
            card: push_out(spot, 0, self._middle)
            for card, spot in zip(
                cards, self._onscreen(tuple(point.pixels() for point in points)), strict=False
            )
        }

    def _settle_ceilings(self) -> None:
        """Read what this village's storages hold when full, once a run, off their tooltips.

        Kept for the rest of the run once it has read, because a storage only
        grows when a builder spends days upgrading one, while asking costs six
        taps and three captures.

        **A partial read is thrown away rather than kept**, which is the whole
        reason this is a method and not an assignment. Every one of these taps
        can be swallowed — a tooltip still animating in, a panel over the bars,
        a village the game had not finished painting — and a `StorageCapacity`
        holding some of its rows is worse than none in both directions. Empty, it
        watches nothing, so an overnight run farms straight past full storages
        and throws the loot away, which is the failure this setting exists to
        prevent. Partial is worse still: gold and elixir failing while dark reads
        its 370 000 leaves the run standing down the moment dark passes 90% with
        the two big storages nearly empty. Left unset, the next round simply
        asks again, and a round costs minutes anyway.

        **The builder base's third row is never tapped.** That village has no
        dark elixir; what sits at that y is its gems bar, and the green + beside
        the number opens the shop. Two rows there is not a limitation — a
        resource with no ceiling is left out of the comparison, which is exactly
        right for one that does not exist.

        The tooltip is a toggle, so a row that reads nothing is left alone rather
        than tapped shut: the one way to read nothing on a village that has the
        bar is to have closed a tooltip that was already open, and the next
        attempt then opens it.

        **A row that fails both tries can leave its own tooltip up, and that is
        measured to be harmless.** The panel hangs *under* the bar that opened
        it, so it covers the rows below rather than its own — and only the last
        row read has nothing after it to close it, since the next row's first tap
        closes whatever is open. Measured on the fixtures: with the dark tooltip
        up `read_stock` still reads all three rows, and with the builder base's
        elixir tooltip up `read_builder_stock` still reads both, which is exactly
        the last row in each village. Closing on failure instead was tried and is
        worse — with the tooltip starting closed, which is the ordinary case, a
        row that fails twice would then end on an opening tap and leave the
        **gold** panel up, and that one does cover the rows under it.
        """
        # Nothing to measure against, so nothing worth six taps: `probe` and
        # `bounds` run at the default 0, and so does a window whose spinbox is
        # at 不監控.
        if self._capacity is not None or not self.stop_at:
            return
        rows = ("gold", "elixir", "dark") if self.world == "day" else ("gold", "elixir")
        found: dict[str, int] = {}
        for row, name in enumerate(rows):
            for _ in range(CAPACITY_TRIES):
                self._tap((STOCK_BAR_X, STOCK_BAR_Y[row]))
                time.sleep(TOOLTIP_SETTLE)
                held = storage_capacity(self._frame(f"capacity-{name}"), row)
                if held is None:
                    continue
                found[name] = held
                self._tap((STOCK_BAR_X, STOCK_BAR_Y[row]))
                time.sleep(TOOLTIP_SETTLE)
                break
        if len(found) < len(rows):
            logger.warning(
                "Only %d of %d storage ceilings read (%s); leaving them for the next round",
                len(found),
                len(rows),
                found or "none",
            )
            return
        self._capacity = StorageCapacity(**found)
        logger.info("Storage ceilings read as %s", found)

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
        self._last = png
        return battle_over(png)

    def _open_attack_menu(self) -> bytes | None:
        """Get to this village's attack menu, clearing whatever is covering the village.

        The village frame comes back with it. That is the one screen the
        storage bars are on, and the frame is already being taken here to check
        for the idle dialog, so reading the storages costs no extra capture.

        One method for both villages, because the way in is the same tap: 攻擊
        sits in the same corner on each, and what separates them is only which
        dialog it opens — `attack_menu_open` does not recognise the builder
        base's and `night_attack_menu` does not recognise the home village's.
        The game reopens on whichever village it was closed on, so a run
        pointed at one can find the other; without the crossing below it fails
        safe but expensively and says the wrong thing, spending every attempt
        here on the other village's dialog and reporting 畫面不在主村, which
        reads as a game that is stuck rather than one in the other village.
        """
        other: World = "night" if self.world == "day" else "day"
        opened = attack_menu_open if self.world == "day" else night_attack_menu
        self._stuck = ""
        waited = False
        for _ in range(HOME_ATTEMPTS):
            home = self._frame("home")
            if idle_disconnected(home):
                logger.info(
                    "The session was dropped (idle, or the connection was lost); restarting"
                )
                self.display = restart_game(self.adb, self.display)
                # A restart boots through 正在載入, and `restart_game` returns
                # a couple of seconds before the village paints, so the next
                # frame here is that screen: a fresh load, not a loaded game
                # dropped back, and it gets a fresh wait. The attempts bound
                # the round, not the stop.
                waited = False
                continue
            if loading_screen(home):
                if not self._wait_out_loading(waited):
                    return None
                waited = True
                continue
            if battle_over(home):
                logger.info("The last battle's result screen is still up; leaving it")
                self._leave_result()
                continue
            # One crossing and then out, for the reason `GameRunner._home` gives:
            # `cross` already spends about a minute trying three spots, and
            # retrying it per attempt would turn a boat nobody can reach into
            # five minutes of silence rather than the one round this costs.
            here = current_world(home)
            if here == other:
                logger.warning(
                    "The game is on the %s village; sailing over before attacking", other
                )
                if cross(self.adb, self.display, self.world) != self.world:
                    logger.warning("The crossing never landed; this round has no village to open")
                    return None
                continue
            # **Something is over the village, and the 攻擊 tap below would land
            # on it.** The game puts full-screen popups up on its own — event
            # rewards, season passes, whatever is running that week — and the one
            # measured here held a run for 40 minutes: five attempts a round
            # tapping behind it, then 畫面不在主村, then the same again. Nothing
            # between rounds clears it either, since the world is picked once per
            # series. `uncovered` is the same step the crossing takes, and it is
            # safe for the same reason — it presses `back` only on a frame that
            # is not a village, and never on a battle.
            if here is None:
                uncovered(self.adb, self.display)
                continue
            # The one moment the run is known to be standing on the right
            # village with nothing over it, which is what tapping the storage
            # bars needs. Past here the attack menu is up and the bars are behind
            # it; before here the frame might be a result screen or the other
            # village. It reads once and every round after this costs nothing.
            self._settle_ceilings()
            self._tap(HOME_ATTACK)
            time.sleep(2)
            if opened(self._frame("attack-menu")):
                return home
            # Never `back` here: on a clear village that is 確定退出遊戲嗎, one
            # tap away from closing the game. A building panel left open does
            # not cover the 攻擊 button in the corner anyway, so the tap above
            # only needs the panel to swallow one press and then retries.
            time.sleep(HOME_RETRY_DELAY)
        return None

    def _wait_out_loading(self, again: bool) -> bool:
        """Sit on 正在載入 until the game leaves it; False when the wait ended without it.

        Nothing here taps, restarts or presses `back`, because none of those
        brings the server back: measured across two outages of 25 to 40
        minutes, a game restart and an emulator restart each left the bar
        where it was and the game came back on its own. What ends the wait is
        the screen changing — to a village, or to the dropped-session dialog
        the attempts above already know how to answer.

        Once per load, which is what `again` says: a game that loaded and
        then dropped back onto this screen is a server that is not staying
        up, and a second wait would be spent on exactly the outage the first
        one measured. A restart starts a new load and the caller clears the
        flag for it. Every way out is written to `_stuck` so the round's
        report says which it was, since a stop and a server that never
        answered are the two things a farming session most needs to tell
        apart.
        """
        if again:
            logger.warning("The loading screen is back; the server is not staying up")
            self._stuck = "遊戲又回到載入畫面，伺服器可能還連不上，這一輪停手"
            return False
        logger.warning(
            "The game is on its loading screen; waiting for the server rather than tapping"
        )
        for poll in range(1, SERVER_POLLS + 1):
            if self.should_stop():
                logger.info("Stop requested while waiting for the game to load")
                self._stuck = "等待載入時收到停止要求，這一輪沒有開打"
                return False
            time.sleep(SERVER_POLL)
            if not loading_screen(self.adb.screenshot(self.display)):
                logger.info("The loading screen went after about %.0fs", poll * SERVER_POLL)
                return True
        minutes = SERVER_POLLS * SERVER_POLL / 60
        logger.warning(
            "Still on the loading screen after %.0f minutes; the server may be unreachable",
            minutes,
        )
        self._stuck = f"遊戲卡在載入畫面 {minutes:.0f} 分鐘，伺服器可能連不上，這一輪停手"
        return False

    def _stood_down(self, home: bytes) -> AttackReport | None:
        """A report standing the run down on full storages, or None to carry on.

        Asked before the search rather than after it, on both villages: a
        village with every storage full has nowhere to put what this run would
        win, and the search costs a fee on the home village and a wait on a
        live player on the builder base — one has run to five and a half
        minutes. **Every** storage, not any: a battle brings home three
        resources, so one of them being at the ceiling is no reason to stop
        earning the other two. An unreadable frame stops nothing, because a
        village that cannot be read is not evidence of a full one.

        The ceilings are this village's own, read off its bars by
        `_settle_ceilings`, which is what lets one percentage serve both. The
        builder base reads two rows, because what sits at its third is the
        gems bar.
        """
        stock = (read_stock if self.world == "day" else read_builder_stock)(home)
        if stock is None:
            return None
        full = self._ceiling.full(stock, self.stop_at)
        if not full:
            return None
        held = f"金幣 {stock.gold}／聖水 {stock.elixir}"
        if self.world == "day":
            held += f"／黑水 {stock.dark}"
        logger.info("Storage limit reached (%s); farming stops with %s", "/".join(full), held)
        self.adb.back(self.display)
        return AttackReport(
            world=self.world,
            stock_full=True,
            message=f"{'、'.join(full)}都滿過 {self.stop_at}%（{held}），停止刷資源",
        )

    def _leave_result(self) -> None:
        """Tap 回營 until the result screen has actually gone.

        One tap was not enough and cost four runs in a row. The loot panel
        vanishes as the result screen starts animating in, so the tap fired the
        moment `_battle_view` reads nothing lands before the button is alive; the
        village then stayed covered and every following run stood down with
        畫面不在主村 without ever attacking.

        **A screen that will not answer 回營 gets `back` rather than another
        round of the same tap.** The game puts its own popups over the result —
        an event reward page, a season pass, whatever it is running that week —
        and those close on a red X in their own corner, so tapping where 回營
        would be does nothing at all however many times it is tried. Measured
        live, one of them held a run for 40 minutes: `battle_over` read its green
        tick marks as the button, every attempt tapped an empty patch of screen,
        and 18 rounds went by reporting 畫面不在建築大師基地 while a battle it
        had already matched into ran out underneath. `back` is safe here for the
        reason it is safe in `uncovered`: this is only reached on a frame that
        read as a result screen, which is never a clear village.
        """
        for _ in range(RESULT_ATTEMPTS):
            if not battle_over(self._frame("result")):
                return
            self._tap(RETURN_HOME)
            time.sleep(RESULT_RETRY_DELAY)
        # Read again before pressing. The last tap of that loop is unchecked, and
        # it is the one most likely to have worked — the button only comes alive
        # once the stars have flown in, which is what the retries are for. On a
        # village that has just come back, `back` is 確定退出遊戲嗎.
        if not battle_over(self._frame("result")):
            return
        logger.warning("回營 will not close this screen; pressing back at whatever is over it")
        self.adb.back(self.display)
        time.sleep(RESULT_RETRY_DELAY)

    def _plan(self, frame: bytes, rage_count: int, freeze_count: int) -> AttackPlan | None:
        """The plan for this opponent: the one handed in, the AI's, or the flat default.

        Falling back to a written-out plan rather than to constants is what makes
        the default readable and editable, and it is the same tactic the loop
        used to hold in `DEPLOY_LINES` and a fixed grid of rage points.
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
        # The whole tactic on one line, in the order it will be played, because
        # that is the question a bad battle asks first: what did it mean to do.
        logger.info(
            "Plan: from %s — %s (%s)",
            plan.deploy_from,
            " → ".join(_reads(step) for step in plan.steps),
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

    def _flank(
        self, battle: bytes | None, plan: AttackPlan | NightPlan | None
    ) -> tuple[tuple[int, int], ...]:
        """The line to deploy along: what the plan drew, bent onto the boundary.

        **Nothing is probed first, and that is deliberate.** This used to drop
        three troops at the ends and middle of a candidate line and ask whether
        a card drained, walking through all four preset flanks until one took
        them. Three troops is three troops sent to die alone in front of the
        defences, before the army that was meant to cover them exists — and it
        was paid on every battle, whether or not the line was ever in doubt.

        What is left is the two things that cost nothing: the camera is dragged
        clear of the card row when the flank needs the room, and the line is bent
        onto the deployment boundary the game itself draws. Where that boundary
        cannot be read the preset stands as it is. A line the village has grown
        over is then answered by `_spread_troops`, which pushes it out when a
        whole pass drains nothing — the same backstop that was always underneath
        the probing, now carrying it alone.
        """
        preset = deploy_candidates(plan)[0]
        battle = self._clear_flank(battle, preset)
        flank = self._onscreen(preset)
        fitted = fitted_line(battle, flank[0], flank[1], centre=self._middle) if battle else None
        return fitted or flank

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

        `pushed` carries on from where the caller left off. Restarting it at
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
            drained = card_drained(before, shot, remaining)
            self._deployed |= bool(drained)
            if not drained and pushed + 1 < DEPLOY_ATTEMPTS:
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
        the midpoint the troops have already been spread along, which the game
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
            SCREEN_CENTRE,
            clear_of_controls((SCREEN_CENTRE[0] + drift[0], SCREEN_CENTRE[1] + drift[1])),
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

        **The report is the home village's alone, because the number is.** The
        builder base is a smaller map, so its boundary is shorter at the same far
        zoom and every stage of every night round read under the floor: swept
        over 47 recorded rounds, 50 of 50 readings warned, at 305 to 466 px,
        while the same day's 22 home rounds warned once, at 440. A check that
        fires on everything is worth less than no check, because it is what
        teaches a reader to skip the line that will matter.

        **A second constant is not the fix, and the spread is why.** Those
        healthy night readings run 161 px wide, which is already wider than the
        89 px between a healthy home village and the clipped one this floor was
        drawn from — so a clipped builder base would land inside the range of
        healthy ones and no pixel floor could separate them. Giving the night
        path its own number would only move the wrong answer. What it would take
        is a reading off a builder base that really is zoomed in, and nothing
        recorded here has one: the loop pinches out before every battle, so the
        camera is never left off the limit for a frame anyone kept.
        """
        before = village_box(frame)
        self.adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, self.display)
        if self.world == "day" and before is not None and before[3] - before[1] < ZOOM_CLIPPED:
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
                SCREEN_CENTRE,
                clear_of_controls((SCREEN_CENTRE[0] + drift[0], SCREEN_CENTRE[1] + drift[1])),
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
        """Play the plan's steps along one flank, and read once what the game refused."""
        # The runner outlives the round, so last round's line has to go before
        # this one can fail to draw a new one: `_dump_leftovers` reads it during
        # the battle wait, which is reached even by a round that deployed
        # nothing at all, and pouring along a line drawn for the village before
        # is worse than pouring nowhere.
        self._line = []
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
        freezes = freeze_cards(frame, spells)
        rages = [x for x in spells if x not in freezes]
        rage_count = sum(card_count(frame, x) or RAGE_BOTTLES for x in rages)
        # One apiece where the count is unreadable: the flat plan carries one
        # freeze point, and asking for eight points for a single bottle would
        # only spend the battle tapping empty ground.
        freeze_count = sum(card_count(frame, x) or 1 for x in freezes)
        plan = self._plan(frame, rage_count, freeze_count) or plans.flat()
        # **Which one-off card holds the siege machine is the plan's own answer,
        # and it used to be arithmetic.** Nothing on the row separates a machine
        # from a hero: neither carries an `xN`, both sit in the same group, and
        # the game draws a health bar over both — measured across a whole
        # recorded run, the 攻城戰車 landed and read as a hero on the next frame.
        # So it used to be settled by counting the plan's heroes against the
        # one-off cards, a bet that came apart whenever the planner miscounted
        # and shifted every hero a slot along with it. A tactic written as steps
        # simply says whether it is sending one.
        row = BattleRow(
            troops=troops,
            machine=singles[:1] if plan.acts("siege") else [],
            heroes=singles[1:] if plan.acts("siege") else singles,
            rages=rages,
            freezes=freezes,
            rage_count=rage_count,
            freeze_count=freeze_count,
            frame=frame,
        )
        logger.info(
            "%d troop card(s), %d machine, %d hero(es), %d spell(s); %d step(s) to play",
            len(row.troops),
            len(row.machine),
            len(row.heroes),
            len(spells),
            len(plan.steps),
        )
        # Nothing can be placed while the scout countdown is still running, and a
        # tap the game ignores raises nothing at all, so probing then drains no
        # card and every flank in turn reads as one the village has grown over.
        # With Gemini in the loop the planning call happens to outlast the
        # countdown, which is what has been hiding this; without a key `_plan`
        # returns at once and the run would probe into the countdown every time.
        battle = self._wait_for_battle()
        # `_flank` bends the plan's own line onto the boundary the game draws,
        # and the tactic is played against whatever comes back.
        anchors = self._flank(battle, plan)
        self._play_tactic(plan, anchors, row)

    def _play_tactic(
        self, plan: AttackPlan, anchors: tuple[tuple[int, int], ...], row: BattleRow
    ) -> None:
        """Run the tactic straight through, reading once what the game refused.

        **The order is the plan's, and every pause in it is a step.** It used to
        be the loop's: siege, then troops, then heroes, and only once all of
        them were down did anything on a clock get a turn. Two things came out
        of that and neither could be fixed inside it. A spell waited out the
        heroes for no reason a rage recognises — it covers the troops, and they
        are already walking. And the clocks were counted from the loop's own
        idea of when the battle opened, which moves with how long Gemini took to
        answer: measured over 23 recorded rounds the gap those numbers were
        really about, troops down to rage cast, ran from 3.0 to 10.8 seconds
        while the planner asked for the same 14 every time.

        A `wait` counted from the previous move finishing has neither problem,
        and interleaving is then free: troops, a rage, then the rest of the
        heroes is three lines in a list.

        **Nothing is checked between steps, and that is where the time went.**
        Measured on one recorded round, putting the army down took 9.15 seconds
        of which about 1.5 was tapping: six captures at roughly 0.7 s each, plus
        the settles that exist only so those captures have something to see —
        84% of the deployment spent watching itself. A refused drop costs
        nothing to discover late, because the game does not consume the card it
        refused, so the whole tactic goes in blind and one reading afterwards
        says what to send again.

        That reading is taken **inside the tactic's first pause** rather than
        added to it: the pause is the only idle time in a battle, and a check
        that fits inside it is free.
        """
        line = deploy_line(LINE_POINTS, *push_line(anchors, 0, self._middle))
        self._line = line
        steps = merged(plan.steps)
        self._sending, self._unsent, self._onfield = {}, list(row.heroes), []
        self._named = {}
        self._deployed = False
        opened = done = time.monotonic()
        for step in steps:
            if step.act == "wait":
                # A pause is the only idle time in a battle, so everything that
                # needs a frame is spent inside one: the single reading of what
                # the burst landed, and the check that there is still a battle
                # to play into. Both are free here and neither is free anywhere
                # else, which is why nothing between two taps looks at anything.
                if step.seconds >= CHECK_BUDGET:
                    if self._sending:
                        self._settle_drops(row.troops, line, anchors)
                    elif self._battle_ended("playing"):
                        logger.info("The battle ended with %d step(s) still to play", len(steps))
                        return
                if (remaining := done + step.seconds - time.monotonic()) > 0:
                    time.sleep(remaining)
                continue
            self._act(step, row, line)
            logger.info("Played %s, %.0fs in", step.act, time.monotonic() - opened)
            done = time.monotonic()
        # Whatever the last pause was too short to cover, or a tactic that ended
        # on a drop, still gets its reading.
        if self._sending:
            self._settle_drops(row.troops, line, anchors)

    def _act(self, step: AttackStep, row: BattleRow, line: list[tuple[int, int]]) -> None:
        """One step of a tactic, as taps, with nothing read back.

        A step naming a card the row does not carry is skipped rather than
        shifting everything after it: an upgrading hero has no card at all, so a
        plan naming one is an ordinary thing to be handed rather than a fault.
        """
        points = self._onscreen(tuple(point.pixels() for point in step.at))
        middle = line[len(line) // 2]
        if step.act == "siege":
            self._drop_at(row.machine, points[0] if points else middle)
        elif step.act == "hero":
            # `who` names the hero for the log and for the prompt to reason
            # with; which card it is comes from the row order, because nothing
            # on the row itself says who is on which card.
            #
            # **`unknown` means every hero still in hand, not the next one.** A
            # tactic that does not know the row — `plans.flat()` cannot, since
            # it is written before any army is seen — has no way to name them
            # one at a time, and taking the next card for each such step sent
            # one hero and left the other two in their cards for the whole
            # battle. Measured live on a row of three: `1 of 2 one-off card(s)
            # never landed` where the army carried four.
            wanted = list(self._unsent) if step.who == "unknown" else self._unsent[:1]
            self._drop_at(wanted, points[0] if points else middle)
            # Which card each named hero went to, so its own `ability` step can
            # find it again. Nothing on the row says who is on which card, so
            # this record is the only link between the two.
            if step.who != "unknown" and wanted:
                self._named[step.who] = wanted[0]
            del self._unsent[: len(wanted)]
        elif step.act == "ability":
            # **The named hero's card, and only that one.** This used to tap
            # every hero the tactic had out, which fires abilities the plan
            # meant to hold: on `hero queen → hero king → ability queen → wait
            # 20s → ability king`, the king's went at 2 s instead of 22. Live it
            # showed up as 你已經用過這項英雄技能了 on the second step, the
            # game refusing a hero already spent by the first.
            #
            # `unknown` still means all of them, the same rule `hero` follows,
            # because a tactic written before any army was seen cannot name one.
            wanted = [self._named[step.who]] if step.who in self._named else []
            if step.who == "unknown":
                wanted = list(row.heroes)
            # A tap on a card whose hero never landed deploys it instead, with
            # nothing around it, so only what is out gets offered one.
            self.adb.tap_many(
                [
                    (card, CARD_ROW_Y)
                    for card in wanted
                    if card in self._onfield or card in self._sending
                ],
                self.display,
            )
        elif step.act == "troops":
            self._pour(row.troops, line, row.frame)
        elif step.act in ("rage", "freeze"):
            cards = row.rages if step.act == "rage" else row.freezes
            # Bottles, not cards, and the same number the planner was asked to
            # draw points for. Freeze counted its *cards* here, so one card of
            # three bottles kept one of the three points it asked for and
            # `_cast` stacked the whole cargo on it.
            wanted = max(row.rage_count if step.act == "rage" else row.freeze_count, 1)
            # `spaced` can drop every point it could not clear, which is what
            # the fallback below covers — so `targets` is never empty, `wanted`
            # is at least one, and the slice always carries something.
            targets = tuple(spaced(list(points))) or (middle,)
            self._cast(cards, targets[:wanted], row.frame)

    def _drop_at(self, cards: list[int], spot: tuple[int, int]) -> None:
        """Send every card in one shell round trip, and write down where they went."""
        spot = clear_of_controls(spot)
        if not cards:
            return
        self.adb.tap_many(
            [tap for card in cards for tap in ((card, CARD_ROW_Y), spot)],
            self.display,
            gap=SINGLE_DROP_DELAY,
        )
        self._sending.update(dict.fromkeys(cards, spot))

    def _pour(self, troops: list[int], line: list[tuple[int, int]], frame: bytes) -> None:
        """Empty every troop card along the line, one card at a time, reading nothing.

        How many taps a card takes is its own `xN` from the opening frame plus
        one for slack, clamped to `DROPS_PER_PASS` — the same arithmetic the
        checked version used, minus the capture between cards. A card the
        artwork swallowed falls back to the ceiling, which is what that constant
        has always been for.
        """
        for seed, card in enumerate(troops):
            count = card_count(frame, card)
            taps = min(count + 1, DROPS_PER_PASS) if count else DROPS_PER_PASS
            self._tap((card, CARD_ROW_Y))
            self.adb.tap_many(drop_points(line, seed, taps), self.display)

    def _settle_drops(
        self, troops: list[int], line: list[tuple[int, int]], anchors: tuple[tuple[int, int], ...]
    ) -> None:
        """The one reading of the burst: what never left its card, sent again.

        Two questions off one frame, because a decode is the expensive part. A
        one-off card is judged by the health bar the game draws over it, and a
        troop card by whether it has gone grey — the same two readings the
        checked deployment took, once instead of six times.

        A whole row of troops still holding something is the line rather than
        the taps: `_spread_troops` answers that by pushing the flank further out,
        which is the backstop that was always underneath the probing.
        """
        after = self._frame("settled")
        sent = list(self._sending)
        # Emptied here rather than by the caller, because that is what makes a
        # tactic dropping heroes *after* its first pause work: the check fires
        # whenever anything is waiting on one, so a later drop gets read too. It
        # used to be a one-shot flag, and measured live on a plan reading
        # `hero queen → ability queen → wait 2s → hero king → hero duke`, that
        # check ran while only the machine and the queen had been sent — so the
        # king and the duke went down afterwards and nothing ever looked at them.
        self._sending = {}
        on_field = field_units(after, sent)
        self._onfield += on_field
        self._deployed |= bool(on_field)
        missing = [card for card in sent if card not in on_field]
        holding = list(live_cards(after, troops))
        logger.info(
            "After the burst: %d of %d one-off card(s) never landed, %d troop card(s) still hold",
            len(missing),
            len(sent),
            len(holding),
        )
        if holding:
            self._spread_troops(holding, anchors, 0)
        if missing:
            again, _ = self._drop_singles(missing, line, "retry")
            self._onfield += again
            self._deployed |= bool(again)

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
            after = self._frame("cast")
            # **A card that will not empty and a battle that has gone look the
            # same from here**, and the retries used to spend themselves on the
            # second. Measured live on the 探礦者 sheet: the battle had ended,
            # every attempt reported the bottles still in their cards, and each
            # one tapped the panel again. The frame is already taken for
            # `live_cards`, so this asks the question that separates them for
            # nothing.
            if not in_battle(after):
                logger.info("The battle went while casting; %d card(s) keep theirs", len(pending))
                return
            pending = list(live_cards(after, pending))
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
            self._dump_leftovers()
        self._leave_result()
        return self._seen is not None and self._seen != opening

    def _dump_leftovers(self) -> None:
        """Anything still sitting in a card mid-battle, poured along the line.

        **A card can appear after the army is down.** Measured live: a row of
        eight at the opening had a ninth in colour at 62% destruction, holding
        23, where every original card had greyed out — an event handing out
        troops, and the tactic had no step for it because the planner never saw
        it. Twenty-three troops carried home is worse than any of them landing
        somewhere imperfect.

        So this asks the one question that does not care why: is anything still
        deployable. It costs nothing, because the battle poll was already taking
        this frame to ask whether the result screen is up, and it needs no
        knowledge of whatever event put the card there.

        It is also the honest guard against a plan that simply left something
        out — a `troops` step the planner forgot, or a card the row grew that no
        step names. That was the one part of a free-form tactic worth worrying
        about, and this covers it without validating anything.

        **Spells go out the same way, and are not told apart.** The first
        version skipped the cards it knew were spells, by the x they sat at when
        the row was read — and a card appearing at the front shifts every card
        after it, so those x's point at the wrong ones. Measured live, that let
        a freeze card still holding three bottles read as a leftover. Which is
        the right outcome anyway: a spell nobody cast is a spell carried home,
        so the honest rule is that anything still holding gets emptied, and the
        only thing the distinction ever bought was the selection delay — which
        `_cast` pays for troops too, at 0.6 s once per card.
        """
        if not self._line:
            return
        # **`card_groups` off a frame that is not a battle invents cards, and
        # this is the caller that then taps them.** Measured live: a battle that
        # had already ended left the shop's 外觀 page on screen, its rows of
        # skins for sale read back as spell cards at x 105 and x 1497 — neither
        # of which is a card position at all — and each was tapped and then
        # poured over twelve points along the drop line, which is what walked
        # the shop from page to page. `Spell card at 105 held None, tapped 12`
        # is the whole record of it. The same shape on another round read three
        # cards off the 探礦者 sheet's row of builder portraits.
        #
        # The frame is already in hand, so asking costs nothing. It is asked
        # here rather than trusted to `battle_over`, which answers only for the
        # result screen and is False for every panel the game puts up.
        if not in_battle(self._last):
            logger.info("No battle on screen; nothing here is a card to empty")
            return
        live = [card for group in card_groups(self._last) for card in group]
        # An `xN` corner is what says a card still holds something to place;
        # heroes and the siege machine carry none, so a hero standing on the
        # field in full colour is not mistaken for a card to empty. These are
        # read off *this* frame, so a shifted row costs nothing here.
        extra = counted_cards(self._last, live)
        if not extra:
            return
        logger.info("%d card(s) still hold something; emptying them", len(extra))
        self._cast(extra, tuple(self._line), self._last)

    def _outcome(self, reason: str, took: bool) -> str:
        """How a battle that was actually fought is reported.

        Four answers rather than two, and the two extra ones are the point: see
        `_wait_out_battle` for why a panel nobody could read is not the same
        thing as an army that never landed, and `_deployed` for why an army that
        landed and took nothing is not that either.

        **Loot that never moved says the attack took nothing; it does not say
        why**, and the difference is where the next person looks. An army that
        never left its cards is this loop's problem — a drop line off the map, a
        camera left somewhere else. An army that went down and came home empty
        is the tactic's, and belongs to `tune-attack`. Measured on a live round,
        the loop reported 部隊可能沒有成功部署 for a battle whose result screen
        read 戰敗, 32%, 你獲得了 0: every troop card had drained and three of
        three retried heroes had landed, and the army had simply gone in on the
        side with no storages on it. `_deployed` is what the loop already
        watched happen, so the message can say which of the two it was instead
        of naming the one it did not check.
        """
        if took:
            return f"{reason}，已進攻並回營"
        if self._seen is None:
            logger.warning("Nothing ever read the loot panel; this round cannot be judged")
            return f"{reason}，已進攻並回營，但整場都讀不到戰利品面板，成果無從判斷"
        if self._deployed:
            logger.warning(
                "The army went down and the battle took nothing; the tactic came up short"
            )
            return f"{reason}，已進攻並回營，但整場戰利品沒有變化，部隊有出去而這一場沒搶到東西"
        logger.warning("The whole battle passed without any loot moving, and nothing left a card")
        return f"{reason}，但整場戰利品沒有變化，而且沒有任何一張卡片出得去，部隊沒有成功部署"

    def _find_opponent(self) -> bytes | None:
        """Hold the matchmaker open until a battle opens, restarting it if it drags.

        **The wait is for another live player**, not for a server, which is why
        it can run to minutes and why cancelling is worth doing: a fresh search
        gets a fresh pass over whoever is queueing now. Nothing is lost by it
        either — the builder base charges no search fee, so a cancelled search
        costs only the seconds it ran for.

        The battle is recognised by the card row rather than by the matchmaker
        going away, because the two are not the same moment: the screen fades
        out over the battle it is opening, and a frame caught mid-fade has
        neither on it.
        """
        for attempt in range(SEARCH_ATTEMPTS):
            self._tap(NIGHT_FIND)
            opened = time.monotonic()
            deadline = opened + SEARCH_PATIENCE
            while time.monotonic() < deadline:
                time.sleep(SEARCH_POLL)
                if self.should_stop():
                    logger.info("Stop pressed while the matchmaker was still looking")
                    self._tap(SEARCH_CANCEL)
                    return None
                png = self._frame("searching")
                if not searching_opponent(png) and card_groups(png):
                    logger.info(
                        "Matched after %.0fs on search %d", time.monotonic() - opened, attempt + 1
                    )
                    return png
            logger.info("No opponent in %.0fs; cancelling and searching again", SEARCH_PATIENCE)
            self._tap(SEARCH_CANCEL)
            time.sleep(2)
        logger.warning("Nobody was matched in %d searches", SEARCH_ATTEMPTS)
        return None

    def _night_plan(self, frame: bytes) -> NightPlan:
        """The tactic for this opponent: the AI's, or the flat one written down.

        There is no "plan handed in" branch the way the home village has one:
        the builder base has no scout screen to decide on, so a run that wants a
        fixed tactic is a run that wants the flat plan, and that is a file.
        """
        if self.ai is None:
            self._played = plans.night_flat()
            return self._played
        try:
            plan = self.ai.generate_structured(NIGHT_PROMPT, NightPlan, frame, PLAN_TIMEOUT)
        except Exception:
            logger.warning("Night planning failed; falling back to the flat plan", exc_info=True)
            self._played = plans.night_flat()
            return self._played
        self._played = plan
        logger.info(
            "Night plan: from %s, line %s to %s, %d machine point(s), troops %ds behind (%s)",
            plan.deploy_from,
            plan.deploy_start,
            plan.deploy_end,
            len(plan.hero_points),
            plan.troops_after,
            plan.reason,
        )
        return plan

    def _deploy_night(self, frame: bytes, stage: int = 0) -> tuple[list[int], list[int]] | None:
        """Put the whole army down one flank, or None if nothing went down at all.

        The machine cards still alive come back on success — every one of
        them, whether or not the drop read said it landed, because the tap that
        offers an ability is harmless on a card whose unit is not out and the
        read has missed one that was (see the second stage below). That list is
        empty as readily as it is full — a machine the boundary refused is
        still an attack, because the troops went in. **None is the different
        answer**: no card row, no troops on it, or a first stage whose flank
        took neither the machine nor a single troop. Counted as a deployment
        those rounds reported 已進攻並回營 for a battle nothing was played in,
        and `commands.attack` took them for real battles too — toward the
        emulator restart, toward the loot cart, and past the barracks wait.

        **The second stage opens on a countdown with the surviving machine's
        card preselected**, so `stage` is what says whether to drop it the
        usual way. Measured on four recorded second stages the card comes back
        drawn at the selected width with a white border, and the machine is on
        the field by the time the row is read — the loop's own camera drag
        sends a selected card along the swipe. Dropping it again there was five
        taps on what is by then its ability button, five spots the game
        answered with 請選擇其他兵種, and `field_units` reading a damaged
        health bar — darker green, then yellow to red as the health falls — as
        no bar at all: `never landed`, some fifteen seconds of retries, the
        troops held behind a machine already fighting. And until `card_groups`
        could read a selected card at all, the card was missing from the row on
        every one of those openings, so the machine was offered no ability for
        the whole stage.

        Everything here is the home village's own machinery — the camera, the
        boundary fit, the passes — with the two things the builder
        base does not have taken out. There are no spells to hold back and no
        ability moment to schedule, so what is left after the drops is the
        clock-free part of an attack: the army goes down and the machine is
        offered its ability until the battle ends.

        Which cards are troops is read rather than positional. The home village
        puts its heroes after the troops and the builder base puts its machine
        first, so a group index would be wrong in one of the two — but the `xN`
        corner means the same thing in both, and only troops carry one.
        """
        frame = self._settle_camera(self._settle_zoom(frame))
        groups = card_groups(frame)
        if not groups:
            logger.warning("No cards found on the battle row; nothing to deploy")
            return None
        slots = [slot for group in groups for slot in group]
        troops = counted_cards(frame, slots)
        # **A machine that died in the stage before cannot be sent out again**,
        # and that is a real state rather than a corner case: the second stage
        # opens with whatever survived, so a machine that tanked the first one
        # is usually gone. Its card stays on the row, greyed, and `_drop_singles`
        # would spend its whole ladder of spots on it — five taps, each with a
        # settle and a capture — before reporting that it took nothing.
        #
        # `live_cards` is what says so, and this is the one thing it measures
        # *well* in this village: a card there greys when the unit it put out
        # dies, which is exactly the question here. It is the same reading that
        # makes it useless for "is this card empty", where what is wanted is
        # whether anything is left to deploy rather than whether it is dead.
        machines = live_cards(frame, [slot for slot in slots if slot not in troops])
        logger.info("%d troop card(s), %d machine card(s) still alive", len(troops), len(machines))
        if not troops:
            logger.warning("Every card on the row reads as a machine; nothing to spread")
            return None
        plan = self._night_plan(frame)
        anchors = self._flank(frame, plan)
        pushed = 0
        # **The machine goes in ahead of the troops, which is the other way
        # round from the home village.** There the siege machine opens the path
        # and the heroes follow the army in; here the machine *is* the army's
        # cover, so it lands first and the troops follow once it has walked far
        # enough to be taking the fire. How long that is comes off the plan
        # rather than out of a constant, for the reason every other clock in
        # this project moved onto one: it is a question about the base in the
        # frame, and only something looking at the base can answer it.
        #
        # Nothing has been dropped before this, so the machine is the first
        # thing the line is tested by — which is what `_drop_singles` is already
        # for: it reads the card afterwards and offers another spot to whatever
        # the game refused.
        line = deploy_line(LINE_POINTS, *push_line(anchors, pushed, self._middle))
        # Skipped outright rather than called with an empty row: `_drop_singles`
        # pays a settle and a capture before it reads what landed, and a stage
        # with no machine to send has nothing for either of them to say. The head
        # start goes with it, because that is the machine's and nobody else's.
        out: list[int] = []
        spots = self._spots(plan.hero_points, machines)
        if machines and stage == 0:
            out, _ = self._drop_singles(machines, line, "machine", spots)
        elif machines:
            # The card comes back preselected, so a tap on the field is what
            # sends it; a tap on the card itself would only deselect it.
            logger.info("The machine came through from the stage before, preselected; sending it")
            self._send_selected(
                machines[0],
                [spots.get(machines[0], line[len(line) // 2]), *single_spots(line, self._middle)],
            )
            out = list(machines)
        if machines:
            # Every live card, not only the ones read as landed: the offer is
            # harmless on a card still holding its unit, and the read misses a
            # machine that is out whenever its health bar is no longer green.
            self._hold(plan.troops_after, machines)
        spread = self._spread_night(troops, anchors, pushed)
        # A machine that went down is an attack even if the troops behind it
        # were refused, so only a round with neither on the field answers None.
        if spread is None and not out:
            return None
        return machines, troops

    def _offer_ability(self, machines: list[int]) -> None:
        """One tap on each machine card, which the game takes if the ability is ready."""
        if machines:
            self.adb.tap_many([(slot, CARD_ROW_Y) for slot in machines], self.display)

    def _hold(self, seconds: int, machines: list[int]) -> None:
        """Wait out the head start the plan gave the machine, using it rather than sleeping.

        The seconds are the plan's; what happens inside them is not a delay of
        this loop's own invention but the same offer `_wait_out_night` makes for
        the rest of the battle. A machine that lands with its ability charged
        should be spending it while it walks.
        """
        if seconds <= 0:
            return
        logger.info("Holding the troops %ds while the machine goes in", seconds)
        self._offer_for(seconds, machines)

    def _offer_for(self, seconds: float, machines: list[int]) -> None:
        """Offer every machine its ability once an `ABILITY_TAP` for this long, blind."""
        for _ in range(max(1, round(seconds / ABILITY_TAP))):
            self._offer_ability(machines)
            time.sleep(ABILITY_TAP)

    def _send_selected(self, card: int, spots: list[tuple[int, int]]) -> bool:
        """Tap the field until a preselected card is no longer selected; True once it went.

        The card's own white border is the verdict, not the health bar: a
        machine carried into the second stage has whatever health it kept, and
        `field_units` reads a bar that is no longer green as no bar at all.
        One blind tap was what a live round paid for — the plan's spot fell
        outside the smaller second base's ground, the game swallowed it, and
        the machine sat selected in its card for the whole stage while every
        ability offer only toggled the selection.
        """
        for spot in spots[:DEPLOY_ATTEMPTS]:
            self.adb.tap(*spot, self.display)
            time.sleep(DROP_SETTLE)
            if card not in selected_cards(self._frame("sent"), [card]):
                return True
            logger.info("The preselected machine is still in its card after %s", spot)
        logger.warning("The preselected machine never left its card")
        return False

    def _spread_night(
        self, troops: list[int], anchors: tuple[tuple[int, int], ...], pushed: int
    ) -> list[tuple[int, int]] | None:
        """Empty the troop cards along the flank; returns the line they went down on.

        **`live_cards` does not hold in the builder base, and that is what this
        exists for.** In the home village a card goes greyscale the moment it is
        empty, which is how `_spread_troops` knows to stop. Here it greys when
        the troops it put out *die*: measured over one recorded attack, all five
        cards read `0x` on the frame after the first pass while every one of
        them was still in colour, and they went grey one at a time over the next
        thirty seconds as the fighting killed them.

        Taken for "still holding something" that cost four more passes tapping
        into empty cards, and worse — each of those passes drained nothing, and
        `_spread_troops` answers a pass that drained nothing by pushing the
        flank further out. So the flank walked 280 px away from a village the
        army had already reached.

        What is used instead is the pass itself, and **whether anything has
        landed yet is what says which of two things a barren pass means.** A
        pass taps `DROPS_PER_PASS` per card against a card holding about four,
        so once one has drained the row empties in that pass and a barren one
        after it is an empty row. Before anything has drained, a barren pass is
        a line the base has grown over, and pushing it out is what the probing
        used to buy without spending three troops to do it.

        Reading the pass *index* instead of that is what an earlier version did,
        and it gave the flank exactly one push: the pass after the pushed one
        has an index of 1, so it read as an empty row and returned with the
        whole army still in its cards. None comes back where nothing ever
        landed, because a round that deployed no troops is not a round that
        fought and `commands.attack` counts it.
        """
        line = deploy_line(LINE_POINTS, *push_line(anchors, pushed, self._middle))
        shot = self._frame("before-pass")
        landed = False
        for index in range(DEPLOY_PASSES):
            for card, slot in enumerate(troops):
                drops = drop_points(line, index * len(troops) + card, DROPS_PER_PASS)
                self.adb.tap_many([(slot, CARD_ROW_Y), *drops], self.display)
            time.sleep(DROP_SETTLE)
            before, shot = shot, self._frame("pass")
            drained = card_drained(before, shot, troops)
            logger.info("Pass %d drained %d of %d card(s)", index + 1, len(drained), len(troops))
            if drained:
                landed = True
                continue
            if landed or pushed + 1 >= DEPLOY_ATTEMPTS:
                break
            pushed += 1
            logger.info("Nothing has landed yet; flank pushed out to %d", pushed)
            line = deploy_line(LINE_POINTS, *push_line(anchors, pushed, self._middle))
        if not landed:
            logger.warning("The flank took nothing at any push; the troops stay in their cards")
        return line if landed else None

    def _wait_out_night(self, machines: list[int], troops: list[int]) -> None:
        """Sit through the battle, offering every machine its ability every second.

        Blind rather than read, and that is the mode's own shape rather than a
        shortcut: the ability recharges for the whole battle, so there is no
        single moment worth finding and a tap the game is not ready for costs
        one `input` call. A card whose unit never made it onto the field answers
        the same tap by selecting itself, which does nothing at all. Only the
        capture is paced, at `ABILITY_POLL`, because that is what costs.
        """
        deadline = time.monotonic() + BATTLE_TIMEOUT
        shot = self._frame("night-battle")
        while time.monotonic() < deadline:
            if battle_over(shot):
                return
            # **A stage can end without a result screen**, and waiting for one
            # cost a whole second stage: the game opened it with a fresh timer
            # and the surviving troops back in their cards, `battle_over` stayed
            # False because a live battle is not a result, and the loop spent
            # four minutes tapping a dead machine's card before timing out.
            #
            # What marks the boundary is the row being repainted — the counts go
            # from the `0x` a spent row shows back to what survived. Measured
            # over the recorded stage-two battle, the corners moved on exactly
            # two frames, once into the second stage and once into the result,
            # against none at all in between; the run without a second stage
            # moved them once. `_next_stage` is what tells the two apart.
            following = self._frame("night-battle")
            if troops and card_drained(shot, following, troops):
                logger.info("The card row was repainted; this stage is over")
                return
            shot = following
            self._offer_for(ABILITY_POLL, machines)
        logger.warning("The battle never ended; leaving it to the result screen")

    def _next_stage(self) -> bytes | None:
        """Leave the stage that just ended, and say whether the game opened another.

        **Nothing here predicts the second stage.** The game offers it after a
        first attack takes the whole base, sending what survived against the
        opponent's other one, and the condition for that is the sort of rule
        that changes between releases. So this asks the only question that
        cannot go stale: are we back on a village. Anything else with a card row
        on it is another stage to play.
        """
        self._leave_result()
        png = self._frame("after-stage")
        if current_world(png) is not None:
            return None
        if card_groups(png):
            logger.info("A second stage opened; the army goes down again")
            return png
        return None

    def _run_night(self) -> AttackReport:
        """One builder base attack, start to finish.

        No thresholds and no skipping: the matchmaker picks the opponent and
        there is nothing to weigh, because the attack is free and both outcomes
        pay — a win brings home more gold and a loss more elixir. The storage
        limits still apply, against this village's own ceilings; see
        `_stood_down`.
        """
        self._panned = (0, 0)
        self._played = None
        home = self._open_attack_menu()
        if home is None:
            logger.warning(
                "The builder base's attack dialog never opened: %s",
                self._stuck or "the game is not on the builder base",
            )
            return AttackReport(
                world="night",
                message=self._stuck or "畫面不在建築大師基地，沒有開啟攻擊選單就停手",
            )
        if (full := self._stood_down(home)) is not None:
            return full
        battle = self._find_opponent()
        if battle is None:
            return AttackReport(world="night", message="等不到對手，已放棄這一輪搜尋")
        played = 0
        for stage in range(NIGHT_PHASES):
            deployed = self._deploy_night(battle, stage)
            if deployed is None:
                break
            played += 1
            self._wait_out_night(*deployed)
            following = self._next_stage()
            if following is None:
                break
            battle = following
        return AttackReport(
            world="night",
            phases=played,
            message=f"已進攻並回營，共出兵 {played} 次" if played else "沒有成功部署任何部隊",
        )

    def run(self) -> AttackReport:
        """One attack on whichever village this runner was pointed at."""
        if self.world == "night":
            logger.info("Builder base attack run starts")
            return self._run_night()
        logger.info("Attack run starts, thresholds=%s", self.thresholds.model_dump())
        return self._run_day()

    def _run_day(self) -> AttackReport:
        """One home village attack: scout opponents, weigh their loot, fight one."""
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
            logger.warning(
                "The attack menu did not open: %s",
                self._stuck or "the game is not on the home village",
            )
            return AttackReport(message=self._stuck or "畫面不在主村，沒有開啟攻擊選單就停手")
        # Before the search fee, like the army check below.
        if (full := self._stood_down(home)) is not None:
            return full
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
