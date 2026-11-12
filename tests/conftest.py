"""What every test here shares: nothing it does reaches outside its own directory.

`commands.claim` writes the state file at both ends of a run, and unpatched that
is the real `~/.ai_coc/state.json`. A suite run would then publish the test
process's own pid over whatever is farming and hand the emulator back as `idle`
on the way out, which is exactly the arrangement `AGENTS.md` sets up: a farming
subagent holds the loop while the main session runs the suite beside it. The
flag this replaced did the same damage in a smaller way, eating a stop somebody
had asked of a loop still playing out its battle. Three helpers reached
`commands.attack` without redirecting it before this fixture existed, and each
new caller would have had to remember to.

`~/.ai_coc/config.json` is the same kind of file: loading it rewrites it to the
settings model, so a suite run on a branch that changed the model rewrote the
real one and dropped the serial the farming loop was driving.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import pytest
import adbutils

from ai_coc import commands, logging_setup
from ai_coc.adapters import config, gamedata

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _state_file_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the state file, the settings file, the journal, the saved exports and the game's tables at paths of this test's own, for every test.

    `_held` goes back with it: it is module state that outlives one test, and
    left set by a test that claimed, the next test's `stop_requested` would read
    its own empty directory as somebody having deleted the file. `_borrowed`
    likewise, or the next test's claim would take a loan for its own, and
    `_before`, or the next test's `hold` would name a driver it never saw.
    """
    monkeypatch.setattr(commands, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(commands, "_held", None)
    monkeypatch.setattr(commands, "_borrowed", None)
    monkeypatch.setattr(commands, "_before", None)
    monkeypatch.setattr(config, "data_root", lambda: tmp_path)
    monkeypatch.setattr(logging_setup, "JOURNAL_PATH", tmp_path / "runs.jsonl")
    monkeypatch.setattr(gamedata, "GAME_DATA_DIR", tmp_path / "game_data")
    # `status` and `export` convert every old file they find there.
    exports = tmp_path / "account_json"
    exports.mkdir()
    monkeypatch.setattr(commands, "ACCOUNT_JSON_DIR", exports)


@pytest.fixture(autouse=True)
def _no_emulator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reach no emulator and no ADB server, whatever a test forgets to fake.

    The emulator here is the one a farming loop or a takeover drives while the
    suite runs beside it: a test that faked `claim` but not `status` ran the real
    export walk on it in the middle of a takeover (2026-11-12). Finding the
    emulators and connecting to the ADB server both raise; a test that drives
    either patches it itself.
    """

    def unreachable(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("a test reached for the emulator; fake what it drives")

    monkeypatch.setattr(commands, "MuMuAdapter", unreachable)
    monkeypatch.setattr(commands, "LDPlayerAdapter", unreachable)
    monkeypatch.setattr(adbutils.AdbClient, "make_connection", unreachable)
