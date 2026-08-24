from __future__ import annotations

from typing import Any, Literal
from datetime import UTC, datetime

from pydantic import Field, BaseModel, RootModel, ConfigDict, AliasChoices, field_validator

from .constants import DEFAULT_ADB_HOST, ENTITY_CATEGORIES, DEFAULT_GEMINI_MODEL

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


class MapFrame(BaseModel):
    """The battle map's diamond in screen pixels, at the camera a battle opens on.

    Calibrated against live frames rather than detected from them: a village
    theme repaints the ground the map's edge runs along, so anything reading that
    edge by colour or brightness falls over on the next theme, while the camera
    itself does not move. The figures are good to about 20 px, which is enough
    for the one thing this is load-bearing for — keeping a drop that is being
    pushed away from the village from being pushed off the map — and not yet
    enough for anything that needs a particular tile to be hit.
    """

    model_config = ConfigDict(frozen=True)

    centre: tuple[int, int]
    half_width: int
    half_height: int
    # The home village grid, which is what the map is drawn from.
    tiles: int = 44

    def contains(self, point: tuple[int, int]) -> bool:
        return (
            abs(point[0] - self.centre[0]) / self.half_width
            + abs(point[1] - self.centre[1]) / self.half_height
        ) <= 1

    def clamp(self, point: tuple[int, int]) -> tuple[int, int]:
        """Pull a point back onto the map along the line from the middle.

        A rectangle cannot do this: the map is a diamond, so a rectangle's own
        corners are off the map entirely, and a drop pushed into one lands
        nowhere the game will accept it.
        """
        dx = (point[0] - self.centre[0]) / self.half_width
        dy = (point[1] - self.centre[1]) / self.half_height
        span = abs(dx) + abs(dy)
        if span <= 1:
            return point
        # Truncated rather than rounded, which moves each offset towards the
        # middle: rounding outwards leaves the result a pixel over the edge and
        # failing `contains`, which is a trap for anyone who checks it later.
        return (
            self.centre[0] + int(dx / span * self.half_width),
            self.centre[1] + int(dy / span * self.half_height),
        )

    def grown(self, tiles: int) -> MapFrame:
        """The same diamond widened by a number of tiles on every side."""
        scale = (self.tiles + 2 * tiles) / self.tiles
        return MapFrame(
            centre=self.centre,
            half_width=round(self.half_width * scale),
            half_height=round(self.half_height * scale),
            tiles=self.tiles + 2 * tiles,
        )

    def tile(self, point: tuple[int, int]) -> tuple[float, float]:
        """Grid coordinates for a screen point, the axes running along the edges."""
        across = (point[0] - self.centre[0]) / (self.half_width / self.tiles)
        down = (point[1] - self.centre[1]) / (self.half_height / self.tiles)
        return ((across + down) / 2 + self.tiles / 2, (down - across) / 2 + self.tiles / 2)

    def pixel(self, tile: tuple[float, float]) -> tuple[int, int]:
        """Where one grid cell's corner sits on screen; the inverse of `tile`."""
        column, row = tile[0] - self.tiles / 2, tile[1] - self.tiles / 2
        return (
            round(self.centre[0] + (column - row) * self.half_width / self.tiles),
            round(self.centre[1] + (column + row) * self.half_height / self.tiles),
        )


class DisplayTarget(BaseModel):
    """The display one package's window sits on.

    MuMu runs several Android displays and keeps display 0 on its own launcher, so
    every capture and every input has to name the one holding the game. The two ids
    travel together because the numbering schemes differ: `screencap -d` takes the
    physical id, `input -d` the logical one.
    """

    model_config = ConfigDict(frozen=True)

    logical_id: str
    physical_id: str


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


