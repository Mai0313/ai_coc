from __future__ import annotations

import math
from typing import Any, Literal
from pathlib import Path
from datetime import UTC, datetime

from pydantic import Field, BaseModel, RootModel, ConfigDict, AliasChoices, field_validator

from .constants import LOG_DIR, DEFAULT_ADB_HOST, ENTITY_CATEGORIES, DEFAULT_GEMINI_MODEL

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


class StorageCapacity(BaseModel):
    """How much each storage holds when it is full, read off the bar's own tooltip.

    None for a resource whose ceiling nobody could read, which is the honest
    answer in two different situations and wants the same treatment in both: the
    builder base has no dark elixir bar to tap at all, and a tooltip the game did
    not open reads as nothing rather than as a number. Either way that resource
    is left out of the comparison, because a ceiling nobody knows is one nothing
    can be judged against.
    """

    model_config = ConfigDict(frozen=True)

    gold: int | None = None
    elixir: int | None = None
    dark: int | None = None

    def full(self, stock: VillageStock, percent: int) -> list[str] | None:
        """Every storage that has a ceiling, named, once they have **all** filled past it.

        None while any of them is still short, which is what the caller keeps
        farming on, and None for a `percent` of 0, which is how a run is told to
        farm without ever standing itself down.

        **All rather than any**, which is a change from how this started. The
        first version stopped on the first storage to fill, reasoning that loot
        past a full one is thrown away on collection — true, but it prices the
        search fee against one resource when a battle brings home three. A
        village whose elixir is at the ceiling still has room for gold, and one
        more round costs the fee either way.

        **A share of the real ceiling rather than a written-down number**, which
        is what the two villages can now share one setting for. The numbers used
        to be typed in per village and went stale on their own: measured on a
        live village whose elixir cap had grown to 21.5M against a configured
        limit of 20M, the run stood down at every check with five million of gold
        room unused, and no amount of farming could ever fill it. The same day
        the builder base was configured at 2M against a real 2 450 000 elixir
        ceiling, standing its runs down 18% early.
        """
        watched = [
            (name, ceiling, held)
            for name, ceiling, held in (
                ("金幣", self.gold, stock.gold),
                ("聖水", self.elixir, stock.elixir),
                ("黑水", self.dark, stock.dark),
            )
            if ceiling
        ]
        if not percent or not watched:
            return None
        if any(held * 100 < ceiling * percent for _, ceiling, held in watched):
            return None
        return [name for name, _, _ in watched]


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


HeroKind = Literal["king", "queen", "warden", "champion", "minion_prince", "duke", "unknown"]

# What Gemini accepts for `generation_config.thinking_level`, cheapest first.
# Every call this application makes is a screen read against a fixed prompt, so
# the thinking budget buys latency rather than a better answer. It is a setting
# because a model that refuses the level, or a prompt that turns out to want the
# reasoning, should be a picker away from working rather than a release away.
ThinkingLevel = Literal["minimal", "low", "medium", "high"]
DEFAULT_THINKING_LEVEL: ThinkingLevel = "low"


class BattleRow(BaseModel):
    """Which card on the army row is which, once the row has been read.

    The classification costs a decode of the frame and every step of a tactic
    needs it, so it is done once and carried rather than asked for again per
    step. Cards are the x of their own centre, which is how everything else in
    the loop addresses them.
    """

    model_config = ConfigDict(frozen=True)

    troops: list[int]
    # Empty on an army carrying no siege machine, which is what the plan having
    # no `siege` step says. Nothing on the row can tell a machine from a hero —
    # neither carries an `xN` and both sit in the same group — and the health
    # bar cannot either, because the game draws one over a machine as readily.
    machine: list[int]
    heroes: list[int]
    rages: list[int]
    freezes: list[int]
    # How many bottles the rage cards hold between them, which is what caps the
    # points a tactic may name: `_cast` cycles back over its targets, so asking
    # for more spots than there are bottles stacks two on one patch of ground.
    rage_count: int
    # The frame the row was read off. It travels with the classification
    # because everything downstream that reads a card — `card_count` for how
    # many taps it takes, `_cast` for the same — has to read the *opening*
    # frame rather than one taken mid-battle, where the counts have moved.
    frame: bytes


