"""One pass of 攻擊 → 偵察 → 進攻或跳過 → 回營.

Deliberately outside the Gemini agent loop. The scout screen expires after 30
seconds and takes the 下一個 button with it, which a vision call per candidate
does not reliably fit inside, and every opponent that is skipped anyway would
still have been paid for. Screen reading is `parsers.scout` instead.
"""

from __future__ import annotations

import time
import logging

from pydantic import BaseModel

from ai_coc.models import (
    HeroKind,
    LootOffer,
    ScoutView,
    AttackPlan,
    HeroTimings,
    AttackReport,
    DisplayTarget,
    LootThresholds,
)
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import AdbController
from ai_coc.parsers.scout import (
    card_count,
    live_cards,
    read_scout,
    card_groups,
    freeze_cards,
    army_strength,
    counted_cards,
    deploy_refused,
    attack_menu_open,
    idle_disconnected,
)

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

# Lines just outside the deployment boundary on each flank. A plan names a side
# rather than free coordinates, because a drop inside the boundary is refused and
# a named side maps onto a line already known to be outside it.
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
# The refusal banner lingers, so it has to clear before the next probe is read.
REFUSAL_CLEAR_DELAY = 3

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


def deploy_line(
    count: int, start: tuple[int, int] = DEPLOY_START, end: tuple[int, int] = DEPLOY_END
) -> list[tuple[int, int]]:
    """Evenly spaced positions along a flank, from one end to the other."""
    (x0, y0), (x1, y1) = start, end
    step = max(count - 1, 1)
    return [
        (round(x0 + (x1 - x0) * i / step), round(y0 + (y1 - y0) * i / step)) for i in range(count)
    ]


def push_out(point: tuple[int, int], steps: int) -> tuple[int, int]:
    """Move a drop further from the middle, clamped to the usable playfield."""
    dx, dy = point[0] - SCREEN_CENTRE[0], point[1] - SCREEN_CENTRE[1]
    span = max((dx * dx + dy * dy) ** 0.5, 1.0)
    left, top, right, bottom = PLAYFIELD
    return (
        min(max(round(point[0] + dx / span * PUSH_STEP * steps), left), right),
        min(max(round(point[1] + dy / span * PUSH_STEP * steps), top), bottom),
    )


