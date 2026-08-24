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
from collections.abc import Callable

from pydantic import BaseModel, PrivateAttr

from ai_coc.models import (
    HeroKind,
    LootOffer,
    ScoutView,
    AttackPlan,
    HeroTimings,
    StockLimits,
    AttackReport,
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
    card_groups,
    freeze_cards,
    army_strength,
    counted_cards,
    deploy_refused,
    attack_menu_open,
    idle_disconnected,
)
from ai_coc.parsers.boundary import DEPLOY_BOUND

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
# The refusal banner lingers, so it has to clear before the next probe is read.
REFUSAL_CLEAR_DELAY = 3
BANNER_CLEAR_ATTEMPTS = 3
# A planned line has to run past the village, not merely start and end clear of
# it. `push_out` moves a point away from SCREEN_CENTRE, so a point near the
# centre has almost no direction to be pushed in and a line drawn straight
# across the village is refused at every probe, which loses the whole army.
# The four preset flanks all sit about 410 px out at their midpoint; this keeps
# a planned line in the same band and falls back to a flank when it is not.
MIN_LINE_RADIUS = 300

BATTLE_TIMEOUT = 240
# Two things routinely cover the home village between runs: a building panel
# left open by a stray tap, which back closes, and the idle-disconnect dialog,
# which only a relogin clears and which a loop that waits for barracks will
# certainly meet.
RELOGIN_BUTTON = (485, 528)
RELOGIN_WAIT = 14
HOME_ATTEMPTS = 3
HOME_RETRY_DELAY = 3
# The army screen comes up before the search fee is charged, so a half-trained
# army can still back out for free rather than paying to attack with nothing.
MIN_ARMY_RATIO = 0.9


def clear_of_controls(point: tuple[int, int]) -> tuple[int, int]:
    """Lift a drop off the 放棄 button, which the playfield reaches over.

    Lifted rather than moved aside: a point ends up in that corner because it is
    heading away from the village, and shifting it right would send it back
    towards the boundary it is trying to clear.
    """
    x, y = point
    return (x, min(y, ABANDON_BUTTON[1])) if x < ABANDON_BUTTON[0] else point


