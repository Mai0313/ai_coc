from __future__ import annotations

from pathlib import Path

from ai_coc.models import AccountSnapshot, VillageDocument

SECTIONS = (
    "buildings",
    "buildings2",
    "traps",
    "traps2",
    "units",
    "units2",
    "heroes",
    "heroes2",
    "pets",
    "spells",
    "equipment",
    "siege_machines",
)


def parse_village(path: str | Path) -> AccountSnapshot:
    return parse_village_text(Path(path).read_text(encoding="utf-8-sig"))


def parse_village_text(text: str) -> AccountSnapshot:
    document = VillageDocument.model_validate_json(text.lstrip("﻿").strip())
    # A snapshot the application saved itself keeps the village payload under `raw`.
    extra = document.model_extra or {}
    if "entities" in extra and isinstance(extra.get("raw"), dict):
        document = VillageDocument.model_validate(extra["raw"])
    entities = [entity for section in SECTIONS for entity in document.entries(section)]
    return AccountSnapshot(tag=document.tag, raw=document, entities=entities)