def drop_points(line: list[tuple[int, int]], seed: int) -> list[tuple[int, int]]:
    """One pass worth of drops, spread over the whole line rather than bunched.

    `seed` rotates the starting point per card and per pass, so a card holding
    a single troop does not put it on the same spot every other card started on.
    """
    step = max(len(line) // DROPS_PER_PASS, 1)
    return [line[(seed + i * step) % len(line)] for i in range(DROPS_PER_PASS)]


PLAN_PROMPT = """這是《部落衝突》的偵察畫面，我要打資源，請看整張圖決定怎麼打。

deploy_from：從哪一側投兵。選防禦最少、而且外圍資源建築（金庫、聖水瓶、金礦、聖水收集器）最密集的那一側。避開迫擊砲、防空火箭、法師塔、地獄塔密集的方向。
rage_points：狂暴法術的落點，**剛好 {rage_count} 個，不能少**。
部隊是沿著那一側的整條邊撒開下去的，所以他們是一整片往村莊中心推進，不是一條線；落點要鋪滿那一片會經過的區域，深度和寬度都要分開，**不要排成一直線**。
一瓶狂暴的覆蓋範圍是寬約畫面 15%、高約畫面 13% 的橢圓（畫面是等角視角，所以橫向比縱向寬）。彼此不要重疊，重疊等於整瓶浪費。
優先蓋在部隊會卡住的地方：外牆的突破口、防禦最密集因而會拖最久的那一段。
freeze_point：冰凍法術的落點，放在那條路線上防禦火力最強的地方（多座防禦交叉的位置，或單一高等防禦）。
heroes：畫面最下方那排卡片裡，**英雄卡由左到右**分別是誰，用 king（野蠻人之王）、queen（弓箭女皇）、warden（大守護者）、champion（皇家守護）、minion_prince（飛盾王子）；認不出來的填 unknown。英雄卡是有等級數字、沒有 xN 數量的那幾張，不要把士兵、攻城機器或法術算進去。正在升級的英雄不能出戰，所以卡片會直接消失，順序不是固定的。
座標是畫面百分比，x_pct 與 y_pct 都是 0 到 100，只能落在村莊範圍內。
reason 用繁體中文一句話說明為什麼選這一側。"""


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
    abilities: HeroTimings = HeroTimings()
    max_skips: int = 20
    ai: GeminiClient | None = None

    def _tap(self, point: tuple[int, int]) -> None:
        self.adb.tap(point[0], point[1], self.display)

    def _open_attack_menu(self) -> bool:
        """Get to the attack menu, clearing whatever is covering the village."""
        for _ in range(HOME_ATTEMPTS):
            if idle_disconnected(self.adb.screenshot(self.display)):
                logger.info("Idle-disconnect dialog is up; logging back in")
                self._tap(RELOGIN_BUTTON)
                time.sleep(RELOGIN_WAIT)
                continue
            self._tap(HOME_ATTACK)
            time.sleep(2)
            if attack_menu_open(self.adb.screenshot(self.display)):
                return True
            # Never `back` here: on the home village that is 確定退出遊戲嗎, one
            # tap away from closing the game. A building panel left open does
            # not cover the 攻擊 button in the corner anyway, so the tap above
            # only needs the panel to swallow one press and then retries.
            time.sleep(HOME_RETRY_DELAY)
        return False

    def _plan(self, frame: bytes, rage_count: int) -> AttackPlan | None:
        if self.ai is None:
            return None
        try:
            plan = self.ai.generate_structured(
                PLAN_PROMPT.format(rage_count=rage_count), AttackPlan, frame
            )
        except Exception:
            logger.warning("Attack planning failed; using the fixed flank", exc_info=True)
            return None
        logger.info(
            "Plan: from %s, %d rage point(s), freeze=%s, heroes=%s (%s)",
            plan.deploy_from,
            len(plan.rage_points),
            plan.freeze_point is not None,
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
            png = self.adb.screenshot(self.display)
            view = read_scout(png)
            if view:
                return view, png
            time.sleep(1)
        return None

    def _usable_line(
        self, card: int, start: tuple[int, int], end: tuple[int, int]
    ) -> list[tuple[int, int]] | None:
        """A flank the game will actually accept drops on.

        Troops are dropped as probes, because a village whose boundary reaches
        past the line refuses them and says so on screen. Both ends go in as
        well as the middle: a flank can sit outside the boundary at its centre
        and inside it at the tips, which would silently lose every troop aimed
        there. Each refusal pushes the line further out; None means even the
        playfield edge was inside the boundary.
        """
        for attempt in range(DEPLOY_ATTEMPTS):
            line = deploy_line(LINE_POINTS, push_out(start, attempt), push_out(end, attempt))
            probes = [line[0], line[len(line) // 2], line[-1]]
            self.adb.tap_many([(card, CARD_ROW_Y), *probes], self.display)
            if not deploy_refused(self.adb.screenshot(self.display)):
                logger.info("Deploying on the flank pushed out %d step(s)", attempt)
                return line
            logger.info("Drop refused inside the boundary; pushing the flank out")
            time.sleep(REFUSAL_CLEAR_DELAY)
        return None

    def _spread_troops(
        self,
        troops: list[int],
        line: list[tuple[int, int]],
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> list[tuple[int, int]]:
        """Empty the troop cards along the flank; returns the line ending up in use.

        Probing the ends is not enough on its own: the village is a diamond, so
        the ground around it is not convex and a line whose ends are both clear
        can still cut through it. A refusal means *some* of this pass's drops
        landed inside, so the flank is pushed out for the passes that follow —
        but the cards are still re-read, because the drops that did land count.
        """
        remaining = list(troops)
        pushed = 0
        for index in range(DEPLOY_PASSES):
            for card, x in enumerate(remaining):
                drops = drop_points(line, index * len(remaining) + card)
                self.adb.tap_many([(x, CARD_ROW_Y), *drops], self.display)
            shot = self.adb.screenshot(self.display)
            if deploy_refused(shot) and pushed + 1 < DEPLOY_ATTEMPTS:
                pushed += 1
                logger.info(
                    "Some drops landed inside the boundary; flank pushed out to %d", pushed
                )
                line = deploy_line(LINE_POINTS, push_out(start, pushed), push_out(end, pushed))
            remaining = live_cards(shot, remaining)
            logger.info("%d troop card(s) still hold something", len(remaining))
            if not remaining:
                break
        return line

    def _deploy(self, frame: bytes) -> None:
        """Spread the main troops along one flank; everything else drops once, mid-line."""
        groups = card_groups(frame)
        if not groups:
            logger.warning("No cards found on the battle row; nothing to deploy")
            return
        # The first group is the troops the attack is built on. Of what follows,
        # spells are held back for the village itself; `xN` is what separates
        # them, since spells carry a count and heroes and the siege machine do
        # not. The group immediately after the troops is the siege machine, and
        # it leads: it is the tank, and it opens the path the troops walk into.
        troops = groups[0]
        rest = [x for group in groups[1:] for x in group]
        spells = counted_cards(frame, rest)
        singles = [x for x in rest if x not in spells]
        vanguard = [x for x in groups[1] if x in singles] if len(groups) > 1 else []
        followers = [x for x in singles if x not in vanguard]
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
        plan = self._plan(frame, rage_count)
        start, end = DEPLOY_LINES[plan.deploy_from] if plan else (DEPLOY_START, DEPLOY_END)
        line = self._usable_line(troops[0], start, end)
        if line is None:
            logger.warning("Every drop was refused; the boundary reaches past the playfield")
            return
        middle = line[len(line) // 2]
        planned = tuple(point.pixels() for point in plan.rage_points) if plan else ()
        # A plan can name fewer spots than the army carries rages, and `_cast`
        # cycles back over its targets — which would stack two rages on one spot
        # and waste one. The fixed grid fills the tail so each gets its own.
        rage_path = planned + tuple(point for point in RAGE_PATH if point not in planned)
        freeze_target = plan.freeze_point.pixels() if plan and plan.freeze_point else FREEZE_TARGET
        # Rage goes down first, along the path the troops are about to take, so
        # they are inside it the whole way in. Freeze waits until the end.
        self._cast(rages, rage_path, frame)
        for x in vanguard:
            self.adb.tap_many([(x, CARD_ROW_Y), middle], self.display, gap=SINGLE_DROP_DELAY)
        line = self._spread_troops(troops, line, start, end)
        # Recomputed, not reused: the flank moves while the troops go down, and
        # a hero sent to the pre-push midpoint is sent somewhere already refused.
        middle = line[len(line) // 2]
        for x in followers:
            self.adb.tap_many([(x, CARD_ROW_Y), middle], self.display, gap=SINGLE_DROP_DELAY)
        logger.info("Dropped %d hero card(s) at %s", len(followers), middle)
        self._fire_abilities(followers, plan.heroes if plan else [])
        # Freeze wants the defences the troops are fighting, which is where they
        # are by now. Some card slots overlap the result screen's 回營 button, so
        # this only runs while the battle is genuinely still on.
        if read_scout(self.adb.screenshot(self.display)) is None:
            return
        self._cast(freezes, (freeze_target,), frame)

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
            if read_scout(self.adb.screenshot(self.display)) is None:
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
        it costs nothing extra because the panel is already being polled.
        """
        deadline = time.monotonic() + BATTLE_TIMEOUT
        taken = False
        while time.monotonic() < deadline:
            time.sleep(10)
            # The result screen is the first one with no loot panel on it.
            view = read_scout(self.adb.screenshot(self.display))
            if view is None:
                break
            taken = taken or view.loot != opening
        self._tap(RETURN_HOME)
        return taken

    def run(self) -> AttackReport:
        logger.info("Attack run starts, thresholds=%s", self.thresholds.model_dump())
        if not self._open_attack_menu():
            logger.warning("The attack menu did not open; the game is not on the home village")
            return AttackReport(message="畫面不在主村，沒有開啟攻擊選單就停手")
        self._tap(FIND_MATCH)
        time.sleep(2)
        strength = army_strength(self.adb.screenshot(self.display))
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
            if forced or self.thresholds.accepts(view.loot):
                reason = "倒數結束被強制開戰" if forced else "戰利品達標"
                logger.info("Attacking after %d skips (%s)", skipped, reason)
                self._deploy(frame)
                if not self._wait_out_battle(view.loot):
                    logger.warning("The whole battle passed without any loot moving")
                    return AttackReport(
                        skipped=skipped,
                        attacked=view.loot,
                        message=f"{reason}，但整場戰利品沒有變化，部隊可能沒有成功部署",
                    )
                return AttackReport(
                    skipped=skipped, attacked=view.loot, message=f"{reason}，已進攻並回營"
                )
            if skipped >= self.max_skips:
                self._tap(END_BATTLE)
                return AttackReport(
                    skipped=skipped, message=f"連續跳過 {skipped} 個對手都未達門檻，已結束搜尋"
                )
            skipped += 1
            self._tap(NEXT_TARGET)