class AttackStep(BaseModel):
    """One move of a tactic, played in the order it is written.

    **A tactic is a sequence, not a set of lists with clocks beside them.** It
    used to be the second: a drop line, a bag of rage points with one delay, a
    bag of freeze points with another, and a hero list with a delay each — while
    the loop imposed a fixed order on top, siege then troops then heroes then
    everything on a clock. Two costs came out of that shape and neither could be
    fixed inside it.

    The delays were measured from the attack opening, but nothing the planner
    can see says when that was: the loop's own idea of it moves with how long
    Gemini took to answer and how long the boundary took to read. Measured over
    23 recorded rounds, the gap the numbers were really about — troops down to
    rage cast — ran from 3.0 to 10.8 seconds while the planner asked for the
    same 14 every time. And the fixed order made a spell wait out the heroes,
    which a rage has no reason to: it covers the troops, and they are already
    walking.

    Written as steps, both go away. `wait` is a step like any other, counted
    from the previous move actually finishing, so nothing upstream of the first
    tap can move it. Anything can be interleaved with anything: troops, a rage,
    then the rest of the heroes is just three lines in a list.

    **Every field is required**, which is the same lesson `HeroOrder` and the
    old plan carry: a field with a default is optional in the JSON schema and
    Gemini leaves those out. So a step that does not need one says so rather
    than omitting it — `who` is `unknown` outside `hero` and `ability`, `at` is
    empty for `ability` and `wait`, and `seconds` is 0 for everything but
    `wait`.
    """

    model_config = ConfigDict(frozen=True)

    act: Literal["siege", "troops", "hero", "ability", "rage", "freeze", "wait"]
    # Which hero this is about, for `hero` and `ability`. The loop matches it
    # against the row left to right, so a kind naming no card on the row is a
    # step that gets skipped rather than one that shifts everything after it.
    who: HeroKind
    # Whatever points the act needs: one for `siege` and `hero`, the two ends of
    # the drop line for `troops`, one per bottle for `rage` and `freeze`, none
    # for `ability` and `wait`.
    at: list[ScreenPoint]
    # How long `wait` holds, from the previous move finishing rather than from
    # any absolute moment. What the loop spent getting here is already spent, so
    # counting from the opening would hand the difference to the battle.
    seconds: int = Field(ge=0, le=180)


