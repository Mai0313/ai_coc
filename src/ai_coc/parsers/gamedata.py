"""The game's own data tables: decoding them as the game ships them, and reading them into entities.

Pure, like every parser: bytes and CSV text in, models out. Taking them off the
emulator and keeping them per game version is `adapters/gamedata.py`'s.
"""

from __future__ import annotations

import io
import csv
import lzma
from typing import TYPE_CHECKING

from ai_coc.models import GameLevel, GameEntity, GameEntities, GameTableSpec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ai_coc.models import Hall, GameTable

# A logic table opens with `Sig:` and a 64-byte signature ahead of its data;
# the localization tables have neither.
SIGNATURE = b"Sig:"
SIGNATURE_SIZE = len(SIGNATURE) + 64
# Supercell's LZMA header is the 5 property bytes and a 4-byte uncompressed
# size, where the `.lzma` format `lzma` reads wants that size in 8 bytes. The
# stream carries no end marker (18.647.5), so the size is what ends it.
SHORT_HEADER = 9
# What a zstd stream opens with. Supercell has shipped other games' tables this
# way, and 18.647.5 ships none. Reading one needs Python 3.14's
# `compression.zstd` or a dependency; until a version needs it, the error says
# so and `game_data` keeps the last version it could read.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# How each logic table is read. Every value here was checked on 18.647.5
# against the community `data_id` names and the account's own export: every
# level the export holds falls inside its entity's levels, and every entity in
# a `...2` section is a `BB ` one. The one disagreement is the helpers' third
# row, Alchemist by position where the community table says Prospector; the
# position is what the game numbers rows by in a table with no `GlobalID`, and
# it has not been settled in the game. Requirements stay on the row they are
# written on in every table: a row's requirement is for having that row's
# level, so Royal Champion's 26th row asks town hall 14 where its 25th, which
# carries the price of 26, asks 13.
TABLES = (
    GameTableSpec(
        table="buildings",
        level="BuildingLevel",
        cost="BuildCost",
        resource="BuildResource",
        time="BuildTime",
        buys_own_level=True,
        needs={"TownHallLevel": "town_hall"},
    ),
    GameTableSpec(
        table="traps",
        level="Level",
        cost="BuildCost",
        resource="BuildResource",
        time="BuildTime",
        buys_own_level=True,
        needs={"TownHallLevel": "town_hall"},
    ),
    GameTableSpec(
        table="characters",
        level="VisualLevel",
        cost="UpgradeCost",
        resource="UpgradeResource",
        time="UpgradeTime",
        needs={"LaboratoryLevel": "laboratory"},
        unlock="BarrackLevel",
        untrained=(
            "DisableProduction",
            "Deprecated",
            "EnabledByCalendar",
            "EnabledBySuperLicence",
        ),
    ),
    GameTableSpec(
        table="spells",
        level="Level",
        cost="UpgradeCost",
        resource="UpgradeResource",
        time="UpgradeTime",
        needs={"LaboratoryLevel": "laboratory"},
        unlock="SpellForgeLevel",
        untrained=("DisableProduction", "EnabledByCalendar"),
    ),
    GameTableSpec(
        table="heroes",
        prefix=28,
        level="VisualLevel",
        cost="UpgradeCost",
        resource="UpgradeResource",
        time="UpgradeTime",
        needs={"RequiredTownHallLevel": "town_hall", "RequiredHeroTavernLevel": "hero_hall"},
    ),
    GameTableSpec(
        table="pets",
        prefix=73,
        level="TroopLevel",
        cost="UpgradeCost",
        resource="UpgradeResource",
        time="UpgradeTime",
        # The pet house, under the laboratory's column name.
        needs={"LaboratoryLevel": "pet_house"},
    ),
    GameTableSpec(
        table="character_items",
        prefix=90,
        level="Level",
        cost="UpgradeCosts",
        resource="UpgradeResources",
        needs={"RequiredBlacksmithLevel": "blacksmith"},
    ),
    # The helpers write no level, one row per level, and a gem `Cost` on every
    # row including the first, so which level that buys is not known and it is
    # left out.
    GameTableSpec(
        table="villager_apprentices", prefix=93, needs={"RequiredTownHallLevel": "town_hall"}
    ),
    # Some guardians write their level on the first row only.
    GameTableSpec(table="guardians", prefix=107, level="Level"),
)

