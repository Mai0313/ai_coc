"""What every test here shares: nothing it does reaches outside its own directory.

`commands.attack` and `commands.walls` take the stop flag away at both ends of a
run, and unpatched that is the real `~/.ai_coc/stop`. A suite run then eats a
stop somebody asked of a loop still playing out its battle, which is exactly
the arrangement `CLAUDE.md` sets up: a farming subagent holds the loop, the
main session asks it to stop, and runs the suite while it waits for the loop to
reach its seam. Three helpers reached `commands.attack` without redirecting the
flag before this existed, and each new caller would have had to remember to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ai_coc import commands

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _stop_flag_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the stop flag at a path of this test's own, for every test."""
    monkeypatch.setattr(commands, "STOP_FLAG", tmp_path / "stop")
