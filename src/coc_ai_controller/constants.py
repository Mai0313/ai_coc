import os
import sys
from pathlib import Path

APP_NAME = "CoC AI Controller"
VERSION = "0.1.0"
VERSION_LABEL = f"VER {VERSION}"
UPDATED_DATE = "2026-08-22"
SCHEMA_VERSION = "1"
MASTER_DB_VERSION = "seed-2026-08-22"
AGENT_PROFILE_VERSION = "0.1.0"
COC_PACKAGE = "com.supercell.clashofclans"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_ADB_HOST = "127.0.0.1"


def bundle_root() -> Path:
    bundle_path = getattr(sys, "_MEIPASS", None)
    if bundle_path:
        return Path(bundle_path)
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
ACCOUNT_JSON_DIR = data_root() / "account_json"
ACCOUNT_JSON_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = data_root() / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "controller.log"
