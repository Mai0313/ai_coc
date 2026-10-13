"""What the builder and laboratory counters say, about whichever village is on screen.

Both villages draw the same two counters in the bottom right — the research
slots and the workmen — and 建造 under them opens one panel with a tab for
each: the running rows, each a progress bar with its countdown, over 建議升級
and 其他升級. So one runner answers for either counter on either village, and
`parsers.home` holds the places to look. The shield is a bar of its own in the
top right, on the home village alone.

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
    PANEL_TABS,
    PANEL_CLOSE,
    BUILD_BUTTON,
    jobs_strip,
    panel_rows,
    plate_count,
    shield_state,
)
from ai_coc.parsers.world import current_world

if TYPE_CHECKING:
    from ai_coc.models import World, PlateRole, ShieldState

logger = logging.getLogger(__name__)


class PlateRunner(ScreenRunner):
    """One counter and the panel tab behind it, on whatever village is already up."""

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
        found: list[PlateCount] = []
        for role in ("builder", "lab"):
            if world is None:
                found.append(PlateCount(role=role, outcome="not_a_village"))
            elif (counted := plate_count(png, role)) is None:
                found.append(PlateCount(world=world, role=role, outcome="unread"))
            else:
                free, total = counted
                found.append(
                    PlateCount(world=world, role=role, free=free, total=total, outcome="counted")
                )
        return found[0], found[1]

    def read(self, role: PlateRole) -> PlateReport:
        """What this counter says, and what its panel tab says is running.

        **The count comes off the frame taken before the tap**, since the panel
        covers the counters. The panel opens on whichever tab was last open, so
        the tab is tapped as well.

        The panel is left closed whether or not it read, because it covers the
        right of the village and the next command along taps there — and it is
        only pressed shut where it was seen, since its X sits over the gold bar
        when it is not up.
        """
        png = self._frame("plates")
        world = current_world(png)
        if world is None:
            return PlateReport(role=role, outcome="not_a_village")
        # **`panel_shut` is what every path that returns before the panel reads
        # means**, so it is what the report is built with rather than a success
        # overwritten later.
        report = PlateReport(world=world, role=role, outcome="panel_shut")
        if (counted := plate_count(png, role)) is not None:
            report.free, report.total = counted
        self._tap(BUILD_BUTTON)
        time.sleep(MENU_SETTLE)
        opened = self._after_tap(PANEL_TABS[world, role], f"{role}_panel")
        rows = panel_rows(opened)
        if rows is None:
            return report
        # **Shut before the model is asked**, which reads the frame already in
        # hand: its call can run past a minute, and a run killed in it would
        # leave the panel over the village, where every village test reads None.
        self._tap(PANEL_CLOSE)
        time.sleep(MENU_SETTLE)
        read = self._names(opened, world, role) if rows else None
        if not rows:
            report.outcome = "idle"
            return report
        named = read.names if read else []
        report.jobs = [
            PlateJob(name=named[index] if index < len(named) else "", remaining=seconds)
            for index, seconds in enumerate(rows)
        ]
        report.suggested = list(read.suggested) if read else []
        report.outcome = "read"
        logger.info("%s: %s/%s with %d running", role, report.free, report.total, len(report.jobs))
        return report

    def _names(self, png: bytes, world: World, role: PlateRole) -> PlateJobNames | None:
        """What each running row is and what the game suggests next, or None without a model.

        None is an ordinary answer and every caller treats it as one: the names
        are what makes a report readable, and the countdowns underneath them are
        what a caller waiting on a builder actually acts on. A miscount is worse
        than a blank, so a short answer leaves the rows after it unnamed rather
        than shifting the ones it did read onto the wrong countdowns.
        """
        if self.namer is None:
            return None
        strip = jobs_strip(png)
        if strip is None:
            return None
        try:
            return self.namer.generate_structured(
                render("read_plate_jobs", world=world), PlateJobNames, strip, SPOT_TIMEOUT
            )
        except Exception:
            logger.warning("The names on the %s panel could not be read", role, exc_info=True)
            return None

    def shield(self) -> ShieldState | None:
        """Whether a shield is up, for the village on screen.

        None on the builder base, which has no such bar: it is real-time
        matchmaking against a live player, so a shield there would contradict
        the mode's own design.
        """
        return shield_state(self._frame("shield"))
