"""What the plates along the top say, about whichever village is on screen.

Both villages draw the same row up there — the research slots, the workmen, and
on the home village a shield — and every one of those plates opens the same
panel behind it: 升級中 over 建議升級 over 其他升級, one green bar per running
row with its countdown written above. So one runner answers for either plate on
either village, and `parsers.home` holds the four measured places to look.

**It reads either village, and that is why it is not in `upkeep`.**
`UpkeepRunner` is a `GameRunner`, and `GameRunner._home` works on the home
village alone — right for a loop about to spend the home village's loot, wrong
for a question about whichever village is up. Neither crosses: `ai_coc world
--go` is how a caller moves the game, and these commands only ever look, the
same bargain `ai_coc stock` makes.

Kept Qt-free like the rest of `ui/` outside the widgets, so `commands.py` can
drive it against the live game without a window.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import PlateJob, PlateCount, PlateReport, PlateJobNames
from ai_coc.prompts import render
from ai_coc.ui.runner import MENU_SETTLE, SPOT_TIMEOUT, ScreenRunner
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
# the second.
PANEL_TRIES = 2


class PlateRunner(ScreenRunner):
    """One plate and the panel behind it, on whatever village is already up."""

    # Who to ask what is being raised. A different tier from `ai` because it is a
    # different kind of call: a label off a crop, not a judgement about a whole
    # screen. None is an ordinary answer — the countdowns are what a caller
    # waiting on a builder needs, and the names are what makes the report
    # readable.
    namer: GeminiClient | None = None

    def counts(self) -> tuple[PlateCount, PlateCount]:
        """The builders' and the research slots' own digits, off one frame and no tap."""
        png = self._frame("plates")
        world = current_world(png)
        badges = plate_badges(png) if world else {}
        found: list[PlateCount] = []
        for role in ("builder", "lab"):
            if world is None:
                found.append(PlateCount(role=role, outcome="not_a_village"))
            elif (centre := badges.get(role)) is None:
                found.append(PlateCount(world=world, role=role, outcome="no_badge"))
            elif (counted := plate_count(png, centre)) is None:
                found.append(PlateCount(world=world, role=role, outcome="unread"))
            else:
                free, total = counted
                found.append(
                    PlateCount(world=world, role=role, free=free, total=total, outcome="counted")
                )
        return found[0], found[1]

    def read(self, role: PlateRole) -> PlateReport:
        """What this plate counts, and what its panel says is running.

        **The count comes off the frame taken before the tap**, which costs
        nothing and is where it already was. The reason written here used to be
        that the panel covers the badge row, and the committed frames say
        otherwise: `info_badges` returns the full row on all four of them,
        identical to a clean frame of the same village, and `plate_count` reads
        through one as well. That is the whole premise `park_camera`'s panel
        check rests on — a panel is a village by every test the loops have,
        which is why nothing else can clear it.

        The panel is left closed whether or not it read, because it covers the
        middle of the village and the next command along taps there.
        """
        png = self._frame("plates")
        world = current_world(png)
        if world is None:
            return PlateReport(role=role, outcome="not_a_village")
        badges = plate_badges(png)
        centre = badges.get(role)
        if centre is None:
            return PlateReport(world=world, role=role, outcome="no_badge")
        # **`panel_shut` is what every path that returns before the panel reads
        # means**, so it is what the report is built with rather than a success
        # overwritten later: the count below and the taps after it can each end
        # the read, and a report that started out claiming `read` would be one
        # value away from saying so.
        report = PlateReport(world=world, role=role, outcome="panel_shut")
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
            # beyond a dirty screen: the panel sits over the middle of the map,
            # so it pins `view_shift` at no-move-at-all and every park under one
            # reports a camera that never started. It does **not** hide the badge
            # row — measured on all four panel frames, that reads through — so
            # the next `status` finds the village and parks under the panel
            # rather than failing loudly.
            #
            # **And no bars means one of two things**, which look identical on
            # screen: the panel never opened, or it opened with nothing running
            # behind it. The plate's own count separates them for nothing —
            # every slot idle is exactly the village with nothing to show — and
            # a count that would not read leaves `panel_shut`, which is the
            # honest answer that nobody can say.
            if report.free is not None and report.free == report.total:
                report.outcome = "idle"
            return report
        self._tap(plate_button(centre))
        time.sleep(MENU_SETTLE)
        named = self._names(opened, world, role)
        report.jobs = [
            PlateJob(name=named[index] if index < len(named) else "", remaining=seconds)
            for index, seconds in enumerate(rows)
        ]
        # Unconditional, because `panel_rows` has no empty answer: a band with no
        # bars in it comes back None and is handled above, so reaching here is a
        # panel with at least one row on it.
        report.outcome = "read"
        logger.info("%s: %s/%s with %d running", role, report.free, report.total, len(report.jobs))
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
