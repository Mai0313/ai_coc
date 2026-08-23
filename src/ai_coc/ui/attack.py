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

from ai_coc.models import LootOffer, ScoutView, AttackReport, DisplayTarget, LootThresholds
from ai_coc.adapters.adb import AdbController
from ai_coc.parsers.scout import (
    card_count,
    live_cards,
    read_scout,
    card_groups,
    freeze_cards,
    counted_cards,
    attack_menu_open,
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

# A line along the village's upper-left flank, outside the deployment boundary.
DEPLOY_START = (600, 110)
DEPLOY_END = (230, 380)
LINE_POINTS = 12

# Rage and freeze go on the strongest defences, which sit towards the middle of
# the village — never on the drop line, where by the time a spell can be cast
# there is nothing left but empty ground. Reading the defences off the frame is
# what an AI pass would add; this is the standing approximation of "the middle".
# Spells go down first and tile the village, so the troops walk into them the
# whole way in. A rage reaches 5 tiles, and the 44x44 map spans about 1040x605
# px here, which puts a tile at roughly 24x12 and the spell's footprint at a
# 240x120 ellipse rather than a circle — the board is drawn isometric, so the
# vertical spacing is half the horizontal. Two that overlap waste one, so these
# are spaced at that pitch and ordered from the middle out, leaving a short
# army still covering where the fighting lands.
# Rage goes where the troops are about to walk rather than where they land: a
# grid running from the flank they drop on towards the middle.
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
# Every spell gets cast: one carried home is worth nothing.
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

HERO_ABILITY_DELAY = 15
BATTLE_TIMEOUT = 240


def deploy_line(count: int) -> list[tuple[int, int]]:
    """Evenly spaced positions along the flank, from one end to the other."""
    (x0, y0), (x1, y1) = DEPLOY_START, DEPLOY_END
    step = max(count - 1, 1)
    return [
        (round(x0 + (x1 - x0) * i / step), round(y0 + (y1 - y0) * i / step)) for i in range(count)
    ]


def drop_points(line: list[tuple[int, int]], seed: int) -> list[tuple[int, int]]:
    """One pass worth of drops, spread over the whole line rather than bunched.

    `seed` rotates the starting point per card and per pass, so a card holding
    a single troop does not put it on the same spot every other card started on.
    """
    step = max(len(line) // DROPS_PER_PASS, 1)
    return [line[(seed + i * step) % len(line)] for i in range(DROPS_PER_PASS)]


class AttackRunner(BaseModel):
    """Drives one attack from the home village and back, with no AI in the loop."""

    adb: AdbController
    display: DisplayTarget
    thresholds: LootThresholds
    max_skips: int = 20

    def _tap(self, point: tuple[int, int]) -> None:
        self.adb.tap(point[0], point[1], self.display)

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
        line = deploy_line(LINE_POINTS)
        middle = line[len(line) // 2]
        # Rage goes down first, along the path the troops are about to take, so
        # they are inside it the whole way in. Freeze waits until the end.
        freezes = freeze_cards(frame, spells)
        self._cast([x for x in spells if x not in freezes], RAGE_PATH, frame)
        for x in vanguard:
            self.adb.tap_many([(x, CARD_ROW_Y), middle], self.display, gap=SINGLE_DROP_DELAY)
        remaining = list(troops)
        for index in range(DEPLOY_PASSES):
            for card, x in enumerate(remaining):
                drops = drop_points(line, index * len(remaining) + card)
                self.adb.tap_many([(x, CARD_ROW_Y), *drops], self.display)
            remaining = live_cards(self.adb.screenshot(self.display), remaining)
            logger.info("%d troop card(s) still hold something", len(remaining))
            if not remaining:
                break
        for x in followers:
            self.adb.tap_many([(x, CARD_ROW_Y), middle], self.display, gap=SINGLE_DROP_DELAY)
        time.sleep(HERO_ABILITY_DELAY)
        # A hero card taps into its ability once the hero is on the field. Some
        # card slots overlap the result screen's 回營 button, so this only runs
        # while the battle is genuinely still on.
        if read_scout(self.adb.screenshot(self.display)) is None:
            return
        self.adb.tap_many([(x, CARD_ROW_Y) for x in singles], self.display)
        # Now the troops are deep in and taking fire, which is what freeze is for.
        self._cast(freezes, (FREEZE_TARGET,), frame)

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
        self._tap(HOME_ATTACK)
        time.sleep(2)
        if not attack_menu_open(self.adb.screenshot(self.display)):
            logger.warning("The attack menu did not open; the game is not on the home village")
            return AttackReport(message="畫面不在主村，沒有開啟攻擊選單就停手")
        self._tap(FIND_MATCH)
        time.sleep(2)
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