class EntityMapping(RootModel[dict[str, dict[int, str]]]):
    """The community data_id → name table, keyed by group such as `th_buildings`."""

    def registry_entries(self, source_url: str) -> list[RegistryEntry]:
        return [
            RegistryEntry(
                data_id=data_id,
                name=name,
                world="builder_base" if group.startswith("bh_") else "home",
                category=ENTITY_CATEGORIES.get(data_id // 1_000_000, "other"),
                source_url=source_url,
                verification_status="COMMUNITY",
            )
            for group, entries in self.root.items()
            for data_id, name in entries.items()
        ]


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


class LootOffer(BaseModel):
    """The lootable resources a scouted opponent shows, read off the screenshot."""

    model_config = ConfigDict(frozen=True)

    gold: int
    elixir: int
    dark: int


class LootThresholds(BaseModel):
    """The minimum loot the automation tab requires before an opponent is worth attacking."""

    model_config = ConfigDict(frozen=True)

    min_gold: int = 0
    min_elixir: int = 0
    min_dark: int = 0

    def accepts(self, offer: LootOffer) -> bool:
        """Every threshold has to be met; a resource that does not matter is left at 0."""
        return (
            offer.gold >= self.min_gold
            and offer.elixir >= self.min_elixir
            and offer.dark >= self.min_dark
        )


class VillageStock(BaseModel):
    """What the village itself is holding, read off the home screen's storage bars.

    The same three resources as `LootOffer` and deliberately not the same type:
    one is what an opponent is carrying and the other is what we already have,
    and nothing in the loop wants to mix them up.
    """

    model_config = ConfigDict(frozen=True)

    gold: int
    elixir: int
    dark: int


class StockLimits(BaseModel):
    """The storage levels the automation stops farming at; 0 leaves one unwatched."""

    model_config = ConfigDict(frozen=True)

    stop_gold: int = 0
    stop_elixir: int = 0
    stop_dark: int = 0

    def reached(self, stock: VillageStock) -> list[str]:
        """Which watched resources are at or past their limit, named for the log.

        Any one of them is enough to stop on: loot past a full storage is thrown
        away on collection, so farming for a second resource that is still short
        means paying a search fee to overfill the first.
        """
        return [
            name
            for name, limit, held in (
                ("金幣", self.stop_gold, stock.gold),
                ("聖水", self.stop_elixir, stock.elixir),
                ("黑水", self.stop_dark, stock.dark),
            )
            if limit and held >= limit
        ]


class ProbeRay(BaseModel):
    """What one ray of a boundary survey found, against what the reader predicted.

    Two drops per ray, one either side of the predicted line. The one inside is
    expected to be refused and the one outside accepted; anything else is the
    reader disagreeing with the game, which is the whole reason to run this.
    """

    model_config = ConfigDict(frozen=True)

    degrees: float
    predicted: int
    inside_refused: bool
    outside_refused: bool

    @property
    def agrees(self) -> bool:
        return self.inside_refused and not self.outside_refused


class BoundarySurvey(BaseModel):
    """A whole survey, and how much of it the boundary reader got right."""

    rays: list[ProbeRay] = Field(default_factory=list)
    unread: list[float] = Field(default_factory=list)

    @property
    def agreement(self) -> str:
        agreed = sum(ray.agrees for ray in self.rays)
        return f"{agreed}/{len(self.rays)} rays agreed, {len(self.unread)} unread"


class ScoutView(BaseModel):
    """What the opponent screen offers, and whether it can still be skipped.

    The loot panel stays on screen once the battle starts, so `can_skip` is what
    separates the 30-second scout window from a battle already under way.
    """

    model_config = ConfigDict(frozen=True)

    loot: LootOffer
    can_skip: bool


class ScreenPoint(BaseModel):
    """A spot on the battle screen, as percentages the way `AgentAction` uses them."""

    model_config = ConfigDict(frozen=True)

    x_pct: float = Field(ge=0, le=100)
    y_pct: float = Field(ge=0, le=100)

    def pixels(self) -> tuple[int, int]:
        return round(self.x_pct * 16), round(self.y_pct * 9)


HeroKind = Literal["king", "queen", "warden", "champion", "minion_prince", "unknown"]


class HeroTimings(BaseModel):
    """How long after landing each hero's ability fires.

    Keyed by hero rather than by card position: a hero being upgraded cannot
    take the field, so its card is simply absent and every position shifts.
    """

    model_config = ConfigDict(frozen=True)

    king: int = 20
    queen: int = 1
    warden: int = 30
    champion: int = 45
    minion_prince: int = 20
    unknown: int = 20

    def seconds(self, kind: HeroKind) -> int:
        return int(getattr(self, kind, self.unknown))


class AttackPlan(BaseModel):
    """How to attack one opponent, chosen by reading its scout screen.

    The drop line comes as two free endpoints, because no fixed set of flanks
    survives the range of layouts. `deploy_from` stays underneath it as the
    fallback: a drop has to land outside the deployment boundary, and a named
    side maps onto a line already known to be outside it, which is what the loop
    falls back to when there is no plan or the planned line crosses the village.
    """

    deploy_from: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = "top_left"
    # Required, and that is the whole point: with defaults they are optional in
    # the JSON schema, and Gemini answered three runs running with a start and no
    # end. Half a line is no line, so the call was paid for and its most
    # important output thrown away every time. A reply that still omits one now
    # fails validation, which `_plan` already answers by falling back — the same
    # place it ended up before, but without pretending it had a plan.
    deploy_start: ScreenPoint
    deploy_end: ScreenPoint
    rage_points: list[ScreenPoint] = Field(default_factory=list)
    freeze_points: list[ScreenPoint] = Field(default_factory=list)
    # Left to right, so each hero card can be matched to its own ability timing.
    heroes: list[HeroKind] = Field(default_factory=list)
    # Carried on the plan so a written-out one is the whole tactic in one file,
    # rather than a set of points whose timing lives somewhere else entirely.
    # None leaves the runner on whatever the caller configured.
    timings: HeroTimings | None = None
    reason: str = ""


class AttackReport(BaseModel):
    """What one run of the attack loop did, for the automation log."""

    skipped: int = 0
    attacked: LootOffer | None = None
    message: str = ""
    # Farming has met its goal, so the automation is meant to stop rather than
    # come round again: the next pass would only read the same full storage.
    stock_full: bool = False


class FrameReading(BaseModel):
    """Everything the parsers make of one frame, for the `read` command.

    A screen the loop mishandled is almost always a screen it misread, and this
    is what says which of the readers disagreed with the eye. It is one model
    rather than a printout so a recorded run can be replayed through it.
    """

    scout: ScoutView | None = None
    stock: VillageStock | None = None
    army: tuple[int, int] | None = None
    attack_menu: bool = False
    idle_dialog: bool = False
    card_groups: list[list[int]] = Field(default_factory=list)
    counted: list[int] = Field(default_factory=list)
    freezes: list[int] = Field(default_factory=list)
    live: list[int] = Field(default_factory=list)
    on_field: list[int] = Field(default_factory=list)
    counts: dict[int, int | None] = Field(default_factory=dict)


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
