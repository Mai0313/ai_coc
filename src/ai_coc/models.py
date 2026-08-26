from __future__ import annotations

import math
from typing import Any, Literal
from pathlib import Path
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


# The middle of the battle map at the camera every attack opens on. It lives
# here because `MapSurvey` fits a diamond around it and `parsers.boundary` reads
# rays out of it, and one of the two would otherwise be importing the other.
DEFAULT_MAP_CENTRE = (800, 400)


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


class LootOverrides(BaseModel):
    """The minimums one run was told to use in place of the configured ones.

    None and 0 mean different things here, and the difference is the whole
    point: an omitted flag leaves the configured threshold alone, while a flag
    passed as zero deliberately takes that threshold out, which is how a run
    being studied gets back to attacking the first opponent it is shown.
    """

    model_config = ConfigDict(frozen=True)

    min_gold: int | None = None
    min_elixir: int | None = None
    min_dark: int | None = None

    def over(self, base: LootThresholds) -> LootThresholds:
        return base.model_copy(update=self.model_dump(exclude_none=True))


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


class MapEdge(BaseModel):
    """The furthest out a drop was accepted along one ray, against the model's guess.

    `reached` is None where the ray was accepted at the very first probe, which
    means the screen ran out before the map did: that ray measures the playfield
    edge, not the map, and says nothing about the diamond.
    """

    model_config = ConfigDict(frozen=True)

    degrees: float
    reached: int | None
    predicted: int


class MapSurvey(BaseModel):
    """Where the game really stopped taking drops, and what a diamond fitted to it looks like.

    `VILLAGE_GRID` was calibrated by eye against an assumed centre, which is
    exactly the sort of number that cannot be argued with from a screenshot. This
    is how to argue with it: drop troops inwards along each ray until one lands.

    The first live survey came back with every ray unmeasured, and that is a
    result rather than a failure: the game took the very first probe on all six,
    so the screen runs out before the map does and the diamond cannot be seen
    from inside it. What it does give is a lower bound, which was enough to show
    the old constant clamping drops the game would have accepted.
    """

    edges: list[MapEdge] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        measured = [edge for edge in self.edges if edge.reached is not None]
        if not measured:
            return f"{len(self.edges)} ray(s) accepted at the screen edge; the map reaches past it"
        return f"{len(measured)} of {len(self.edges)} ray(s) found the map edge -> {self.fitted}"

    @property
    def fitted(self) -> MapFrame | None:
        """The diamond whose edge best matches every ray that measured one."""
        measured = [
            (math.radians(edge.degrees), edge.reached)
            for edge in self.edges
            if edge.reached is not None
        ]
        if len(measured) < 3:
            return None
        # |dx|/half_width + |dy|/half_height == 1 on the edge, so each ray gives
        # one linear equation in 1/half_width and 1/half_height. Two unknowns and
        # more equations than that, solved by least squares in closed form.
        rows = [(abs(math.cos(a)) * r, abs(math.sin(a)) * r) for a, r in measured]
        sxx = sum(x * x for x, _ in rows)
        syy = sum(y * y for _, y in rows)
        sxy = sum(x * y for x, y in rows)
        sx = sum(x for x, _ in rows)
        sy = sum(y for _, y in rows)
        determinant = sxx * syy - sxy * sxy
        if not determinant:
            return None
        across = (sx * syy - sy * sxy) / determinant
        down = (sy * sxx - sx * sxy) / determinant
        if across <= 0 or down <= 0:
            return None
        return MapFrame(
            centre=DEFAULT_MAP_CENTRE, half_width=round(1 / across), half_height=round(1 / down)
        )


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

# What Gemini accepts for `generation_config.thinking_level`, cheapest first.
# Every call this application makes is a screen read against a fixed prompt, so
# the thinking budget buys latency rather than a better answer. It is a setting
# because a model that refuses the level, or a prompt that turns out to want the
# reasoning, should be a picker away from working rather than a release away.
ThinkingLevel = Literal["minimal", "low", "medium", "high"]
DEFAULT_THINKING_LEVEL: ThinkingLevel = "low"


