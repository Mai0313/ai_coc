from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging
from pathlib import Path
from datetime import UTC, datetime

from ai_coc.models import ClockTower, UpgradeTimer, AccountSnapshot, VillageDocument

if TYPE_CHECKING:
    from ai_coc.models import World, UpgradeRole, VillageExport

logger = logging.getLogger(__name__)

# Which village and whose slot an upgrade in each section is waiting on. The
# first eight were seen carrying a `timer`. Traps and siege machines have not
# been yet, but a trap is always a builder's job and a siege machine always a
# research slot's, and leaving them out would hide a builder they hold. One that
# turns up with a timer and is not here is reported with no role and no
# speed-up rather than guessed at.
TIMED_SECTIONS: dict[str, tuple[World, UpgradeRole]] = {
    "buildings": ("day", "builder"),
    "heroes": ("day", "builder"),
    "units": ("day", "lab"),
    "spells": ("day", "lab"),
    "pets": ("day", "pet"),
    "buildings2": ("night", "builder"),
    "heroes2": ("night", "builder"),
    "units2": ("night", "lab"),
    "traps": ("day", "builder"),
    "siege_machines": ("day", "lab"),
    "traps2": ("night", "builder"),
}

# The `boosts` key that speeds each slot up, and by how much. Measured on
# 2026-10-10 off exports taken a known number of seconds apart: with all four
# running, 59 s took 585 off every row under a 10 and 1404 off every row under a
# 24, and 130 s with the builder potion alone took 1302 off the home village's
# builders and 130 off everything else. The builder base's clock tower speeds
# up its builders and its laboratory alike. A boost's own counter keeps close
# to the clock but not exactly on it: 747 s gone over 758 s across a farming
# run that crossed between villages, which is seconds on any finish time here.
SPEED_UPS: dict[tuple[World, UpgradeRole], tuple[str, int]] = {
    ("day", "builder"): ("builder_boost", 10),
    ("day", "lab"): ("lab_boost", 24),
    ("day", "pet"): ("pet_boost", 24),
    ("night", "builder"): ("clocktower_boost", 10),
    ("night", "lab"): ("clocktower_boost", 10),
}

# The builder base's clock tower, by the community mapping's id.
CLOCK_TOWER = 1000039


def parse_village(path: str | Path) -> AccountSnapshot:
    return parse_village_text(Path(path).read_text(encoding="utf-8-sig"))


def parse_village_text(text: str) -> AccountSnapshot:
    """Every section the export carries, whichever ones this release happens to hold.

    There used to be a written-down list of twelve, which is what a game that
    adds a section between releases silently drops. Measured against one real
    export it was dropping ten of them — `helpers`, `decos`, `obstacles`,
    `skins`, `house_parts` and their builder-base twins — including the three
    named helpers the mapping does know. So the sections are whatever the
    document holds a list under, which is also what the tolerance rule in
    AGENTS.md asks for: a section nobody has heard of survives instead of
    needing a code change first.
    """
    document = VillageDocument.model_validate_json(text.lstrip("﻿").strip())
    entities = [entity for section in document.sections() for entity in document.entries(section)]
    return AccountSnapshot(tag=document.tag, raw=document, entities=entities)


def upgrade_timers(export: VillageExport) -> list[UpgradeTimer]:
    """Every upgrade still running on either village, soonest first, speed-ups included.

    The Builder's Apprentice and the Lab Assistant are not counted: one comes
    back to the job it is assigned to whenever its cooldown ends, so a row with
    `helper_recurrent` finishes earlier than this says. Left that way on
    purpose for now.
    """
    start = _start(export)
    timers: list[UpgradeTimer] = []
    for entity in export.entities:
        if entity.timer is None:
            continue
        place = TIMED_SECTIONS.get(entity.section)
        if place is None:
            logger.warning(
                "Section %s carries a timer and has no known slot; reported unboosted",
                entity.section,
            )
        boost, rate = SPEED_UPS[place] if place else (None, 1)
        left = getattr(export.boosts, boost) if boost else None
        seconds = _real_seconds(entity.timer, left, rate)
        timers.append(
            UpgradeTimer(
                world=place[0] if place else ("night" if entity.section.endswith("2") else "day"),
                role=place[1] if place else None,
                section=entity.section,
                data_id=entity.data_id,
                name=entity.name,
                level=entity.level,
                timer=entity.timer,
                boost=boost if left else None,
                seconds=seconds,
                done_at=_clock(start + seconds),
            )
        )
    return sorted(timers, key=lambda timer: timer.seconds)


def clock_tower(export: VillageExport) -> ClockTower | None:
    """When the builder base's clock tower can next be started, or None for no tower.

    Neither of its two counters in `boosts` means it can be started now: seen
    that way until the player started it, after which both appeared. A tower
    that is being upgraded is None as well, since whether it can be started
    then has not been seen either way.
    """
    tower = next((entity for entity in export.entities if entity.data_id == CLOCK_TOWER), None)
    if tower is None or tower.timer is not None:
        return None
    ready_in = export.boosts.clocktower_cooldown or 0
    return ClockTower(
        ready_in=ready_in,
        ready_at=_clock(_start(export) + ready_in),
        boosting=export.boosts.clocktower_boost,
    )


def _start(export: VillageExport) -> int:
    """The moment every counter in an export was read at."""
    return export.timestamp if export.timestamp is not None else int(time.time())


def _real_seconds(timer: int, left: int | None, rate: int) -> int:
    """How long a job really has, at `rate` times speed for `left` more seconds."""
    if not left:
        return timer
    if timer <= rate * left:
        return -(-timer // rate)
    return left + timer - rate * left


def _clock(epoch: int) -> str:
    """An instant as this machine's local time, to the second."""
    return datetime.fromtimestamp(epoch, UTC).astimezone().isoformat(timespec="seconds")
