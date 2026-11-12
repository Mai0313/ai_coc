from __future__ import annotations

from typing import TYPE_CHECKING
import logging
from pathlib import Path
from datetime import UTC, datetime
from collections import Counter

from ai_coc.models import (
    SearchHit,
    ClockTower,
    SearchNeed,
    SearchNext,
    LevelChange,
    NextUpgrade,
    UpgradeTimer,
    SearchUpgrade,
    VillageExport,
    HelperCooldown,
    VillageDocument,
)

from .gamedata import TABLES, READING

if TYPE_CHECKING:
    from ai_coc.models import (
        Hall,
        World,
        Boosts,
        ExportRow,
        GameTable,
        GameEntity,
        UpgradeRole,
        GameEntities,
        LegacyExport,
        SearchOptions,
        SearchSection,
    )

logger = logging.getLogger(__name__)

# Whose slot upgrading a row of each section takes, by the home village's name
# (a builder base section carries a trailing 2 and the same role). The first
# eight sections were seen carrying a `timer`. Traps and siege machines have
# not been yet, but a trap is always a builder's job and a siege machine always
# a research slot's, and leaving them out would hide a builder they hold.
# Helpers, guardians and hero equipment take none of the three. The order is
# the one `growth` lists in: heroes first, as the user reads it.
ROLES: dict[str, UpgradeRole] = {
    "heroes": "builder",
    "buildings": "builder",
    "traps": "builder",
    "units": "lab",
    "spells": "lab",
    "siege_machines": "lab",
    "pets": "pet",
}

