"""The game's own tables: decoding them, reading them into entities, and keeping them per version.

Every table here is a few synthetic rows laid out and compressed the way the
game ships them, so no game file is committed and nothing reaches a device:
the emulator is a fake that answers `dumpsys`, `pm path` and `unzip -p`.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING
import unittest
from unittest.mock import MagicMock

import pytest

from ai_coc.models import GameEntities
from ai_coc.adapters import gamedata
from ai_coc.adapters.adb import AdbControlError
from ai_coc.parsers.gamedata import READING, ZSTD_MAGIC, GameDataError, build, decode

if TYPE_CHECKING:
    from ai_coc.models import GameTable, GameEntity

# Every table with the columns its reader needs and no rows, for a test to
# replace the ones it is about.
COLUMNS: dict[GameTable, str] = {
    "buildings": "Name,GlobalID,BuildingLevel,TID,BuildTimeD,BuildTimeH,BuildTimeM,BuildTimeS,BuildResource,BuildCost,TownHallLevel",
    "traps": "Name,GlobalID,Level,TID,BuildResource,BuildTimeD,BuildTimeH,BuildTimeM,BuildCost,TownHallLevel,VillageType",
    "characters": "Name,GlobalID,VisualLevel,TID,LaboratoryLevel,UpgradeTimeH,UpgradeTimeM,UpgradeResource,UpgradeCost,ProductionBuilding,BarrackLevel,DisableProduction,Deprecated,EnabledByCalendar,EnabledBySuperLicence",
    "spells": "Name,GlobalID,Level,TID,LaboratoryLevel,UpgradeTimeH,UpgradeResource,UpgradeCost,ProductionBuilding,SpellForgeLevel,DisableProduction,EnabledByCalendar",
    "heroes": "Name,VisualLevel,TID,UpgradeTimeH,UpgradeResource,UpgradeCost,RequiredTownHallLevel,RequiredHeroTavernLevel",
    "pets": "Name,TroopLevel,TID,LaboratoryLevel,UpgradeTimeH,UpgradeTimeM,UpgradeResource,UpgradeCost",
    "character_items": "Name,Level,TID,RequiredBlacksmithLevel,UpgradeResources,UpgradeCosts",
    "villager_apprentices": "Name,TID,RequiredTownHallLevel",
    "guardians": "Name,Level,TID",
}
ZH = "TID,CNT\nstring,string\nTID_TH,大本營\nTID_HERO,神盾勇者\nTID_GONE,\n"
EN = "TID,EN\nstring,string\nTID_TH,Town Hall\nTID_HERO,Royal Champion\n"
# The halls the requirements below name, as building rows.
TOWN_HALL = "Town Hall,1000001,1,TID_TH,,,,,Gold,0,"
LABORATORY = "Laboratory,1000007,1,,,,,,Elixir,100,1"
HERO_HALL = "Hero Hall,1000071,1,,,,,,Gold,100,7"
BUILDER_HALL = "BB Builder Hall,1000034,1,,,,,,Gold2,0,"
STAR_LABORATORY = "BB Star Laboratory,1000046,1,,,,,,Elixir2,100,2"
BARRACKS = "Barracks,1000006,1,,,,,,Elixir,100,1"
SPELL_FACTORY = "Spell Factory,1000020,1,,,,,,Elixir,150000,5"


def _table(table: GameTable, *rows: str, header: str | None = None) -> str:
    """A table the way the game lays it out: names, each column's type, then the rows."""
    names = header or COLUMNS[table]
    return "\n".join([names, ",".join("String" for _ in names.split(",")), *rows]) + "\n"


def _build(**tables: str) -> GameEntities:
    return build("1.2.3", ZH, EN, {name: tables.get(name, _table(name)) for name in COLUMNS})


def _entity(tables: GameEntities, data_id: int) -> GameEntity:
    return next(entity for entity in tables.entities if entity.data_id == data_id)


