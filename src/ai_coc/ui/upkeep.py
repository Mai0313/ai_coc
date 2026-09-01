"""Village housekeeping: empty the collectors, and put idle builders to work.

Two jobs with nothing to do with fighting and everything to do with a village
not standing still. A collector fills up whether anyone is watching and then
stops, so a village left alone for hours was idle for most of them; an idle
builder is the same waste in the other direction, and builders are what a
village is actually short of rather than loot.

Collecting is the shortest loop in the project — tapping a marker collects it
outright, no menu and nothing to confirm. Upgrading is the longest, because the
building to put a builder on has to be found by tapping across the village
first: no building can be recognised by its artwork, and none of them sits in
the same place in two villages.

Kept Qt-free like the rest of `ui/` outside the widgets, so `commands.py` can
drive it against the live game without a window.
"""

from __future__ import annotations

import time
import logging

from pydantic import Field

from ai_coc.models import BuildReport, VillageStock, BuilderReport, CollectReport, BuildCandidate
from ai_coc.ui.runner import MENU_SETTLE, GameRunner
from ai_coc.parsers.home import BUILDER_BUTTON, builder_jobs, free_builders, collect_bubbles
from ai_coc.parsers.scout import read_stock
from ai_coc.parsers.building import wall_menu, game_dialog, upgrade_sheet, upgrade_buttons

logger = logging.getLogger(__name__)