class HeroOrder(BaseModel):
    """One hero card: who holds it, where that hero goes, and when it fires.

    All three used to live somewhere else. Which hero was a bare name on the
    plan, where they went was the middle of the drop line for every one of
    them, and when to fire was a table of per-kind constants in the settings
    file. That table was a guess made without seeing the village — the same
    thing the planner does, minus the village — so it now answers all three at
    once, per card, against the layout in front of it.

    Grouping them is what makes the second one possible at all. Heroes do
    different jobs in the same attack: one or two walk the outside clearing the
    stray buildings that pull an army off course, the rest go in behind the
    troops. A single point for the lot of them cannot express that, and a list
    of points beside a list of names would have to be kept in step by hand.
    """

    model_config = ConfigDict(frozen=True)

    kind: HeroKind
    drop: ScreenPoint
    # Seconds after **this hero lands**, not after the attack opens: that is what
    # a queen's cloak is worth timing against, and the leading card lands a whole
    # troop deployment before the rest. Read it as the earliest moment rather
    # than the exact one — the loop is single threaded and nothing on the clock
    # runs until the last hero is down, so anything shorter than the deployment
    # is served the moment it ends.
    ability_after: int = Field(ge=0, le=180)


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
    # defaults underneath them. `LootThresholds()` means "take anything", which
    # is the right neutral value for a model and the wrong one to farm with — and
    # it was only ever reached by a caller that had no way of asking the user.
    thresholds: LootThresholds = LootThresholds(
        min_gold=500_000, min_elixir=500_000, min_dark=5_000
    )
    # How full every storage has to be before a run stands itself down, as a
    # share of what that storage actually holds; 0 never stands one down.
    #
    # **One number for both villages, because it is a share rather than an
    # amount.** This used to be six: three ceilings for the home village and
    # three for the builder base, whose storages are a different size. Both sets
    # were somebody's typed-in guess and both went stale on their own — the home
    # village's cap grew past its configured 20M, and the builder base's real
    # elixir ceiling turned out to be 2 450 000 against the 2M written here.
    #
    # **The capacity is written on the bar, if you tap it**, which is what makes
    # the share enough: the game answers with 最大儲存量 in a tooltip, and
    # `storage_capacity` reads it. A percentage is also the only form of this
    # setting that means the same thing in a village at town hall 8 and at 15.
    stop_at: int = 90
    # No ability or spell timings here any more. They were a table of per-hero
    # constants a user could edit, and editing them meant guessing how long an
    # army takes to walk across a village nobody had looked at — which is the
    # planner's job, done with the village on screen. They live on `AttackPlan`
    # now, so a tactic is one document rather than points here and a clock there.
    # How many battles to fight before restarting the emulator and the game, 0
    # turning it off. MuMu drops frames after running for a while and nothing
    # short of a restart clears it — that is a property of the emulator rather
    # than of anything here, so this number is where it starts hurting on one
    # machine rather than anything this code can measure. It lives in the config
    # file because that is the only place a number nobody can measure belongs:
    # whoever is watching the frame rate is the one who gets to change it.
    restart_every: int = 50
    # How often to nudge the screen while holding the session open. Clash of
    # Clans will not let anyone attack a village whose owner is online, so a run
    # that has finished farming is safer idling in the game than leaving it —
    # and what keeps a session alive is touch input rather than a connection.
    # Two minutes is comfortably inside the game's own idle timeout without
    # spending an input every few seconds all night; it lives here because the
    # timeout is the game's and can move under us.
    keepalive_seconds: float = 120.0
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
    # The tactic itself, in the order it is played. Required, and that is the
    # whole point: with a default it is optional in the JSON schema, and Gemini
    # leaves those out — three runs running once came back with a drop line's
    # start and no end, so the call was paid for and its most important output
    # thrown away every time. A reply that omits this now fails validation,
    # which `_plan` answers by falling back to the flat plan rather than by
    # pretending it had one.
    steps: list[AttackStep]
    reason: str = ""

    def acts(self, act: str) -> list[AttackStep]:
        """Every step of one kind, in order, which is how the loop reads the row."""
        return [step for step in self.steps if step.act == act]

    # The drop line, under the names the rest of the loop already reads it by.
    # `planned_line`, `deploy_candidates` and `_flank` all take a plan and ask
    # for these two, and they work the same whether the line was a field of its
    # own or the first `troops` step's own pair of ends — which is the whole
    # reason to spell it this way rather than rewrite three call sites that were
    # not what changed. A tactic that never deploys troops has no line, and None
    # is what those callers already answer by falling back to a named flank.
    @property
    def deploy_start(self) -> ScreenPoint | None:
        return self._line[0] if self._line else None

    @property
    def deploy_end(self) -> ScreenPoint | None:
        return self._line[1] if self._line else None

    @property
    def _line(self) -> list[ScreenPoint] | None:
        drops = self.acts("troops")
        return drops[0].at if drops and len(drops[0].at) >= 2 else None


