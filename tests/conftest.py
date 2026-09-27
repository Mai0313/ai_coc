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

from typing import TYPE_CHECKING

import pytest

from ai_coc import commands
from ai_coc.adapters import config

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _state_file_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the state file and the settings file at paths of this test's own, for every test.

    `_held` goes back with it: it is module state that outlives one test, and
    left set by a test that claimed, the next test's `stop_requested` would read
    its own empty directory as somebody having deleted the file. `_borrowed`
    likewise, or the next test's claim would take a loan for its own.
    """
    monkeypatch.setattr(commands, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(commands, "_held", None)
    monkeypatch.setattr(commands, "_borrowed", None)
    monkeypatch.setattr(config, "data_root", lambda: tmp_path)