def deploy_line(
    count: int, start: tuple[int, int] = DEPLOY_START, end: tuple[int, int] = DEPLOY_END
) -> list[tuple[int, int]]:
    """Evenly spaced positions along a flank, from one end to the other.

    Every point is cleared of the button, not only the two ends the caller
    pushed: the line is a chord, so both ends can sit outside that corner while
    the span between them cuts straight across it.
    """
    (x0, y0), (x1, y1) = start, end
    step = max(count - 1, 1)
    return [
        clear_of_controls((round(x0 + (x1 - x0) * i / step), round(y0 + (y1 - y0) * i / step)))
        for i in range(count)
    ]


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
    left, top, right, bottom = PLAYFIELD
    on_map = DEPLOY_BOUND.clamp((
        round(point[0] + dx / span * PUSH_STEP * steps),
        round(point[1] + dy / span * PUSH_STEP * steps),
    ))
    return clear_of_controls((min(max(on_map[0], left), right), min(max(on_map[1], top), bottom)))


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
    abilities: HeroTimings = HeroTimings()
    max_skips: int = 20
    ai: GeminiClient | None = None
    # Checked between opponents only. A battle already under way is played out:
    # abandoning one mid-deploy would leave the army on the field and the game
    # on a screen the next run does not know how to get home from.
    should_stop: Callable[[], bool] = lambda: False
    # Where to keep every frame the loop reads, for a run being studied afterwards.
    frame_dir: Path | None = None

    _captures: int = PrivateAttr(default=0)
    _seen: LootOffer | None = PrivateAttr(default=None)

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

    def _plan(self, frame: bytes, rage_count: int, freeze_count: int) -> AttackPlan | None:
        if self.ai is None:
            return None
        try:
            plan = self.ai.generate_structured(
                PLAN_PROMPT.format(rage_count=rage_count, freeze_count=freeze_count),
                AttackPlan,
                frame,
            )
        except Exception:
            logger.warning("Attack planning failed; using the fixed flank", exc_info=True)
            return None
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

    def _usable_line(self, card: int, start: tuple[int, int], end: tuple[int, int]) -> int | None:
        """How far out the flank has to be pushed before the game accepts drops on it.

        Troops are dropped as probes, because a village whose boundary reaches
        past the line refuses them and says so on screen. Both ends go in as
        well as the middle: a flank can sit outside the boundary at its centre
        and inside it at the tips, which would silently lose every troop aimed
        there. Each refusal pushes the line further out; None means even the
        playfield edge was inside the boundary.

        The step count is what comes back rather than the line, because everyone
        downstream keeps pushing from it and a line cannot say how far out it
        already is.
        """
        for attempt in range(DEPLOY_ATTEMPTS):
            line = deploy_line(LINE_POINTS, push_out(start, attempt), push_out(end, attempt))
            probes = [line[0], line[len(line) // 2], line[-1]]
            self.adb.tap_many([(card, CARD_ROW_Y), *probes], self.display)
            if not deploy_refused(self._frame("probe")):
                logger.info("Deploying along %s-%s, pushed out %d step(s)", start, end, attempt)
                return attempt
            logger.info("Drop refused inside the boundary; pushing the flank out")
            time.sleep(REFUSAL_CLEAR_DELAY)
        logger.info("The %s-%s line is refused at every push; trying the next flank", start, end)
        return None

    def _spread_troops(
        self, troops: list[int], start: tuple[int, int], end: tuple[int, int], pushed: int
    ) -> list[tuple[int, int]]:
        """Empty the troop cards along the flank; returns the line ending up in use.

        Probing the ends is not enough on its own: the village is a diamond, so
        the ground around it is not convex and a line whose ends are both clear
        can still cut through it. A refusal means *some* of this pass's drops
        landed inside, so the flank is pushed out for the passes that follow —
        but the cards are still re-read, because the drops that did land count.

        `pushed` carries on from where `_usable_line` left off. Restarting it at
        zero made the first push land back on the line already in use, so a real
        refusal was answered by moving nothing at all.
        """
        remaining = list(troops)
        line = deploy_line(LINE_POINTS, push_out(start, pushed), push_out(end, pushed))
        for index in range(DEPLOY_PASSES):
            for card, x in enumerate(remaining):
                drops = drop_points(line, index * len(remaining) + card)
                self.adb.tap_many([(x, CARD_ROW_Y), *drops], self.display)
            shot = self._frame("pass")
            refused = deploy_refused(shot)
            if refused and pushed + 1 < DEPLOY_ATTEMPTS:
                pushed += 1
                logger.info(
                    "Some drops landed inside the boundary; flank pushed out to %d", pushed
                )
                line = deploy_line(LINE_POINTS, push_out(start, pushed), push_out(end, pushed))
            remaining = live_cards(shot, remaining)
            # The banner outlives the pass that earned it, so without this the
            # next pass reads the same one again and pushes the flank a second
            # time for drops that were never refused.
            if refused:
                time.sleep(REFUSAL_CLEAR_DELAY)
            logger.info("%d troop card(s) still hold something", len(remaining))
            if not remaining:
                break
        return line

    def _drop_single(self, card: int, point: tuple[int, int]) -> bool:
        """One unit off one card onto one spot; False when the game refused the spot.

        The screen has to be clear of an older banner *before* the drop, because
        judging one against a stale banner is not a free mistake here: a hero
        already on the field answers a second tap on its card with its ability,
        so a drop wrongly called refused burns the cloak or the tome on a retry
        and leaves `_fire_abilities` tapping an empty card later.
        """
        for _ in range(BANNER_CLEAR_ATTEMPTS):
            if not deploy_refused(self._frame("banner")):
                break
            time.sleep(REFUSAL_CLEAR_DELAY)
        self.adb.tap_many([(card, CARD_ROW_Y), point], self.display, gap=SINGLE_DROP_DELAY)
        return not deploy_refused(self._frame("dropped"))

    def _drop_singles(self, cards: list[int], point: tuple[int, int], what: str) -> int:
        """Empty the one-off cards onto the same spot, pushing it out when refused.

        The troop line is probed before it is used and pushed again whenever a
        pass is refused; these drops never were, and that is how a hero went
        missing. A refused hero is silent — the troops already on the field keep
        the loot moving, so `_wait_out_battle` sees a battle going fine.
        """
        landed = 0
        pushed = 0
        for card in cards:
            while pushed < DEPLOY_ATTEMPTS and not self._drop_single(
                card, push_out(point, pushed)
            ):
                pushed += 1
                logger.info("A %s drop was refused; the spot is pushed out to %d", what, pushed)
            if pushed < DEPLOY_ATTEMPTS:
                landed += 1
        logger.info(
            "%d of %d %s card(s) landed at %s",
            landed,
            len(cards),
            what,
            push_out(point, min(pushed, DEPLOY_ATTEMPTS - 1)),
        )
        return landed

    def _deploy(self, frame: bytes) -> None:
        """Spread the main troops along one flank; everything else drops once, mid-line."""
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
        for start, end in deploy_candidates(plan):
            pushed = self._usable_line(troops[0], start, end)
            if pushed is not None:
                break
        else:
            logger.warning("Every flank was refused; the boundary reaches past the playfield")
            return
        line = deploy_line(LINE_POINTS, push_out(start, pushed), push_out(end, pushed))
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
        # Rage goes down first, along the path the troops are about to take, so
        # they are inside it the whole way in. Freeze waits until the end.
        self._cast(rages, rage_path, frame)
        self._drop_singles(vanguard, middle, "siege")
        line = self._spread_troops(troops, start, end, pushed)
        # Recomputed, not reused: the flank moves while the troops go down, and
        # a hero sent to the pre-push midpoint is sent somewhere already refused.
        middle = line[len(line) // 2]
        # No hero on the field means every ability tap would only select a card,
        # and the schedule would sit through its longest timer to do it.
        if self._drop_singles(followers, middle, "hero"):
            self._fire_abilities(followers, plan.heroes if plan else [])
        # Freeze wants the defences the troops are fighting, which is where they
        # are by now. Some card slots overlap the result screen's 回營 button, so
        # this only runs while the battle is genuinely still on.
        if self._battle_view("before-freeze") is None:
            return
        self._cast(freezes, freeze_targets, frame)

    def _fire_abilities(self, heroes: list[int], kinds: list[HeroKind]) -> None:
        """Tap each hero's card again at its own moment, which is its ability.

        Timing is per hero rather than per card slot: an upgrading hero cannot
        take the field, so its card is absent and every slot after it shifts.
        A queen wants her cloak almost immediately, a warden's eternal tome is
        worth holding until the push is deep enough to be worth saving.
        """
        schedule = sorted(
            (self.abilities.seconds(kinds[i] if i < len(kinds) else "unknown"), x)
            for i, x in enumerate(heroes)
        )
        landed = time.monotonic()
        for delay, x in schedule:
            remaining = landed + delay - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if self._battle_view("ability") is None:
                logger.info("Battle ended before every ability fired")
                return
            self._tap((x, CARD_ROW_Y))
            logger.info("Fired the ability on the card at %d after %ds", x, delay)

    def _cast(self, cards: list[int], targets: tuple[tuple[int, int], ...], frame: bytes) -> None:
        """Empty each spell card over `targets`.

        One slow selection per card, then the placements as a burst, since the
        card stays selected while it still holds something. How many to tap
        comes from the card's own `xN` plus one for slack, falling back to the
        whole target list when the artwork makes the count unreadable.
        """
        for index, x in enumerate(cards):
            count = card_count(frame, x)
            cast_count = count + 1 if count else len(targets)
            cells = [targets[(index + i) % len(targets)] for i in range(cast_count)]
            self._tap((x, CARD_ROW_Y))
            time.sleep(SPELL_SELECT_DELAY)
            self.adb.tap_many(cells, self.display, gap=SPELL_PLACE_GAP)
            logger.info("Spell card at %d holds %s, tapped %d", x, count, cast_count)

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
        self._tap(RETURN_HOME)
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
