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
from pathlib import Path

from pydantic import BaseModel, PrivateAttr

from ai_coc.models import (
    WallMenu,
    WallBatch,
    WallReport,
    WallUpgrade,
    VillageStock,
    DisplayTarget,
    WallCandidate,
)
from ai_coc.adapters.adb import AdbController
from ai_coc.parsers.wall import wall_menu, game_dialog
from ai_coc.parsers.scout import read_stock

logger = logging.getLogger(__name__)

# Where a tap could land on a wall. The row of buttons a selected building opens
# runs across the bottom of the screen, so a scan that reached down there would
# be pressing the previous tap's own buttons; the left and right edges are the
# village's own UI columns for the same reason.
SCAN_X = (260, 420, 580, 740, 900, 1060, 1220)
SCAN_Y = (140, 260, 380, 500)

# How long a menu takes to open, and how long the game takes to charge for a
# batch and repaint the row. Both measured against this emulator by walking the
# menus by hand: a capture taken 0.8 s after the tap already had the new menu on
# it, and this leaves room for the emulator being busy.
MENU_SETTLE = 1.0
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

# Getting back to the village, which two different things can be in the way of.
# A game still loading wants waiting on and nothing else — measured, it takes
# around twenty seconds from a cold launch — so nothing is pressed until the
# patience runs out. A screen a scan tap opened wants `back`, which closes it.
HOME_TRIES = 16
LOADING_PATIENCE = 10
LOAD_WAIT = 2.0
BACK_SETTLE = 1.2


class WallRunner(BaseModel):
    """Buys wall upgrades until the storages will not pay for another one."""

    adb: AdbController
    display: DisplayTarget
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
    frame_dir: Path | None = None

    _captures: int = PrivateAttr(default=0)

    def _tap(self, point: tuple[int, int]) -> None:
        self.adb.tap(point[0], point[1], self.display)

    def _frame(self, label: str) -> bytes:
        """One capture, kept on disk when the run is being recorded."""
        png = self.adb.screenshot(self.display)
        if self.frame_dir is not None:
            self._captures += 1
            (self.frame_dir / f"{self._captures:04d}_{label}.png").write_bytes(png)
        return png

    def _after_tap(self, point: tuple[int, int], label: str) -> bytes:
        self._tap(point)
        time.sleep(MENU_SETTLE)
        return self._frame(label)

    def _home(self) -> VillageStock | None:
        """The village's storages, once nothing is covering the village any more.

        The storage bars double as the check that the home village is up at all:
        `read_stock` answers None for every other screen, which is exactly what a
        scan tap that opened a barracks needs to be told.

        **`back` is only ever pressed on a frame that is not a clear village**,
        and that restriction is the whole reason this is not three lines.
        Measured live: on the home village `back` raises 確定退出遊戲嗎 — with a
        wall menu open as readily as without — and that dialog's 確定 is the same
        green in the same pixels as the one that pays for a batch. So a frame
        that reads as nothing at all is waited on rather than pressed at, since
        far more often than a panel it is the game still loading.

        A dialog already standing is answered with 取消, not with another `back`
        whose effect on one this has no business guessing at. Any dialog: the
        only one that can be up here is that exit prompt, and 取消 is the
        harmless answer to every other one the game raises too. It has to be
        answered rather than read past, because its own dimming is what stops the
        storages reading — measured, the bars stay perfectly legible to the eye
        and come back as nothing at all.
        """
        for attempt in range(HOME_TRIES):
            png = self._frame("home")
            dialog = game_dialog(png)
            if dialog is not None:
                logger.info("A dialog is covering the village; answering 取消")
                self._tap(dialog.cancel)
                time.sleep(BACK_SETTLE)
                continue
            stock = read_stock(png)
            if stock is not None:
                return stock
            if attempt < LOADING_PATIENCE:
                time.sleep(LOAD_WAIT)
                continue
            self.adb.back(self.display)
            time.sleep(BACK_SETTLE)
        return None

    def _scan(self) -> list[WallCandidate]:
        """Tap across the village and keep every point that opened a wall menu.

        Walls cannot be found by looking for one — the art changes at every level
        — so they are found by tapping and reading what the game opens. Buildings
        move between villages and layouts, which is why this is a sweep rather
        than a remembered spot.
        """
        found: list[WallCandidate] = []
        for y in SCAN_Y:
            for x in SCAN_X:
                png = self._after_tap((x, y), f"scan_{x:04d}_{y:04d}")
                menu = wall_menu(png)
                if menu is not None:
                    logger.info("Wall at (%d, %d), asking %d", x, y, menu.price)
                    found.append(WallCandidate(point=(x, y), price=menu.price))
                elif read_stock(png) is None:
                    # The tap opened a screen rather than a menu, which is what a
                    # barracks or a laboratory does. Nothing more can be tapped
                    # until the village is back.
                    logger.info("The tap at (%d, %d) covered the village; backing out", x, y)
                    if self._home() is None:
                        return found
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
        paid = read_stock(self._frame("bought"))
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
            stock = read_stock(self._frame("stock"))
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