def _lzma(data: bytes) -> bytes:
    """LZMA as the game writes it: properties, a 4-byte size, and a stream with no end marker.

    Encoded here, every byte a literal, because `lzma` can only write a stream
    that ends in an end marker, which the game's files never carry (measured
    on 18.647.5: they do not decode with the size unknown). Under a known size
    the CI runner's Python refused that marker as corrupt input while this
    machine's accepted it, so tables `lzma` compressed passed here and failed
    there.
    """
    is_match, literal = [1024] * (12 << 4), [1024] * (0x300 << 3)
    low, span, cache, pending, out = 0, 0xFFFFFFFF, 0, 1, bytearray()

    def shift() -> None:
        nonlocal low, cache, pending
        if low < 0xFF000000 or low >= 1 << 32:
            carry, byte = low >> 32, cache
            while pending:
                out.append((byte + carry) & 0xFF)
                byte, pending = 0xFF, pending - 1
            cache = (low >> 24) & 0xFF
        pending += 1
        low = (low & 0x00FFFFFF) << 8

    def encode(probs: list[int], index: int, value: int) -> None:
        nonlocal low, span
        bound = (span >> 11) * probs[index]
        if value:
            low, span = low + bound, span - bound
            probs[index] -= probs[index] >> 5
        else:
            span = bound
            probs[index] += (2048 - probs[index]) >> 5
        while span < 1 << 24:
            span <<= 8
            shift()

    previous = 0
    for position, byte in enumerate(data):
        encode(is_match, position & 3, 0)
        base, symbol = 0x300 * (previous >> 5), 1
        for place in range(7, -1, -1):
            value = (byte >> place) & 1
            encode(literal, base + symbol, value)
            symbol = symbol << 1 | value
        previous = byte
    for _ in range(5):
        shift()
    return bytes([0x5D]) + struct.pack("<II", 1 << 16, len(data)) + bytes(out)


def _ship(text: str, *, signed: bool) -> bytes:
    """A table as the game ships it: its LZMA, and a logic table signed ahead of it."""
    packed = _lzma(text.encode())
    return b"Sig:" + bytes(64) + packed if signed else packed


class DecodeTests(unittest.TestCase):
    def test_a_shipped_table_decodes_with_or_without_its_signature(self) -> None:
        text = _table("guardians", "Logger,1,")
        for signed in (True, False):
            assert decode(_ship(text, signed=signed)) == text.encode(), signed

    def test_a_zstd_table_is_refused_by_name(self) -> None:
        with pytest.raises(GameDataError, match="zstd"):
            decode(b"Sig:" + bytes(64) + ZSTD_MAGIC + bytes(16))

    def test_bytes_that_are_not_a_table_raise_a_game_data_error(self) -> None:
        with pytest.raises(GameDataError):
            decode(b"not a table at all")


