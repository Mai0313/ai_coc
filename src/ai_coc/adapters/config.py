"""The one settings file, beside the key store the window and the terminal already share."""

from __future__ import annotations

import os
import json
from pathlib import Path

from pydantic import Field, BaseModel

from ai_coc.models import AppConfig
from ai_coc.constants import data_root


class ConfigStore(BaseModel):
    path: Path = Field(default_factory=lambda: data_root() / "config.json")

    def load(self) -> AppConfig:
        """The saved settings, or the defaults where nothing has been saved yet.

        A file that will not parse raises rather than falling back to the
        defaults: those defaults mean "attack anything, stop at nothing", and
        arriving at them because a comma was typed wrong is exactly the silent
        failure this file exists to close.

        **A file holding anything the model no longer reads is rewritten to what
        it does.** Pydantic ignores an unknown key, which keeps an upgrade from
        failing and leaves the key sitting there looking like a setting still
        being honoured — `timings` stayed in every existing file for a release
        after every clock moved onto the plan, so a user editing 大守護者's
        thirty seconds would have been editing nothing at all. The same pass
        fills in a key the file never had, which is what makes it readable as a
        statement of what this run will do rather than of what was saved once.
        """
        if not self.path.is_file():
            return AppConfig()
        raw = self.path.read_text(encoding="utf-8")
        config = AppConfig.model_validate_json(raw)
        if json.loads(raw) != json.loads(config.model_dump_json()):
            self.save(config)
        return config

    def save(self, config: AppConfig) -> None:
        """Replace the file rather than rewrite it, so no reader sees it half-built.

        The same reasoning as `_write_state`, and it became reachable for the
        same reason that one did: `write_text` truncates before it writes, and
        this file is read from a worker thread — every `commands.*` that farms
        or spends opens it — while the window's own preview switches now write
        it. A read landing in that window gets zero bytes, which `load` answers
        with a `ValidationError` on a file that was never broken, and `load`
        raises by design, so that ends the pass.

        The scratch name carries this process's pid because the writers really
        are concurrent: `load` itself rewrites a file whose keys have moved, so
        a headless run started while the window is open is a second writer.
        """
        scratch = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        scratch.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        os.replace(scratch, self.path)
