from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BattleScript:
    script_id: str
    name: str
    world: str
    requirements: dict[str, list[dict[str, Any]]]
    battle_controller: str


def load_battle_script(path: str | Path) -> BattleScript:
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    requirements = raw.get("army_requirements")
    if not isinstance(requirements, dict):
        raise ValueError("Battle Script requires army_requirements")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for key in ("troops", "heroes", "spells", "siege", "reinforcements"):
        values = requirements.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"army_requirements.{key} must be a list")
        normalized[key] = [v for v in values if isinstance(v, dict)]
    return BattleScript(
        script_id=str(raw.get("script_id") or ""),
        name=str(raw.get("name") or raw.get("script_id") or "Unnamed"),
        world=str(raw.get("world") or "unknown"),
        requirements=normalized,
        battle_controller=str((raw.get("battle_controller") or {}).get("type") or "RESERVED_RL"),
    )
