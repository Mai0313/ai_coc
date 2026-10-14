"""What the builder and laboratory counters say, about whichever village is on screen.

Both villages draw the same two counters in the bottom right — the research
slots and the workmen — so one runner answers for either counter on either
village, and `parsers.home` holds the places to look. The shield is a bar of
its own in the top right, on the home village alone.

**It reads either village, and that is why it is not in `upkeep`.**
`UpkeepRunner` is a `GameRunner`, and `GameRunner._home` works on the home
village alone — right for a loop about to spend the home village's loot, wrong
for a question about whichever village is up. Neither crosses: `ai_coc world
--go` is how a caller moves the game, and `status` only ever looks, the same
bargain `ai_coc stock` makes.

Kept Qt-free like the rest of `ui/` outside the widgets, so `commands.py` can
drive it against the live game without a window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_coc.models import PlateCount
from ai_coc.ui.runner import ScreenRunner
from ai_coc.parsers.home import plate_count, shield_state
from ai_coc.parsers.world import current_world

if TYPE_CHECKING:
    from ai_coc.models import ShieldState


class PlateRunner(ScreenRunner):
    """The two counters and the shield, on whatever village is already up."""

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

    def shield(self) -> ShieldState | None:
        """Whether a shield is up, for the village on screen.

        None on the builder base, which has no such bar: it is real-time
        matchmaking against a live player, so a shield there would contradict
        the mode's own design.
        """
        return shield_state(self._frame("shield"))