class AttackTimings(BaseModel):
    """How long after the army is down each thing that waits on a clock happens.

    The abilities are keyed by hero rather than by card position: a hero being
    upgraded cannot take the field, so its card is simply absent and every
    position shifts.

    Both spells are here rather than left to fall out of the code's ordering,
    which is what they used to do. Freeze was cast after the last ability, so it
    waited out the slowest hero on the field and a champion's 45 seconds put it a
    minute and a half into a three-minute battle. Rage was cast the moment the
    troop cards emptied, which was fine while emptying them took half a minute
    and is not now that it takes five seconds: a rage lasts 18 seconds, and cast
    as the troops land it has expired before they reach anything worth raging.

    They are also measured from a different moment than the abilities. An
    ability's delay runs from its own hero landing; a spell's runs from the
    attack opening, which is how both are judged on screen — rage as the push
    reaches the outer wall, freeze as it reaches the first line of defences —
    and hanging them off the heroes would move them by however long the army
    happened to take to go down.

    Read them as the earliest moment rather than the exact one: the loop is
    single threaded and nothing on the clock runs until the last hero is down,
    so a delay shorter than the deployment takes is served the moment it ends.
    """

    model_config = ConfigDict(frozen=True)

    king: int = 20
    queen: int = 1
    warden: int = 30
    champion: int = 45
    minion_prince: int = 20
    unknown: int = 20
    rage: int = 15
    freeze: int = 30

    def seconds(self, kind: HeroKind) -> int:
        return int(getattr(self, kind, self.unknown))


class AppConfig(BaseModel):
    """Everything a run needs that the window and the terminal both read.

    These used to live in QSettings, which only the window can reach, so a run
    started from a terminal got the field defaults instead — all three loot
    thresholds at zero, which is "attack the first opponent shown". That was
    written down as deliberate and was, for a loop being studied; it stopped
    being deliberate the moment the same command became how the game is played.
    The model picker had the same split, quietly sending every headless call to
    `DEFAULT_GEMINI_MODEL` whatever the settings tab said.

    What is here is what both sides use. Which checkboxes are ticked and whether
    the live preview is on stay in QSettings, because no terminal command asks.
    """

    # The defaults are the ones the window has always shown, not the field
    # defaults underneath them. `LootThresholds()` means "take anything" and
    # `StockLimits()` means "never stop", which are the right neutral values for
    # a model and the wrong ones to farm with — and they were only ever reached
    # by a caller that had no way of asking the user.
    thresholds: LootThresholds = LootThresholds(
        min_gold=500_000, min_elixir=500_000, min_dark=5_000
    )
    stock: StockLimits = StockLimits(stop_gold=15_000_000, stop_elixir=15_000_000)
    timings: AttackTimings = AttackTimings()
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_endpoint: str = ""
    gemini_thinking: ThinkingLevel = DEFAULT_THINKING_LEVEL


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
    # Required for the same reason as the two endpoints, and measured the same
    # way: with defaults these were optional in the JSON schema, and a live run
    # came back naming five rage points, no freeze point and no hero at all,
    # against a screen holding a freeze bottle and four hero cards. The loop then
    # stacked the freeze on its fallback spot and gave every hero the unknown
    # ability delay, which is a queen's cloak thrown away on every attack.
    rage_points: list[ScreenPoint]
    freeze_points: list[ScreenPoint]
    # Left to right, so each hero card can be matched to its own ability timing.
    heroes: list[HeroKind]
    # Carried on the plan so a written-out one is the whole tactic in one file,
    # rather than a set of points whose timing lives somewhere else entirely.
    # None leaves the runner on whatever the caller configured.
    timings: AttackTimings | None = None
    reason: str = ""


class AttackReport(BaseModel):
    """What one run of the attack loop did, for the automation log."""

    skipped: int = 0
    attacked: LootOffer | None = None
    message: str = ""
    # Farming has met its goal, so the automation is meant to stop rather than
    # come round again: the next pass would only read the same full storage.
    stock_full: bool = False