class BuildTests(unittest.TestCase):
    def test_a_building_row_buys_its_own_level_and_a_troop_row_the_next(self) -> None:
        tables = _build(
            buildings=_table(
                "buildings",
                TOWN_HALL,
                LABORATORY,
                BARRACKS,
                "Cannon,1000008,1,,,,,,Gold,250,1",
                ",,2,,1,2,,,,1000,2",
            ),
            characters=_table(
                "characters",
                "Barbarian,4000000,1,,1,1,,Elixir,300,Barracks,1",
                ",,2,,1,2,30,,900",
                ",,3,,2,,,,",
            ),
        )
        assert [(level.cost, level.time) for level in _entity(tables, 1000008).levels] == [
            ({"gold": 250}, None),
            ({"gold": 1000}, 86_400 + 2 * 3_600),
        ]
        barbarian = _entity(tables, 4000000).levels
        assert [(level.cost, level.time) for level in barbarian] == [
            ({}, None),
            ({"elixir": 300}, 3_600),
            ({"elixir": 900}, 2 * 3_600 + 30 * 60),
        ]
        # A requirement is for having the level of the row it is written on,
        # so it does not move with the price; the first level is unlocked by
        # the barracks, not researched.
        assert [level.needs for level in barbarian] == [
            {"barracks": 1},
            {"laboratory": 1},
            {"laboratory": 2},
        ]

    def test_blank_cells_carry_down_within_an_entity_and_not_into_the_next(self) -> None:
        tables = _build(
            buildings=_table("buildings", TOWN_HALL),
            traps=_table(
                "traps",
                "Bomb,12000000,1,,Gold,,,,400,3",
                ",,2,,,,,1,1000,",
                "Spring Trap,12000002,1,,Elixir,,,,800,",
            ),
        )
        bomb = _entity(tables, 12000000)
        assert [(level.needs, level.cost, level.time) for level in bomb.levels] == [
            ({"town_hall": 3}, {"gold": 400}, None),
            ({"town_hall": 3}, {"gold": 1000}, 60),
        ]
        assert _entity(tables, 12000002).levels[0].needs == {}

    def test_ids_come_from_the_globalid_or_from_position_in_a_table_without_one(self) -> None:
        tables = _build(
            buildings=_table("buildings", "Cannon,1000008,1,,,,,,Gold,250,", TOWN_HALL, HERO_HALL),
            heroes=_table(
                "heroes",
                "Barbarian King,1,,1,DarkElixir,5000,7,",
                ",2,,2,,6000,,",
                "Royal Champion,1,TID_HERO,4,DarkElixir,10000,13,7",
            ),
        )
        assert [entity.data_id for entity in tables.entities] == [
            1000001,
            1000008,
            1000071,
            28000000,
            28000001,
        ]
        champion = _entity(tables, 28000001)
        assert (champion.zh, champion.en, champion.table) == (
            "神盾勇者",
            "Royal Champion",
            "heroes",
        )
        assert champion.levels[0].needs == {"town_hall": 13, "hero_hall": 7}

    def test_a_level_not_written_counts_on_from_the_row_above(self) -> None:
        tables = _build(
            buildings=_table("buildings", TOWN_HALL),
            guardians=_table("guardians", "Logger,1,", ",,", ",,"),
            villager_apprentices=_table(
                "villager_apprentices", "BuilderApprentice,,10", ",,10", ",,11"
            ),
        )
        logger = _entity(tables, 107000000)
        assert ([level.lvl for level in logger.levels], logger.max_lvl) == ([1, 2, 3], 3)
        helper = _entity(tables, 93000000)
        assert [(level.lvl, level.needs) for level in helper.levels] == [
            (1, {"town_hall": 10}),
            (2, {"town_hall": 10}),
            (3, {"town_hall": 11}),
        ]

    def test_a_builder_base_entity_is_night_and_names_its_own_halls(self) -> None:
        tables = _build(
            buildings=_table(
                "buildings",
                BUILDER_HALL,
                STAR_LABORATORY,
                "BB Barracks,1000040,1,,,,,,Elixir2,1000,1",
            ),
            characters=_table(
                "characters",
                "BB Night Witch,4000042,9,,6,48,,Elixir2,1400000,BB Barracks,8",
                ",,10,,6,,,,",
            ),
            heroes=_table("heroes", "BB Battle Machine,1,,12,Elixir2,1000000,5,0"),
            # Not named `BB `, and the builder base's all the same.
            traps=_table("traps", "AirTrap2,12000012,1,,Gold2,,,,600,3,1"),
        )
        barracks = _entity(tables, 1000040)
        assert (barracks.world, barracks.levels[0].needs) == ("night", {"builder_hall": 1})
        air_trap = _entity(tables, 12000012)
        assert (air_trap.world, air_trap.levels[0].needs) == ("night", {"builder_hall": 3})
        witch = _entity(tables, 4000042)
        assert (witch.world, witch.max_lvl, [level.lvl for level in witch.levels]) == (
            "night",
            10,
            [9, 10],
        )
        assert witch.levels[1].model_dump() == {
            "lvl": 10,
            "needs": {"star_laboratory": 6},
            "cost": {"elixir": 1_400_000},
            "time": 48 * 3_600,
        }
        # Its first level is unlocked by the builder base's own barracks.
        assert witch.levels[0].needs == {"builder_barracks": 8}
        # The builder base's heroes write their hall as 0, which is no requirement.
        assert _entity(tables, 28000000).levels[0].needs == {"builder_hall": 5}

    def test_equipment_prices_several_ores_at_once(self) -> None:
        tables = _build(
            character_items=_table(
                "character_items",
                "Barbarian Puppet,1,,0,CommonOre,120",
                ",2,,0,CommonOre;RareOre,2700;600",
                ",3,,0,,",
            )
        )
        assert [level.cost for level in _entity(tables, 90000000).levels] == [
            {},
            {"shiny_ore": 120},
            {"shiny_ore": 2700, "glowy_ore": 600},
        ]

    def test_a_price_that_does_not_match_its_resources_is_refused(self) -> None:
        with pytest.raises(GameDataError, match="對不上"):
            _build(
                character_items=_table(
                    "character_items", "Barbarian Puppet,1,,0,CommonOre,2700;600"
                )
            )

    def test_halls_resolve_to_building_ids_and_a_missing_one_is_refused(self) -> None:
        troop = _table(
            "characters", "Barbarian,4000000,1,,1,1,,Elixir,300,Barracks,1", ",,2,,1,,,,"
        )
        tables = _build(
            buildings=_table("buildings", TOWN_HALL, LABORATORY, BARRACKS), characters=troop
        )
        assert tables.halls == {"town_hall": 1000001, "laboratory": 1000007, "barracks": 1000006}
        for kept, missing in ((LABORATORY, "barracks"), (BARRACKS, "laboratory")):
            with pytest.raises(GameDataError, match=missing):
                _build(buildings=_table("buildings", TOWN_HALL, kept), characters=troop)

    def test_a_troop_or_spell_is_unlocked_by_the_building_that_trains_it(self) -> None:
        tables = _build(
            buildings=_table("buildings", TOWN_HALL, LABORATORY, BARRACKS, SPELL_FACTORY),
            characters=_table(
                "characters",
                "Barbarian,4000000,1,,1,,,Elixir,300,Barracks,1",
                # A super troop writes its base troop's barracks level, and an
                # untrainable, retired or event one a building that does not
                # unlock it, each flagged by its own column.
                "Inferno Dragon,4000063,1,,10,,,Elixir,,Barracks,11,,,,TRUE",
                ",,2,,,,,,",
                "Prototype3,4000093,1,,1,,,Elixir,,Barracks,1,TRUE",
                "UnusedSiegePrototype,4000189,1,,9,,,Elixir,,Siege Workshop,1,,TRUE",
                "Ice Wizard,4000030,1,,1,,,Elixir,,Barracks,1,,,TRUE",
            ),
            spells=_table(
                "spells",
                "Freeze,26000005,1,,7,24,Elixir,1200000,Spell Factory,4",
                ",,2,,7,36,,1700000,,",
                "Santas Surprise,26000006,1,,1,,Elixir,,Spell Factory,1,,TRUE",
                "Slow,26000007,1,,1,,Elixir,,Spell Factory,1,TRUE",
            ),
        )
        assert _entity(tables, 4000000).levels[0].needs == {"barracks": 1}
        freeze = _entity(tables, 26000005)
        assert [level.needs for level in freeze.levels] == [
            {"spell_factory": 4},
            {"laboratory": 7},
        ]
        # A flagged row is not unlocked by the building it writes, and its own
        # first laboratory cell is no unlock either; a later level keeps it.
        dragon = _entity(tables, 4000063)
        assert [level.needs for level in dragon.levels] == [{}, {"laboratory": 10}]
        for flagged in (4000093, 4000189, 4000030, 26000006, 26000007):
            assert _entity(tables, flagged).levels[0].needs == {}, flagged

    def test_a_troop_trained_in_a_building_with_no_hall_is_refused(self) -> None:
        with pytest.raises(GameDataError, match="Hut"):
            _build(
                buildings=_table("buildings", TOWN_HALL),
                characters=_table("characters", "Hut Troop,4000999,1,,1,,,Elixir,,Hut,1"),
            )

    def test_a_renamed_column_is_refused_rather_than_read_as_blank(self) -> None:
        renamed = COLUMNS["buildings"].replace("BuildCost", "BuildPrice")
        with pytest.raises(GameDataError, match="BuildCost"):
            _build(buildings=_table("buildings", TOWN_HALL, header=renamed))

    def test_names_come_from_each_language_and_an_empty_one_is_none(self) -> None:
        tables = _build(
            buildings=_table("buildings", TOWN_HALL, "Old Cannon,1000099,1,TID_GONE,,,,,Gold,1,")
        )
        town_hall = _entity(tables, 1000001)
        assert (town_hall.zh, town_hall.en, town_hall.world) == ("大本營", "Town Hall", "day")
        old = _entity(tables, 1000099)
        assert (old.zh, old.en) == (None, None)


