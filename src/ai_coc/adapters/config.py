"""The one settings file, beside the key store the window and the terminal already share."""

from __future__ import annotations

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
        """
        if not self.path.is_file():
            return AppConfig()
        return AppConfig.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, config: AppConfig) -> None:
        self.path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
