from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class EmulatorInstance:
    emulator_id: str
    index: int
    name: str
    android_version: str
    adb_serial: str
    process_started: bool
    android_started: bool
    state: str
    pid: int
    main_hwnd: int
    render_hwnd: int
    resolution: str = "unknown"
    dpi: str = "unknown"
    coc_running: bool = False


@dataclass(frozen=True)
class Frame:
    frame_id: str
    emulator_id: str
    account_tag: str
    captured_at: str
    png: bytes

    @classmethod
    def create(cls, emulator_id: str, account_tag: str, png: bytes, sequence: int) -> "Frame":
        now = datetime.now(timezone.utc)
        return cls(
            frame_id=f"{emulator_id}:{sequence}:{int(now.timestamp() * 1000)}",
            emulator_id=emulator_id,
            account_tag=account_tag,
            captured_at=now.isoformat(),
            png=png,
        )


@dataclass
class AccountSnapshot:
    tag: str
    raw: dict[str, Any]
    entities: list[dict[str, Any]] = field(default_factory=list)