PM_PATH = """package:/data/app/~~x==/com.supercell.clashofclans-y==/base.apk
package:/data/app/~~x==/com.supercell.clashofclans-y==/split_config.zh.apk
package:/data/app/~~x==/com.supercell.clashofclans-y==/split_install_time_asset_pack.apk
"""
SHIPPED_BUILDINGS = _table("buildings", TOWN_HALL)


def _members() -> dict[str, bytes]:
    """Every member `game_data` reads, as the asset pack holds it."""
    members = {f"assets/logic/{name}.csv": _ship(_table(name), signed=True) for name in COLUMNS}
    members["assets/logic/buildings.csv"] = _ship(SHIPPED_BUILDINGS, signed=True)
    members["assets/localization/cnt.csv"] = _ship(ZH, signed=False)
    members["assets/localization/texts.csv"] = _ship(EN, signed=False)
    return members


def _emulator(version: str, members: dict[str, bytes] | None = None) -> MagicMock:
    """A controller answering as the game's emulator does; a member it lacks comes back empty, as `unzip -p` does."""
    shipped = _members() if members is None else members
    adb = MagicMock()
    adb.shell.side_effect = lambda command, **_: (
        f"    versionCode=180647006 minSdk=24\n    versionName={version}\n"
        if command[0] == "dumpsys"
        else PM_PATH
    )
    adb.shell_bytes.side_effect = lambda command, **_: shipped.get(command[-1], b"")
    return adb


