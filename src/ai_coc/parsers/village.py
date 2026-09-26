from __future__ import annotations

from pathlib import Path

from ai_coc.models import AccountSnapshot, VillageDocument


def parse_village(path: str | Path) -> AccountSnapshot:
    return parse_village_text(Path(path).read_text(encoding="utf-8-sig"))


def parse_village_text(text: str) -> AccountSnapshot:
    """Every section the export carries, whichever ones this release happens to hold.

    There used to be a written-down list of twelve, which is what a game that
    adds a section between releases silently drops. Measured against one real
    export it was dropping ten of them — `helpers`, `decos`, `obstacles`,
    `skins`, `house_parts` and their builder-base twins — including the three
    named helpers the mapping does know. So the sections are whatever the
    document holds a list under, which is also what the tolerance rule in
    AGENTS.md asks for: a section nobody has heard of survives instead of
    needing a code change first.
    """
    document = VillageDocument.model_validate_json(text.lstrip("﻿").strip())
    entities = [entity for section in document.sections() for entity in document.entries(section)]
    return AccountSnapshot(tag=document.tag, raw=document, entities=entities)