# A builder base entity has `VillageType` 1 or an internal name starting with
# `BB `. Either alone misses some on 18.647.5: every `BB ` entity has the 1,
# and so do seven that are not named so (`AirTrap2`, paid in builder gold, and
# six builder base troops' abilities), while the tables with no `VillageType`
# column hold no builder base entity.
NIGHT_PREFIX = "BB "
NIGHT_VILLAGE = "1"
# What a builder base entity's requirement columns name instead.
NIGHT_HALLS: dict[Hall, Hall] = {"town_hall": "builder_hall", "laboratory": "star_laboratory"}
# The internal building names `GameEntities.halls` is resolved through.
HALL_NAMES: dict[Hall, str] = {
    "town_hall": "Town Hall",
    "laboratory": "Laboratory",
    "hero_hall": "Hero Hall",
    "pet_house": "Pet House",
    "blacksmith": "Blacksmith",
    "builder_hall": "BB Builder Hall",
    "star_laboratory": "BB Star Laboratory",
    "barracks": "Barracks",
    "dark_barracks": "Dark Barracks",
    "siege_workshop": "Siege Workshop",
    "spell_factory": "Spell Factory",
    "dark_spell_factory": "Dark Spell Factory",
    "builder_barracks": "BB Barracks",
}
# The column naming the building that trains a troop, siege machine or spell,
# by the internal name `HALL_NAMES` gives. Its level in the table's `unlock`
# column, written on the first row only, is what unlocks the entity's first
# level, which is unlocked rather than researched: that row's laboratory cell
# gates nothing (the Barbarian's asks laboratory 1, which needs town hall 3,
# while the barracks that trains it stands from town hall 1). Checked on
# 18.647.5 against the account's export of 2026-11-11: every troop, siege
# machine and spell it holds stands at or past its building's level, and the
# two builder base troops it lacks ask a Builder Barracks past its 10. A row
# flagged `untrained` is not unlocked by the building it writes: one that
# cannot be trained, an event's (its calendar), or a super troop (its licence,
# which these tables do not hold, asks for its base troop's level, and its
# `BarrackLevel` is that troop's).
TRAINED_IN = "ProductionBuilding"
# The game's resource names as `GameLevel.cost` spells them; the builder base's
# `Gold2` and `Elixir2` are its own gold and elixir.
RESOURCES = {
    "Gold": "gold",
    "Gold2": "gold",
    "Elixir": "elixir",
    "Elixir2": "elixir",
    "DarkElixir": "dark",
    "Diamonds": "gems",
    "CommonOre": "shiny_ore",
    "RareOre": "glowy_ore",
    "EpicOre": "starry_ore",
}
TIME_UNITS = (("D", 86_400), ("H", 3_600), ("M", 60), ("S", 1))
# Which reading of the tables `build` does. A kept `entities.json` written by
# another is read again from the emulator rather than used, so raise this with
# any change to what `build` writes from the same tables, a value as much as
# the shape.
READING = 2
# A table with no `GlobalID` numbers its entities from its class times this.
CLASS_SIZE = 1_000_000

Row = dict[str, str]


class GameDataError(ValueError):
    """The game's files are not what this reader knows how to read."""


