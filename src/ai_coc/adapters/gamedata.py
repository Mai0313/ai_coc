"""The game's own tables, read off the emulator once per game version and kept under `~/.ai_coc/game_data/`.

A version's folder holds the decoded CSVs as the game ships them (`cnt.csv`,
`texts.csv`, `logic/<table>.csv`) and `entities.json` (`GameEntities`) built
from them. Only files are read, over ADB, and nothing touches the screen.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
import logging

from ai_coc.models import GameEntities, GameDataReport
from ai_coc.constants import COC_PACKAGE, GAME_DATA_DIR
from ai_coc.parsers.gamedata import TABLES, READING, GameDataError, build, decode

if TYPE_CHECKING:
    from pathlib import Path
    from collections.abc import Callable

    from ai_coc.adapters.adb import AdbController

logger = logging.getLogger(__name__)

# The split APK the game installs its assets in; on 18.647.5 none of the tables
# is anywhere else. The shell user can read it, and the device's `unzip -p`
# streams one member out of its 651 MB without copying anything, all eleven in
# 0.56 s. A member it does not find comes back empty with exit 0, which is
# how a missing one is told.
ASSET_PACK = "split_install_time_asset_pack.apk"
LOCALIZATION = {"cnt": "assets/localization/cnt.csv", "texts": "assets/localization/texts.csv"}
ENTITIES = "entities.json"
MEMBER_WAIT = 30


def installed_version(adb: AdbController) -> str:
    """The game version the emulator has installed, as `dumpsys package` names it."""
    found = re.search(r"versionName=(\S+)", adb.shell(["dumpsys", "package", COC_PACKAGE]))
    # Held to what a version looks like, since it becomes a folder name.
    if found is None or not re.fullmatch(r"\d[\w.-]*", found.group(1)):
        raise GameDataError(f"讀不到 {COC_PACKAGE} 的版本")
    return found.group(1)


def _asset_pack(adb: AdbController) -> str:
    for line in adb.shell(["pm", "path", COC_PACKAGE]).splitlines():
        path = line.strip().removeprefix("package:")
        if path.endswith(f"/{ASSET_PACK}"):
            return path
    raise GameDataError(f"{COC_PACKAGE} 沒有 {ASSET_PACK}")


def _member(adb: AdbController, apk: str, member: str) -> bytes:
    blob = adb.shell_bytes(["unzip", "-p", apk, member], timeout=MEMBER_WAIT)
    if not blob:
        raise GameDataError(f"{ASSET_PACK} 裡沒有 {member}")
    return decode(blob)


def _extract(adb: AdbController, version: str) -> GameEntities:
    """Read one version's tables off the emulator and write its folder.

    Everything is decoded and read before the first file is written, so a
    table this code cannot read leaves the folder as it was. `entities.json`
    goes last: one cut short does not parse, and the next run extracts again.
    """
    apk = _asset_pack(adb)
    texts = {name: _member(adb, apk, member) for name, member in LOCALIZATION.items()}
    tables = {spec.table: _member(adb, apk, f"assets/logic/{spec.table}.csv") for spec in TABLES}
    entities = build(
        version,
        texts["cnt"].decode("utf-8"),
        texts["texts"].decode("utf-8"),
        {table: blob.decode("utf-8") for table, blob in tables.items()},
    )
    folder = GAME_DATA_DIR / version
    (folder / "logic").mkdir(parents=True, exist_ok=True)
    for name, blob in texts.items():
        (folder / f"{name}.csv").write_bytes(blob)
    for table, blob in tables.items():
        (folder / "logic" / f"{table}.csv").write_bytes(blob)
    (folder / ENTITIES).write_text(entities.model_dump_json(indent=1), encoding="utf-8")
    return entities


def _kept(folder: Path) -> GameEntities | None:
    """A version folder's `entities.json`, or None when it has none this code can read."""
    path = folder / ENTITIES
    if not path.exists():
        return None
    try:
        return GameEntities.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("%s does not read as this code's tables", path, exc_info=True)
        return None


def _version_key(folder: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", folder.name))


def game_data(connect: Callable[[], AdbController], refresh: bool = False) -> GameDataReport:
    """The installed version's tables: kept ones when the version was read before, else read now.

    `refresh` reads them again whatever is kept. `connect` is called inside, so
    an emulator that is down falls back like any other failure: to the newest
    version kept on disk, with a warning, much as the community mapping falls
    back to its cached copy. With nothing kept the failure is raised.
    """
    installed = None
    try:
        adb = connect()
        installed = installed_version(adb)
        folder = GAME_DATA_DIR / installed
        kept = None if refresh else _kept(folder)
        if kept is not None and kept.reading != READING:
            logger.info(
                "The tables kept for %s were read by older code; reading them again", installed
            )
        elif kept is not None:
            logger.info("Using the tables kept for %s in %s", installed, folder)
            return GameDataReport(
                outcome="cached", installed=installed, directory=folder, tables=kept
            )
        tables = _extract(adb, installed)
        logger.info(
            "Read %d entities of %s off the emulator into %s",
            len(tables.entities),
            installed,
            folder,
        )
        return GameDataReport(
            outcome="extracted", installed=installed, directory=folder, tables=tables
        )
    # RuntimeError covers ADB failing and the emulator being down; ValueError
    # covers tables this code cannot read, and a file that does not decode.
    except (RuntimeError, OSError, ValueError):
        folders = sorted(GAME_DATA_DIR.glob("*/"), key=_version_key, reverse=True)
        for folder in folders:
            if (kept := _kept(folder)) is not None:
                logger.warning(
                    "Could not read the game's tables off the emulator; using %s's",
                    folder.name,
                    exc_info=True,
                )
                return GameDataReport(
                    outcome="fallback", installed=installed, directory=folder, tables=kept
                )
        raise
