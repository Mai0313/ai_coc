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
from collections.abc import Callable, Sequence

from pydantic import BaseModel, PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    LootOffer,
    ScoutView,
    AttackPlan,
    StockLimits,
    AttackReport,
    AttackTimings,
    DisplayTarget,
    LootThresholds,
)
from ai_coc.prompts import PROMPTS
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import AdbController
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
    army_strength,
    counted_cards,
    attack_menu_open,
    idle_disconnected,
)
from ai_coc.parsers.boundary import DEPLOY_BOUND, fitted_line, village_box

logger = logging.getLogger(__name__)

# Every coordinate is the 1600x900 layout, the one `_apply_agent_action` assumes.
HOME_ATTACK = (105, 830)
FIND_MATCH = (272, 665)
ARMY_ATTACK = (1411, 803)
NEXT_TARGET = (1450, 630)
END_BATTLE = (118, 670)
RETURN_HOME = (798, 768)

# Card positions come from the frame rather than a constant, because the row
# depends on the army. Cards are then emptied in small passes: `live_cards`
# reports which ones are done, so a card is never tapped more than a few times
# past its last troop, and a card holding one troop is not tapped a dozen times.
CARD_ROW_Y = 800
DROPS_PER_PASS = 4
DEPLOY_PASSES = 10

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

# A one-off drop needs far longer between selecting the card and placing it than
# a troop does. A troop card is tapped repeatedly, so a swallowed tap costs one
# troop; a hero or siege machine gets one attempt.
SINGLE_DROP_DELAY = 0.6
# Selecting a spell opens a radius indicator that has to be up before a
# placement lands: measured live, 0.6 s was still swallowed where 1.5 s went
# through. Only the selection is slow, though — the card stays selected while it
# still holds something, so the rest of a card's spells follow as a fast burst.
# Casting them one full select-and-place at a time took 18 s, by which point a
# rage cast first had nearly run its 18 s out before the troops even landed.
SPELL_SELECT_DELAY = 1.2
SPELL_PLACE_GAP = 0.3

# A card takes a moment to show that it lost something: the digits redraw and a
# hero's health bar fades in about a second behind the drop. Measured across the
# recorded runs, a card read straight after the tap still showed the old corner
# and the bar was not there yet, which read as a drop the game had refused —
# and on a hero that is not a free mistake, because the retry taps its card
# again, which is its ability.
DROP_SETTLE = 1.5

# A village whose boundary reaches past the chosen line refuses every drop, and
# layouts vary far more than one line can allow for. So the line is tested with a
# single troop and pushed further from the middle until the game accepts it,
# staying inside the playfield the UI leaves free.
SCREEN_CENTRE = (800, 400)
PUSH_STEP = 70
DEPLOY_ATTEMPTS = 5
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
CAMERA_GRIP = (800, 400)
CAMERA_DRAG_MS = 350
CAMERA_SETTLE = 1.5

# The scout countdown is 30 seconds; this polls a second at a time and leaves
# room for a slow frame rather than sitting through a whole battle.
COUNTDOWN_ATTEMPTS = 45
# Consecutive unreadable frames that end the wait early. One is a moment of
# animation over the panel; three in a row is a screen this cannot read.
UNREADABLE_ATTEMPTS = 3

BATTLE_TIMEOUT = 240
# Two things routinely cover the home village between runs: a building panel
# left open by a stray tap, which back closes, and the idle-disconnect dialog,
# which only a relogin clears and which a loop that waits for barracks will
# certainly meet.
RELOGIN_BUTTON = (485, 528)
RELOGIN_WAIT = 14
HOME_ATTEMPTS = 3
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


def push_out(point: tuple[int, int], steps: int) -> tuple[int, int]:
    """Move a drop further from the middle, kept on the map, the screen and off the UI.

    The map is a diamond, so clamping to the playfield rectangle alone pushed
    drops into corners that are not on the map at all: a hero refused four times
    ended up at (30, 175), where the map only spans x 596 to 1004, and the game
    had nothing to accept. `DEPLOY_BOUND.clamp` is what keeps a push heading
    outwards from leaving the board.
    """
    dx, dy = point[0] - SCREEN_CENTRE[0], point[1] - SCREEN_CENTRE[1]
    span = max((dx * dx + dy * dy) ** 0.5, 1.0)
    return clear_of_controls(
        DEPLOY_BOUND.clamp((
            round(point[0] + dx / span * PUSH_STEP * steps),
            round(point[1] + dy / span * PUSH_STEP * steps),
        ))
    )


