"""Spend whatever the storages can spare on wall upgrades.

Walls are the one thing in the village that upgrade the instant they are paid
for: no builder is tied up and no timer runs. So a village with every builder
busy and both storages filling up has nowhere else to put the loot a farming run
brings home, which is what this is for — and why it runs to the same shape as
`attack.py`, driven from screen reads with no Gemini call anywhere in it.

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
import logging

from ai_coc.models import WallMenu, WallBatch, WallReport, WallUpgrade, VillageStock, WallCandidate
from ai_coc.ui.runner import MENU_SETTLE, GameRunner
from ai_coc.parsers.building import wall_menu, game_dialog

logger = logging.getLogger(__name__)

# How long the game takes to charge for a batch and repaint the row, measured
# against this emulator by walking the menus by hand.
BUY_SETTLE = 1.5
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


class WallRunner(GameRunner):
    """Buys wall upgrades until the storages will not pay for another one."""

    # What to leave behind rather than spend. A run that empties the storages
    # leaves nothing to train an army with, which is the other half of farming.
    keep_gold: int = 0
    keep_elixir: int = 0
    # 0 keeps buying until neither resource will pay for another batch.
    rounds: int = 0
    # A wall to start from instead of scanning for one. The scan is the part most
    # likely to disagree with a village this was not written against, so naming a
    # wall is how to exercise everything downstream of it.
    at: tuple[int, int] | None = None

    def _scan(self) -> list[WallCandidate]:
        """Every point on the sweep that opened a wall menu, and what it asked for."""
        found: list[WallCandidate] = []
        for point, png in self._sweep("scan"):
            menu = wall_menu(png)
            if menu is not None:
                logger.info("Wall at (%d, %d), asking %d", point[0], point[1], menu.price)
                found.append(WallCandidate(point=point, price=menu.price))
        return found

    def _pick(self, prices: dict[tuple[int, int], int]) -> tuple[int, int]:
        """Which wall to upgrade next. Cheapest wins, which is the lowest level.

        Walls get dearer at every level, so the cheapest menu on the map belongs
        to the lowest wall on it, and nothing has to read a level to know that.
        Lowest-first is the ordinary way to spend on walls — the weakest section
        is what an attacker walks through — and it is also the seam an AI planner
        would replace, since which section is worth raising first is a judgement
        about the layout rather than arithmetic about the price.
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
        report = WallReport()
        if self._home() is None:
            report.message = "畫面沒辦法回到村莊，城牆升級沒有開始"
            return report
        # A named wall starts at nothing, so the affordability check below cannot
        # stand the run down before the price has been read off the game.
        walls = [WallCandidate(point=self.at, price=0)] if self.at is not None else self._scan()
        if not walls:
            report.message = "掃過村莊都沒有找到城牆"
            return report
        logger.info("%d wall(s) to choose from", len(walls))
        # Every wall the run may still spend on, against what its menu last
        # asked for. A price only stands until that wall is upgraded, so it is
        # re-read each time rather than carried from the scan.
        prices = {wall.point: wall.price for wall in walls}
        while prices and (self.rounds <= 0 or len(report.upgrades) < self.rounds):
            # Through `_home` rather than a bare read: between one batch and the
            # next the screen can be anything from a settling animation to the
            # idle-disconnect dialog, and a run that stopped on the first of
            # those had bought one wall out of the six it could afford.
            stock = self._home()
            if stock is None:
                report.message = "看不到村莊的儲量，先停下來"
                break
            cheapest = min(prices.values())
            if max(stock.gold - self.keep_gold, stock.elixir - self.keep_elixir) < cheapest:
                report.message = f"剩下的資源買不起下一批城牆，最便宜的一批要 {cheapest}"
                break
            point = self._pick(prices)
            bought = self._buy(point, stock)
            if bought is None:
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
        if not report.message:
            report.message = (
                f"升級了 {report.walls} 面城牆"
                if report.upgrades
                else "每一個位置都沒有買成，紀錄裡有各自停在哪一步"
            )
        return report
