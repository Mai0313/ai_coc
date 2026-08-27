"""Raise a hero in the 英雄殿堂, and read what the hall is asking for each of them.

The fourth upkeep job, and the one with the longest reach: a hero level takes
the better part of a day and there is exactly one builder's worth of it running
at a time, so a hall left alone is the village standing still in the way that
costs the most. Like the rest of `ui/` outside the widgets it is Qt-free, so
`commands.py` drives it against the live game with no window.

It follows the wall loop's rule — **read the UI the game paints on top, never
the thing underneath**. The hall is found by tapping across the village and
asking what opened, the hero is identified by the flat colour of his card's
banner, and the price is read off the green button rather than from any table of
what a level ought to cost.

**Nothing is tapped that the run has not first worked out it can pay for.** The
game answers an upgrade it cannot afford with a gem purchase, and the whole
authorization story in this project is that gems stay structurally unreachable:
the price is read, the storages are read, and the button is only tapped when the
arithmetic says the village can cover it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import HeroCard, HeroKind, HeroReport, VillageStock
from ai_coc.ui.runner import BACK_SETTLE, MENU_SETTLE, SWEEP_STAGGER, GameRunner
from ai_coc.parsers.hero import SCROLL_LEFT, SCROLL_RIGHT, can_scroll, hero_cards, hall_buttons
from ai_coc.parsers.home import free_builders
from ai_coc.parsers.building import upgrade_sheet

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# How long the game takes to charge for an upgrade and repaint. The same figure
# the wall loop and the building loop both measured for the same thing.
BUY_SETTLE = 1.5
# How long the card row takes to slide one place along.
SCROLL_SETTLE = 0.8
# How many screenfuls to walk before giving up on a row that will not stop
# scrolling. Six heroes fit in two screenfuls today; this leaves room for the
# ones the game has not added yet.
MAX_PAGES = 8
# How many times to tap a remembered spot before deciding the hall is not there.
# Buildings overlap and a tap on two of them cycles between them, so the same
# coordinate opens a different building each time: measured at the hall's own
# spot, the cycle was hall, archer tower, hall. Four covers a three-deep pile.
AT_TRIES = 4


class HeroRunner(GameRunner):
    """Opens the 英雄殿堂, reads every card in it, and raises the one asked for."""

    # Which hero to raise. None reads the hall and starts nothing, which is the
    # half of this command worth running on its own: what each hero costs next
    # is the number the decision rests on, and reading it spends nothing.
    hero: HeroKind | None = None
    # Where the hall is, for a run that already knows and would rather not spend
    # a minute sweeping the village to find out again.
    at: tuple[int, int] | None = None

    def _pages(self, arrow: tuple[int, int]) -> Iterator[list[HeroCard]]:
        """Each screenful of cards from here on, scrolling until the row stops.

        The arrow is read before it is tapped. An end the row has run out of is
        not drawn, and what stands in its place is a card — so a run that tapped
        blindly would open a hero's own screen instead of scrolling.
        """
        for _ in range(MAX_PAGES):
            png = self._frame("cards")
            page = hero_cards(png)
            if not page:
                logger.info("The hall is no longer on screen; the walk stops here")
                return
            yield page
            if not can_scroll(png, arrow):
                return
            self._tap(arrow)
            time.sleep(SCROLL_SETTLE)

    def _walk(self) -> dict[HeroKind, HeroCard]:
        """Every hero the hall holds, walked right to the end and back again.

        Both directions, because the row remembers where it was left: a run that
        only went right would see nothing new when the last run had already
        scrolled to the far end.
        """
        found: dict[HeroKind, HeroCard] = {}
        for arrow in (SCROLL_RIGHT, SCROLL_LEFT):
            for page in self._pages(arrow):
                for card in page:
                    # A card is seen on several pages, and a priced reading beats
                    # an unpriced one whichever came first: keeping the first
                    # sighting would let one frame that failed to resolve a price
                    # report a hero as 升級中 and cost him a builder-day.
                    if card.price is not None or card.hero not in found:
                        found[card.hero] = card
        return found

    def _bring_on(self, hero: HeroKind) -> HeroCard | None:
        """Scroll until this hero's card is on screen, and hand that card back.

        `_walk` ends wherever the row ran out, which is the far end from the
        hero it was looking for as often as not — so the card it read is not the
        card in front of the run any more. Tapping the remembered point would
        tap whichever card has since slid into that place.
        """
        for arrow in (SCROLL_RIGHT, SCROLL_LEFT):
            for page in self._pages(arrow):
                found = next((card for card in page if card.hero == hero), None)
                if found is not None:
                    return found
        return None

    def _try_menu(self, png: bytes) -> bytes | None:
        """Open whatever sits right of this menu's 升級, if it turns out to be the hall.

        Tapping and reading is the whole test. Nothing on the button says what it
        is — it is a gold crown, which is artwork — so what settles it is whether
        the screen that came up has hero cards on it. A screen that does not is
        backed out of and costs one capture.
        """
        for point in hall_buttons(png):
            shot = self._after_tap(point, "hall")
            if hero_cards(shot):
                return shot
            logger.debug("The button at (%d, %d) did not open the hall", *point)
            if self._home() is None:
                return None
        return None

    def _open(self) -> bytes | None:
        """Get the 英雄殿堂 on screen, by the remembered spot or by sweeping."""
        if self.at is not None:
            # Buildings overlap, and a tap that lands on two of them cycles:
            # measured live at (990, 430), three taps in a row selected the hall,
            # then the archer tower beside it, then the hall again. So one tap
            # on the right spot is not the same thing as opening the right
            # building, and the way to tell is to open it and look.
            for _ in range(AT_TRIES):
                found = self._try_menu(self._after_tap(self.at, "menu"))
                if found is not None:
                    return found
            logger.info("Nothing at (%d, %d) opened the hall; sweeping for it", *self.at)
            if self._home() is None:
                return None
        # Two passes, the second landing between the first one's points. The
        # hall is smaller than the grid steps, so one pass can step over it
        # entirely — measured on this village it sits 86 px from the nearest
        # grid point and 14 px from the staggered one, and the first pass walked
        # the whole village without ever opening it. The second pass is only
        # paid for when the first came back empty.
        for offset in ((0, 0), SWEEP_STAGGER):
            for point, png in self._sweep("hall", offset):
                found = self._try_menu(png)
                if found is not None:
                    logger.info("The 英雄殿堂 is at (%d, %d)", *point)
                    return found
            # A pass ends either because it finished or because it lost the
            # village, and only one of those is worth starting another on: the
            # second would open with a blind tap on whatever screen the first
            # could not get out of.
            if self._home() is None:
                return None
        return None

    def _close(self) -> None:
        """Shut the hall, if that is still what is on screen.

        **`back` is safe here and only here.** The hall is a panel over the
        village, and on the village itself the same key raises 確定退出遊戲嗎 —
        drawn as the same panel with 確定 in the same green in the same pixels as
        the sheet that pays for an upgrade. So the frame is read first, and a
        run that has already made its way home presses nothing.

        It also reads the hall before pressing rather than leaving the whole job
        to `_home`, which cannot: the storages do not read on the hall either, so
        `_home` would answer it with the same `back` having spent a capture and a
        settle finding out what this already knows.
        """
        if hero_cards(self._frame("close")):
            self.adb.back(self.display)
            time.sleep(BACK_SETTLE)

    def _affordable(self, card: HeroCard, stock: VillageStock) -> bool:
        """Whether the village is holding what this level asks for.

        This is the guard, not a courtesy. An upgrade the village cannot cover is
        answered by the game with a gem purchase, and nothing below the prompt
        layer would stop a loop walking into one.
        """
        if card.price is None or card.resource is None:
            return False
        held = stock.elixir if card.resource == "elixir" else stock.dark
        if held < card.price:
            logger.info(
                "%s asks %d %s and the village holds %d",
                card.hero,
                card.price,
                card.resource,
                held,
            )
            return False
        return True

    def _start(self, card: HeroCard) -> bool:
        """Tap 升級 and answer the sheet, and say whether the sheet came up.

        The sheet is what confirms; a tap the game swallows raises nothing at
        all, so a run that assumed one landed would report an upgrade it never
        started. Whether the money really moved is settled by the caller against
        the storages, exactly as in the other two loops that spend.
        """
        self._tap(card.point)
        time.sleep(MENU_SETTLE)
        asked = self._frame("confirm")
        # `hero_cards` first, because the hall's own cards carry a green button
        # in the same place the sheet carries 確認 and `upgrade_sheet` answers
        # either of them. A frame still showing cards is a tap that never landed.
        if hero_cards(asked):
            logger.warning("升級 was tapped but the hall is still showing its cards")
            return False
        confirm = upgrade_sheet(asked)
        if confirm is None:
            logger.warning("升級 was tapped but no confirmation sheet came up")
            return False
        self._tap(confirm)
        time.sleep(BUY_SETTLE)
        return True

    def run(self) -> HeroReport:
        """Read the hall, and put a builder on the hero this run was given."""
        report = HeroReport()
        stock = self._home()
        if stock is None:
            report.message = "畫面沒辦法回到村莊，英雄升級沒有開始"
            return report
        builders = free_builders(self._frame("builders"))
        if builders is None:
            report.message = "讀不到工人數量，先停下來"
            return report
        logger.info("%d of %d builder(s) are free", builders[0], builders[1])
        # A run that was asked to spend and has no builder to spend with is
        # finished here, before the sweep. Every builder busy is the ordinary
        # state of a farming village, and finding the hall costs minutes — a
        # tap on each of up to 52 grid points across two passes, plus a candidate
        # button on every menu that offers one, some of which open a full screen
        # to back out of.
        # Reading the hall still runs, because that costs nothing to be wrong
        # about and is the half worth having when nothing can be started.
        if self.hero is not None and builders[0] == 0:
            report.message = f"{builders[1]} 個工人都在忙，{self.hero} 現在派不出去"
            return report
        if self._open() is None:
            report.message = "掃過村莊都沒有找到英雄殿堂"
            return report
        cards = self._walk()
        report.cards = list(cards.values())
        logger.info(
            "The hall holds %s",
            "／".join(
                f"{card.hero}:{card.price if card.price is not None else '升級中'}"
                for card in report.cards
            ),
        )
        if self.hero is None:
            report.message = f"英雄殿堂裡讀到 {len(report.cards)} 個英雄"
        else:
            report.message = self._raise(report, cards, stock)
        self._close()
        return report

    def _blocked(self, card: HeroCard, stock: VillageStock) -> str | None:
        """Why this hero cannot be raised now, or None with nothing in the way.

        Both are answered off what the walk already read, so a run that stops
        here spends nothing and taps nothing further. The third reason to stop —
        no builder free — is settled before the sweep, because that one is known
        before anything has been looked for.
        """
        if card.price is None:
            return f"{self.hero} 現在沒有升級按鈕，多半正在升級中"
        if not self._affordable(card, stock):
            return f"資源不夠，{self.hero} 要 {card.price} {card.resource}"
        return None

    def _purchase(self, card: HeroCard, stock: VillageStock) -> tuple[HeroCard | None, str]:
        """Buy this level, and say both what was started and what happened."""
        here = self._bring_on(card.hero)
        if here is None or here.price is None or here.price != card.price:
            return None, f"{self.hero} 不在畫面上了，這一輪先不動它"
        if not self._start(here):
            return None, f"點了 {self.hero} 的升級，但沒有出現確認畫面"
        self._close()
        paid = self._home()
        if paid is None:
            return None, f"開始升級 {self.hero} 之後讀不到村莊，先停下來"
        # The storage having really moved is what separates an upgrade from a
        # report of one. A swallowed tap raises nothing at all, and the sheet
        # closing looks the same either way.
        spent = stock.elixir - paid.elixir if here.resource == "elixir" else stock.dark - paid.dark
        if spent < here.price:
            return None, f"確認了 {self.hero} 的升級，但{here.resource}只少了 {spent}"
        return here, f"{self.hero} 開始升級，花了 {here.price} {here.resource}"

    def _raise(
        self, report: HeroReport, cards: dict[HeroKind, HeroCard], stock: VillageStock
    ) -> str:
        """Start the upgrade this run was asked for, and say what came of it."""
        card = cards.get(self.hero) if self.hero is not None else None
        if card is None:
            return f"英雄殿堂裡沒有看到 {self.hero}"
        blocked = self._blocked(card, stock)
        if blocked is not None:
            return blocked
        report.started, message = self._purchase(card, stock)
        return message
