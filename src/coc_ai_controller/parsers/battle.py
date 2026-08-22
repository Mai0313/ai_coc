from __future__ import annotations

from pathlib import Path

from coc_ai_controller.models import BattleScript


def load_battle_script(path: str | Path) -> BattleScript:
    """Validate a Battle Script file; a missing `army_requirements` is the only hard error."""
    return BattleScript.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