class PlayedPlan(BaseModel):
    """One round's tactic, as one line of a run's `plans.jsonl`.

    The round number is what makes the line worth having: `run.log` and
    `result.json` both count rounds the same way, so a line here pins the
    coordinates the planner drew to the round whose outcome is recorded there.
    Without it a series of identical flat plans says nothing at all.
    """

    model_config = ConfigDict(frozen=True)

    round: int
    # Either village's tactic. They are different documents — the builder base
    # has no spells and no ability clock — and the round number beside them is
    # what says which one a line belongs to, since `result.json` records the
    # village per round.
    plan: AttackPlan | NightPlan


class NightPlan(BaseModel):
    """How to attack one builder base opponent: a drop line and where each hero goes.

    Deliberately not an `AttackPlan` with the spell fields left empty. The
    builder base has no spells at all, so `rage_points`, `freeze_points` and
    both of their clocks would be fields nothing could ever fill — and a
    required field a planner cannot answer is how a call gets thrown away.

    It carries no ability timing either, and that is the mode's own rule rather
    than an omission: the builder base's machine recharges its ability for the
    whole battle instead of firing once, so there is no moment to name. The loop
    offers it the tap on every pass and the game takes the ones that are ready.
    """

    deploy_from: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = "top_left"
    # Required for the reason `AttackPlan`'s are: with a default they are
    # optional in the JSON schema, and a planner that leaves one out has drawn
    # half a line, which is no line.
    deploy_start: ScreenPoint
    deploy_end: ScreenPoint
    # Left to right, matching the cards on the row that carry no `xN` — the
    # machine, and the copter beside it once the base is high enough for one.
    hero_points: list[ScreenPoint]
    # How long after the machine lands the troops follow it, in seconds.
    #
    # **The machine goes first here, where the home village's heroes go last**,
    # and this is the number that says how much of a head start it gets. It is
    # the one clock on this plan, and it is on the plan rather than in the code
    # for the reason every other clock moved out of the settings file: what it
    # is really asking is how far the machine has to walk before the troops are
    # worth committing, which is a property of the base in the frame and of
    # nothing else. Required, so a planner cannot quietly leave it out and have
    # the loop fall back to a number nobody chose.
    troops_after: int = Field(ge=0, le=60)
    reason: str = ""


class AttackReport(BaseModel):
    """What one run of the attack loop did, for the automation log."""

    # Which village it played, because the two are different games under one
    # command and a report that does not say is a report nobody can place.
    world: World = "day"
    # How many times the army went down. More than one is the builder base's
    # second stage, which the game only offers after a first attack takes the
    # whole base; 0 is a round that never deployed.
    phases: int = 0
    skipped: int = 0
    # The opponent that was fought, as its scout screen advertised it — what was
    # **on offer**, not what came home. None means no battle was fought at all,
    # which is the one thing to test a round on. What actually landed is the
    # difference between two `read_stock` readings and nothing else: a full
    # storage takes none of what it is handed, and the star bonus pays on top.
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
    # The run's own plan log, one line per round. `plan_out` is the caller's
    # path and holds whichever round went last; this is the whole series.
    # None only for a caller with no run directory to write into.
    plan_log: Path | None = None
    # Which village to play. **None means whichever one is up**, which is the
    # default because it is the honest one: the game reopens on the village it
    # was closed on, so a run that insisted on a village would refuse half the
    # time for no reason. Naming one crosses to it first, and that is what a
    # scripted night of farming both wants — otherwise a game left on the
    # builder base has the whole series quietly playing the wrong one.
    world: World | None = None
    minimums: LootOverrides = LootOverrides()
    # 0 keeps going until it is interrupted, which is what watching the loop play
    # needs: a tactic is judged over a run of battles rather than one.
    rounds: int = 1
    # 0 records nothing beyond the frames the loop reads for itself.
    shot_every: float = 0.0
    # Overrides `AppConfig.restart_every` for one run, and `None` is not `0` for
    # the reason the loot overrides are not: omitting the flag keeps whatever the
    # config file says, while passing zero turns the restart off for this run
    # alone. That is how a run being watched gets to skip it.
    #
    # **Battles, not rounds**, wherever the number comes from. A round that found
    # no opponent, or backed out on a half-trained army, barely touched the
    # emulator — counting those would spend a restart on a run that has mostly
    # been waiting for barracks, and worse, the rounds a restart itself costs
    # would feed back and make it restart more often still.
    restart_every: int | None = None


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