def decode(blob: bytes) -> bytes:
    """One table out of the APK, as the CSV the game reads."""
    body = blob[SIGNATURE_SIZE:] if blob.startswith(SIGNATURE) else blob
    if body.startswith(ZSTD_MAGIC):
        raise GameDataError(
            "這個版本的表用 zstd 壓縮，要 Python 3.14 的 compression.zstd 才讀得了"
        )
    try:
        return lzma.decompress(
            body[:SHORT_HEADER] + bytes(4) + body[SHORT_HEADER:], format=lzma.FORMAT_ALONE
        )
    except lzma.LZMAError as exc:
        raise GameDataError(f"表解不開：{exc}") from exc


def _rows(text: str) -> tuple[list[str], list[Row]]:
    """A table's header and its rows; the second line names each column's type and is left out."""
    lines = list(csv.reader(io.StringIO(text)))
    header = lines[0]
    return header, [dict(zip(header, line, strict=False)) for line in lines[2:] if line]


def _texts(text: str) -> dict[str, str]:
    """A localization table as TID -> text; its second column is the language."""
    header, rows = _rows(text)
    if header[:1] != ["TID"] or len(header) < 2:
        raise GameDataError(f"語言表的欄位不對：{header[:3]}")
    return {row["TID"]: row[header[1]] for row in rows if row.get(header[1])}


def _number(cell: str) -> int | None:
    cell = cell.strip()
    return int(cell) if cell.lstrip("-").isdigit() else None


def _price(cost: str, resource: str) -> dict[str, int]:
    """A row's price as resource -> amount; equipment writes several, `;`-separated, in both cells."""
    amounts = [part.strip() for part in cost.split(";") if part.strip()]
    if not amounts:
        return {}
    names = [part.strip() for part in resource.split(";") if part.strip()]
    if len(names) != len(amounts):
        raise GameDataError(f"價格 {cost!r} 對不上資源 {resource!r}")
    return {
        RESOURCES.get(name, name): int(amount) for name, amount in zip(names, amounts, strict=True)
    }


def _seconds(row: Row, prefix: str, header: list[str]) -> int | None:
    cells = [
        (row.get(prefix + unit, "").strip(), size)
        for unit, size in TIME_UNITS
        if prefix + unit in header
    ]
    if not any(cell for cell, _ in cells):
        return None
    return sum(int(cell) * size for cell, size in cells if cell)


def _check_columns(spec: GameTableSpec, header: list[str]) -> None:
    wanted = ["Name", "TID", *spec.needs]
    wanted += [column for column in (spec.level, spec.cost, spec.resource) if column]
    if spec.unlock:
        wanted += [TRAINED_IN, spec.unlock, *spec.untrained]
    if spec.prefix is None:
        wanted.append("GlobalID")
    missing = [column for column in wanted if column not in header]
    if spec.time and not any(spec.time + unit in header for unit, _ in TIME_UNITS):
        missing.append(f"{spec.time}*")
    if missing:
        raise GameDataError(f"{spec.table} 少了欄位 {missing}")


def _levels(
    spec: GameTableSpec, rows: list[Row], header: list[str], night: bool
) -> list[GameLevel]:
    """Every level of one entity, each carrying what buying it takes.

    A blank level cell is the row above's plus one, and a blank resource or
    requirement cell is the row above's, within the entity. Where a row's price
    buys the next level, level L takes the price and time of the row at level
    L-1 and keeps its own requirements. A level written twice keeps its first
    row. In a table with an `unlock` column, the first row's requirement is
    what unlocks it (`_unlock`) in place of its own.
    """
    carried: Row = {}
    read: dict[int, tuple[dict[Hall, int], dict[str, int], int | None]] = {}
    lvl = 0
    for row in rows:
        for column in (spec.resource, *spec.needs):
            if column and row.get(column, "").strip():
                carried[column] = row[column]
        written = _number(row.get(spec.level, "")) if spec.level else None
        lvl = lvl + 1 if written is None else written
        needs: dict[Hall, int] = {}
        for column, hall in spec.needs.items():
            if value := _number(carried.get(column, "")):
                needs[NIGHT_HALLS.get(hall, hall) if night else hall] = value
        if spec.unlock and row is rows[0]:
            needs = _unlock(spec, row)
        price = (
            _price(row.get(spec.cost, ""), carried.get(spec.resource or "", ""))
            if spec.cost
            else {}
        )
        time = _seconds(row, spec.time, header) if spec.time else None
        read.setdefault(lvl, (needs, price, time))
    levels = []
    for lvl, (needs, *_) in sorted(read.items()):
        bought = read[lvl] if spec.buys_own_level else read.get(lvl - 1)
        cost, time = (bought[1], bought[2]) if bought else ({}, None)
        levels.append(GameLevel(lvl=lvl, needs=needs, cost=cost, time=time))
    return levels


