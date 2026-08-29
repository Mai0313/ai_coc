"""The one settings file, beside the key store the window and the terminal already share."""

from __future__ import annotations

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
        self.path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
