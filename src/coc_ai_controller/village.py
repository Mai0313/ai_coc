from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from .models import AccountSnapshot

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


def _integer(value: Any, default: int | None = None) -> int | None:  # noqa: ANN401 - arbitrary JSON value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_village(path: str | Path) -> AccountSnapshot:
    return parse_village_text(Path(path).read_text(encoding="utf-8-sig"))


def parse_village_text(text: str) -> AccountSnapshot:
    raw = json.loads(text.lstrip("\ufeff").strip())
    if not isinstance(raw, dict):
        raise ValueError("Village JSON root must be an object")
    tag = str(raw.get("tag") or raw.get("player_tag") or "UNKNOWN").strip()
    entities: list[dict[str, Any]] = []
    for section in SECTIONS:
        values = raw.get(section, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            data_id = _integer(item.get("data", item.get("data_id", item.get("id"))))
            if data_id is None:
                continue
            entities.append({
                "section": section,
                "data_id": data_id,
                "level": _integer(item.get("lvl", item.get("level"))),
                "count": _integer(item.get("cnt", item.get("count")), 1) or 1,
                "raw": item,
            })
    return AccountSnapshot(tag=tag, raw=raw, entities=entities)
