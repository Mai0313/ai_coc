from pathlib import Path

from ai_coc import __version__

APP_NAME = "AI CoC"
# The company name in the executable's Windows file properties, which is the
# whole of what it is for now that no setting lives in the registry.
ORGANISATION = "Mai0313"

VERSION_LABEL = f"v{__version__}"
COC_PACKAGE = "com.supercell.clashofclans"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
# The cheap tier, for the one question the parsers provably cannot answer: which
# building a menu belongs to. That is a line of Chinese on a strip of screen, so
# it is a classification rather than a judgement, and it is asked once per
# candidate rather than once per run — which is the call pattern a smaller model
# is actually for.
DEFAULT_LITE_MODEL = "gemini-3.5-flash-lite"
DEFAULT_ADB_HOST = "127.0.0.1"


def data_root() -> Path:
    root = Path.home() / ".ai_coc"
    root.mkdir(parents=True, exist_ok=True)
    return root


ACCOUNT_JSON_DIR = data_root() / "account_json"
ACCOUNT_JSON_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = data_root() / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# How long a run's recorded frames are kept. **The log beside them is never
# culled**, and the size split is what makes that easy to draw: measured across
# 438 runs on this machine, 11 456 PNGs came to 27.2 GB while every `run.log`,
# `result.json` and `plans.jsonl` together came to 4.6 MB. The directory names
# are the history and `grep -r` over the logs is how a pattern is found across
# runs, so those cost nothing worth reclaiming; a week-old frame nobody has
# opened is the whole of it.
FRAME_RETENTION_DAYS = 7

# Which command is driving the emulator, and whether it has been asked to stand
# down. Stopping a headless run has to be a file rather than a signal: the
# window has a stop button, but a run started from a terminal has nothing, since
# whatever put it in the background cannot send it a Ctrl-C — and a killed
# process never reaches the `KeyboardInterrupt` handler, which leaves the army
# on the field and the game on a screen the next run cannot get home from.
#
# **One file answering both questions, because they are one question asked at
# two moments.** It replaced a `stop` flag that existed only while somebody was
# asking, so nothing on this machine recorded that a run was under way: a second
# session had no way to tell a farming run from an idle emulator and was told in
# the chat instead, which is a step the person driving has to remember on every
# session and which nothing catches when they forget.
#
# **Never deleted, so `idle` is a record rather than an absence.** A file that
# existed only while a run did could not separate "nothing has run" from "a run
# just finished", and the second is what a session wanting the screen actually
# asks. Deleting it by hand still stands the loop down, which is the one move
# left to somebody whose agent has died mid-run and who would otherwise be in
# the task manager looking for a pid.
STATE_PATH = data_root() / "state.json"

# The community keeps this data_id → name table current with each game update.
# Without it every imported entity shows as UNKNOWN.
ENTITY_MAPPING_URL = "https://gist.githubusercontent.com/rahulkhatri137/a8449943df45100c5f1e1359cd9ec67a/raw/cocMapping.json"
ENTITY_MAPPING_PATH = data_root() / "cocMapping.json"
