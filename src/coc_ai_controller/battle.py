from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from .models import BattleScript, ArmyRequirement

REQUIREMENT_SECTIONS = ("troops", "heroes", "spells", "siege", "reinforcements")


def load_battle_script(path: str | Path) -> BattleScript:
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    requirements = raw.get("army_requirements")
    if not isinstance(requirements, dict):
        raise ValueError("Battle Script requires army_requirements")
    normalized: dict[str, list[ArmyRequirement]] = {}
    for key in REQUIREMENT_SECTIONS:
        values = requirements.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"army_requirements.{key} must be a list")
        normalized[key] = [
            ArmyRequirement.model_validate(value) for value in values if isinstance(value, dict)
        ]
    controller: Any = raw.get("battle_controller") or {}
    return BattleScript(
        script_id=str(raw.get("script_id") or ""),
        name=str(raw.get("name") or raw.get("script_id") or "Unnamed"),
        world=str(raw.get("world") or "unknown"),
        requirements=normalized,
        battle_controller=str(controller.get("type") or "RESERVED_RL"),
    )