class BuildQueue(BaseModel):
    """What the builder panel shows running, read off the progress bars.

    `running` counts the bars and `remaining` carries only the times that read,
    so a row whose countdown could not be made out is visible as the two
    disagreeing rather than as a queue quietly one short.
    """

    model_config = ConfigDict(frozen=True)

    running: int = 0
    # Seconds, soonest first, which is the order the question is asked in: the
    # first of them is when the next builder comes free.
    remaining: list[int] = Field(default_factory=list)


class BuilderReport(BaseModel):
    """Who is busy and for how long, for a run deciding whether to wait."""

    free: int = 0
    total: int = 0
    queue: BuildQueue = BuildQueue()
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


class ViewReport(BaseModel):
    """What one `view` command asked of the camera.

    There is nothing to read back. The game exposes no zoom level and the scale
    is not written anywhere on screen, so this says what was sent rather than
    what the camera ended up at — and a zoom the camera was already at is a
    no-op rather than an error.
    """

    model_config = ConfigDict(frozen=True)

    message: str = ""


class RunLog(BaseModel):
    """One execution's own directory: its log, its answer, and what it saw.

    This answers "what did *that* run do", which is the question anyone looking
    into a bad round actually has. A rotating `controller.log` used to sit
    beside it answering "what has this machine been doing"; it was removed
    because the directory names answer that already — they are `<when>-<what>`,
    so a listing is the history — and because `grep -r` across the run logs
    finds a pattern over a week while naming which run each hit came from,
    which a merged file cannot.

    It exists because the layout was a convention rather than a feature. The
    skills told a session to build this directory with shell redirection, so it
    existed only for the reader who could have built it anyway — a person
    running the CLI by hand, or using the window, got a rotating file and
    nothing else.

    `recording` is off by default because it is not free: the frames a loop
    reads are a PNG encode each on the emulator, and the heartbeat competes with
    the loop for the same ADB connection.
    """

    model_config = ConfigDict(frozen=True)

    directory: Path
    recording: bool = False

    @classmethod
    def open(cls, command: str, recording: bool = False) -> RunLog:
        """Make the directory a run about to start will write into.

        Named `<when>-<what>` so that a plain listing of the log directory reads
        as a history, which is what makes a run from three days ago findable
        without opening any of them.
        """
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        base = LOG_DIR / f"{stamp}-{command}"
        # Two runs of the same command inside one second would otherwise land in
        # one directory, one `result.json` overwriting the other and both logs
        # interleaved. A shell loop over `ai_coc read` is how that really
        # happens, and it is the documented way to answer a misread question.
        directory = base
        attempt = 2
        while directory.exists():
            directory = base.with_name(f"{base.name}-{attempt}")
            attempt += 1
        directory.mkdir(parents=True)
        return cls(directory=directory, recording=recording)

    @property
    def log_path(self) -> Path:
        return self.directory / "run.log"

    @property
    def frames(self) -> Path | None:
        """Where a loop keeps what it reads, or None when nothing is recorded.

        None rather than a directory left empty, because every loop here already
        takes `frame_dir: Path | None` and reads None as "do not save".
        """
        if not self.recording:
            return None
        path = self.directory / "frames"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def plan_log(self) -> Path:
        """One line per round, holding the tactic that round played.

        Unconditional where `frames` is not, because the two cost nothing alike:
        a plan is a few hundred bytes of JSON written once a battle, against a
        PNG encode on the emulator for every frame a loop reads.

        It is the only record of what the planner actually answered. `run.log`
        carries a one-line summary and `--plan-out` keeps the last round alone,
        overwriting every earlier one, so a series had no way to say which round
        drew which line — and the loop's own frames cannot answer it either,
        since a drop is over in a fraction of the gap between two captures.

        **One file rather than one per round.** `--repeat 0` runs until it is
        stopped, so a night of farming is a hundred rounds or more, and a
        hundred small files is a directory nobody opens. A line carries its own
        round number, so it lines up with `run.log` and `result.json`, and the
        whole series greps and diffs as one document. Pulling a single round
        back out for `--plan-in` is a line of `jq`.
        """
        return self.directory / "plans.jsonl"

    def answer(self, text: str) -> None:
        """Keep a run's result beside the log that explains how it got there."""
        (self.directory / "result.json").write_text(text, encoding="utf-8")


