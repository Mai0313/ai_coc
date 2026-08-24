"""The headless side of the application: what `cli.py` runs when given a command.

Nothing here builds a window. Everything below the `ui/` package is already
Qt-free, so the only thing the window was ever providing was the wiring — find
the emulator, resolve the display it put the game on, hand over the API key —
and that is what these functions are. A feature that can only be reached through
a widget cannot be run against the live game while it is being worked on, which
is the whole reason the attack loop grew a `frame_dir` at the same time.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import logging

from ai_coc.models import (
    HeroTimings,
    StockLimits,
    AttackReport,
    FrameReading,
    GeminiSettings,
    LootThresholds,
)
from ai_coc.constants import COC_PACKAGE, DEFAULT_GEMINI_MODEL
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.parsers.scout import (
    card_count,
    live_cards,
    read_scout,
    read_stock,
    card_groups,
    freeze_cards,
    army_strength,
    counted_cards,
    deploy_refused,
    attack_menu_open,
    idle_disconnected,
)
from ai_coc.adapters.secrets import SecretStore

from .ui.attack import AttackRunner

if TYPE_CHECKING:
    from pathlib import Path

    from ai_coc.adapters.adb import AdbController

logger = logging.getLogger(__name__)


def _controller() -> AdbController:
    """The first MuMu instance, with Clash of Clans already up on it."""
    mumu = MuMuAdapter()
    instances = mumu.enumerate_instances()
    if not instances:
        raise RuntimeError("找不到任何 MuMu instance")
    return mumu.controller(mumu.ensure_coc(instances[0].index).adb_serial)


def _planner() -> GeminiClient | None:
    """The saved key, or None so the attack falls back to its fixed flank."""
    try:
        key = SecretStore().load()
    except Exception:
        logger.warning("No saved API key could be read", exc_info=True)
        return None
    if not key:
        logger.info("No API key is saved; the attack will use the fixed flank and spell grid")
        return None
    return GeminiClient(settings=GeminiSettings(api_key=key, model=DEFAULT_GEMINI_MODEL))


def attack(
    frame_dir: Path | None = None, thresholds: LootThresholds | None = None
) -> AttackReport:
    """One pass of the attack loop, with no window in the way.

    Thresholds default to zero, so a run started from a terminal attacks the
    first opponent it is shown. That is what a run being studied wants: skipping
    is already covered by its own tests, and the code worth watching is the part
    that only runs once an opponent has been accepted.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    report = AttackRunner(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        thresholds=thresholds or LootThresholds(),
        stock=StockLimits(),
        abilities=HeroTimings(),
        ai=_planner(),
        frame_dir=frame_dir,
    ).run()
    logger.info("Attack finished: %s", report.message)
    return report


def capture(out_dir: Path, count: int = 1, gap: float = 1.5) -> list[Path]:
    """Save frames off the live game, for measuring a screen the parsers cannot read yet.

    A burst rather than a single shot: the screens worth measuring are the ones
    that only exist while something is happening, and those cannot be reached by
    asking for one frame at the right moment.
    """
    adb = _controller()
    display = adb.display_for(COC_PACKAGE)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index in range(count):
        path = out_dir / f"frame_{index:03d}.png"
        path.write_bytes(adb.screenshot(display))
        logger.info("Saved %s", path)
        saved.append(path)
        if index + 1 < count:
            time.sleep(gap)
    return saved


def read(png: bytes) -> FrameReading:
    """Put one frame through every parser at once and report what each one saw."""
    groups = card_groups(png)
    slots = [slot for group in groups for slot in group]
    return FrameReading(
        scout=read_scout(png),
        stock=read_stock(png),
        army=army_strength(png),
        attack_menu=attack_menu_open(png),
        refused=deploy_refused(png),
        idle_dialog=idle_disconnected(png),
        card_groups=groups,
        counted=counted_cards(png, slots),
        freezes=freeze_cards(png, slots),
        live=live_cards(png, slots),
        counts={slot: card_count(png, slot) for slot in slots},
    )
