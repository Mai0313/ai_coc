from __future__ import annotations

from typing import Any, Literal
from datetime import UTC, datetime

from pydantic import Field, BaseModel, RootModel, ConfigDict, AliasChoices, field_validator

from .constants import DEFAULT_ADB_HOST, DEFAULT_GEMINI_MODEL

# Village JSON, Battle Scripts and the MuMu CLI all gain fields between game and
# emulator releases. Models that mirror them allow extras so an unknown field is
# preserved instead of failing the import.
TOLERANT = ConfigDict(extra="allow")


def tolerant_int(value: Any, default: int | None = None) -> int | None:  # noqa: ANN401 - arbitrary external JSON value
    """Read an integer out of a payload that may hold a string, a null or junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        return tolerant_int(value, 0) or 0

    @property
    def endpoint(self) -> AdbEndpoint:
        return AdbEndpoint(host=self.adb_host_ip, port=self.adb_port)

    def hwnd(self, field: str) -> int:
        value = str(getattr(self, field, "0")) or "0"
        try:
            return int(value, 16)
        except ValueError:
            return 0


class MuMuInstanceTable(RootModel[dict[str, Any]]):
    """`info --vmindex all` keyed by instance index, alongside CLI status fields."""

    def instances(self) -> list[MuMuInstanceInfo]:
        return [
            MuMuInstanceInfo.model_validate(value)
            for value in self.root.values()
            if isinstance(value, dict) and "index" in value
        ]


class MuMuCliVersion(BaseModel):
    model_config = TOLERANT

    version: str = "unknown"


class MuMuCliResult(BaseModel):
    """What a `control` command answers; validating it proves the CLI replied."""

    model_config = TOLERANT


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


class VillageEntry(BaseModel):
    """One row of a Village JSON section, under any of its historical key names."""

    model_config = TOLERANT

    data_id: int = Field(validation_alias=AliasChoices("data", "data_id", "id"))
    level: int | None = Field(default=None, validation_alias=AliasChoices("lvl", "level"))
    count: int = Field(default=1, validation_alias=AliasChoices("cnt", "count"))

    @field_validator("level", mode="before")
    @classmethod
    def _tolerant_level(cls, value: Any) -> int | None:  # noqa: ANN401 - arbitrary JSON value
        return tolerant_int(value)

    @field_validator("count", mode="before")
    @classmethod
    def _tolerant_count(cls, value: Any) -> int:  # noqa: ANN401 - arbitrary JSON value
        return tolerant_int(value, 1) or 1


class VillageEntity(VillageEntry):
    """One building, troop, hero or spell read out of a Village JSON export."""

    section: str


class VillageDocument(BaseModel):
    """A Village JSON export; sections the game adds later survive as extras."""

    model_config = TOLERANT

    tag: str = Field(default="UNKNOWN", validation_alias=AliasChoices("tag", "player_tag"))

    @field_validator("tag", mode="before")
    @classmethod
    def _clean_tag(cls, value: Any) -> str:  # noqa: ANN401 - arbitrary JSON value
        return str(value or "").strip() or "UNKNOWN"

    def entries(self, section: str) -> list[VillageEntity]:
        """Rows of one section; a row without a usable data_id is skipped, not fatal."""
        values = (self.model_extra or {}).get(section)
        if not isinstance(values, list):
            return []
        entities: list[VillageEntity] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            try:
                entities.append(VillageEntity.model_validate({**item, "section": section}))
            except ValueError:
                continue
        return entities


class AccountSnapshot(BaseModel):
    tag: str
    raw: VillageDocument
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


class AccountRowList(RootModel[list[AccountRow]]):
    """Account rows on their way into a prompt."""


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


class ArmyRequirements(BaseModel):
    """The `army_requirements` block of a Battle Script, one list per category."""

    model_config = TOLERANT

    troops: list[ArmyRequirement] = Field(default_factory=list)
    heroes: list[ArmyRequirement] = Field(default_factory=list)
    spells: list[ArmyRequirement] = Field(default_factory=list)
    siege: list[ArmyRequirement] = Field(default_factory=list)
    reinforcements: list[ArmyRequirement] = Field(default_factory=list)

    def categories(self) -> list[tuple[str, list[ArmyRequirement]]]:
        """The declared categories only; one the game adds later stays in extras."""
        return [(name, getattr(self, name)) for name in ArmyRequirements.model_fields]


class BattleController(BaseModel):
    """The handoff boundary a Battle Script names; V1 always reserves it for RL."""

    model_config = TOLERANT

    kind: str = Field(default="RESERVED_RL", validation_alias="type")


class BattleScript(BaseModel):
    model_config = TOLERANT

    script_id: str = ""
    name: str = ""
    world: str = "unknown"
    army_requirements: ArmyRequirements
    battle_controller: BattleController = Field(default_factory=BattleController)

    @property
    def display_name(self) -> str:
        return self.name or self.script_id or "Unnamed"


class UiElement(BaseModel):
    """One clickable or labelled node from Android's accessibility hierarchy."""

    model_config = ConfigDict(frozen=True)

    text: str
    resource_id: str
    clickable: bool
    x: int
    y: int


class UiElementList(RootModel[list[UiElement]]):
    """Accessibility nodes on their way into a prompt."""


class GeminiSettings(BaseModel):
    api_key: str = ""
    model: str = DEFAULT_GEMINI_MODEL
    base_url: str = ""


class GeminiTextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class GeminiImagePart(BaseModel):
    type: Literal["image"] = "image"
    data: str
    mime_type: str = "image/png"


class GeminiResponseFormat(BaseModel):
    """Structured output: the JSON schema Gemini must answer with."""

    type: Literal["text"] = "text"
    mime_type: str = "application/json"
    json_schema: dict[str, Any] = Field(serialization_alias="schema")


class GeminiRequest(BaseModel):
    """One `interactions.create` body, built here instead of as a loose dict."""

    model: str
    input: list[GeminiTextPart | GeminiImagePart]
    response_format: GeminiResponseFormat | None = None

    def body(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


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


ChatRole = Literal["user", "assistant", "system"]


class ChatMessage(BaseModel):
    """One entry of the AI 助手 transcript; `body` is Markdown."""

    role: ChatRole
    heading: str
    body: str = ""


class ChatTranscript(RootModel[list[ChatMessage]]):
    """The AI 助手 conversation, re-rendered to HTML as a streamed reply grows."""

    root: list[ChatMessage] = Field(default_factory=list)

    def add(self, role: ChatRole, heading: str, body: str = "") -> ChatMessage:
        message = ChatMessage(role=role, heading=heading, body=body)
        self.root.append(message)
        return message

    def tail(self, characters: int) -> str:
        """The end of the conversation as plain text, for the next prompt's context."""
        joined = "\n\n".join(f"{item.heading}\n{item.body}" for item in self.root)
        return joined[-characters:]