class AttackOptions(BaseModel):
    """What one `attack` command was told to do, beyond the shared config file.

    Everything here is about the run rather than the tactic: where to keep the
    frames, how many rounds to play, whether to record a heartbeat alongside the
    frames the loop reads. The tactic itself lives in the config file and in
    whatever plan is handed in.
    """

    frame_dir: Path | None = None
    plan_in: Path | None = None
    plan_out: Path | None = None
    minimums: LootOverrides = LootOverrides()
    # 0 keeps going until it is interrupted, which is what watching the loop play
    # needs: a tactic is judged over a run of battles rather than one.
    rounds: int = 1
    # 0 records nothing beyond the frames the loop reads for itself.
    shot_every: float = 0.0


class AttackSeries(RootModel[list[AttackReport]]):
    """Every round one `attack` command ran, in the order they ran.

    A run told to keep going answers with all of them rather than only the last:
    what a session is judged on is how the rounds differ, and a single report
    cannot say whether the army was ready three times out of five.
    """

    root: list[AttackReport] = Field(default_factory=list)


class ResourceBubble(BaseModel):
    """A collector marker on the home village, and what it is holding.

    Tapping one collects it outright: no menu opens and nothing asks. So the
    marker is both the target and the receipt, since a collected one disappears.
    """

    model_config = ConfigDict(frozen=True)

    resource: Literal["gold", "elixir", "dark"]
    point: tuple[int, int]


class CollectReport(BaseModel):
    """What one sweep of the collectors picked up.

    The amounts are what the storages actually gained rather than what the
    markers promised, because a storage already full takes none of it and a
    marker tapped twice pays once. `markers` is what was tapped, so the two
    disagreeing is the interesting case rather than an inconsistency.
    """

    markers: int = 0
    gold: int = 0
    elixir: int = 0
    dark: int = 0
    message: str = ""


class BuildCandidate(BaseModel):
    """A building the sweep found with an upgrade on offer, and what it asks for.

    Only where it is and what it costs. Nothing here names the building, because
    nothing on its menu does — and nothing downstream needs it named: what an
    idle builder is worth putting on is a judgement about price and time, and
    only one of those is on screen.
    """

    model_config = ConfigDict(frozen=True)

    point: tuple[int, int]
    resource: Literal["gold", "elixir"]
    price: int


class BuildReport(BaseModel):
    """What one `upgrade` run put the idle builders on."""

    started: list[BuildCandidate] = Field(default_factory=list)
    message: str = ""

    def paid(self, resource: str) -> int:
        return sum(job.price for job in self.started if job.resource == resource)


class DonateReport(BaseModel):
    """What one `donate` run gave away.

    `gifts` is where each tap landed rather than what it gave, because nothing on
    the panel names a troop. `offered` is how many cards stood in colour at the
    richest point, which is what a dry run has to report instead.
    """

    gifts: list[tuple[int, int]] = Field(default_factory=list)
    offered: int = 0
    message: str = ""


class UpgradeButton(BaseModel):
    """One 升級 button on a building's menu: where it is and what it asks for.

    The price is what makes it an upgrade rather than any other button carrying a
    resource icon — 收集 on a collector's menu has a gold coin on it too, and so
    does the wall ring. Nothing here says which building the menu belongs to,
    because nothing on the row does.
    """

    model_config = ConfigDict(frozen=True)

    resource: Literal["gold", "elixir"]
    point: tuple[int, int]
    price: int


class WallMenu(BaseModel):
    """The wall menu's own buttons, located by the icons the game paints on them.

    Nothing in this row sits at a fixed x. The buttons are laid out from the
    middle of the screen outwards, so the row loses one the moment a batch has no
    more walls left to add and every button after it shifts a place along. What
    is fixed is the spacing, which is why these are read off each frame.
    """

    model_config = ConfigDict(frozen=True)

    gold: tuple[int, int]
    elixir: tuple[int, int]
    # 升級更多 on a plain menu and 新增城牆 on a batch, which as far as the loop
    # is concerned is one button: both put another wall into the selection, and
    # both sit one place to the left of the gold button. That is what saves the
    # loop from having to tell the two menus apart at all.
    add: tuple[int, int]
    # What the whole selection costs — the same number in gold as in elixir, and
    # that is a wall's own signature. Every other building takes one resource, so
    # no other menu carries both icons with one price between them.
    price: int