def push_line(anchors: tuple[tuple[int, int], ...], steps: int) -> list[tuple[int, int]]:
    """Every anchor of a flank moved the same distance further from the middle."""
    return [push_out(anchor, steps) for anchor in anchors]


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


def drop_points(line: list[tuple[int, int]], seed: int) -> list[tuple[int, int]]:
    """One pass worth of drops, spread over the whole line rather than bunched.

    `seed` rotates the starting point per card and per pass, so a card holding
    a single troop does not put it on the same spot every other card started on.
    """
    step = max(len(line) // DROPS_PER_PASS, 1)
    return [line[(seed + i * step) % len(line)] for i in range(DROPS_PER_PASS)]


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
    abilities: AttackTimings = AttackTimings()
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
    # Whatever `_plan` settled on, kept so a run can be written down and replayed.
    _played: AttackPlan | None = PrivateAttr(default=None)

    @property
    def played(self) -> AttackPlan | None:
        """The plan this run actually used, once one has been settled on."""
        return self._played

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

    def _battle_view(self, label: str) -> ScoutView | None:
        """The loot panel mid-battle, kept here so every caller's reading counts.

        Three places poll it to ask whether the battle is still on, and all three
        readings are evidence of the same thing, so the last one lives on the
        runner rather than in whichever method happened to take it. A battle that
        ends quickly is what this is for: the loot had visibly fallen while the
        abilities were firing, but `_wait_out_battle` trusted only its own
        ten-second poll, met the result screen on the first one, and reported a
        village it had three-starred as one where nothing was ever deployed.
        """
        view = read_scout(self._frame(label))
        if view is not None:
            self._seen = view.loot
        return view

    def _open_attack_menu(self) -> bytes | None:
        """Get to the attack menu, clearing whatever is covering the village.

        The home village frame comes back with it. That is the one screen the
        storage bars are on, and the frame is already being taken here to check
        for the idle dialog, so reading the storages costs no extra capture.
        """
        for _ in range(HOME_ATTEMPTS):
            home = self._frame("home")
            if idle_disconnected(home):
                logger.info("Idle-disconnect dialog is up; logging back in")
                self._tap(RELOGIN_BUTTON)
                time.sleep(RELOGIN_WAIT)
                continue
            if battle_over(home):
                logger.info("The last battle's result screen is still up; leaving it")
                self._leave_result()
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
            )
        except Exception:
            logger.warning("Attack planning failed; falling back to the flat plan", exc_info=True)
            self._played = plans.flat()
            return self._played
        # Its own timings are dropped. `timings` is on the model, so it reaches
        # the schema and the model will happily fill it in, but a still frame
        # says nothing about how long this machine takes to put an army down —
        # and a number invented there would silently replace the schedule the
        # user set in 英雄大招時機. Only a written plan gets to carry timings.
        plan = plan.model_copy(update={"timings": None})
        self._played = plan
        logger.info(
            "Plan: from %s, line %s to %s, %d rage point(s), %d freeze point(s), heroes=%s (%s)",
            plan.deploy_from,
            plan.deploy_start,
            plan.deploy_end,
            len(plan.rage_points),
            len(plan.freeze_points),
            plan.heroes,
            plan.reason,
        )
        return plan

    def _scout(self, timeout: float = 30) -> tuple[ScoutView, bytes] | None:
        """Poll until an opponent is on screen; 正在搜尋對手 reads as nothing at all.

        The frame comes back with the view because `card_groups` only holds on a
        full card row, and this is the last moment one is guaranteed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            png = self._frame("scout")
            view = read_scout(png)
            if view:
                return view, png
            time.sleep(1)
        return None

    def _usable_line(self, card: int, anchors: tuple[tuple[int, int], ...]) -> int | None:
        """How far out the flank has to be pushed before the game accepts drops on it.

        Troops are dropped as probes, because a village whose boundary reaches
        past the line swallows them. Both ends go in as well as the middle: a
        flank can sit outside the boundary at its centre and inside it at the
        tips, which would silently lose every troop aimed there. A probe that
        takes nothing pushes the line further out; None means even the playfield
        edge was inside the boundary.

        The card is what says whether the probe landed. Reading the game's red
        warning instead never worked: the tap is as often swallowed without one,
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
            line = deploy_line(LINE_POINTS, *push_line(anchors, attempt))
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
        line = deploy_line(LINE_POINTS, *push_line(anchors, pushed))
        shot = self._frame("before-pass")
        for index in range(DEPLOY_PASSES):
            for card, x in enumerate(remaining):
                drops = drop_points(line, index * len(remaining) + card)
                self.adb.tap_many([(x, CARD_ROW_Y), *drops], self.display)
            time.sleep(DROP_SETTLE)
            before, shot = shot, self._frame("pass")
            if not card_drained(before, shot, remaining) and pushed + 1 < DEPLOY_ATTEMPTS:
                pushed += 1
                logger.info("The whole pass landed nothing; flank pushed out to %d", pushed)
                line = deploy_line(LINE_POINTS, *push_line(anchors, pushed))
            remaining = live_cards(shot, remaining)
            logger.info("%d troop card(s) still hold something", len(remaining))
            if not remaining:
                break
        return line

    def _drop_single(self, card: int, point: tuple[int, int]) -> bool:
        """One unit off one card onto one spot; False when the game did not take it.

        The card is the evidence and the screen is not. A hero keeps its card
        once it is down — the card becomes the ability button — so what says it
        landed is the health bar the game draws over it; the siege machine has
        no bar and instead greys out, which `live_cards` already reads.

        This used to answer the red warning banner, which cannot say it: a tap
        inside the boundary is as often swallowed in silence, so every hero came
        back "landed" whether it went down or not. One recorded run reported
        four of four while the fourth never left its card.

        Getting it wrong the other way is not free either, which is what the
        settle is for: a second tap on a hero already on the field is its
        ability, so a drop wrongly called refused burns the cloak or the tome and
        leaves `_fire_abilities` tapping a card that has nothing left to give.
        """
        before = self._frame("before-drop")
        self.adb.tap_many([(card, CARD_ROW_Y), point], self.display, gap=SINGLE_DROP_DELAY)
        time.sleep(DROP_SETTLE)
        after = self._frame("dropped")
        return bool(field_units(after, [card])) or (
            bool(live_cards(before, [card])) and not live_cards(after, [card])
        )

    def _drop_singles(self, cards: list[int], point: tuple[int, int], what: str) -> list[int]:
        """Empty the one-off cards onto the same spot, pushing it out when nothing lands.

        Every card gets its own attempts, starting from wherever the last one
        worked. Sharing one budget across the row meant a spot that took five
        pushes to work for the first hero left the other three never tapped at
        all, and a hero that stays in its card is silent — the troops already on
        the field keep the loot moving, so `_wait_out_battle` sees a battle going
        fine and the report says the attack went in.
        """
        landed: list[int] = []
        pushed = 0
        for card in cards:
            for attempt in range(pushed, DEPLOY_ATTEMPTS):
                if self._drop_single(card, push_out(point, attempt)):
                    landed.append(card)
                    # The next card starts where this one worked rather than back
                    # on the spot already known to take nothing.
                    pushed = attempt
                    break
                logger.info("The %s card at %d took nothing at push %d", what, card, attempt)
            else:
                logger.warning("The %s card at %d never landed; its unit stays put", what, card)
        logger.info(
            "%d of %d %s card(s) landed at %s",
            len(landed),
            len(cards),
            what,
            push_out(point, pushed),
        )
        return landed

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

    def _settle_camera(self, frame: bytes) -> bytes:
        """Put the village in the middle of the screen, and hand back what it looks like.

        Everything downstream reads the camera without being able to check it:
        `push_out` moves a drop away from the screen centre, the preset flanks are
        screen coordinates, and the spell grid is spaced off the middle. The game
        does open every attack centred — measured across nine battles it was never
        more than 35 px out — but that is the sort of fact that is true until it
        is not, and a camera left anywhere else puts the whole army somewhere
        nobody asked for. This runs before the plan is drawn so the planner is
        looking at the same screen the drops will land on.
        """
        for _ in range(CAMERA_ATTEMPTS):
            box = village_box(frame)
            if box is None:
                logger.info("The village will not measure; the camera is left where it is")
                return frame
            middle = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            drift = (SCREEN_CENTRE[0] - middle[0], SCREEN_CENTRE[1] - middle[1])
            if max(abs(drift[0]), abs(drift[1])) <= CAMERA_TOLERANCE:
                logger.info("Village sits at %s, %s off the middle", middle, drift)
                return frame
            logger.info("Village sits at %s; dragging the camera by %s", middle, drift)
            self.adb.swipe(
                CAMERA_GRIP,
                clear_of_controls((CAMERA_GRIP[0] + drift[0], CAMERA_GRIP[1] + drift[1])),
                CAMERA_DRAG_MS,
                self.display,
            )
            time.sleep(CAMERA_SETTLE)
            frame = self._frame("camera")
        return frame

    def _deploy(self, frame: bytes) -> None:
        """Spread the main troops along one flank; everything else drops once, mid-line."""
        frame = self._settle_camera(frame)
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
        # only the followers reach `_fire_abilities`, not one of their abilities
        # was ever fired.
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
        # tap the game ignores raises no refusal banner either, so probing then
        # reads every drop as accepted and the whole army is deployed into
        # nothing. With Gemini in the loop the planning call happens to outlast
        # the countdown, which is what has been hiding this; without a key `_plan`
        # returns at once and the run would deploy into the countdown every time.
        battle = self._wait_for_battle()
        for preset in deploy_candidates(plan):
            # The boundary the game draws beats a flank drawn for a village that
            # does not exist, and fitting to it is what saves probing outwards one
            # refused troop at a time. It is still probed once before it is used.
            anchors = (fitted_line(battle, *preset) if battle else None) or preset
            pushed = self._usable_line(troops[0], anchors)
            if pushed is not None:
                break
        else:
            logger.warning("Every flank was refused; the boundary reaches past the playfield")
            return
        line = deploy_line(LINE_POINTS, *push_line(anchors, pushed))
        middle = line[len(line) // 2]
        planned = tuple(point.pixels() for point in plan.rage_points) if plan else ()
        # A plan can name fewer spots than the army carries rages, and `_cast`
        # cycles back over its targets — which would stack two rages on one spot
        # and waste one. The fixed grid fills the tail so each gets its own.
        rage_path = planned + tuple(point for point in RAGE_PATH if point not in planned)
        # Every freeze used to stack on one spot, which is one spell's worth of
        # effect for the whole cargo. A plan names one per bottle instead.
        freeze_targets = tuple(point.pixels() for point in plan.freeze_points) if plan else ()
        freeze_targets = freeze_targets or (FREEZE_TARGET,)
        # The siege machine opens the path, then the troops go down, and only
        # then does rage land on the route ahead of them.
        #
        # Rage used to be cast first, on the reasoning that the troops should be
        # inside it the whole way in. That holds only if they land at once, and
        # they do not: measured across three battles, the last troop is down 30
        # seconds after the first spell and the heroes 39, while a rage lasts 18.
        # Cast first, it had expired before most of the army was on the field.
        # Cast here it starts as they begin walking, and the heroes join them
        # inside it. Freeze is the one thing still held back, and it now waits on
        # its own clock rather than on the last hero's.
        self._drop_singles(vanguard, middle, "siege")
        line = self._spread_troops(troops, anchors, pushed)
        self._cast(rages, rage_path, frame)
        # Recomputed, not reused: the flank moves while the troops go down, and
        # a hero sent to the pre-push midpoint is sent somewhere already refused.
        middle = line[len(line) // 2]
        # Only the heroes that actually went down get an ability. A hero still in
        # its card answers an ability tap by deploying instead, with nothing
        # around it and no ability fired.
        down = self._drop_singles(followers, middle, "hero")
        kinds = list(plan.heroes) if plan else []
        kinds += ["unknown"] * (len(followers) - len(kinds))
        timings = plan.timings if plan and plan.timings else self.abilities
        moves = [
            (
                timings.seconds(kinds[i]),
                f"ability on the card at {x}",
                partial(self._tap, (x, CARD_ROW_Y)),
            )
            for i, x in enumerate(followers)
            if x in down
        ]
        if freezes:
            moves.append((
                timings.freeze,
                f"{len(freezes)} freeze card(s)",
                partial(self._cast, freezes, freeze_targets, frame),
            ))
        self._run_schedule(moves)

    def _run_schedule(self, moves: Sequence[tuple[int, str, Callable[[], None]]]) -> None:
        """Everything that waits on the clock once the army is down, in time order.

        One list rather than the abilities and then the freeze, because ordering
        by position in the code made the freeze wait out the slowest hero on the
        field: with a champion at 45 seconds it landed a minute and a half into a
        three-minute battle, long after the defences it was meant to stop had
        done their work. Timing is per hero rather than per card slot for the
        same reason it always was — an upgrading hero has no card, so every slot
        after it shifts — and a queen wants her cloak almost immediately where a
        warden's tome is worth holding until the push is deep enough to save.

        Some card slots overlap the result screen's 回營 button, so nothing here
        runs unless the battle is genuinely still on.
        """
        started = time.monotonic()
        for delay, what, act in sorted(moves, key=lambda move: move[0]):
            remaining = started + delay - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if self._battle_view("scheduled") is None:
                logger.info("Battle ended with the %s still to come", what)
                return
            act()
            logger.info("Played the %s, %ds after the army was down", what, delay)

    def _cast(self, cards: list[int], targets: tuple[tuple[int, int], ...], frame: bytes) -> None:
        """Empty each spell card over `targets`, and say so when a card would not go.

        One slow selection per card, then the placements as a burst, since the
        card stays selected while it still holds something. How many to tap
        comes from the card's own `xN` plus one for slack, falling back to the
        whole target list when the artwork makes the count unreadable.

        A cargo that never leaves the card is the quiet half of the same bug the
        heroes had: nothing downstream notices, and the report says the attack
        went in. The card is asked, and a card that did not move is offered the
        run once more — measured live, a spell placed straight after another was
        the one that got swallowed, so a second selection is usually all it wants.
        """
        for index, x in enumerate(cards):
            count = card_count(frame, x)
            cast_count = count + 1 if count else len(targets)
            cells = [targets[(index + i) % len(targets)] for i in range(cast_count)]
            for attempt in range(2):
                before = self._frame("before-cast")
                self._tap((x, CARD_ROW_Y))
                time.sleep(SPELL_SELECT_DELAY)
                self.adb.tap_many(cells, self.display, gap=SPELL_PLACE_GAP)
                time.sleep(DROP_SETTLE)
                after = self._frame("cast")
                if card_drained(before, after, [x]) or not live_cards(after, [x]):
                    logger.info("Spell card at %d held %s, tapped %d", x, count, cast_count)
                    break
                logger.info("Nothing left the spell card at %d on attempt %d", x, attempt + 1)
            else:
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
        """
        deadline = time.monotonic() + BATTLE_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(10)
            # The result screen is the first one with no loot panel on it.
            if self._battle_view("battle") is None:
                break
        self._leave_result()
        return self._seen is not None and self._seen != opening

    def run(self) -> AttackReport:
        logger.info("Attack run starts, thresholds=%s", self.thresholds.model_dump())
        home = self._open_attack_menu()
        if home is None:
            logger.warning("The attack menu did not open; the game is not on the home village")
            return AttackReport(message="畫面不在主村，沒有開啟攻擊選單就停手")
        # Before the search fee, like the army check below: a full storage means
        # the loot this run wins is thrown away when it is collected. An
        # unreadable frame stops nothing, because a village that cannot be read
        # is not evidence of a full one.
        stock = read_stock(home)
        if stock and (full := self.stock.reached(stock)):
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
                return AttackReport(skipped=skipped, message="等不到對手畫面，已放棄這一輪搜尋")
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
                if not took:
                    logger.warning("The whole battle passed without any loot moving")
                return AttackReport(
                    skipped=skipped,
                    attacked=view.loot,
                    message=f"{reason}，已進攻並回營"
                    if took
                    else f"{reason}，但整場戰利品沒有變化，部隊可能沒有成功部署",
                )
            if stopping or skipped >= self.max_skips:
                self._tap(END_BATTLE)
                return AttackReport(
                    skipped=skipped,
                    message="已停止，未開打就離開搜尋"
                    if stopping
                    else f"連續跳過 {skipped} 個對手都未達門檻，已結束搜尋",
                )
            skipped += 1
            self._tap(NEXT_TARGET)