class GameDataTests(unittest.TestCase):
    def test_a_version_read_for_the_first_time_is_extracted_and_kept(self) -> None:
        report = gamedata.game_data(lambda: _emulator("18.647.5"))
        folder = gamedata.GAME_DATA_DIR / "18.647.5"
        assert (report.outcome, report.installed, report.directory) == (
            "extracted",
            "18.647.5",
            folder,
        )
        assert (report.game_version, report.entities) == ("18.647.5", 1)
        assert (folder / "cnt.csv").read_bytes() == ZH.encode()
        assert (folder / "texts.csv").read_bytes() == EN.encode()
        assert (folder / "logic" / "buildings.csv").read_bytes() == SHIPPED_BUILDINGS.encode()
        kept = (folder / "entities.json").read_text(encoding="utf-8")
        assert GameEntities.model_validate_json(kept) == report.tables

    def test_the_answer_leaves_the_tables_out(self) -> None:
        answer = gamedata.game_data(lambda: _emulator("18.647.5")).model_dump()
        assert set(answer) == {"outcome", "installed", "directory", "game_version", "entities"}

    def test_a_version_already_kept_is_not_read_again(self) -> None:
        first = gamedata.game_data(lambda: _emulator("18.647.5"))
        adb = _emulator("18.647.5")
        again = gamedata.game_data(lambda: adb)
        assert (again.outcome, again.tables) == ("cached", first.tables)
        adb.shell_bytes.assert_not_called()

    def test_refresh_reads_a_kept_version_again(self) -> None:
        gamedata.game_data(lambda: _emulator("18.647.5"))
        adb = _emulator("18.647.5")
        assert gamedata.game_data(lambda: adb, refresh=True).outcome == "extracted"
        adb.shell_bytes.assert_called()

    def test_a_kept_file_this_code_cannot_read_is_read_again(self) -> None:
        folder = gamedata.GAME_DATA_DIR / "18.647.5"
        folder.mkdir(parents=True)
        (folder / "entities.json").write_text('{"entities": [{"data": 1}]}', encoding="utf-8")
        assert gamedata.game_data(lambda: _emulator("18.647.5")).outcome == "extracted"

    def test_tables_an_older_reading_kept_are_read_again_and_still_serve_a_fallback(self) -> None:
        gamedata.game_data(lambda: _emulator("18.647.5"))
        path = gamedata.GAME_DATA_DIR / "18.647.5" / "entities.json"
        kept = GameEntities.model_validate_json(path.read_text(encoding="utf-8"))
        older = kept.model_copy(update={"reading": READING - 1})
        path.write_text(older.model_dump_json(), encoding="utf-8")
        down = gamedata.game_data(MagicMock(side_effect=AdbControlError("ADB 連線失敗")))
        assert (down.outcome, down.tables.reading) == ("fallback", READING - 1)
        again = gamedata.game_data(lambda: _emulator("18.647.5"))
        assert (again.outcome, again.tables.reading) == ("extracted", READING)

    def test_a_failure_falls_back_to_the_newest_kept_version(self) -> None:
        """Newest by version number, not by name: 9.10.0 is after 9.9.0."""
        for version in ("9.10.0", "9.9.0"):
            gamedata.game_data(lambda version=version: _emulator(version))
        down = gamedata.game_data(MagicMock(side_effect=AdbControlError("ADB 連線失敗")))
        assert (down.outcome, down.installed, down.directory.name) == ("fallback", None, "9.10.0")
        members = _members()
        del members["assets/logic/guardians.csv"]
        short = gamedata.game_data(lambda: _emulator("9.11.0", members))
        assert (short.outcome, short.installed, short.directory.name) == (
            "fallback",
            "9.11.0",
            "9.10.0",
        )
        assert not (gamedata.GAME_DATA_DIR / "9.11.0").exists()

    def test_a_failure_with_nothing_kept_is_raised(self) -> None:
        with pytest.raises(RuntimeError, match="沒開"):
            gamedata.game_data(MagicMock(side_effect=RuntimeError("模擬器沒開")))

    def test_a_version_that_cannot_be_a_folder_name_is_refused(self) -> None:
        with pytest.raises(GameDataError, match="版本"):
            gamedata.game_data(lambda: _emulator("../18.647.5"))
