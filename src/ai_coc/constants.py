from pathlib import Path

from ai_coc import __version__

APP_NAME = "AI CoC"
# Also the QSettings organisation, so the registry key and the executable's file
# properties cannot drift apart.
ORGANISATION = "Hsien0818666"

VERSION_LABEL = f"v{__version__}"
SCHEMA_VERSION = "1"
MASTER_DB_VERSION = "seed-2026-08-22"
AGENT_PROFILE_VERSION = "0.1.0"
COC_PACKAGE = "com.supercell.clashofclans"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_ADB_HOST = "127.0.0.1"


def data_root() -> Path:
    root = Path.home() / ".ai_coc"
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

# The community keeps this data_id → name table current with each game update.
# Without it every imported entity shows as UNKNOWN.
ENTITY_MAPPING_URL = "https://gist.githubusercontent.com/rahulkhatri137/a8449943df45100c5f1e1359cd9ec67a/raw/cocMapping.json"
ENTITY_MAPPING_PATH = data_root() / "cocMapping.json"

# The mapping groups only separate home village from builder base, so the kind of
# entity comes from the data_id block instead: 4000123 // 1_000_000 == 4, a troop.
ENTITY_CATEGORIES = {
    1: "building",
    4: "troop",
    12: "trap",
    26: "spell",
    28: "hero",
    73: "pet",
    93: "helper",
}