def _unlock(spec: GameTableSpec, row: Row) -> dict[Hall, int]:
    """The building that trains a troop or spell, at the level of it its first row says unlocks it.

    Empty for a row that names no building or level, or that a flag marks as
    not unlocked by it.
    """
    building = row.get(TRAINED_IN, "").strip()
    level = _number(row.get(spec.unlock or "", ""))
    if not building or not level or any(row.get(flag) == "TRUE" for flag in spec.untrained):
        return {}
    hall = next((hall for hall, name in HALL_NAMES.items() if name == building), None)
    if hall is None:
        raise GameDataError(f"{spec.table} 的 {row['Name']} 由 {building} 訓練，不認得這棟建築")
    return {hall: level}


def _entities(
    spec: GameTableSpec, text: str, zh: dict[str, str], en: dict[str, str]
) -> Iterator[tuple[str, GameEntity]]:
    """Each entity of one logic table with its internal name: a row with a `Name` starts one."""
    header, rows = _rows(text)
    _check_columns(spec, header)
    groups: list[list[Row]] = []
    for row in rows:
        if row.get("Name"):
            groups.append([row])
        elif groups:
            groups[-1].append(row)
    for index, group in enumerate(groups):
        first = group[0]
        name = first["Name"]
        if spec.prefix is not None:
            data_id = spec.prefix * CLASS_SIZE + index
        elif first["GlobalID"].isdigit():
            data_id = int(first["GlobalID"])
        else:
            raise GameDataError(f"{spec.table} 的 {name} 沒有 GlobalID")
        night = first.get("VillageType") == NIGHT_VILLAGE or name.startswith(NIGHT_PREFIX)
        levels = _levels(spec, group, header, night)
        yield (
            name,
            GameEntity(
                data_id=data_id,
                zh=zh.get(first["TID"]),
                en=en.get(first["TID"]),
                world="night" if night else "day",
                table=spec.table,
                max_lvl=max(level.lvl for level in levels),
                levels=levels,
            ),
        )


def build(version: str, zh: str, en: str, tables: dict[GameTable, str]) -> GameEntities:
    """Every entity of one game version, out of its two localization tables and its logic tables.

    Raises `GameDataError` where the tables have moved away from what is read
    here — a column renamed, a hall a requirement names that the buildings no
    longer hold — rather than writing a file that is quietly short.
    """
    names, texts = _texts(zh), _texts(en)
    entities: list[GameEntity] = []
    buildings: dict[str, int] = {}
    for spec in TABLES:
        for name, entity in _entities(spec, tables[spec.table], names, texts):
            entities.append(entity)
            if spec.table == "buildings":
                buildings.setdefault(name, entity.data_id)
    halls = {hall: buildings[name] for hall, name in HALL_NAMES.items() if name in buildings}
    named = {hall for entity in entities for level in entity.levels for hall in level.needs}
    if missing := named - halls.keys():
        raise GameDataError(f"建築表裡找不到 {sorted(missing)}")
    return GameEntities(
        game_version=version,
        reading=READING,
        halls=halls,
        entities=sorted(entities, key=lambda entity: entity.data_id),
    )