def _spell_out(seconds: int) -> str:
    """A countdown as the game writes it, for a message a person reads."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分鐘"
    return f"{minutes} 分鐘"


# How far apart the taps go, and how long to leave the game to swallow them
# before looking again. Wider than the attack loop's spacing because nothing here
# is racing a battle timer, and a tap the game drops costs a collector.
COLLECT_GAP = 0.15
COLLECT_SETTLE = 2.0
# How many times to look again. A marker vanishes once it is collected, so a
# second read is what catches the taps the game did not take — and it stops as
# soon as a read comes back empty, which is the normal way out after one pass.
COLLECT_PASSES = 3

# How long the game takes to charge for an upgrade and repaint the menu. The same
# figure the wall loop measured for the same thing.
BUY_SETTLE = 1.5

# How many buildings to ask Gemini for, and how to describe one.
#
# **More than the run can use, because a builder is what is scarce here.** Only
# the free builders get spent, so a village at 1/5 uses one of these — but
# `_pick` takes the dearest it can afford, and that comparison is only as good as
# the list it is given. Measured live, 9 of 14 answers had a priced upgrade on
# them, and **only two of those nine sat within 45 px of a sweep grid point**
# against buildings 70 to 120 px across: the grid stops at x 1220 and y 500,
# which leaves most of a village outside it.
BUILD_SPOTS = 14
BUILD_TARGET = (
    "**可以升級的建築**（防禦塔、資源採集器、儲存罐、兵營、實驗室這一類，有實體屋頂的建築）"
)
BUILD_NOTES = "不要回答城牆、樹木石頭這種裝飾物、或地上的軍隊。這些點要散開在村莊的不同區塊。"


class UpkeepRunner(GameRunner):
    """Runs the jobs a village needs doing that have nothing to do with fighting."""

    # What to leave in the storages rather than spend. A run that empties them
    # leaves nothing to train an army with, which is the other half of farming.
    keep_gold: int = 0
    keep_elixir: int = 0
    # The buildings this run was pointed at instead of looking for them. Empty
    # means ask, and then sweep if that answers nothing.
    at: list[tuple[int, int]] = Field(default_factory=list)

    def collect(self) -> CollectReport:
        """Tap every collector marker on screen, and report what the storages gained.

        The markers are re-read between passes rather than tapped from one list,
        because that read is the only evidence a tap landed: a collected marker
        disappears. The gains come from the storage bars for the same reason —
        counting markers would report a full storage as a haul.
        """
        report = CollectReport()
        before = self._home()
        if before is None:
            report.message = "畫面沒辦法回到村莊，收集沒有開始"
            return report
        # The set of markers the last pass tapped. A pass that finds the same set
        # again made no progress, which is what a full storage looks like: the
        # game keeps drawing the marker because there is something in there, and
        # goes on refusing to move it. Without this the passes would all be spent
        # tapping it.
        tapped: set[tuple[int, int]] = set()
        for index in range(COLLECT_PASSES):
            # Every pass but the first re-checks the village first. An empty read
            # means "all collected" and "the screen is not a village any more"
            # equally, and one of those is worth recovering from — measured, a
            # run walked into the idle-disconnect dialog between two passes.
            if index and self._home() is None:
                break
            markers = collect_bubbles(self._frame("markers"))
            standing = {marker.point for marker in markers}
            if not markers or standing == tapped:
                break
            logger.info(
                "%d marker(s) waiting: %s",
                len(markers),
                "/".join(f"{m.resource}@{m.point[0]},{m.point[1]}" for m in markers),
            )
            self.adb.tap_many([marker.point for marker in markers], self.display, gap=COLLECT_GAP)
            report.markers += len(markers)
            tapped = standing
            time.sleep(COLLECT_SETTLE)
        # Through `_home` rather than a bare capture: a marker that turned out to
        # be a building has left its menu open, and this is what closes it as
        # well as what reads the storages.
        after = self._home()
        if after is None:
            report.message = f"點了 {report.markers} 個採集標記，但收完之後讀不到儲量"
            return report
        report.gold = after.gold - before.gold
        report.elixir = after.elixir - before.elixir
        report.dark = after.dark - before.dark
        report.message = (
            f"收了 {report.markers} 個採集器，"
            f"金幣 +{report.gold}／聖水 +{report.elixir}／黑水 +{report.dark}"
            if report.markers
            else "沒有採集器等著收"
        )
        return report

    def _buildings(self) -> list[BuildCandidate]:
        """The upgrades to choose between, from whichever of three finders answers.

        Named points first, then Gemini, then the sweep — the same order the wall
        loop runs, and for the same reason: the sweep is a blind grid that takes
        two and a half minutes and lands where it lands, so it is the fallback
        rather than the default. It stays underneath because a village whose
        buildings nobody can place still has buildings.
        """
        if self.at:
            return self._priced(self.at)
        if (spotted := self._spotted(BUILD_TARGET, BUILD_NOTES, BUILD_SPOTS)) and (
            found := self._priced(spotted)
        ):
            return found
        return self._scan()

    def _offer(self, point: tuple[int, int], png: bytes) -> BuildCandidate | None:
        """What this menu is offering, if it is an upgrade this loop may take.

        Walls are skipped. They upgrade instantly and tie up no builder, so
        putting one on a wall would be the single mistake this loop exists to
        avoid, and `ai_coc walls` is where they belong.
        """
        if wall_menu(png) is not None:
            return None
        offers = upgrade_buttons(png)
        if not offers:
            return None
        logger.info(
            "Upgrade at (%d, %d): %d in %s",
            point[0],
            point[1],
            offers[0].price,
            offers[0].resource,
        )
        return BuildCandidate(point=point, resource=offers[0].resource, price=offers[0].price)

    def _priced(self, points: list[tuple[int, int]]) -> list[BuildCandidate]:
        """What each of these points is really offering, read off its own menu.

        **Each tap goes in on a confirmed village frame**, the rule the sweep is
        built around: these are raw village coordinates, so one that misses opens
        whatever is standing there, and a full-screen panel swallows every tap
        after it. A named point misses because the camera moved since somebody
        looked; a spotted one misses because it was a guess.
        """
        found: list[BuildCandidate] = []
        for point in points:
            png = self._after_tap(point, f"named_{point[0]:04d}_{point[1]:04d}")
            if read_stock(png) is None:
                logger.info("The tap at (%d, %d) covered the village; backing out", *point)
                if self._home() is None:
                    return found
                continue
            offer = self._offer(point, png)
            if offer is None:
                logger.info("Nothing upgradeable at (%d, %d)", *point)
                continue
            found.append(offer)
        return found

    def _scan(self) -> list[BuildCandidate]:
        """Every point on the sweep whose menu offers an upgrade, and what it costs."""
        found: list[BuildCandidate] = []
        for point, png in self._sweep("build"):
            offer = self._offer(point, png)
            if offer is not None:
                found.append(offer)
        return found

    def _pick(self, offers: list[BuildCandidate], gold: int, elixir: int) -> BuildCandidate | None:
        """The dearest upgrade the village can pay for, or None if it can pay for none.

        Dearest rather than cheapest, which is the opposite of the wall loop and
        for the opposite reason. There the loot is the scarce thing, so the
        cheapest batch buys the most wall. Here the *builder* is scarce — a
        village has five and each is busy for days — so one spent on something
        cheap is one not available for the upgrade that was holding the village
        back.

        This is the seam a planner would replace, since which building is worth
        raising is a judgement about the base rather than about the price.
        """
        purse = {"gold": gold, "elixir": elixir}
        affordable = [offer for offer in offers if offer.price <= purse[offer.resource]]
        if not affordable:
            return None
        return max(affordable, key=lambda offer: offer.price)

    def _start(self, offer: BuildCandidate, stock: VillageStock) -> bool:
        """Put a builder on this upgrade, and say whether the game took the money."""
        png = self._after_tap(offer.point, "menu")
        button = next(
            (
                found
                for found in upgrade_buttons(png)
                if found.resource == offer.resource and found.price == offer.price
            ),
            None,
        )
        if button is None:
            logger.info("The menu at (%d, %d) no longer offers what it did", *offer.point)
            return False
        self._tap(button.point)
        time.sleep(MENU_SETTLE)
        # Confirmation comes in two shapes and the building decides which. A wall
        # gets the little dialog; everything else opens a full-screen sheet —
        # 將金礦升至10級？ — with what the level buys and 確認 along the bottom.
        # Neither is waited for: whichever is there is answered, and a building
        # that starts without asking simply has neither.
        #
        # 確定 is safe here for the reason it is in the wall loop: nothing
        # between `_home` and this line presses `back`, and `back` is the only
        # thing that raises the dialog that must never be confirmed.
        asked = self._frame("confirm")
        dialog = game_dialog(asked)
        sheet = upgrade_sheet(asked)
        if dialog is not None:
            self._tap(dialog.confirm)
        elif sheet is not None:
            self._tap(sheet)
        time.sleep(BUY_SETTLE)
        # Through `_home` rather than a bare capture, because the sheet is still
        # closing and it covers the storage bars while it does. Read directly,
        # that came back as "could not be read" on an upgrade that had in fact
        # started — the builder count had dropped by the next run — which is a
        # loop under-reporting its own work.
        paid = self._home()
        if paid is None:
            logger.warning("The storages could not be read after starting the upgrade")
            return False
        # The storage having really moved is what separates an upgrade from a
        # report of one, exactly as in the wall loop: a tap the game swallows
        # raises nothing at all.
        spent = stock.gold - paid.gold if offer.resource == "gold" else stock.elixir - paid.elixir
        if spent < offer.price:
            logger.warning("Tapped 升級 but %s only moved by %d", offer.resource, spent)
            return False
        return True

    def builders(self) -> BuilderReport:
        """Read the builder panel: who is busy, and how long each of them has left.

        This is what makes waiting a decision rather than a guess. Every other
        loop here can only see the 1/5 beside the builder's head, so a village
        with none free reads the same whether the next one comes back in ten
        minutes or in two days — and the wall loop walked into exactly that, its
        every batch answered with 所有建築工人都在忙碌中 and nothing to say when
        trying again would be worth it.

        **The button toggles.** A panel left open by an earlier run closes on the
        first tap, so a read that comes back with no panel is worth one more tap
        before it is called a failure. The last tap shuts it again, because the
        panel covers the middle of the village and the next loop along taps there.
        """
        report = BuilderReport()
        if self._home() is None:
            report.message = "畫面沒辦法回到村莊，讀不到工人"
            return report
        counted = free_builders(self._frame("builders"))
        if counted is None:
            report.message = "讀不到工人數量，先停下來"
            return report
        report.free, report.total = counted
        queue = builder_jobs(self._after_tap(BUILDER_BUTTON, "panel"))
        if queue is None:
            queue = builder_jobs(self._after_tap(BUILDER_BUTTON, "panel"))
        if queue is None:
            report.message = f"工人 {report.free}/{report.total}，但工人面板打不開"
            return report
        report.queue = queue
        self._tap(BUILDER_BUTTON)
        time.sleep(MENU_SETTLE)
        logger.info(
            "%d of %d builder(s) free, %d upgrade(s) running",
            report.free,
            report.total,
            queue.running,
        )
        if not queue.remaining:
            report.message = f"工人 {report.free}/{report.total}，沒有在跑的升級"
            return report
        report.message = (
            f"工人 {report.free}/{report.total}，{queue.running} 個升級在跑，"
            f"最快的還要 {_spell_out(queue.remaining[0])}"
        )
        return report

    def upgrade(self) -> BuildReport:
        """Put every idle builder on the dearest upgrade the village can pay for."""
        report = BuildReport()
        stock = self._home()
        if stock is None:
            report.message = "畫面沒辦法回到村莊，建築升級沒有開始"
            return report
        builders = free_builders(self._frame("builders"))
        if builders is None:
            report.message = "讀不到工人數量，先停下來"
            return report
        logger.info("%d of %d builder(s) are free", builders[0], builders[1])
        if builders[0] == 0:
            report.message = f"{builders[1]} 個工人都在忙，沒有可以派的"
            return report
        offers = self._buildings()
        if not offers:
            report.message = "掃過村莊都沒有找到可以升級的建築"
            return report
        while offers and len(report.started) < builders[0]:
            offer = self._pick(
                offers, stock.gold - self.keep_gold, stock.elixir - self.keep_elixir
            )
            if offer is None:
                report.message = (
                    f"剩下的資源買不起任何升級，最便宜的要 {min(o.price for o in offers)}"
                )
                break
            if self._start(offer, stock):
                report.started.append(offer)
            # Either way this one is finished with: started, it is no longer on
            # offer, and refused, it is not worth asking about again this run.
            offers = [other for other in offers if other.point != offer.point]
            after = self._home()
            if after is None:
                report.message = "升級之後讀不到村莊，先停下來"
                break
            stock = after

        report.message = report.message or f"開始了 {len(report.started)} 個升級"
        return report
