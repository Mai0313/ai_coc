"""Written-out attack plans, so a tactic can be read, edited and replayed as a file.

`flat.json` is the one the loop falls back to with no AI and nothing passed in.
It spreads everything evenly — troops along one flank, rage over the whole route
they walk, freeze held for the middle — which is not a good tactic so much as a
safe one: it runs against any village and gives a baseline to compare a real plan
against. It is exactly what the loop used to do from constants, written down.
"""

from __future__ import annotations

from typing import Annotated
from pathlib import Path

from pydantic import Tag, TypeAdapter, Discriminator

from ai_coc.models import NightPlan, AttackPlan

# Beside this module for the same reason as the prompts: PyInstaller lays the
# bundle out as a directory tree, so a path relative to the module finds these
# frozen or not, given the matching `--add-data`.
PLAN_DIR = Path(__file__).parent


def _kind(data: object) -> str:
    """Which village a plan document is for: only a home village plan has `steps`."""
    return "day" if isinstance(data, dict) and "steps" in data else "night"


# Either village's plan, decided before validating so a hand-edited file that
# is slightly wrong reports its own type's errors rather than both types'.
_EITHER: TypeAdapter[AttackPlan | NightPlan] = TypeAdapter(
    Annotated[
        Annotated[AttackPlan, Tag("day")] | Annotated[NightPlan, Tag("night")],
        Discriminator(_kind),
    ]
)


def load(path: Path) -> AttackPlan:
    return AttackPlan.model_validate_json(path.read_text(encoding="utf-8"))


def load_any(path: Path) -> AttackPlan | NightPlan:
    """A written plan for whichever village it was written for, which is what `--plan` takes."""
    return _EITHER.validate_json(path.read_text(encoding="utf-8"))


def flat() -> AttackPlan:
    """The everything-spread-evenly plan, used when nothing better was supplied."""
    return load(PLAN_DIR / "flat.json")


def load_night(path: Path) -> NightPlan:
    """One builder base plan off disk; a handed-in one goes through `load_any`."""
    return NightPlan.model_validate_json(path.read_text(encoding="utf-8"))


def night_flat() -> NightPlan:
    """The builder base's own flat plan, which is the same flank and no spells.

    Its whole tactic is the drop line, because that mode has nothing else to
    decide: no spells to place and no ability moment to pick, since the machine
    recharges instead of firing once. So this is a shorter document than
    `flat.json` rather than a poorer one.
    """
    return load_night(PLAN_DIR / "night_flat.json")