class WallCandidate(BaseModel):
    """Somewhere on the village a tap opened a wall menu, and what it asked for.

    The price stands in for the level without anything having to read one: walls
    get dearer every level, so the cheapest candidate is the lowest wall on the
    map. It is what it cost *when it was tapped*, which on a wall left selected
    from an earlier batch is several walls' worth — the loop works the real unit
    price out for itself before spending anything.
    """

    model_config = ConfigDict(frozen=True)

    point: tuple[int, int]
    price: int


class GameDialog(BaseModel):
    """The game's own yes/no panel, which very different questions all share.

    Both buttons come back because which one to press is the caller's to decide
    and the two are not interchangeable: 升級城牆 and 確定退出遊戲嗎 are drawn as
    the same panel with 確定 in the same green in the same place, so a caller
    that reached for 確定 by reflex would sooner or later close the game.
    """

    model_config = ConfigDict(frozen=True)

    confirm: tuple[int, int]
    cancel: tuple[int, int]


class WallBatch(BaseModel):
    """A batch grown to what the village can pay for, and what it now holds.

    The menu comes back with it because growing a batch can drop a button from
    the row — the game takes 新增城牆+10 away once fewer than ten walls are left
    to add — and every button after it shifts a place along. The menu read before
    the batch grew points at the wrong buttons afterwards.
    """

    model_config = ConfigDict(frozen=True)

    menu: WallMenu
    unit: int
    count: int


class WallUpgrade(BaseModel):
    """One batch of walls that was paid for."""

    model_config = ConfigDict(frozen=True)

    unit: int
    count: int
    resource: Literal["gold", "elixir"]

    @property
    def spent(self) -> int:
        return self.unit * self.count


class WallReport(BaseModel):
    """What one `walls` run bought, and what stopped it."""

    upgrades: list[WallUpgrade] = Field(default_factory=list)
    message: str = ""

    @property
    def walls(self) -> int:
        return sum(upgrade.count for upgrade in self.upgrades)

    def paid(self, resource: str) -> int:
        return sum(u.spent for u in self.upgrades if u.resource == resource)


class WallOptions(BaseModel):
    """What one `walls` command was told to do."""

    frame_dir: Path | None = None
    # What to leave in the storages rather than spend. A run that empties them
    # leaves nothing to train an army with, which is the other half of farming.
    keep_gold: int = 0
    keep_elixir: int = 0
    # 0 keeps buying until neither storage will pay for another wall.
    rounds: int = 0
    # A wall to start from, for a run that would rather not spend the scan. The
    # scan is the part most likely to go wrong on a village this was not written
    # against, so naming a wall is how to work on everything downstream of it.
    at: tuple[int, int] | None = None


class FrameReading(BaseModel):
    """Everything the parsers make of one frame, for the `read` command.

    A screen the loop mishandled is almost always a screen it misread, and this
    is what says which of the readers disagreed with the eye. It is one model
    rather than a printout so a recorded run can be replayed through it.
    """

    scout: ScoutView | None = None
    stock: VillageStock | None = None
    army: tuple[int, int] | None = None
    wall_menu: WallMenu | None = None
    bubbles: list[ResourceBubble] = Field(default_factory=list)
    builders: tuple[int, int] | None = None
    upgrades: list[UpgradeButton] = Field(default_factory=list)
    donatable: int = 0
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
    thinking_level: ThinkingLevel = DEFAULT_THINKING_LEVEL


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


class GeminiGenerationConfig(BaseModel):
    """How hard the model is asked to think before it answers."""

    thinking_level: ThinkingLevel


class GeminiRequest(BaseModel):
    """One `interactions.create` body, built here instead of as a loose dict."""

    model: str
    input: list[GeminiTextPart | GeminiImagePart]
    response_format: GeminiResponseFormat | None = None
    generation_config: GeminiGenerationConfig | None = None

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
