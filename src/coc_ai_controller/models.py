from __future__ import annotations

from typing import Any, Literal
from datetime import UTC, datetime

from pydantic import Field, BaseModel, ConfigDict, field_validator

from .constants import DEFAULT_ADB_HOST, DEFAULT_GEMINI_MODEL

# Village JSON, Battle Scripts and the MuMu CLI all gain fields between game and
# emulator releases. Models that mirror them allow extras so an unknown field is
# preserved instead of failing the import.
TOLERANT = ConfigDict(extra="allow")


class AdbEndpoint(BaseModel):
    """One emulator's ADB address; MuMu gives every instance its own port."""

    model_config = ConfigDict(frozen=True)

    host: str = DEFAULT_ADB_HOST
    port: int = 0

    @property
    def serial(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def ready(self) -> bool:
        """MuMu reports port 0 until the instance's ADB bridge is listening."""
        return self.port > 0

    @classmethod
    def parse(cls, serial: str) -> AdbEndpoint:
        host, _, port = serial.strip().rpartition(":")
        return cls(host=host or DEFAULT_ADB_HOST, port=int(port) if port.isdigit() else 0)


class MuMuInstanceInfo(BaseModel):
    """One entry of `mumu-cli info --vmindex all`."""

    model_config = TOLERANT

    index: int
    name: str = ""
    android_version: str = "unknown"
    adb_host_ip: str = DEFAULT_ADB_HOST
    adb_port: int = 0
    is_process_started: bool = False
    is_android_started: bool = False
    player_state: str = "unknown"
    pid: int = 0
    main_wnd: str = "0"
    render_wnd: str = "0"

    @field_validator("adb_port", "pid", mode="before")
    @classmethod
    def _tolerant_int(cls, value: Any) -> int:  # noqa: ANN401 - arbitrary CLI JSON value
        text = str(value).strip()
        return int(text) if text.lstrip("-").isdigit() else 0

    @property
    def endpoint(self) -> AdbEndpoint:
        return AdbEndpoint(host=self.adb_host_ip, port=self.adb_port)

    def hwnd(self, field: str) -> int:
        value = str(getattr(self, field, "0")) or "0"
        try:
            return int(value, 16)
        except ValueError:
            return 0


class EmulatorInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

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

    @property
    def endpoint(self) -> AdbEndpoint:
        return AdbEndpoint.parse(self.adb_serial)


class Frame(BaseModel):
    model_config = ConfigDict(frozen=True)

    frame_id: str
    emulator_id: str
    account_tag: str
    captured_at: str
    png: bytes

    @classmethod
    def create(cls, emulator_id: str, account_tag: str, png: bytes, sequence: int) -> Frame:
        now = datetime.now(UTC)
        return cls(
            frame_id=f"{emulator_id}:{sequence}:{int(now.timestamp() * 1000)}",
            emulator_id=emulator_id,
            account_tag=account_tag,
            captured_at=now.isoformat(),
            png=png,
        )


class VillageEntity(BaseModel):
    """One building, troop, hero or spell read out of a Village JSON export."""

    model_config = TOLERANT

    section: str
    data_id: int
    level: int | None = None
    count: int = 1
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountSnapshot(BaseModel):
    tag: str
    raw: dict[str, Any]
    entities: list[VillageEntity] = Field(default_factory=list)


class AccountRow(BaseModel):
    """An account entity joined with the ID registry and any known level data."""

    model_config = TOLERANT

    tag: str
    section: str
    data_id: int
    level: int | None = None
    count: int | None = None
    raw_json: str = ""
    name: str | None = None
    world: str | None = None
    category: str | None = None
    next_level: int | None = None
    upgrade_cost: int | None = None
    resource_type: str | None = None
    upgrade_seconds: int | None = None
    requirement: str | None = None


class RegistryEntry(BaseModel):
    model_config = TOLERANT

    data_id: int
    name: str
    world: str
    category: str
    source_url: str | None = None
    verification_status: str


class KnowledgeItem(BaseModel):
    id: int
    emulator_id: str | None = None
    frame_id: str | None = None
    statement: str
    status: str
    created_at: str


class TaskRecord(BaseModel):
    id: int
    instruction: str
    status: str
    progress: str = ""
    created_at: str
    updated_at: str


class ArmyRequirement(BaseModel):
    model_config = TOLERANT

    data_id: int
    name: str = ""
    required: bool = False


class BattleScript(BaseModel):
    model_config = ConfigDict(frozen=True)

    script_id: str = ""
    name: str = "Unnamed"
    world: str = "unknown"
    requirements: dict[str, list[ArmyRequirement]] = Field(default_factory=dict)
    battle_controller: str = "RESERVED_RL"


class UiElement(BaseModel):
    """One clickable or labelled node from Android's accessibility hierarchy."""

    model_config = ConfigDict(frozen=True)

    text: str
    resource_id: str
    clickable: bool
    x: int
    y: int


class GeminiSettings(BaseModel):
    api_key: str = ""
    model: str = DEFAULT_GEMINI_MODEL
    base_url: str = ""


class LocatedTarget(BaseModel):
    """Where Gemini says a requested control sits, as screen percentages."""

    found: bool
    x_pct: float = 50.0
    y_pct: float = 50.0
    reason: str = ""


class AgentAction(BaseModel):
    """One step of the agent loop: what to do next, or that the task is done."""

    done: bool = False
    action: Literal["tap", "back", "swipe_up", "swipe_down", "none"] = "none"
    x_pct: float = 50.0
    y_pct: float = 50.0
    message: str = ""