# Which `search --section` each export section is listed under, as the build
# panel groups them (the user's grouping of 2026-11-12): the builders' tab
# holds the guardians too, though `ROLES` gives them no slot. A section missing
# here is listed under its own name.
SECTIONS: dict[str, SearchSection] = {
    "heroes": "builder",
    "buildings": "builder",
    "traps": "builder",
    "guardians": "builder",
    "units": "lab",
    "spells": "lab",
    "siege_machines": "lab",
    "pets": "pets",
    "equipment": "equipment",
    "helpers": "helpers",
}
# The export section one the village lacks would sit in, by its game table.
# A siege machine is a `characters` row too, told from a troop by the workshop
# unlocking it (`_kind`).
TABLE_KINDS: dict[GameTable, str] = {
    "heroes": "heroes",
    "buildings": "buildings",
    "traps": "traps",
    "guardians": "guardians",
    "characters": "units",
    "spells": "spells",
    "pets": "pets",
    "character_items": "equipment",
    "villager_apprentices": "helpers",
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

# Sections the saved export leaves out on both villages: bought or cleared
# rather than upgraded, and long (the user's call of 2026-11-11).
DROPPED = frozenset({"decos", "obstacles", "house_parts", "skins", "sceneries"})

# The builder base's clock tower, by its `GlobalID` in the game's buildings table.
CLOCK_TOWER = 1000039

# The tables whose first level `search` says what unlocks: the troops' and
# spells', where rows that building does not unlock are told apart. Elsewhere
# the village lacks mostly rows it never gets, whose first level asks what it
# already has (a duplicate of an equipment it owns asks blacksmith 1) or never
# will (a retired trap asks town hall 9999).
UNLOCKED = frozenset(spec.table for spec in TABLES if spec.unlock)


def parse_village(path: str | Path) -> VillageDocument:
    return parse_village_text(Path(path).read_text(encoding="utf-8-sig"))


def parse_village_text(text: str) -> VillageDocument:
    """The game's export as it comes off the clipboard, every section it holds included.

    There used to be a written-down list of twelve sections, which is what a
    game that adds a section between releases silently drops. So a section is
    whatever the document holds a list under (`VillageDocument.sections`),
    which is also what the tolerance rule in .agents/rules/runtime.md asks
    for: a section nobody has heard of survives instead of needing a code
    change first.
    """
    return VillageDocument.model_validate_json(text.lstrip("﻿").strip())


def merge(document: VillageDocument, tables: GameEntities | None, at: int) -> VillageExport:
    """The game's export with the game's own tables merged into every row, taken at `at`.

    Without `tables` the rows keep what the game wrote plus what needs no
    table: `role`, `done_at` and `ready_at`, a working helper left uncounted.
    """
    known = {entity.data_id: entity for entity in tables.entities} if tables else {}
    sections = {section: document.entries(section) for section in document.sections()}
    halls = _hall_levels([row for rows in sections.values() for row in rows], tables)
    villages: dict[World, dict[str, list[ExportRow]]] = {"day": {}, "night": {}}
    for section, rows in sections.items():
        world: World = "night" if section.endswith("2") else "day"
        name = section.removesuffix("2") if world == "night" else section
        if name in DROPPED:
            continue
        villages[world][name] = [
            row.model_copy(
                update=_tabled(row, name, known.get(row.data), halls) | _clocked(row, name, at)
            )
            for row in rows
        ]
    # Timed once every row is tabled, since a job's finish needs its helper's level.
    helping = _helping(villages["day"].get("helpers", []))
    for world, named in villages.items():
        for name, rows in named.items():
            named[name] = [
                row.model_copy(update=_done(row, world, document.boosts, helping, at))
                for row in rows
            ]
    tower = _tower(villages["night"].get("buildings", []), document.boosts, at)
    return VillageExport(
        tag=document.tag,
        exported_at=_clock(at),
        timestamp=at,
        game_version=tables.game_version if tables else None,
        halls=halls,
        boosts=document.boosts,
        clock_tower_ready_at=tower.ready_at if tower else None,
        day=villages["day"],
        night=villages["night"],
    )


def _hall_levels(rows: list[ExportRow], tables: GameEntities | None) -> dict[Hall, int]:
    """The village's level of each hall the tables name that it has.

    A data_id is one entity across both villages, so its highest level
    anywhere is that hall's level.
    """
    levels: dict[int, int] = {}
    for row in rows:
        if row.lvl is not None:
            levels[row.data] = max(levels.get(row.data, 0), row.lvl)
    halls = tables.halls if tables else {}
    return {hall: levels[data_id] for hall, data_id in halls.items() if data_id in levels}


def _tabled(
    row: ExportRow, section: str, entity: GameEntity | None, halls: dict[Hall, int]
) -> dict[str, object]:
    """What the game's tables add to one row: its names, its level caps and its next upgrade.

    A helper's row also gets the slot it speeds and its level's multiplier.
    """
    if entity is None:
        return {}
    added: dict[str, object] = {"zh": entity.zh, "en": entity.en, "max_lvl": entity.max_lvl}
    if entity.speeds is not None:
        level = next((level for level in entity.levels if level.lvl == row.lvl), None)
        if level is not None and level.multiplier:
            added |= {"speeds": entity.speeds, "multiplier": level.multiplier}
    if any(level.needs for level in entity.levels):
        allowed = [
            level.lvl
            for level in entity.levels
            if all(halls.get(hall, 0) >= need for hall, need in level.needs.items())
        ]
        if allowed:
            added["limited_lvl"] = max(allowed)
    # A row with no level (the Crafting Station writes none) has no next one.
    if row.lvl is None:
        return added
    if row.lvl > entity.max_lvl:
        logger.warning(
            "%s %s is at level %d, past the %d its tables know: the tables are behind the game",
            section,
            entity.en or row.data,
            row.lvl,
            entity.max_lvl,
        )
    if any(level.cost for level in entity.levels):
        # An upgrading row is already buying the level above its own.
        target = row.lvl + (2 if row.timer is not None else 1)
        level = next((level for level in entity.levels if level.lvl == target), None)
        added["next"] = (
            NextUpgrade(lvl=level.lvl, cost=level.cost, time=level.time) if level else None
        )
    return added


def _clocked(row: ExportRow, section: str, at: int) -> dict[str, object]:
    """Whose slot upgrading one row takes, and when its helper cooldown runs out."""
    added: dict[str, object] = {}
    role = ROLES.get(section)
    if role is not None:
        added["role"] = role
    elif row.timer is not None:
        logger.warning(
            "Section %s carries a timer and has no known slot; reported unboosted", section
        )
    if row.helper_cooldown is not None:
        added["ready_at"] = _clock(at + row.helper_cooldown)
    return added


def _done(
    row: ExportRow, world: World, boosts: Boosts, helping: dict[UpgradeRole, ExportRow], at: int
) -> dict[str, object]:
    """When a running row's timer runs out, speed-ups and a working helper included."""
    if row.timer is None:
        return {}
    _, _, seconds = _finish(world, row, boosts, helping)
    return {"done_at": _clock(at + seconds)}


def from_legacy(old: LegacyExport, tables: GameEntities) -> VillageExport:
    """A file saved before the merge, merged as if the game had just exported it.

    The rows go back to the game's own keys, which the old file kept under
    other names. It wrote a count of 1 on every row, so a `cnt` of 1 the game
    did write cannot be told from one it left out, and neither is written;
    both mean one.
    """
    sections: dict[str, list[dict[str, object]]] = {}
    for row in old.entities:
        raw: dict[str, object] = {"data": row.data_id}
        if row.level is not None:
            raw["lvl"] = row.level
        if row.count != 1:
            raw["cnt"] = row.count
        sections.setdefault(row.section, []).append(raw | (row.model_extra or {}))
    document = VillageDocument.model_validate({
        "tag": old.tag,
        "timestamp": old.timestamp,
        "boosts": old.boosts,
        **sections,
    })
    at = old.timestamp or int(datetime.fromisoformat(old.exported_at).timestamp())
    return merge(document, tables, at)


def upgrade_timers(export: VillageExport) -> list[UpgradeTimer]:
    """Every upgrade still running on either village, soonest first, speed-ups included.

    A Builder's Apprentice or Lab Assistant is counted for what it has left of
    its work on the job (`helper_timer`), and nothing past that: one comes back
    to a job with `helper_recurrent` some time after its cooldown ends (30 to
    70 minutes later on 2026-11-10 and 11-11), which cannot be timed, so such
    a row can still finish earlier than this says.
    """
    helping = _helping(export.day.get("helpers", []))
    timers: list[UpgradeTimer] = []
    for world, section, row in export.rows():
        if row.timer is None:
            continue
        boost, helper, seconds = _finish(world, row, export.boosts, helping)
        timers.append(
            UpgradeTimer(
                world=world,
                role=row.role,
                section=section,
                data_id=row.data,
                zh=row.zh,
                en=row.en,
                level=row.lvl,
                max_lvl=row.max_lvl,
                limited_lvl=row.limited_lvl,
                next=row.next,
                timer=row.timer,
                extra=bool(row.extra),
                boost=boost,
                helper=helper.data if helper else None,
                helper_until=_clock(export.timestamp + row.helper_timer)
                if row.helper_timer
                else None,
                seconds=seconds,
                done_at=_clock(export.timestamp + seconds),
            )
        )
    return sorted(timers, key=lambda timer: timer.seconds)


def clock_tower(export: VillageExport) -> ClockTower | None:
    """When the builder base's clock tower can next be started, or None for no tower."""
    return _tower(export.night.get("buildings", []), export.boosts, export.timestamp)


def _tower(buildings: list[ExportRow], boosts: Boosts, at: int) -> ClockTower | None:
    """The clock tower among the builder base's buildings, as of `at`.

    Neither of its two counters in `boosts` means it can be started now: seen
    that way until the player started it, after which both appeared. A tower
    that is being upgraded is None as well, since whether it can be started
    then has not been seen either way.
    """
    tower = next((row for row in buildings if row.data == CLOCK_TOWER), None)
    if tower is None or tower.timer is not None:
        return None
    ready_in = boosts.clocktower_cooldown or 0
    return ClockTower(
        ready_in=ready_in, ready_at=_clock(at + ready_in), boosting=boosts.clocktower_boost
    )


def helper_cooldowns(export: VillageExport) -> list[HelperCooldown]:
    """When each helper of the Helper Hut can next be sent, soonest first.

    One with no `helper_cooldown` can be sent now: the Lab Assistant carried
    none in both exports of 2026-11-07, while the player had not sent it, and
    69437 the next day, after he had.
    """
    return sorted(
        (
            HelperCooldown(
                data_id=row.data,
                zh=row.zh,
                en=row.en,
                level=row.lvl,
                ready_in=row.helper_cooldown or 0,
                ready_at=_clock(export.timestamp + (row.helper_cooldown or 0)),
            )
            for row in export.day.get("helpers", [])
        ),
        key=lambda helper: helper.ready_in,
    )


def growth(start: VillageExport, end: VillageExport) -> list[LevelChange]:
    """Every row a builder or a research slot upgrades whose levels differ between two exports of one account.

    Counted per level rather than per row, since the export lumps a building's
    copies of one level into a row and keeps no identity across exports. Pets,
    hero equipment and everything bought or cleared rather than upgraded are
    left out (the user's call of 2026-11-07). The later export's name wins.
    """
    before, earlier = _levels(start)
    after, later = _levels(end)
    order = list(ROLES)
    changes = [
        LevelChange(
            world=key[0],
            role=role,
            section=section,
            data_id=key[1],
            zh=zh,
            before=dict(before.get(key, {})),
            after=dict(after.get(key, {})),
        )
        for key, (role, section, zh) in (earlier | later).items()
        if before.get(key) != after.get(key)
    ]
    return sorted(
        changes,
        key=lambda change: (
            change.role != "builder",
            order.index(change.section),
            change.zh or "",
            change.data_id,
        ),
    )


def search(
    tables: GameEntities | None, export: VillageExport | None, asked: SearchOptions
) -> list[SearchHit]:
    """Every data_id the query and the filters leave, with the player's rows of it, quickest to upgrade first.

    The order is by the list time of the quickest next upgrade a copy can
    start now; the rest follow, the owned ones first.

    An all-digit query is one exact data_id; anything else is a piece of the
    Chinese name or, ignoring case, of the English one. A query searches every
    entity of the tables, plus any data_id the export holds that they lack;
    without one only what the export holds is listed. Each filter narrows
    either the same way: `section` keeps one `SearchSection` (`all` and None
    keep every one), `world` one village, and `skip_max_lvl` drops a data_id
    whose every row stands at its last level, and under `every_row` each such
    row. One held under its last level by a hall stays. A troop or
    spell the export does not hold carries what its first level needs, against the
    levels the export's rows stand at, which an export saved with older
    tables lacks in its own `halls`. Tables an older `READING` built carry
    none: they give a troop's first level the laboratory rather than what
    unlocks it.
    """
    known = {entity.data_id: entity for entity in tables.entities} if tables else {}
    owned: dict[int, list[tuple[World, str, ExportRow]]] = {}
    for where, name, row in export.rows() if export else ():
        owned.setdefault(row.data, []).append((where, name, row))
    ids = [*known, *(i for i in owned if i not in known)] if asked.query else list(owned)
    current = tables is not None and tables.reading == READING
    halls = tables.halls if tables else {}
    village = _hall_levels([row for _, _, row in export.rows()], tables) if export else {}
    found = []
    for i in ids:
        entity, rows = known.get(i), owned.get(i, [])
        shown = [row for _, _, row in rows if not (asked.skip_max_lvl and _maxed(row, entity))]
        if rows and not shown:
            continue
        hit = _hit(
            i,
            entity,
            rows,
            shown if asked.every_row else None,
            _unlock(entity, known, halls, village)
            if current and export is not None and not rows
            else [],
        )
        if (
            asked.section in (None, "all", hit.section)
            and asked.world in (None, hit.world)
            and _named(hit, asked.query)
        ):
            found.append(hit)
    return sorted(found, key=_order)


def _order(hit: SearchHit) -> tuple[bool, int, bool, int]:
    """Where a hit is listed: by the list time of the quickest next upgrade a copy can start now, then the rest."""
    quickest = min(
        (one.time for one in hit.next if one.ready and one.time is not None), default=None
    )
    return (quickest is None, quickest or 0, hit.count == 0, hit.data_id)


def _maxed(row: ExportRow, entity: GameEntity | None) -> bool:
    """Whether a row stands at the game's last level of it; False where either level is unknown."""
    top = entity.max_lvl if entity is not None else row.max_lvl
    return row.lvl is not None and top is not None and row.lvl >= top


def _named(hit: SearchHit, query: str) -> bool:
    if query.isdigit():
        return hit.data_id == int(query)
    return query in (hit.zh or "") or query.casefold() in (hit.en or "").casefold()


def _unlock(
    entity: GameEntity | None,
    known: dict[int, GameEntity],
    halls: dict[Hall, int],
    village: dict[Hall, int],
) -> list[SearchNeed]:
    """Each hall the first level of a troop or spell the village lacks needs, with the village's level of it."""
    if entity is None or entity.table not in UNLOCKED or not entity.levels:
        return []
    return [
        SearchNeed(
            hall=hall,
            zh=building.zh if (building := known.get(halls.get(hall, 0))) else None,
            lvl=lvl,
            village=village.get(hall, 0),
        )
        for hall, lvl in entity.levels[0].needs.items()
    ]


def _hit(
    data_id: int,
    entity: GameEntity | None,
    rows: list[tuple[World, str, ExportRow]],
    shown: list[ExportRow] | None,
    unlock: list[SearchNeed],
) -> SearchHit:
    """One data_id as the tables describe it, with its rows summed up; `rows` is never empty without `entity`.

    `shown` is the rows it lists one by one, None when it lists none.
    """
    named: GameEntity | ExportRow = entity if entity is not None else rows[0][2]
    # Where its rows sit; the tables place only one the village lacks.
    if entity is not None and not rows:
        world, kind = entity.world, _kind(entity)
    else:
        world, kind = rows[0][0], rows[0][1]
    levels: Counter[int] = Counter()
    # Each next level's price, and how many copies stand idle or upgrading below it.
    nexts: dict[int, NextUpgrade] = {}
    idle: Counter[int] = Counter()
    waiting: Counter[int] = Counter()
    limited = next((row.limited_lvl for _, _, row in rows), None)
    for _, _, row in rows:
        if row.lvl is not None:
            levels[row.lvl] += row.cnt or 1
        if row.next is not None:
            nexts[row.next.lvl] = row.next
            (idle if row.timer is None else waiting)[row.next.lvl] += row.cnt or 1
    return SearchHit(
        data_id=data_id,
        zh=named.zh,
        en=named.en,
        world=world,
        kind=kind,
        section=SECTIONS.get(kind, kind),
        table=entity.table if entity is not None else None,
        max_lvl=named.max_lvl,
        levels=entity.levels if entity is not None else [],
        count=sum(row.cnt or 1 for _, _, row in rows),
        owned=dict(sorted(levels.items())),
        unlock=unlock,
        limited_lvl=limited,
        upgrading=sorted(
            (
                SearchUpgrade(lvl=row.lvl, done_at=row.done_at, extra=bool(row.extra))
                for _, _, row in rows
                if row.timer is not None
            ),
            key=lambda one: one.done_at or "",
        ),
        next=[
            SearchNext(
                lvl=lvl,
                cost=one.cost,
                time=one.time,
                ready=idle[lvl] if allowed else 0,
                waiting=waiting[lvl] if allowed else 0,
            )
            for lvl, one in sorted(nexts.items())
            for allowed in [limited is None or lvl <= limited]
        ],
        ready_at=min((row.ready_at for _, _, row in rows if row.ready_at), default=None),
        rows=shown,
    )


def _kind(entity: GameEntity) -> str:
    """The export section one the village lacks would sit in.

    Tables an older `READING` built give a siege machine's first level the
    laboratory, so there it reads as a troop.
    """
    if (
        entity.table == "characters"
        and entity.levels
        and "siege_workshop" in entity.levels[0].needs
    ):
        return "siege_machines"
    return TABLE_KINDS[entity.table]


def _levels(
    export: VillageExport,
) -> tuple[
    dict[tuple[World, int], Counter[int]],
    dict[tuple[World, int], tuple[UpgradeRole, str, str | None]],
]:
    """How many of each kind stand at each level, and each kind's role, section and name."""
    levels: dict[tuple[World, int], Counter[int]] = {}
    kinds: dict[tuple[World, int], tuple[UpgradeRole, str, str | None]] = {}
    for world, section, row in export.rows():
        role = row.role
        if role not in ("builder", "lab") or row.lvl is None:
            continue
        key = (world, row.data)
        levels.setdefault(key, Counter())[row.lvl] += row.cnt or 1
        kinds[key] = (role, section, row.zh)
    return levels, kinds


def _helping(helpers: list[ExportRow]) -> dict[UpgradeRole, ExportRow]:
    """Each helper row that speeds a slot, by that slot; a row saved without its multiplier is none."""
    return {row.speeds: row for row in helpers if row.speeds and row.multiplier}


def _finish(
    world: World, row: ExportRow, boosts: Boosts, helping: dict[UpgradeRole, ExportRow]
) -> tuple[str | None, ExportRow | None, int]:
    """The speed-up and the helper a running row is under, if any, and the real seconds it has left."""
    role = row.role
    boost, rate = SPEED_UPS.get((world, role), (None, 1)) if role else (None, 1)
    left = getattr(boosts, boost) if boost else None
    helper = helping.get(role) if role and row.helper_timer else None
    windows = [(left, rate - 1)] if left else []
    if helper is not None and row.helper_timer and helper.multiplier:
        windows.append((row.helper_timer, helper.multiplier))
    return (boost if left else None), helper, _real_seconds(row.timer or 0, windows)


def _real_seconds(timer: int, windows: list[tuple[int, int]]) -> int:
    """How long a job really has, each (seconds, extra) window adding that much work per second."""
    done = 0
    for end in sorted({seconds for seconds, _ in windows}):
        rate = 1 + sum(extra for seconds, extra in windows if seconds >= end)
        if timer <= rate * (end - done):
            return done + -(-timer // rate)
        timer -= rate * (end - done)
        done = end
    return done + timer


def _clock(epoch: int) -> str:
    """An instant as this machine's local time, to the second."""
    return datetime.fromtimestamp(epoch, UTC).astimezone().isoformat(timespec="seconds")