# How much to tear down before bringing the game back up. `none` is the ordinary
# case and the cheapest: `ensure_coc` starts whatever is not already running, so
# a village that is already up costs a couple of seconds. The other two are for
# a game or an emulator that is up but no longer answering, which nothing below
# can detect — only the person watching it can.
RestartScope = Literal["none", "game", "emulator"]


# The game's two villages. Supercell calls them the Home Village and the Builder
# Base; this project calls them day and night because that is what the player
# calls them, and because `builder` already means the workman here — `ai_coc
# builders` reads the panel saying which of the five are free, and a
# `--world builder` standing next to it would be read as belonging to that.
World = Literal["day", "night"]


class Crossing(BaseModel):
    """How to reach one village's boat, and where it sits once the camera stops.

    `start` and `drift` are one drag of the map, repeated until the camera is
    clamped at the corner the boat is moored in; `spots` are the places to tap
    for it from that view, tried in turn. There is more than one because nothing
    recognises the boat — it is a sprite the game dresses up for events — so a
    miss and a hit look the same until the world is read again.
    """

    model_config = ConfigDict(frozen=True)

    start: tuple[int, int]
    drift: tuple[int, int]
    spots: tuple[tuple[int, int], ...]


class WorldReport(BaseModel):
    """Which village one `world` command found, and which one it left the game on.

    `found` and `world` differ only when the command was asked to cross, which is
    the whole answer to "did anything happen": a command that was already where
    it was asked for taps nothing at all.

    Both are optional because "no village is on screen" is a real answer here
    rather than an error, and it is a different one from either village.
    """

    model_config = ConfigDict(frozen=True)

    found: World | None
    world: World | None
    crossed: bool = False
    message: str = ""


