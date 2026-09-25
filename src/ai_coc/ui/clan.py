"""Give troops to whoever in the clan is asking for them.

Donating is the one job here that cannot be done on demand: it needs somebody
else to have asked first, and the request expires. So this is written to be run
often and to cost nothing when there is nothing to give — it opens the chat,
looks for a request, and comes straight back if there is none.

The panel does not confirm anything. A tap on a troop card gives that many
troops away there and then, which is why `dry_run` exists: it walks the whole
path and stops with the panel open, reporting what it would have given.
"""

from __future__ import annotations

import time
import logging

from ai_coc.models import DonateReport
from ai_coc.ui.runner import BACK_SETTLE, GameRunner
from ai_coc.parsers.clan import donation_panel, donatable_cards, panel_difference, reinforce_button

logger = logging.getLogger(__name__)

# The chat tab down the left edge of the village. One of the few coordinates in
# the project that is genuinely fixed: it is the game's own furniture, not
# anything standing on the map.
CHAT_TAB = (65, 400)
# How long a donation takes to register. The card repaints with one fewer troop,
# or greys out entirely once the village has no more of them.
DONATE_SETTLE = 1.2
# How many cards to give from in one run. Nothing stops at a number the game
# imposes — the panel simply runs out of cards in colour — so this is only here
# to keep a run bounded.
MAX_GIFTS = 12
# How much of the panel has to repaint for a tap to count as a donation.
# Measured on a live donation: one barbarian given away moved 28605 bytes, and
# the cards themselves moved none at all — a card holding twenty gives five and
# still reads five, which is why the cards cannot be the evidence. What did
# change is the 已增援 row along the bottom, which gained an icon.
PANEL_MOVED = 2000


class ClanRunner(GameRunner):
    """Gives troops away for as long as somebody is asking and the village has some."""

    # 0 gives until the request is full or the village has nothing left to give.
    rounds: int = 0
    # Walk the whole path and stop before the tap that actually gives something.
    dry_run: bool = False

    def _leave(self) -> None:
        """Close the donation panel and the chat behind it.

        `back` is safe on both — neither is the home village, which is the one
        screen where it raises 確定退出遊戲嗎 — and `_home` picks up whatever it
        does not manage to close.
        """
        for _ in range(2):
            self.adb.back(self.display)
            time.sleep(BACK_SETTLE)

    def donate(self) -> DonateReport:
        """Give what the village can to the first request in the clan chat."""
        report = DonateReport(outcome="nothing_given")
        if self._home() is None:
            report.outcome = self._lost()
            return report
        button = reinforce_button(self._after_tap(CHAT_TAB, "chat"))
        if button is None:
            self._leave()
            report.outcome = "nobody_asking"
            return report
        logger.info("A request is asking; 增援 is at (%d, %d)", *button)
        png = self._after_tap(button, "panel")
        if not donation_panel(png):
            self._leave()
            report.outcome = "panel_shut"
            return report
        limit = self.rounds if self.rounds > 0 else MAX_GIFTS
        while len(report.gifts) < limit:
            cards = donatable_cards(png)
            if not cards:
                break
            report.offered = max(report.offered, len(cards))
            if self.dry_run:
                logger.info("Dry run: %d card(s) could be given, giving none", len(cards))
                report.outcome = "dry_run"
                break
            # Leftmost first, which is cheapest first: the panel lays troops out
            # in unlock order. This is the seam a planner would replace, since
            # which troop a request actually wants is written in its message and
            # nothing here reads Chinese.
            self._tap(cards[0])
            time.sleep(DONATE_SETTLE)
            after = self._frame("given")
            moved = panel_difference(png, after)
            logger.info("The panel moved by %d bytes after the tap", moved)
            if moved < PANEL_MOVED:
                # A tap the game swallowed looks exactly like one it took, so
                # the panel repainting is the only evidence either way.
                logger.warning("Tapped a card but the panel did not change")
                break
            report.gifts.append(cards[0])
            png = after
        self._leave()
        if report.gifts:
            report.outcome = "donated"
        return report
