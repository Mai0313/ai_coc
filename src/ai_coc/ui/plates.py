"""What the plates along the top say, about whichever village is on screen.

Both villages draw the same row up there — the research slots, the workmen, and
on the home village a shield — and every one of those plates opens the same
panel behind it: 升級中 over 建議升級 over 其他升級, one green bar per running
row with its countdown written above. So one runner answers for either plate on
either village, and `parsers.home` holds the four measured places to look.

**Nothing here crosses, and that is why it is not in `upkeep`.** `UpkeepRunner`
is a `GameRunner`, and `GameRunner._home` sails to the home village the moment
it finds the builder base — right for a loop about to spend the home village's
loot, wrong for a question about whichever village is up. `ai_coc world --go` is
how a caller moves the game; these commands only ever look, the same bargain
`ai_coc stock` makes.

Kept Qt-free like the rest of `ui/` outside the widgets, so `commands.py` can
drive it against the live game without a window.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import PlateJob, PlateReport, PlateJobNames
from ai_coc.prompts import render
from ai_coc.ui.runner import MENU_SETTLE, SPOT_TIMEOUT, ScreenRunner, spell_out
from ai_coc.adapters.ai import GeminiClient
from ai_coc.parsers.home import (
    jobs_strip,
    panel_rows,
    plate_count,
    plate_badges,
    plate_button,
    shield_state,
)
from ai_coc.parsers.world import current_world

if TYPE_CHECKING:
    from ai_coc.models import World, PlateRole, ShieldState

logger = logging.getLogger(__name__)

# How many times to tap a plate before calling the panel shut. The button
# toggles, so a panel somebody left open closes on the first tap and opens on
# the second; `UpkeepRunner.builders` has paid for this one already.
PANEL_TRIES = 2

# Only ever used to build a message. The parsers read none of these — they are
# what a person reads in a report.
PLATE_NAMES: dict[str, str] = {"lab": "實驗室", "builder": "建築工人", "shield": "護盾"}


class PlateRunner(ScreenRunner):
    """One plate and the panel behind it, on whatever village is already up."""

    # Who to ask what is being raised. A different tier from `ai` because it is a
    # different kind of call: a label off a crop, not a judgement about a whole
    # screen. None is an ordinary answer — the countdowns are what a caller
    # waiting on a builder needs, and the names are what makes the report
    # readable.
    namer: GeminiClient | None = None

    def read(self, role: PlateRole) -> PlateReport:
        """What this plate counts, and what its panel says is running.

        **The count comes off the frame taken before the tap.** Opening the
        panel covers the row that was just read — measured, `info_badges` finds
        nothing at all on a frame with one up — so the two readings cannot come
        from the same capture, and the one that places the plate has to be first.

        The panel is left closed whether or not it read, because it covers the
        middle of the village and the next command along taps there.
        """
        png = self._frame("plates")
        world = current_world(png)
        if world is None:
            return PlateReport(role=role, message="畫面不在村莊,讀不到上排的牌子")
        badges = plate_badges(png)
        centre = badges.get(role)
        if centre is None:
            return PlateReport(
                world=world, role=role, message=f"這個畫面上看不到{PLATE_NAMES[role]}的牌子"
            )
        report = PlateReport(world=world, role=role)
        if (counted := plate_count(png, centre)) is not None:
            report.free, report.total = counted
        rows: list[int | None] | None = None
        for _ in range(PANEL_TRIES):
            opened = self._after_tap(plate_button(centre), f"{role}_panel")
            if (rows := panel_rows(opened, world, role)) is not None:
                break
        if rows is None:
            # **Before the closing tap, not after it.** Two taps have already
            # left the panel however it was found — open, closed, open, or
            # closed, open, closed — and a third would open it. That matters
            # beyond a dirty screen: the panel covers the badge row, so the
            # next plate `status` reads answers 畫面不在村莊 on a village that
            # is plainly there. `UpkeepRunner.builders` returns here for the
            # same reason.
            #
            # **And no bars means one of two things**, which look identical on
            # screen: the panel never opened, or it opened with nothing running
            # behind it. The plate's own count separates them for nothing —
            # every slot idle is exactly the village with nothing to show — and
            # a count that would not read leaves the honest answer, which is
            # that nobody can say.
            report.message = (
                f"{PLATE_NAMES[role]} {report.free}/{report.total},沒有在跑的項目"
                if report.free is not None and report.free == report.total
                else f"{PLATE_NAMES[role]}的面板打不開,只讀到牌子上的數字"
            )
            return report
        self._tap(plate_button(centre))
        time.sleep(MENU_SETTLE)
        named = self._names(opened, world, role)
        report.jobs = [
            PlateJob(name=named[index] if index < len(named) else "", remaining=seconds)
            for index, seconds in enumerate(rows)
        ]
        logger.info("%s: %s/%s with %d running", role, report.free, report.total, len(report.jobs))
        report.message = self._sentence(report)
        return report

    def _names(self, png: bytes, world: World, role: PlateRole) -> list[str]:
        """What each running row is, or nothing at all without a model to ask.

        Empty is an ordinary answer and every caller treats it as one: the names
        are what makes a report readable, and the countdowns underneath them are
        what a caller waiting on a builder actually acts on. A miscount is worse
        than a blank, so a short answer leaves the rows after it unnamed rather
        than shifting the ones it did read onto the wrong countdowns.
        """
        if self.namer is None:
            return []
        strip = jobs_strip(png, world, role)
        if strip is None:
            return []
        try:
            answer = self.namer.generate_structured(
                render("read_plate_jobs"), PlateJobNames, strip, SPOT_TIMEOUT
            )
        except Exception:
            logger.warning("The names on the %s panel could not be read", role, exc_info=True)
            return []
        return answer.names

    def shield(self) -> ShieldState | None:
        """Whether a shield is up, for the village on screen.

        None on the builder base, which has no such plate: it is real-time
        matchmaking against a live player, so a shield there would contradict
        the mode's own design.
        """
        png = self._frame("shield")
        centre = plate_badges(png).get("shield")
        return None if centre is None else shield_state(png, centre)

    def _sentence(self, report: PlateReport) -> str:
        """The report as a line for a person, built from the fields rather than kept on one."""
        held = PLATE_NAMES[report.role]
        # Only the plate's own count, because the running count is on every line
        # below this one — carrying it here as well printed it twice.
        counted = f"{report.free}/{report.total}" if report.free is not None else "數字讀不到"
        timed = [job for job in report.jobs if job.remaining is not None]
        if not timed:
            return f"{held} {counted},{len(report.jobs)} 個在跑,但每一個的倒數都讀不到"
        next_up = min(timed, key=lambda job: job.remaining or 0)
        named = f"是{next_up.name}," if next_up.name else ""
        return (
            f"{held} {counted},{len(report.jobs)} 個在跑,"
            f"最快的{named}還要 {spell_out(next_up.remaining or 0)}"
        )
