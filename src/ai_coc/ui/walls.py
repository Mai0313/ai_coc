"""Spend whatever the storages can spare on wall upgrades.

Walls are the one thing in the village that upgrade the instant they are paid
for: no builder is tied up and no timer runs, and one run buys as many batches as
the storages will stretch to. So a village whose builders are all on multi-day
jobs and whose storages are filling up has nowhere else to put the loot a farming
run brings home, which is what this is for — and why it runs to the same shape as
`attack.py`: one Gemini call, for the one question a screen read is worst at, and
every screen read after it verifying the answer.

**It still wants one builder standing idle, though it never uses them.** Measured
live on a village at 0/5, every batch was confirmed and then not charged for, the
game answering 所有建築工人都在忙碌中 and offering to finish something with gems
— one wall at 1 600 000 refused exactly as an eight-wall batch at 12 800 000 was.
So the loop stops on that rather than walking the rest of the village at forty
seconds a wall, and `ai_coc builders` is what says how long the wait is.

The game does most of the work. Tapping a wall opens its menu; 升級更多 turns
that into a batch and 新增城牆 grows it, with the game itself deciding which
walls belong in the batch — always the ones at the level of the wall that was
tapped. What the loop contributes is arithmetic the player would otherwise do by
eye: how many walls this batch can afford, which resource to pay from, and which
wall on the map is the one worth upgrading next.

Kept Qt-free like the attack loop, so `commands.py` can drive it against the
live game without a window.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from pydantic import Field

from ai_coc.models import (
    WallMenu,
    WallBatch,
    WallReport,
    WallOutcome,
    WallUpgrade,
    VillageStock,
    WallCandidate,
)
from ai_coc.ui.runner import BUY_SETTLE, MENU_SETTLE, GameRunner
from ai_coc.parsers.home import free_builders
from ai_coc.parsers.building import wall_menu, game_dialog

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# The confirmation slides in, so the first capture after 升級 can miss it.
DIALOG_TRIES = 4
DIALOG_WAIT = 0.6
# How far apart the taps that grow a batch go. Wider than the deployment's own
# spacing because nothing here is racing a battle timer, and a tap the game drops
# costs a wall off the batch.
ADD_GAP = 0.12

# The most walls one batch may hold. Not a limit the game imposes, and not
# normally the binding one either — what a batch can hold is already capped by
# what the storages can pay for. This is for the other end: a level-1 wall costs
# a few thousand, so a full storage would size a batch at four figures and spend
# minutes tapping 新增城牆 for it. It also keeps the price inside its own 127 px
# button, which the number every decision here rests on has to stay legible in.
MAX_BATCH = 25

# How far apart two neighbouring walls sit on this camera. Measured live: taps
# 45 px apart opened two different walls, one asking 1 600 000 and the other
# 4 000 000.
#
# **The village sweep steps 160 px, so it passes over three walls between
# samples, and what it lands on is *a* wall rather than the cheapest one.** A
# section is not one level: swept at this pitch over one live village, the strip
# held 32 walls at 1 600 000, three at 2 400 000, two at 3 200 000 and three at
# 4 000 000 — and the sweep's own six samples all landed on the 4 000 000 ones,
# which are the level the town hall caps. `_pick` then chose the cheapest of
# those, the game refused every batch, and the run bought nothing at all while
# 32 walls three levels lower stood beside them.
#
# One pitch for both axes, which the camera argues against — it is isometric, so
# 45 px down crosses about twice the tiles 45 px across does. Measured on the run
# that proved this fix, it makes no difference: of the eight neighbours that
# opened a wall, four came from the horizontal offsets and four from the
# vertical. A section is a solid block of walls several deep, so a tap that
# overshoots by a tile lands on another wall of the same section.
WALL_PITCH = 45

# How many walls to ask Gemini for, and how to describe one.
#
# **More than the run needs, because a wrong point is nearly free and a missing
# one is not.** Every spot is opened and priced off its own menu before it can be
# spent on, so a miss costs one tap and one capture — about 1.7 s — while a run
# left with too few candidates has nothing for `_pick` to compare and buys the
# first thing it sees. Measured live at twelve, nine opened a wall menu and the
# nine were priced 3 600 000, 4 900 000 and 6 300 000 — so the comparison `_pick`
# exists for survived three misses. The whole find took 23 s including the call.
#
# Nothing here is racing anything, so the model is the one in the settings file
# rather than a cheaper one picked for speed: this and the attack planner are the
# same kind of work — one fixed prompt against one frame — and a second setting
# would exist only to hold a smaller model.
WALL_SPOTS = 12
WALL_TARGET = "**城牆**（連成長線或圍成方框的灰色方塊，村莊裡數量最多的東西）"
# Spread out because a section is usually one level and so one price, and what
# `_pick` is for is the comparison. Measured live the model draws a line along
# one wall run whatever this says, which worked because that run crossed three
# levels — but the instruction costs nothing and a compartmented village would
# need it.
WALL_NOTES = "不要回答防禦塔、資源建築、兵營或裝飾物。這些點要散開在村莊的不同區塊，不要全部落在同一段牆上。"


class WallRunner(GameRunner):
    """Buys wall upgrades until the storages will not pay for another one."""

    # What to leave behind rather than spend. A run that empties the storages
    # leaves nothing to train an army with, which is the other half of farming.
    keep_gold: int = 0
    keep_elixir: int = 0
    # 0 keeps buying until neither resource will pay for another batch.
    rounds: int = 0
    # The walls this run was pointed at instead of scanning for them; see
    # `WallOptions.at` for why it is a list and not one wall.
    at: list[tuple[int, int]] = Field(default_factory=list)

    def _neighbours(self, point: tuple[int, int]) -> Iterator[WallCandidate]:
        """The walls immediately around this one, and what each of them asks.

        Level varies within a section, so the wall the sweep happened to land on
        says nothing about the cheapest one beside it — see `WALL_PITCH` for the
        run this cost. Four taps is what separates "a wall" from "the wall worth
        buying", and they are cheap: the sweep is already open on the village,
        so each is a tap and a capture. A neighbour can be a barracks as easily
        as a wall, which is what `_opened` is for.

        Nothing needs bounds-checking, though. The sweep's own grid stops more
        than a pitch inside every edge these offsets could step over: its lowest
        row is y 500 against the button row at 622, and its columns run 260 to
        1220 on a 1600 px screen.
        """
        around = [
            (point[0] + dx, point[1] + dy)
            for dx, dy in ((-WALL_PITCH, 0), (WALL_PITCH, 0), (0, -WALL_PITCH), (0, WALL_PITCH))
        ]
        for spot, png in self._opened(around, "near"):
            menu = wall_menu(png)
            if menu is not None:
                logger.info("Wall beside it at (%d, %d), asking %d", spot[0], spot[1], menu.price)
                yield WallCandidate(point=spot, price=menu.price)

    def _candidates(self) -> list[WallCandidate]:
        """The walls to choose between, from whichever of three finders answers.

        Named spots first, because a caller that went to the trouble of naming
        them has already looked at the screen. Then Gemini, which looks at the
        same frame and answers in seconds. The sweep is last and is the fallback
        rather than the default: it taps a blind grid for two and a half minutes
        and what it lands on is *a* wall rather than a cheap one — see
        `WALL_PITCH` for the run where every sample it took was a wall the town
        hall had capped.

        The stop is read before any of them, because looking costs taps
        whichever one answers. `_scan` reads it inside its own loop as well,
        since that is the long one, but none of them should start on a run
        already asked to stand down.
        """
        if self.should_stop():
            return []
        if self.at:
            return self._verify(self.at)
        # Falling through to the sweep rather than giving up: a village whose
        # walls Gemini could not place still has walls, and the alternative to
        # two and a half minutes of grid taps is a run that buys nothing.
        if (spotted := self._spotted(WALL_TARGET, WALL_NOTES, WALL_SPOTS)) and (
            found := self._verify(spotted)
        ):
            return found
        return self._scan()

    def _verify(self, points: list[tuple[int, int]]) -> list[WallCandidate]:
        """What each of these spots is really asking, read off its own menu.

        The price comes off the game rather than being assumed, because it is
        what `_pick` compares and what the affordability check stands the run
        down on. Naming one wall used to hand `_pick` a price of zero, which it
        then dutifully chose: measured live, that paid 9 000 000 for a wall
        while 4 000 000 ones stood in the same village.

        A point that opens no wall menu is dropped with a line saying so rather
        than tried again — buying whatever is there instead is worse than saying
        nothing is.
        """
        found: list[WallCandidate] = []
        for point, png in self._opened(points, "named"):
            menu = wall_menu(png)
            if menu is None:
                logger.info("Nothing at (%d, %d) opens a wall menu", *point)
                continue
            logger.info("Wall at (%d, %d), asking %d", point[0], point[1], menu.price)
            found.append(WallCandidate(point=point, price=menu.price))
        return found

    def _scan(self) -> list[WallCandidate]:
        """Every wall this run could spend on, and what each of them asks for.

        The sweep finds the sections and the neighbours find the levels. A wall
        the sweep lands on is only a sample of its section, and a section holds
        several levels at once, so a run that took the sample at face value would
        pick the cheapest *sample* rather than the cheapest wall.
        """
        found: dict[tuple[int, int], int] = {}
        for point, png in self._sweep("scan"):
            # The scan is the longest unguarded stretch of a run that was not
            # told where to start: a grid tap costs a `MENU_SETTLE` and a
            # capture, and every wall it lands on costs four more. Leaving here
            # is safe in a way that leaving a batch is not, because the sweep
            # already backs out of whatever each tap opened.
            if self.should_stop():
                break
            menu = wall_menu(png)
            if menu is None:
                continue
            logger.info("Wall at (%d, %d), asking %d", point[0], point[1], menu.price)
            found[point] = menu.price
            for near in self._neighbours(point):
                found.setdefault(near.point, near.price)
        return [WallCandidate(point=point, price=price) for point, price in found.items()]

    def _pick(self, prices: dict[tuple[int, int], int]) -> tuple[int, int]:
        """Which wall to upgrade next. Cheapest wins, which is the lowest level.

        Walls get dearer at every level, so the cheapest menu found belongs to
        the lowest wall found, and nothing has to read a level to know that.
        Lowest-first is the ordinary way to spend on walls — the weakest section
        is what an attacker walks through — and it is also the seam an AI planner
        would replace, since which section is worth raising first is a judgement
        about the layout rather than arithmetic about the price.

        Found, not "on the map": nothing here sees every wall, and what the scan
        did see decides this. That is why `_scan` looks beside each wall the
        sweep lands on — a sample of one wall per section is what had this
        picking a town-hall-capped wall while 32 cheaper ones stood beside it.
        """
        return min(prices, key=lambda point: prices[point])

    def _sized(self, menu: WallMenu, purse: int) -> WallBatch | None:
        """Grow the batch to what `purse` can pay for, and say what it holds.

        The unit price is worked out rather than read: tapping 新增城牆 once and
        subtracting is the whole of it. That also settles which of the two menus
        is on screen without anything having to tell them apart — a plain menu
        answers the same price back, because 升級更多 turns it into a batch of
        the one wall already selected, and a batch answers one wall more.
        """
        grown = wall_menu(self._after_tap(menu.add, "added"))
        if grown is None:
            logger.info("The menu did not survive being asked for another wall")
            return None
        # No change means the batch holds the one wall that was already selected.
        unit = grown.price - menu.price or grown.price
        if unit <= 0:
            # A batch cannot get cheaper by growing, so one of the two prices was
            # misread. Everything below divides by this, and a negative unit
            # would size a batch out of nonsense.
            logger.warning("Adding a wall took the price from %d to %d", menu.price, grown.price)
            return None
        count = grown.price // unit
        target = min(purse // unit, MAX_BATCH)
        if target < count:
            logger.info(
                "This batch already holds %d walls at %d each, and only %d are affordable",
                count,
                unit,
                target,
            )
            return None
        if target > count:
            self.adb.tap_many([grown.add] * (target - count), self.display, gap=ADD_GAP)
            time.sleep(MENU_SETTLE)
            grown = wall_menu(self._frame("sized"))
            if grown is None:
                logger.info("The menu did not survive being grown to %d walls", target)
                return None
            # However many the game actually took, which is not always what was
            # asked for: a batch stops growing once the village runs out of walls
            # at this level, and it says so with a notice rather than by refusing
            # the tap.
            count = grown.price // unit
        return WallBatch(menu=grown, unit=unit, count=count)

    def _confirm(self) -> bool:
        """Answer the spend confirmation, which is also the proof 升級 landed.

        A tap the game ignored raises nothing at all, so the dialog failing to
        appear is the only way to find out — and it is worth finding out, because
        the alternative is a loop reporting upgrades it never bought. Measured,
        the dialog slides in and the first capture after 升級 regularly misses it,
        which is what the retries are for.

        This is the one place 確定 is pressed, and it is only safe because of
        where it sits: the caller has just tapped 升級, and the only other dialog
        that could be standing — 確定退出遊戲嗎, drawn identically — is raised by
        `back`, which nothing between `_home` and here presses. `_home` also
        never leaves one up.
        """
        for _ in range(DIALOG_TRIES):
            dialog = game_dialog(self._frame("dialog"))
            if dialog is not None:
                self._tap(dialog.confirm)
                return True
            time.sleep(DIALOG_WAIT)
        logger.warning("升級 was tapped but no confirmation came up")
        return False

    def _buy(self, point: tuple[int, int], stock: VillageStock) -> WallUpgrade | None:
        """Upgrade the batch at this point, and report what it cost.

        None means nothing was spent, and the caller is meant to try somewhere
        else: this point is no longer a wall, its batch is already dearer than
        the village can pay for, or a tap went missing. Which of those it was is
        in the log; none of them is a reason to stop the whole run, because the
        next wall along is a different level at a different price.
        """
        menu = wall_menu(self._after_tap(point, "menu"))
        if menu is None:
            logger.info("Nothing at (%d, %d) opens a wall menu any more", *point)
            return None
        # Whichever storage has more to spare, since the two prices are equal.
        resource, purse = max(
            (("gold", stock.gold - self.keep_gold), ("elixir", stock.elixir - self.keep_elixir)),
            key=lambda pair: pair[1],
        )
        batch = self._sized(menu, purse)
        if batch is None:
            return None
        logger.info("Paying %d x %d in %s", batch.count, batch.unit, resource)
        self._tap(batch.menu.gold if resource == "gold" else batch.menu.elixir)
        if not self._confirm():
            return None
        time.sleep(BUY_SETTLE)
        paid = self._home()
        if paid is None:
            logger.warning("The storages could not be read after paying")
            return None
        # The storage having really moved is what separates an upgrade from a
        # report of one. Confirming the dialog is not enough on its own: a tap
        # the game swallows raises nothing, and the loop would otherwise carry on
        # counting walls it never bought.
        spent = (stock.gold - paid.gold) if resource == "gold" else (stock.elixir - paid.elixir)
        if spent < batch.unit:
            logger.warning("Confirmed the spend but %s only moved by %d", resource, spent)
            return None
        return WallUpgrade(unit=batch.unit, count=batch.count, resource=resource)

    def run(self) -> WallReport:
        """Upgrade walls until the storages, or the round count, run out.

        It finishes with the last batch's menu still open, and deliberately so:
        the only ways to close one are `back`, which on the village is the exit
        prompt, and a tap on empty ground, which on a village this full is not
        reliably empty. Measured, that menu costs the next run nothing — the
        storage bars read straight through it, and a tap on 攻擊 opens the attack
        menu with the wall menu gone.
        """
        # **Built with the answer for a run that walks its whole list**, so the
        # exits below each name their own reason and the end of the loop needs
        # no flag to know nothing did.
        report = WallReport(outcome="nothing_bought")
        if self._home() is None:
            report.outcome = self._lost()
            return report
        walls = self._candidates()
        if not walls:
            report.outcome = "stopped" if self.should_stop() else "no_walls_found"
            return report
        logger.info("%d wall(s) to choose from", len(walls))
        # Every wall the run may still spend on, against what its menu last
        # asked for. A price only stands until that wall is upgraded, so it is
        # re-read each time rather than carried from the scan.
        prices = {wall.point: wall.price for wall in walls}
        # `should_stop` sits in the condition rather than in a branch of its own
        # because between batches is the only safe place to leave anyway: a batch
        # is a menu, a confirmation and a storage read, and walking away mid-way
        # strands a dialog over the village. Whatever was already bought stays
        # bought, since a wall upgrades the moment it is paid for.
        while (
            prices
            and not self.should_stop()
            and (self.rounds <= 0 or len(report.upgrades) < self.rounds)
        ):
            # Through `_home` rather than a bare read: between one batch and the
            # next the screen can be anything from a settling animation to the
            # idle-disconnect dialog, and a run that stopped on the first of
            # those had bought one wall out of the six it could afford.
            stock = self._home()
            if stock is None:
                report.outcome = "no_stock"
                break
            cheapest = min(prices.values())
            if max(stock.gold - self.keep_gold, stock.elixir - self.keep_elixir) < cheapest:
                logger.info("The cheapest batch left costs %d, which is out of reach", cheapest)
                report.outcome = "cannot_afford"
                break
            point = self._pick(prices)
            bought = self._buy(point, stock)
            if bought is None:
                # A batch the game confirms and then does not charge for is
                # nearly always one thing, and it says so: measured live with
                # every builder busy, 確定 raised 所有建築工人都在忙碌中 and
                # nothing was spent — one wall at 1 600 000 refused exactly as an
                # eight-wall batch at 12 800 000 was, so it is not the size or
                # the purse. That dialog is answered and gone by the time this
                # runs, but the counter it was complaining about is not, and
                # walking the rest of the village would fail the same way at
                # forty seconds a wall.
                counted = free_builders(self._frame("builders"))
                if counted is not None and counted[0] == 0:
                    logger.info(
                        "All %d builders are busy; the game refuses every batch", counted[1]
                    )
                    report.outcome = "builders_busy"
                    break
                # This point is spent as far as this run is concerned. Dropping
                # it rather than retrying is what keeps a wall the game will not
                # sell from being asked about once a round for the whole run.
                del prices[point]
                continue
            report.upgrades.append(bought)
            logger.info(
                "Bought %d wall(s) at %d in %s", bought.count, bought.unit, bought.resource
            )
            # The menu is still up on the batch that was just paid for, showing
            # what its next level costs. That is this point's new price, and it
            # is what moves the loop on to a different wall next round rather
            # than pushing one section of the village further and further ahead.
            latest = wall_menu(self._frame("next"))
            if latest is None:
                del prices[point]
            else:
                prices[point] = latest.price
        report.outcome = self._ended(report)
        return report

    def _ended(self, report: WallReport) -> WallOutcome:
        """What to call a run whose loop condition ran out rather than breaking.

        **Only when nothing above named a reason**, which is what the value the
        report was built with means: reaching here with `nothing_bought` still on
        it says the loop walked its whole list, so this is a stop, a full list
        bought, or the one case the loop cannot explain.
        """
        if report.outcome != "nothing_bought":
            return report.outcome
        if self.should_stop():
            return "stopped"
        return "bought" if report.upgrades else "nothing_bought"