class LaunchReport(BaseModel):
    """Which instance one `launch` command left the game running on.

    There is deliberately no "is it running now" field: `ensure_coc` either
    returns an instance with the game up or raises, so such a field could only
    ever read True and would say nothing. `was_running` is the answer that is
    actually worth having — whether this command found the game already up or
    had to bring it there, which is the difference between a cold machine and
    a wasted call.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    serial: str
    was_running: bool
    # Whether the village really came up and the camera was put back at the far
    # zoom, which is a different question from whether the process is running:
    # `ensure_coc` is satisfied by a pid, and a game still on its loading screen
    # answers every command by silently missing whatever it aimed at.
    at_village: bool = False
    message: str = ""


class OnlineReport(BaseModel):
    """What one `online` command did while it held the session open.

    The count is what separates a run that idled all night from one that fell
    over on its first nudge, since both end the same way — quietly, on a stop.
    """

    nudges: int = 0
    seconds: float = 0.0
    message: str = ""


class HeroCard(BaseModel):
    """One card on the 英雄殿堂 screen: who is on it, and what raising him costs.

    `price` and `resource` are read off the same button, so they are both set or
    both absent. A card without them is a hero the screen is showing but not
    offering — one already being upgraded has a countdown where its button was —
    which is a different thing from a card that is not on screen at all, and the
    two are told apart because only the second is missing from the list.

    A banner whose colour matches no hero never reaches here at all: the reader
    drops it, because far more often than a hero the game has just added it is
    some other screen with coloured plates on it. What that costs is a hero
    added by an update reading as absent, which is a run that says it cannot
    find him — and what it buys is a run that does not mistake the 我的軍隊
    screen for the hall.
    """

    model_config = ConfigDict(frozen=True)

    hero: HeroKind
    point: tuple[int, int]
    price: int | None = None
    resource: Literal["elixir", "dark"] | None = None
    # Whether that button can be pressed, which is not what a price says. A hero
    # the Hero Hall's own level has capped keeps its price and loses its button:
    # the plate greys, the number stays, and a tap is answered with a red line
    # naming the hall level it wants first — Chinese, which nothing here reads.
    # So a run that went by the price alone reported "no confirmation sheet came
    # up", which reads as a swallowed tap rather than a button never live.
    upgradable: bool = False


class HeroReport(BaseModel):
    """What one `hero` run saw in the hall, and which upgrade it started.

    The cards are reported whether or not anything was started, because that is
    the read-only half of the command and the half worth running on its own:
    what each hero costs next is the number a player decides against.
    """

    cards: list[HeroCard] = Field(default_factory=list)
    started: HeroCard | None = None
    message: str = ""


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


class WallSpots(BaseModel):
    """Where Gemini thinks the walls are, before anything has been tapped.

    These are guesses and are treated as such: every one of them is opened and
    priced off its own menu before it can be spent on, which is the same check a
    hand-named wall goes through. So a wrong point costs one tap and one capture
    and is dropped with a line saying so, and asking for more points than the run
    needs is the right shape rather than a waste.
    """

    spots: list[ScreenPoint] = Field(default_factory=list)


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
    # The walls this run was pointed at, instead of scanning for them. Empty
    # means scan.
    #
    # **A list rather than one, because one is not a choice.** `_pick` exists to
    # take the cheapest wall found, which is the lowest level and so the one
    # worth the loot; naming a single wall collapses that to whatever was named.
    # Measured live, a run given one coordinate paid 9 000 000 for a wall while
    # 4 000 000 ones stood in the same village.
    #
    # Which is also the division of labour worth keeping: finding walls is what
    # the sweep is worst at — it taps a grid and hopes — and what a pair of eyes
    # on a screenshot does in one look. Reading prices, batching and buying
    # without ever reaching a gem button is the other way round.
    at: list[tuple[int, int]] = Field(default_factory=list)


class FrameReading(BaseModel):
    """Everything the parsers make of one frame, for the `read` command.

    A screen the loop mishandled is almost always a screen it misread, and this
    is what says which of the readers disagreed with the eye. It is one model
    rather than a printout so a recorded run can be replayed through it.
    """

    # Which village the frame was taken on, and the reason it leads: every other
    # field below is read against one of the two, and `stock` in particular
    # answers on both while meaning something different on each.
    world: World | None = None
    scout: ScoutView | None = None
    stock: VillageStock | None = None
    # What each bar's tooltip says it holds when full, which is empty on a frame
    # with no tooltip open — meaning every frame but the ones taken during a
    # ceiling read, since opening one takes a tap.
    capacity: StorageCapacity = StorageCapacity()
    army: tuple[int, int] | None = None
    wall_menu: WallMenu | None = None
    bubbles: list[ResourceBubble] = Field(default_factory=list)
    heroes: list[HeroCard] = Field(default_factory=list)
    builders: tuple[int, int] | None = None
    queue: BuildQueue | None = None
    upgrades: list[UpgradeButton] = Field(default_factory=list)
    donatable: int = 0
    attack_menu: bool = False
    # Whether 下一個 is up, which `scout` cannot answer where it matters: it is
    # None both for 正在搜尋對手 and for an opponent whose loot will not read,
    # and this is what tells a run debugging the second one which it is looking at.
    skip_offered: bool = False
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
