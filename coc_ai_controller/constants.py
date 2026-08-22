from pathlib import Path
import os
import sys

APP_NAME = "CoC AI Controller"
VERSION = "0.1.0"
VERSION_LABEL = f"VER {VERSION}"
UPDATED_DATE = "2026-08-22"
SCHEMA_VERSION = "1"
MASTER_DB_VERSION = "seed-2026-08-22"
AGENT_PROFILE_VERSION = "0.1.0"
COC_PACKAGE = "com.supercell.clashofclans"


def bundle_root() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def data_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CoC_AI_Controller"
    root.mkdir(parents=True, exist_ok=True)
    return root


DB_PATH = data_root() / "controller.sqlite3"
FRAME_DIR = data_root() / "frames"
FRAME_DIR.mkdir(parents=True, exist_ok=True)
