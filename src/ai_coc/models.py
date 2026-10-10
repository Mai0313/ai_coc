from __future__ import annotations

import re
import math
import shutil
from typing import Any, Literal
from pathlib import Path
from datetime import UTC, datetime, timedelta

from pydantic import (
    Field,
    BaseModel,
    RootModel,
    ConfigDict,
    AliasChoices,
    computed_field,
    field_validator,
)

from .constants import (
    LOG_DIR,
    DEFAULT_ADB_HOST,
    DEFAULT_LITE_MODEL,
    DEFAULT_GEMINI_MODEL,
    FRAME_RETENTION_DAYS,
)

# Village JSON and the MuMu CLI both gain fields between game and emulator
# releases. Models that mirror them allow extras so an unknown field is
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
        """`host:port`, or adb's own `emulator-<console port>`, whose ADB port is the next one up.

        So LDPlayer's `emulator-5554` and `127.0.0.1:5555` are the same instance,
        which is how the adb server lists them side by side.
        """
        serial = serial.strip()
        if serial.startswith("emulator-") and serial[9:].isdigit():
            return cls(port=int(serial[9:]) + 1)
        host, _, port = serial.rpartition(":")
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


class TouchNode(BaseModel):
    """What a gesture written straight to one multi-touch input node has to match.

    `swapped` is whether the node's x range is the screen's short side, which
    MuMu's is and LDPlayer's is not. `pressure` is whether the node reports
    `ABS_MT_PRESSURE` with any range: Android then reads a finger that never set
    it as zero pressure, a hover rather than a touch, and the game ignores it —
    observed on LDPlayer (0 to 2), a tap written to the node that did nothing
    until it carried one. MuMu's node lists the axis with a range of 0 to 0 and
    takes a finger without it, so a range of nothing counts as no axis.
    """

    model_config = ConfigDict(frozen=True)

    swapped: bool
    pressure: bool


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


class LDPlayerInstanceInfo(BaseModel):
    """One line of `ldconsole list2`.

    Positional and comma-separated: index, title, top window, render window,
    where Android is, the player's pid (-1 when down), the VM's pid, then width,
    height and dpi. Android is 0 while the instance is down, 2 while it boots
    and 1 once it is up: watched through a reboot, 0 for two seconds, 2 for ten,
    then 1.
    """

    index: int
    title: str
    top_hwnd: int
    bind_hwnd: int
    android: int
    pid: int
    vbox_pid: int
    width: int
    height: int
    dpi: int

    @classmethod
    def parse(cls, line: str) -> LDPlayerInstanceInfo:
        # The title is the one field a user types, so a comma in it is taken
        # back into the title rather than shifting every field after it.
        index, *title, top, bind, android, pid, vbox, width, height, dpi = line.split(",")
        return cls.model_validate({
            "index": index,
            "title": ",".join(title),
            "top_hwnd": top,
            "bind_hwnd": bind,
            "android": android,
            "pid": pid,
            "vbox_pid": vbox,
            "width": width,
            "height": height,
            "dpi": dpi,
        })

    @property
    def android_started(self) -> bool:
        return self.android == 1


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
    # Seconds still to run at normal speed, on a row being upgraded; the game
    # writes it on those rows alone, and so does the file this is saved back to.
    timer: int | None = Field(default=None, exclude_if=lambda value: value is None)

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


def _absent(value: int | None) -> bool:
    return value is None


class Boosts(BaseModel):
    """The export's `boosts`: seconds left on each speed-up that is running.

    A key is there only while its speed-up runs, so a missing one is a potion
    not drunk rather than a zero, and the file keeps writing them that way.
    `clocktower_cooldown` is the one that is not a speed-up: it is how long
    until the builder base's clock tower can be started again. Keys the game
    adds later survive as extras.
    """

    model_config = TOLERANT

    builder_boost: int | None = Field(default=None, exclude_if=_absent)
    lab_boost: int | None = Field(default=None, exclude_if=_absent)
    pet_boost: int | None = Field(default=None, exclude_if=_absent)
    clocktower_boost: int | None = Field(default=None, exclude_if=_absent)
    clocktower_cooldown: int | None = Field(default=None, exclude_if=_absent)


class VillageDocument(BaseModel):
    """A Village JSON export; sections the game adds later survive as extras."""

    model_config = TOLERANT

    tag: str = Field(default="UNKNOWN", validation_alias=AliasChoices("tag", "player_tag"))
    # Declared rather than left in the extras because both are read: the game's
    # own export time is what says a payload is this export and not the last
    # one, and the boosts are the only counters that belong to no section.
    timestamp: int | None = None
    boosts: Boosts = Field(default_factory=Boosts)

    @field_validator("tag", mode="before")
    @classmethod
    def _clean_tag(cls, value: Any) -> str:  # noqa: ANN401 - arbitrary JSON value
        return str(value or "").strip() or "UNKNOWN"

    def sections(self) -> list[str]:
        """Every key holding a list of entities, which is what a section is."""
        return [key for key, value in (self.model_extra or {}).items() if isinstance(value, list)]

    def entries(self, section: str) -> list[VillageEntity]:
        """Rows of one section; a row without a usable data_id is skipped, not fatal.

        A row is usually an object, but `skins` and `house_parts` are written as
        bare data_ids — so a plain number is one too, rather than something to
        skip.
        """
        values = (self.model_extra or {}).get(section)
        if not isinstance(values, list):
            return []
        entities: list[VillageEntity] = []
        for item in values:
            row = {"data": item} if isinstance(item, int) else item
            if not isinstance(row, dict):
                continue
            try:
                entities.append(VillageEntity.model_validate({**row, "section": section}))
            except ValueError:
                continue
        return entities


class AccountSnapshot(BaseModel):
    tag: str
    raw: VillageDocument
    entities: list[VillageEntity] = Field(default_factory=list)


class NamedEntity(VillageEntity):
    """One entity with whatever the community mapping calls it.

    None rather than the number when the mapping has no entry, so the file stays
    honest about which names are real: measured on one real export, 65 of 226
    have no name — 39 hero equipment, 7 house parts, 6 skins, 8 obstacles, 4
    decorations and 1 helper, none of which the community gist covers. Writing
    the data_id into `name` would hide which ones to revisit once the mapping
    catches up, and the id is right there in its own field anyway.
    """

    name: str | None = None


# How one `export` ended. `read_back` is `--last`, off the disk without touching
# the emulator, and it is a success rather than a lesser one. The four in the
# middle are the walk through the settings menus, one per step, because a menu
# that did not open and a copy the clipboard never received send whoever is
# reading to different places.
ExportOutcome = Literal[
    "exported",
    "read_back",
    "never_exported",
    "wrong_format",
    "no_village",
    "settings_shut",
    "more_settings_shut",
    "row_not_found",
    "nothing_copied",
    "not_a_village",
]


class VillageExport(BaseModel):
    """What `ai_coc export` answers with, and what it writes to `account_json/`.

    The game's own payload is not kept. Everything in it is here — every
    section, every field of every row through `VillageEntity`'s tolerance, the
    export time and the boosts — so this is the same data named rather than a
    subset of it.
    """

    tag: str
    # The game's own export time, which is what says a clipboard payload is this
    # export rather than the one still sitting there from last time. ISO for
    # reading, the raw counter for comparing.
    exported_at: str
    timestamp: int | None = None
    entities: list[NamedEntity] = Field(default_factory=list)
    boosts: Boosts = Field(default_factory=Boosts)
    # Required, and an export written before this field existed therefore reads
    # back as `wrong_format` rather than as one that cannot say how it ended.
    # Re-running the command is a few taps, which is cheaper than a file that
    # looks current and is not.
    outcome: ExportOutcome


class EntityMapping(RootModel[dict[str, dict[int, str]]]):
    """The community data_id → name table, keyed by group such as `th_buildings`."""

    def names(self) -> dict[int, str]:
        """Every data_id the table has a name for, flattened across its groups.

        The groups only separate the home village from the builder base, which
        is not a question anything asks: the one caller wants a name for an id.
        A few ids appear in two groups — the heroes are also under
        `th_buildings` — with the same name in each, so flattening loses
        nothing.

        This used to build a `RegistryEntry` per row, carrying a world, a
        category derived from the data_id block, a source URL and a
        verification status. All four were read by the `id_registry` table and
        the join it fed, and both went with the database.
        """
        return {
            data_id: name for entries in self.root.values() for data_id, name in entries.items()
        }


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


# How a survey ended. Both measure the home village's battlefield and nothing
# here sails, so `builder_base` is the game standing on the other village and
# nothing measured at all — which an empty list of rays could not say apart
# from a survey that ran and found nothing.
SurveyOutcome = Literal["surveyed", "builder_base"]


class BoundarySurvey(BaseModel):
    """A whole survey, and how much of it the boundary reader got right."""

    rays: list[ProbeRay] = Field(default_factory=list)
    unread: list[float] = Field(default_factory=list)
    outcome: SurveyOutcome

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

    `DEPLOY_BOUND` was calibrated by eye against an assumed centre, which is
    exactly the sort of number that cannot be argued with from a screenshot. This
    is how to argue with it: drop troops inwards along each ray until one lands.

    The first live survey came back with every ray unmeasured, and that is a
    result rather than a failure: the game took the very first probe on all six,
    so the screen runs out before the map does and the diamond cannot be seen
    from inside it. What it does give is a lower bound, which was enough to show
    the old constant clamping drops the game would have accepted.
    """

    edges: list[MapEdge] = Field(default_factory=list)
    outcome: SurveyOutcome

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
    """A spot on the battle screen, as the percentages every model answers in."""

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
    # The same count for the freeze cards, and it is a count of bottles rather
    # than of cards for the reason `rage_count` is. Slicing the freeze points to
    # the number of *cards* is what the loop used to do, and one card holds
    # three bottles: the planner was asked for three points, two were thrown
    # away, and `_cast` then put all three bottles on the one that survived —
    # which is one spell's worth of effect for the whole cargo. Measured over a
    # day of recorded farming, `held 3, tapped 4` on 65 rounds against a tactic
    # line reading `freeze x3` on every one of them.
    freeze_count: int
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

    **Every field is required**, which is the lesson the old plan's optional
    fields taught: a field with a default is optional in the JSON schema and
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


class GeminiSetting(BaseModel):
    """Which model to send one kind of call to, and how to reach it."""

    model: str = DEFAULT_GEMINI_MODEL
    base_url: str = ""
    thinking_level: ThinkingLevel = DEFAULT_THINKING_LEVEL


class GeminiSettings(BaseModel):
    """The tiers this application sends work to, by what the work is.

    Two rather than one because the two call patterns are different, which is
    the only thing that justifies a second model at all. `main` answers once per
    run against a whole screenshot — the attack plan, the target finder — so
    nothing there is racing anything and the better model is simply the right
    one. `lite` answers **once per candidate** on a cropped strip, which is where
    a cheaper model earns its place.

    Named for the role rather than for the product: a key called `flash` holding
    `gemini-4-pro` is a lie the file keeps telling, and this project already has
    a scar from a settings key that outlived what it configured.
    """

    # One key for both tiers, in plain text, because an OS key store tied the
    # app to one OS.
    api_key: str = ""
    main: GeminiSetting = GeminiSetting()
    lite: GeminiSetting = GeminiSetting(model=DEFAULT_LITE_MODEL)


class UiJobs(BaseModel):
    """Which of the five commands the window's cycle round-robins.

    The field names are the sub-command names, deliberately: each one is a
    `commands.*` function that `cli.py` dispatches to and that the window's own
    `run_*` calls, so the file says what a pass will do in the same words a
    terminal would. The registry's names — `auto_collect` and its four siblings
    — said nothing the checkbox label did not, and matched nothing.
    """

    collect: bool = False
    donate: bool = False
    upgrade: bool = False
    walls: bool = False
    attack: bool = False


class UiSettings(BaseModel):
    """What only the window reads, in the file both sides already share.

    These lived in `QSettings`, which is the Windows registry, on the rule that
    a setting no terminal command asks for has no business in the shared file.
    What that rule actually bought was a second place for settings to live, one
    no editor opens and nothing outside the window can read: knowing which jobs
    the window would run meant opening `regedit`. Being read by one side is not
    a reason to be stored somewhere only that side can reach.
    """

    jobs: UiJobs = UiJobs()
    # Only ever paces a cycle that found nothing to do: a finished job queues
    # the next pass straight away, `NEXT_CYCLE_DELAY` later.
    #
    # Bounded to what the spin box takes, because the two disagreeing is the
    # `timings` failure again: a file saying 0 is clamped to 1 by the widget,
    # run as 1, and written back as 1 — a key that looks read and is not. Out
    # of range it raises on load instead, which is what this file does with a
    # value it cannot honour.
    cycle_minutes: int = Field(default=10, ge=1, le=120)
    live_view: bool = True
    # Off by default because it is not free — a PNG encode on the emulator for
    # every frame a loop reads, and a `frames/` under every run to hold them.
    record_frames: bool = False


class AppConfig(BaseModel):
    """Everything a run needs that the window and the terminal both read.

    These used to live in QSettings, which only the window can reach, so a run
    started from a terminal got the field defaults instead — all three loot
    thresholds at zero, which is "attack the first opponent shown". That was
    written down as deliberate and was, for a loop being studied; it stopped
    being deliberate the moment the same command became how the game is played.
    The model picker had the same split, quietly sending every headless call to
    `DEFAULT_GEMINI_MODEL` whatever the settings tab said.

    What is here is everything either side reads. The five automation
    checkboxes, the two preview switches and the retry interval were the last
    thing left in the registry, kept there on the rule that a setting no
    terminal command asks for does not belong in a shared file — which is how
    they ended up somewhere no editor opens.
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
    # Which emulator instance every command drives, by its ADB serial:
    # `127.0.0.1:16384` or `emulator-5554`, whichever form `adb devices` shows.
    # Empty until the first run, which takes the instance running the game —
    # MuMu's first where both are — and writes its serial here; the window's
    # emulator picker writes it as well. A serial rather than an emulator name
    # and an index, because it is what `adb devices` shows and what the picker
    # lists, and each emulator fixes its instances' ports by index anyway.
    adb_serial: str = ""
    # Nested rather than three more flat keys, and it earns that twice over. At
    # the top level `model` would sit beside `adb_serial` with nothing saying
    # which subsystem it belongs to, and that only gets worse as tiers are added.
    # More usefully, a tier *is* a `GeminiSetting`, so building a client stopped
    # being four lines of copying one field name onto another.
    gemini: GeminiSettings = GeminiSettings()
    # Nested for the reason `gemini` is, and for one more: a block says which
    # keys are the window's without a prefix on each of them, so the five job
    # names underneath can be the sub-command names and nothing else.
    ui: UiSettings = UiSettings()


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


# How one round of the attack loop ended, which is the field a farming session
# actually decides on and was a sentence for the whole of this project's life.
#
# **Five of these are one battle that was fought, told apart by what came of
# it**, and the difference is where the next reader goes: `took_loot` is the
# ordinary win; `loot_unread` means nothing ever resolved the loot panel on a
# village that has one, so the round cannot be judged; `no_loot` means the army
# went down and came home empty, which is the tactic's problem; and
# `nothing_deployed` means not one card left the row, which is the loop's.
# Reporting the last two as one sentence sent readers to the wrong half more
# than once.
#
# **`deployed` is the builder base's own success and says less on purpose.**
# Nothing on that village reads loot at all — its elixir goes into a cart rather
# than the storages, and the box the home village's panel is read from is
# battlefield there — so what a night round can honestly claim is that troops
# went out. Sharing `took_loot` would put two meanings under one name on the two
# villages, which is the whole thing this replaced.
#
# The rest never reached a battle. `army_short` and `stock_full` are the two
# deliberate stand-downs, both before the search fee, and both end the whole
# series rather than the round: a storage does not empty itself while a run is
# going, and training is instant in the current game, so an army under the
# threshold is a composition that cannot reach it. `builder_free` and
# `lab_free` are two more, only on a run asked to watch for them: an idle
# builder or research slot is what the player wanted to hear about, and nothing
# a battle wins changes it. `no_opponent` is a
# matchmaker or a scout screen that never produced one, `all_skipped` is every
# candidate under the thresholds, and `stopped` is somebody asking for the run
# to end — including during the server wait. `no_attack_menu` is a screen the
# round could not open the menu on; `server_loading` is that screen outlasting
# the whole 45-minute wait and `server_flapping` is it loading and dropping
# back. `emulator_silent` is ADB not answering, and `other_village` is the game
# standing on the village this series does not play, which ends it: nothing here
# sails, and `ai_coc world --go` is how the game gets moved. `session_taken` is
# another device logging in to the account, between rounds or mid-battle, which
# ends the series too: the player is on, and logging back in would log them out.
AttackOutcome = Literal[
    "took_loot",
    "deployed",
    "loot_unread",
    "no_loot",
    "nothing_deployed",
    "army_short",
    "stock_full",
    "builder_free",
    "lab_free",
    "no_opponent",
    "all_skipped",
    "stopped",
    "no_attack_menu",
    "server_loading",
    "server_flapping",
    "emulator_silent",
    "other_village",
    "session_taken",
]


class AttackReport(BaseModel):
    """What one run of the attack loop did, for the automation log."""

    # Which village it played, because the two are different games under one
    # command and a report that does not say is a report nobody can place. None
    # is a round that ended before it read either.
    world: World | None = "day"
    # How many times the army went down, **on the builder base**. More than one
    # is its second stage, which the game only offers after a first attack takes
    # the whole base, and 0 there is a round that never deployed.
    #
    # **The home village leaves it at 0 whatever happened**, which is worth
    # saying because the field reads as if it counted both: there `attacked`
    # answers the same question and answers it better, since it carries what the
    # opponent was advertising rather than only that there was one. So a caller
    # asking "did this round really fight" has to ask both ways, which is what
    # `commands.attack` does before it decides whether to rest between rounds.
    phases: int = 0
    skipped: int = 0
    # The opponent that was fought, as its scout screen advertised it — what was
    # **on offer**, not what came home. None means no battle was fought at all,
    # which is the one thing to test a round on. What actually landed is the
    # difference between two `read_stock` readings and nothing else: a full
    # storage takes none of what it is handed, and the star bonus pays on top.
    attacked: LootOffer | None = None
    outcome: AttackOutcome
    # Whether the scout countdown forced this battle rather than the loot
    # clearing the thresholds, which is only meaningful on a round that fought.
    # It is its own field rather than two more outcomes because it is orthogonal
    # to how the battle went: a forced opponent can take loot or deploy nothing
    # exactly as a chosen one can, and `can_skip` — the reading it comes from —
    # is not on the report.
    forced: bool = False

    @computed_field
    @property
    def stock_full(self) -> bool:
        """Farming has met its goal, so the automation is meant to stop.

        **Derived rather than stored**, because it was the one thing the old
        sentence said that a field also said, and two places to look is how they
        drift. Kept under its own name because `farm` owns where it leads and
        both that skill and the window read it — and computed rather than plain
        so `result.json` carries it exactly as it did.
        """
        return self.outcome == "stock_full"


# The plates `attack --until-idle` can be told to watch. The shield plate
# counts nothing, so it is not one of them.
IdleRole = Literal["builder", "lab"]


class AttackOptions(BaseModel):
    """What one `attack` command was told to do, beyond the shared config file.

    Everything here is about the run rather than the tactic: where to keep the
    frames, how many rounds to play, whether to record a heartbeat alongside the
    frames the loop reads. The tactic itself lives in the config file and in
    whatever plan is handed in.
    """

    frame_dir: Path | None = None
    # A written plan for either village, played instead of asking the AI. It
    # names its own village, and a game on the other one ends the series.
    plan: Path | None = None
    plan_out: Path | None = None
    # The run's own plan log, one line per round. `plan_out` is the caller's
    # path and holds whichever round went last; this is the whole series.
    # None only for a caller with no run directory to write into.
    plan_log: Path | None = None
    minimums: LootOverrides = LootOverrides()
    # 0 keeps going until it is interrupted, which is what watching the loop play
    # needs: a tactic is judged over a run of battles rather than one.
    rounds: int = 1
    # 0 records nothing beyond the frames the loop reads for itself.
    shot_every: float = 0.0
    # Overrides `AppConfig.stop_at` for one run, with the `None` against `0`
    # split the loot overrides have: omitted keeps the file's percentage, zero never stands the run
    # down. Zero is what a test battle against a village the farming has just
    # filled needs — every storage is past the line, so the file's value would
    # end the series before it searched, and the code under test never runs.
    stop_at: int | None = None
    # Also stands the series down once one of these plates on this village
    # shows an idle slot, which is how a farming session waits on a builder or
    # the laboratory without a clock of its own. Empty watches neither.
    until_idle: list[IdleRole] = Field(default_factory=list)


class AttackSeries(RootModel[list[AttackReport]]):
    """Every round one `attack` command ran, in the order they ran.

    A run told to keep going answers with all of them rather than only the last:
    what a session is judged on is how the rounds differ, and a single report
    cannot say whether the army was ready three times out of five.
    """

    root: list[AttackReport] = Field(default_factory=list)


class Patch(BaseModel):
    """One connected patch of one colour, grown a row at a time by `parsers.regions`.

    Mutable, because that is what growing means: `absorb` folds a patch on the
    row above into this one, and a frozen model would have to rebuild both.

    **It was a `__slots__` class in `parsers.home`, and its own docstring gave
    the reason: "it lives for the length of one scan and never leaves this
    module".** The second half had stopped being true — `parsers.clan`
    annotated with it and `parsers.building` scanned with it — which is what
    made it the one structured value here crossing a module boundary without
    being a model.

    The other half of that reasoning was speed, and it does not survive being
    measured either. Swept over the committed fixtures the worst frame builds
    3644 of these; the whole scan on that frame costs 5.1 ms as a slotted class
    and 11.0 ms as a model, inside a `collect_bubbles` that costs 54 ms and
    behind a capture that costs 700. The 5.8 ms bought a rule with one
    unexplained exception in it.
    """

    left: int
    right: int
    top: int
    bottom: int
    count: int

    @classmethod
    def row(cls, left: int, right: int, row: int) -> Patch:
        """A patch that is so far one horizontal run on one row.

        Pydantic takes keywords only, where the slotted class this replaced was
        built positionally from three of its five fields and derived the other
        two. This is where that derivation went.
        """
        return cls(left=left, right=right, top=row, bottom=row, count=right - left + 1)

    def absorb(self, other: Patch) -> None:
        self.left = min(self.left, other.left)
        self.right = max(self.right, other.right)
        self.top = min(self.top, other.top)
        self.bottom = max(self.bottom, other.bottom)
        self.count += other.count

    @property
    def middle(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def sized(self, width: tuple[int, int], height: tuple[int, int], fill: float) -> bool:
        across, down = self.right - self.left + 1, self.bottom - self.top + 1
        return (
            width[0] <= across <= width[1]
            and height[0] <= down <= height[1]
            and self.count / (across * down) >= fill
        )


class ResourceBubble(BaseModel):
    """A collector marker on the home village, and what it is holding.

    Tapping one collects it outright: no menu opens and nothing asks. So the
    marker is both the target and the receipt, since a collected one disappears.
    """

    model_config = ConfigDict(frozen=True)

    resource: Literal["gold", "elixir", "dark"]
    point: tuple[int, int]


# How one `collect` run ended on the **home village**. The builder base has no
# collectors at all and one cart instead, which is why `cart` is its own answer
# rather than more values here: what happened there is `CartReport`'s to
# say, and duplicating its outcomes would be the same answer in two
# vocabularies.
CollectOutcome = Literal[
    "collected", "nothing_to_collect", "no_village", "builder_base", "stock_unread", "cart"
]


class CollectReport(BaseModel):
    """What one sweep of the collectors picked up.

    The amounts are what the storages actually gained rather than what the
    markers promised, because a storage already full takes none of it and a
    marker tapped twice pays once. `markers` is what was tapped, so the two
    disagreeing is the interesting case rather than an inconsistency.

    **The two villages do different jobs under one command**, so the builder
    base's trip comes back whole in `cart` rather than squeezed into fields
    named for collector markers — which is what it used to be, with `markers=1`
    standing in for "the cart paid something".
    """

    markers: int = 0
    gold: int = 0
    elixir: int = 0
    dark: int = 0
    outcome: CollectOutcome
    cart: CartReport | None = None


# How a trip to the builder base's 聖水車 ended. `collected` and `empty` are both
# a cart that was opened and pressed, told apart by whether the storage moved;
# the three `locked*` are a cart that was opened and whose 收集 the game had
# greyed, which is the one the loop used to report as an empty cart.
#
# **The greyed button is split by the `held / capacity` line beside it**
# (`loot_cart_load`): `locked_holding` is loot banked behind a full 聖水 storage,
# `locked_empty` a cart with nothing in it, and `locked` a greyed button whose
# line would not read, which says what the button looks like and not why.
#
# The rest are failures: `not_parked` never went looking, `wrong_world` was not
# on the builder base at all, `not_found` tapped every candidate spot without
# opening anything, and `unreadable` collected against a storage bar that would
# not resolve on one side or the other.
CartOutcome = Literal[
    "collected",
    "empty",
    "locked",
    "locked_holding",
    "locked_empty",
    "not_found",
    "not_parked",
    "wrong_world",
    "unreadable",
]


class CartReport(BaseModel):
    """What one trip to the builder base's loot cart did.

    **Named answers because the old two were wrong about five of them.** This
    used to be an `int | None`, where None meant the camera never parked and 0
    meant everything else — an empty cart, a cart nobody found, a village that
    was not the builder base, a cart whose sheet would not read, and a cart
    behind a button the game had locked. Measured live, that last one is the
    ordinary state of a village that has been farmed: both storages at capacity,
    135 843 elixir of a 1 600 000 cart, and a report saying 推車裡沒有東西可以收.
    **The cart was 8% full rather than standing full**, which is worth being
    exact about.

    **`locked` then said what the button looked like and not why**, and the two
    reasons behind it want opposite things done: a cart the loop emptied minutes
    ago is nothing to act on, and one holding loot behind a full 聖水 storage is
    a village banking elixir in its cart, which stops earning only once the cart
    is full too. The sheet
    writes the amount beside the button, so `locked_holding` and `locked_empty`
    are read rather than inferred, and `locked` is kept for the button grey with
    that line unread. **`locked_empty` is the game greying the button over an
    empty cart**, which it does whatever the storages hold: measured on the
    committed frame, 0 / 1 600 000 with the builder base's elixir at 3%.

    `elixir` is what the storage really gained rather than what the cart
    promised, for the reason `collect` judges its markers that way: a full
    storage takes none of what it is handed. `held` is the other number — what
    the cart says is in it — and `capacity` the one after the slash, the most it
    will hold. Those two come as a pair, and only ever on a trip that collected
    nothing.
    """

    model_config = ConfigDict(frozen=True)

    outcome: CartOutcome
    elixir: int = 0
    held: int | None = None
    capacity: int | None = None


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


class ShieldState(BaseModel):
    """Whether a shield is up over the home village, and for how much longer.

    **The plate has a state that carries no countdown at all**, which is why a
    countdown alone would not do: with no shield the game writes 無 there beside
    a green +, and a reader that only looked for digits would report that
    exactly as it reports a frame it could not make out. Those are opposite
    instructions — one says the village is being farmed by other people right
    now, the other says to look again.

    **So `up` is derived from `remaining` rather than stored beside it.** 無
    carries no countdown to read, and a shield that is up but whose countdown
    will not resolve is reported as no `ShieldState` at all rather than as one
    with an empty countdown. That is because it cannot be told apart from the
    plate being covered — swept over every committed frame, a full-screen panel
    over the village leaves the badge readable and the plate under it
    unreadable, so `hero_hall_menu.png` would otherwise claim a shield is up on
    a frame that says no such thing. Two stored fields let that combination be
    written down anyway: `shield_state` never returned it, `_shield_line` had a
    branch testing for it that nothing could reach, and that dead branch was the
    only thing in the code suggesting the two were distinguishable.

    The plate being absent altogether is not a state here — the builder base has
    no shield plate, so its callers get no `ShieldState` rather than one saying
    False. **Which of those two a None is comes off the world**: on the builder
    base it is structural and needs no following up, and on the home village it
    is a reading to take again.
    """

    model_config = ConfigDict(frozen=True)

    remaining: int | None = None

    @computed_field
    @property
    def up(self) -> bool:
        """Whether a shield is up at all, which is exactly a countdown having read.

        **Computed rather than stored**, on `RoundReport.stock_full`'s reasoning:
        the name is what a reader of `result.json` looks for, so it stays in
        the file, while the state the docstring
        above rules out stops being expressible at all.

        The model is what carries the third answer, not this: a caller with no
        `ShieldState` has either a builder base, which structurally has no
        plate, or a reading to take again. Collapsing this to a bare
        `int | None` would lose that, which is the cleanup to refuse.
        """
        return self.remaining is not None


class PlateJob(BaseModel):
    """One row of a plate's panel: what is being raised, and how long it has left.

    `remaining` is None for a row whose countdown would not resolve, which is
    counted all the same — the row is there and something is in it, so dropping
    it would report a plate as emptier than it is. `name` is empty without a
    model to read it with, since it is Chinese and no parser here reads any.
    """

    model_config = ConfigDict(frozen=True)

    name: str = ""
    remaining: int | None = None


class SuggestedUpgrade(BaseModel):
    """One row of a panel's 建議升級 block: what the game suggests raising, and for how much.

    **The price is read by the model, not by the digit templates**, so it is
    good for telling a person what is on offer and not yet for deciding to
    spend: before anything buys off it, it is checked against a crop of the
    row the way a building menu's price is. `price` is None where the row's
    price would not read.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    price: int | None
    resource: Literal["gold", "elixir", "dark", "other"]


class PlateJobNames(BaseModel):
    """What a model read off a plate's panel: the 升級中 names, and the 建議升級 rows.

    One call per panel rather than one per row, so the order is what matches a
    name to its countdown. A row it could not make out comes back as an empty
    string and still takes its place, since a short list would shift every name
    after it onto the wrong row. Neither field has a default, because a model
    leaves out a field that has one.
    """

    model_config = ConfigDict(frozen=True)

    names: list[str]
    suggested: list[SuggestedUpgrade]


class PlateReport(BaseModel):
    """What one plate says, and what the panel behind it is running.

    **About the village on screen, and it never sails.** `world` says which one
    that turned out to be, the way `StockReport` does and for the same reason:
    crossing is `ai_coc world --go`, and a status check that moves the game is
    no longer one.

    `free` and `total` are None together for a plate whose count would not
    resolve — the plate is translucent, so the camera decides. That costs less
    than it looks: the number a caller waiting on a builder actually wants is
    `soonest`, which comes off the panel rather than off the plate.

    **`outcome` names the branch this came back from, and there is no sentence.**
    `StockReport` argues the whole case already: a reading has no outcome to
    narrate, and every state it can be in is readable off its own fields. What
    earns a field here rather than a sentence is one pair the fields genuinely
    cannot separate — a plate whose badge was not on the frame, against a panel
    that would not open **on a frame whose count would not read either**. Both
    leave a village, no counts and no rows. Read off an enum they are two
    different things to do next; read off prose they are two wordings nobody can
    match on. The commoner shape of that second one keeps its count, so those
    two really are told apart by the fields — the pair is narrow, and it is
    still the reason.

    **It has no default**, because every path through the reader knows how it
    ended and a report that quietly claimed one of them would be wrong for four
    of the five.
    """

    world: World | None = None
    role: PlateRole = "builder"
    free: int | None = None
    total: int | None = None
    jobs: list[PlateJob] = Field(default_factory=list)
    # The panel's 建議升級 rows, empty without a model to read them or on a
    # panel with nothing running, whose open state nothing here can confirm.
    suggested: list[SuggestedUpgrade] = Field(default_factory=list)
    outcome: PlateOutcome

    def soonest(self) -> int | None:
        """Seconds until the next one finishes, or None if no row's countdown read."""
        times = [job.remaining for job in self.jobs if job.remaining is not None]
        return min(times) if times else None


class StockReport(BaseModel):
    """What the village on screen is holding, against what its storages take when full.

    **The question nothing could answer without starting a run.** The storages
    are read on the way past by every loop that farms or spends, and the numbers
    reached a caller only as a line in that run's log — so a session that wanted
    to know where a village stood had to start something that drives the game
    for minutes, or capture a frame and read it by hand at whatever camera
    happened to be up. Both are how a session ends up telling somebody a village
    is full when its loot was spent an hour ago.

    Read-only and about the village that is on screen: `world` says which one
    that turned out to be rather than promising either, because crossing is
    `ai_coc world`'s job and a status check that sails a boat is no longer one.

    `filled` carries the share of each ceiling that has been read, which is the
    form every decision here is actually made in — `stop_at` is a percentage,
    and a caller working it out from two numbers is a caller that can get it
    wrong.

    **The first report here with no `message`, and the argument it made is now
    the house rule.** This reports three numbers, and every state it can be in
    is already readable off the fields: no `world` is a screen that is not a
    village, a `world` with no `held` is a village whose bars this frame could
    not resolve, and a name missing from `filled` is a ceiling that would not
    read. A sentence saying any of that again is the same answer twice, which is
    what it was doing. Where a report's fields really cannot say which branch a
    run came back from it carries a named outcome instead of prose — see
    `PlateOutcome`: a wording nobody can match on is what made a sentence worse
    than a field rather than merely redundant. No report here carries a
    `message` any more, and the Chinese a person reads is built from the outcome
    at the edge, in `commands.py`.
    """

    world: World | None = None
    held: VillageStock | None = None
    capacity: StorageCapacity = StorageCapacity()
    # Written with its sign, because the one thing a reader does with this is
    # read it. A resource whose ceiling would not read is absent rather than
    # zero: nothing is known about how full it is, and a 0 there reads as empty.
    filled: dict[str, str] = Field(default_factory=dict)


# How one plate's count came back, read off the village frame without opening
# anything. `unread` and `no_badge` both leave a village and no numbers, which
# is why this is a field: the first is a plate that is there and would not
# resolve, the second one this frame did not show at all.
PlateCountOutcome = Literal["counted", "unread", "no_badge", "not_a_village"]


class PlateCount(BaseModel):
    """What one plate's own digits say: how many slots are idle, out of how many.

    The village on screen only, since the plates are drawn on the village they
    count; the timers on `StatusReport` cover both.
    """

    world: World | None = None
    role: PlateRole = "builder"
    free: int | None = None
    total: int | None = None
    outcome: PlateCountOutcome


# Who an upgrade is waiting on: a builder, a research slot, or the pet house.
UpgradeRole = Literal["builder", "lab", "pet"]


class UpgradeTimer(BaseModel):
    """One upgrade still running, and when it really finishes.

    `timer` is what the export says, which is seconds at normal speed — the
    same number the panel draws, and the one a potion leaves alone while it
    makes the clock run faster. `seconds` is that worked through the speed-up
    the export says is running, from the export's own moment, and `done_at` is
    the same instant on the clock. `role` is None for a section nobody has seen
    carry a timer yet, which is reported unboosted rather than dropped.
    """

    model_config = ConfigDict(frozen=True)

    world: World
    role: UpgradeRole | None
    section: str
    data_id: int
    name: str | None = None
    level: int | None = None
    timer: int
    boost: str | None = None
    seconds: int
    done_at: str


class ClockTower(BaseModel):
    """When the builder base's clock tower can be started again.

    `ready_in` 0 is a tower that can be started now. `boosting` is the seconds
    left on one running, which the cooldown counts through rather than after.
    """

    model_config = ConfigDict(frozen=True)

    ready_in: int
    ready_at: str
    boosting: int | None = None


class StatusReport(BaseModel):
    """Where both villages' upgrades stand, and what the village on screen holds.

    **The timers come out of the game's own export, for both villages at once.**
    The panels used to be read instead, one village at a time with a model
    naming each row, and they draw the countdown at normal speed whatever is
    running — a builder potion left a job reading six hours that finished in
    forty minutes. The export carries every running upgrade and every speed-up,
    so `timers` is that worked through, soonest first, and nothing sails.

    What the export does not carry comes off the village on screen: the plates'
    idle counts, the storages, and the shield. `export` says whether the timers
    were read at all, since an empty list is also a village with nothing
    running.
    """

    # Built on demand rather than at class definition: an annotation resolves
    # later but a default is evaluated where it is written, and `World` is
    # defined further down this file than any of these.
    world: World | None = None
    builder: PlateCount = Field(
        default_factory=lambda: PlateCount(role="builder", outcome="not_a_village")
    )
    lab: PlateCount = Field(
        default_factory=lambda: PlateCount(role="lab", outcome="not_a_village")
    )
    stock: StockReport = Field(default_factory=StockReport)
    # None on the builder base, which has no shield plate at all rather than one
    # saying there is no shield.
    shield: ShieldState | None = None
    export: ExportOutcome
    exported_at: str | None = None
    boosts: Boosts = Field(default_factory=Boosts)
    timers: list[UpgradeTimer] = Field(default_factory=list)
    # None where the export has no clock tower, or one being upgraded.
    clock_tower: ClockTower | None = None


class BuildingName(BaseModel):
    """What the game says is selected, read off the label rather than the artwork.

    The one question the parsers provably cannot answer: 金礦 and 箭塔 open menus
    that are identical to every reader here, and what separates them is a line of
    Chinese. So it goes to a model — a cheap one, because reading a label is a
    classification rather than a judgement.
    """

    model_config = ConfigDict(frozen=True)

    # Empty when the strip carried no label, which is an ordinary answer: a frame
    # can be caught between the tap and the game drawing it.
    name: str = ""
    level: int | None = None

    def __str__(self) -> str:
        """How the game writes it, which is what a log line should say."""
        return f"{self.name}({self.level}級)" if self.level is not None else self.name or "?"


class BuildCandidate(BaseModel):
    """A building found with an upgrade on offer, and what it asks for.

    Where it is, what it costs, and — when there is a model to ask — what it is.
    The name buys two things nothing here had before: a report that says 金礦
    rather than a coordinate, and `--only`, which is the difference between
    spending a scarce builder on the dearest thing going and spending it on the
    thing that was actually holding the village back.
    """

    model_config = ConfigDict(frozen=True)

    point: tuple[int, int]
    resource: Literal["gold", "elixir"]
    price: int
    building: BuildingName = BuildingName()


# How one `upgrade` run ended. `started` says what it put builders on; this says
# why it stopped putting them on things. **`builders_busy` and `cannot_afford`
# are the two that mean "come back later"** and `nothing_found` is the one that
# means the finder came up empty, which is a different thing to go and look at.
BuildOutcome = Literal[
    "started",
    "nothing_found",
    "cannot_afford",
    "only_no_match",
    "builders_busy",
    "count_unread",
    "no_village",
    "builder_base",
    "village_lost",
]


class BuildReport(BaseModel):
    """What one `upgrade` run put the idle builders on."""

    started: list[BuildCandidate] = Field(default_factory=list)
    outcome: BuildOutcome
    # What the cheapest upgrade on offer would have cost, on a run that could
    # not pay for any of them. "Come back later" is only useful with how much
    # later on it, and this is the number the sentence used to carry. **Only
    # ever set on `cannot_afford`**: the `--only` refusal is about a name rather
    # than about money, and the cheapest of every offer there is a price for
    # buildings the filter had already thrown away.
    cheapest: int | None = None
    # The `--only` substring this run was given. Nothing else records it —
    # `cli.py` logs the run directory and not the arguments — and it is the
    # whole subject of an `only_no_match`.
    only: str = ""

    def paid(self, resource: str) -> int:
        return sum(job.price for job in self.started if job.resource == resource)


# How one `view` command left the camera. `parked` is the whole job done;
# `not_a_village` is a screen the park may not be aimed at, since a swipe with a
# card selected deploys troops along its path; `zoomed_in` is `--zoom in`, where
# the park would undo the request and could not settle anyway; and
# `never_settled` is a camera that would not stop moving, which leaves every
# remembered coordinate off by however far it was short.
ViewOutcome = Literal["parked", "not_a_village", "zoomed_in", "never_settled"]


class ViewReport(BaseModel):
    """What one `view` command asked of the camera, and what it settled.

    There is nothing to read back for the scale. The game exposes no zoom level
    and it is not written anywhere on screen, so `zoom` and `times` say what was
    sent rather than what the camera ended up at — and a zoom the camera was
    already at is a no-op rather than an error. The **position** is different:
    the park answers whether it arrived, and `outcome` is that answer.

    **This model was one `message` and nothing else**, which is why it gained
    four fields rather than losing one: everything it said was in the sentence.
    """

    model_config = ConfigDict(frozen=True)

    zoom: Literal["in", "out"] = "out"
    times: int = 0
    world: World | None = None
    outcome: ViewOutcome


RunnerStatus = Literal["running", "stopping", "idle"]


class Caller(BaseModel):
    """Who asked for a run, in the caller's own words from the command line.

    Several agents and the user drive the one emulator, and the state file used
    to say only which command held it. The day an agent took a running emulator
    for a leftover and killed it, finding out which session had done that meant
    digging through that agent's own conversation database. Nothing checks these
    three: they are whatever the caller passed, and blank when it passed none.
    """

    model_config = ConfigDict(frozen=True)

    agent: str = ""
    session: str = ""
    mission: str = ""


LoanEnd = Literal["returned", "cancelled"]


class Loan(BaseModel):
    """The emulator taken from one agent's run by another, to be handed back.

    Agents in different runtimes cannot message each other, so the lender learns
    it was borrowed from, and when to start its run again, from this record
    alone. `until` is None while the borrower is driving, and otherwise when an
    idle loan lapses. Every field has a default, because a loan that fails to
    validate would fail the whole record with it.
    """

    model_config = ConfigDict(frozen=True)

    lender: Caller = Field(default_factory=Caller)
    borrower: Caller = Field(default_factory=Caller)
    since: datetime | None = None
    until: datetime | None = None
    ended: LoanEnd | None = None


class Watch(BaseModel):
    """An agent watching the upgrade timers, which drives nothing between its reads.

    It sleeps between reads, so no process stands for it and the run fields
    above cannot show it: without this record a second agent starts a second
    watch, and `ai_coc stop` reaches nobody. `next_at` is when it next wakes,
    which is also what says a record is stale once its agent has gone.
    """

    model_config = ConfigDict(frozen=True)

    caller: Caller = Field(default_factory=Caller)
    since: datetime | None = None
    next_at: datetime | None = None
    about: str = ""


class RunnerState(BaseModel):
    """Who is driving the emulator, kept between runs rather than during one.

    There is one emulator, so two commands at once interleave taps on the same
    display. Nothing wrote that down before this: a session that wanted the
    screen had to be told in the chat whether a farming run was up, and a run
    killed outright left the same trace as one that never started.

    **`idle` is the answer to a different question than a missing file.** A run
    that has finished leaves this behind holding what it was and where its log
    went, which is what a session asking "can I have the screen" is really
    asking. Deleting the file by hand is the escape hatch for whoever no longer
    has an agent to ask nicely with, and reads as a stop for that reason.

    `pid` is what separates a run still going from one that was killed, and what
    keeps a release from writing over a claim that is not its own. `pid_created`
    is when that process started, which is what tells the holder from a later
    process that happened to be handed the same pid: `commands.claim` treats the
    file as a lock and waits out a live holder, so a stale record must not read
    as one.
    """

    model_config = ConfigDict(frozen=True)

    status: RunnerStatus = "idle"
    pid: int = 0
    command: str = ""
    started: datetime | None = None
    ended: datetime | None = None
    # Where this run's own log went, so a reader is one step from the whole of
    # it. The alternative was copying the progress in here and keeping two
    # records of one run in step, which is the arrangement that goes stale.
    log: Path | None = None
    caller: Caller = Field(default_factory=Caller)
    pid_created: float | None = None
    # Who asked this run to stand down, kept on the `idle` it ends with, so the
    # agent whose run was stopped can read who took the emulator and why.
    stop_by: Caller | None = None
    # Background work (`--yield`): it stands nobody down, and is lent rather
    # than stopped when anything else needs the emulator.
    yields: bool = False
    # Carried from record to record until the lender runs again or another
    # loan replaces it, so a lender that looks late still finds out.
    loan: Loan | None = None
    # Who is watching the upgrade timers, carried from record to record like
    # the loan until the watcher clears it or a stop does.
    watch: Watch | None = None


def _labelled(label: str) -> str:
    """A label reduced to what can safely be one part of a directory name.

    Anything that is not a letter, a digit, an underscore or a hyphen becomes a
    hyphen. A path separator getting through would put the run somewhere nobody
    goes looking, which is the failure the label exists to end rather than to
    reproduce in a new place.
    """
    return re.sub(r"[^\w-]+", "-", label, flags=re.UNICODE).strip("-")[:40]


def _cull_old_frames(keep: timedelta = timedelta(days=FRAME_RETENTION_DAYS)) -> int:
    """Delete the `frames/` of runs older than the window, and nothing else.

    Judged on the frames' own mtime rather than the run directory's, because
    `result.json` is written after the last frame and would keep a directory
    looking fresh for as long as anything else in it was still being touched.

    **Housekeeping never fails the run it rides on.** Whatever cannot be read or
    removed is left where it is and met again next time; a frame this could not
    delete is worth less than the run it would have taken down with it.

    **It answers how many runs lost their frames rather than logging it**, which
    is the long way round for a reason: this runs from `RunLog.open`, which is
    called *before* `configure_logging`, so a line written here would reach no
    handler at all on the CLI path. The count rides on the `RunLog` and is said
    out loud once the sinks exist. Saying it matters because this is the one
    destructive thing in the project — a session following a path out of an old
    report to a directory that is gone has otherwise no way to tell a cull from
    a bug, or a partial failure from a clean sweep.
    """
    cutoff = datetime.now(UTC) - keep
    try:
        runs = list(LOG_DIR.iterdir())
    except OSError:
        return 0
    culled = 0
    for run in runs:
        frames = run / "frames"
        try:
            stale = (
                frames.is_dir() and datetime.fromtimestamp(frames.stat().st_mtime, UTC) < cutoff
            )
        except OSError:
            continue
        if stale:
            shutil.rmtree(frames, ignore_errors=True)
            culled += 1
    return culled


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
    # How many runs lost their frames to the cull that ran when this one opened.
    # It travels here because the cull happens before logging is configured; see
    # `_cull_old_frames`, and `logging_setup._attach_run` for where it is said.
    culled: int = 0

    @classmethod
    def open(cls, command: str, recording: bool = False, label: str = "") -> RunLog:
        """Make the directory a run about to start will write into.

        Named `<when>-<what>` so that a plain listing of the log directory reads
        as a history, which is what makes a run from three days ago findable
        without opening any of them. `label` adds a third part, because a
        session capturing evidence wants to find that one again afterwards and
        the alternative was inventing a path: measured on this machine, 20 of
        438 run directories had been hand-named, four of them ending in `.png`
        because somebody read a directory argument as a filename.

        Culling old frames rides here rather than in a command of its own, since
        nobody would remember to run one.
        """
        culled = _cull_old_frames()
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        base = LOG_DIR / f"{stamp}-{command}{f'-{slug}' if (slug := _labelled(label)) else ''}"
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
        return cls(directory=directory, recording=recording, culled=culled)

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
    def no_village(self) -> Path:
        """Where `world` and `launch` keep the frame they could not find a village on.

        Unconditional where `frames` is not: neither command has a `--debug`,
        and this is one PNG written only on that failure, which is exactly the
        one that needs a frame to be debugged from.
        """
        return self.directory / "frames" / "no_village.png"

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
        back out for `--plan` is a line of `jq`, on either village.
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
# worker` reads the panel saying which of the five are free, and a
# `world --go builder` standing next to it would be read as belonging to that.
World = Literal["day", "night"]


# The plates along the top of either village, named for what each one counts.
# `lab` is the research slots — on the home village two research jobs have been
# seen running side by side, and the pet house is not on this plate at all —
# and `builder` the workmen. `shield` is the home village's
# alone: the builder base is real-time matchmaking against a live player, so a
# shield there would contradict the mode's own design.
PlateRole = Literal["lab", "builder", "shield"]

# How a plate reading ended, which is the one thing about it the fields cannot
# say. `read` is the only one that saw the panel: it opened and had at least one
# row on it, which is the only thing `panel_rows` ever answers with — a band with
# no bars comes back as nothing at all rather than as an empty list.
#
# **`idle` is inferred rather than observed**, and that is worth knowing before
# leaning on it: it is the no-bars case where the plate's own count says every
# slot is free, so it is a panel nobody watched open. A count that disagrees or
# will not read leaves `panel_shut` instead, which is the honest answer that
# nobody can say. The remaining two are failures a caller answers differently:
# `not_a_village` is a screen to clear or wait out, and it is the only one whose
# `world` is None; `no_badge` is a plate not on this frame, which on the home
# village is worth another capture and on the builder base's missing shield is
# structural and never will be.
PlateOutcome = Literal["read", "idle", "no_badge", "panel_shut", "not_a_village"]


class Pinch(BaseModel):
    """One two-finger gesture along a row: each finger's distance from `centre` at either end.

    The game zooms about the point between the fingers, so `centre` is the
    pixel that stays put, and `far / near` is how much one gesture zooms.
    """

    model_config = ConfigDict(frozen=True)

    near: int
    far: int
    centre: tuple[int, int]


class Crossing(BaseModel):
    """How to reach one village's boat, and where it sits once the camera stops.

    `start` and `drift` are one drag of the map, repeated until the camera is
    clamped at the corner the boat is moored in; `spots` are the places to tap
    for it from that view, tried in turn. There is more than one because nothing
    recognises the boat itself — it is a sprite the game dresses up for events —
    so a miss and a hit look the same until the world is read again.

    `marked` is whether the marker the game floats over the boat is in view
    from there, and so is looked for ahead of `spots`, which were measured on
    one scenery and miss on another.
    """

    model_config = ConfigDict(frozen=True)

    start: tuple[int, int]
    drift: tuple[int, int]
    spots: tuple[tuple[int, int], ...]
    marked: bool


# How one `world` command ended. `here` is a read, or a crossing already at its
# destination; `crossed` and `crossing_failed` are the two halves of a sail.
# **`no_village` and `no_display` are separated because the answer differs**:
# one is a screen to clear or wait out, the other is a game with no window yet,
# which `ai_coc launch` is the command for.
WorldOutcome = Literal["here", "crossed", "crossing_failed", "no_village", "no_display"]


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
    outcome: WorldOutcome


LaunchOutcome = Literal["at_village", "no_village", "stopped"]


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
    # Which scope was asked for, which nothing else on the report says and the
    # sentence used to: "已重開部落衝突" against "已重開模擬器與部落衝突" is the
    # difference between a game restarted under a live emulator and both.
    restart: RestartScope = "none"
    # Whether the village really came up and the camera was put back at the far
    # zoom, which is a different question from whether the process is running:
    # `ensure_coc` is satisfied by a pid, and a game still on its loading screen
    # answers every command by silently missing whatever it aimed at. `stopped`
    # is an `ai_coc stop` that landed while it waited.
    outcome: LaunchOutcome

    @computed_field
    @property
    def at_village(self) -> bool:
        """Whether the village came up, which is what the skills check."""
        return self.outcome == "at_village"


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


# How one `hero` run ended. **`read` is the read-only half and a success**: the
# command answers what each hero's next level costs unless it is told to spend.
# Of the refusals, `already_upgrading` / `button_dead` / `cannot_afford` are the
# card's own three and they are not interchangeable — a hero the hall's level has
# capped keeps its price and loses its button, which a run going by the price
# alone reported as a swallowed tap.
HeroOutcome = Literal[
    "read",
    "started",
    "hall_not_found",
    "hero_absent",
    "already_upgrading",
    "button_dead",
    "cannot_afford",
    "builders_busy",
    "count_unread",
    "card_moved",
    "no_confirmation",
    "undercharged",
    "no_village",
    "builder_base",
    "village_lost",
]


class HeroReport(BaseModel):
    """What one `hero` run saw in the hall, and which upgrade it started.

    The cards are reported whether or not anything was started, because that is
    the read-only half of the command and the half worth running on its own:
    what each hero costs next is the number a player decides against.
    """

    # Which hero this run was asked to raise, or None for a read. Nothing else
    # on the report says it, and every refusal is about that one hero.
    hero: HeroKind | None = None
    cards: list[HeroCard] = Field(default_factory=list)
    started: HeroCard | None = None
    outcome: HeroOutcome


# How one `donate` run ended. `nobody_asking` is the ordinary one and costs
# almost nothing, which is what this loop is written to be: donating cannot be
# done on demand, somebody else has to ask first.
DonateOutcome = Literal[
    "donated",
    "dry_run",
    "nobody_asking",
    "nothing_given",
    "panel_shut",
    "no_village",
    "builder_base",
]


class DonateReport(BaseModel):
    """What one `donate` run gave away.

    `gifts` is where each tap landed rather than what it gave, because nothing on
    the panel names a troop. `offered` is how many cards stood in colour at the
    richest point, which is what a dry run has to report instead.
    """

    gifts: list[tuple[int, int]] = Field(default_factory=list)
    offered: int = 0
    outcome: DonateOutcome


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


class ScreenSpots(BaseModel):
    """Where Gemini thinks the things a loop is looking for are.

    These are guesses and are treated as such: every one of them is tapped and
    the screen that comes up is checked by whatever the caller was looking for —
    a wall menu, a priced upgrade button, hero cards. So a wrong point costs one
    tap and one capture and is dropped with a line saying so, and asking for more
    points than the run needs is the right shape rather than a waste.

    One model for all three because the answer is the same shape whatever was
    asked for; what differs is the question and the check, and both of those
    belong to the caller.
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


# Why a `walls` run stopped, which is the whole of what a caller has to decide
# on and was a sentence for three releases. **The three that used to be told
# apart by wording are the reason this exists**: a skill reads them to pick what
# happens next, and `每一個位置都沒有買成` has been read as "the walls are
# finished" and taken for it.
#
# `bought` and `nothing_bought` are both a run that walked its whole list —
# `upgrades` says which. `cannot_afford` means go and farm; `builders_busy`
# means the game refuses every batch until one workman is idle, which
# `ai_coc status` dates; `nothing_bought` is the one where the loop cannot say
# why, and the likeliest reason is the town hall capping every wall it found,
# which is a decision for the player rather than for the loop. The rest are the
# run not getting started: `no_walls_found` found none — swept, or verified the
# spots a caller named, which does not sweep at all — `no_village`
# never got back to one, `builder_base` found the game on the other village,
# `no_stock` lost the storage bars between batches, and `stopped` is somebody
# asking it to stand down.
WallOutcome = Literal[
    "bought",
    "nothing_bought",
    "cannot_afford",
    "builders_busy",
    "no_walls_found",
    "no_village",
    "builder_base",
    "no_stock",
    "stopped",
]


class WallReport(BaseModel):
    """What one `walls` run bought, and what stopped it.

    **`outcome` is a name rather than a sentence, and that is not a tidy-up.**
    Three of its values used to be separated only by their wording, and a
    project skill told its reader to tell them apart that way — so a reworded
    line would have silently changed what the next session did, with nothing to
    catch it. `upgrades` already says what was bought; this says why it stopped.
    """

    upgrades: list[WallUpgrade] = Field(default_factory=list)
    outcome: WallOutcome

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


class UpgradeOptions(BaseModel):
    """What one `upgrade` command was told to do."""

    frame_dir: Path | None = None
    # What to leave in the storages rather than spend, as for the walls.
    keep_gold: int = 0
    keep_elixir: int = 0
    # The buildings this run was pointed at, instead of asking and then
    # sweeping for them. Empty means find them.
    at: list[tuple[int, int]] = Field(default_factory=list)
    # Only a building whose name contains this; empty takes the dearest
    # affordable one, which is what the loop did before names existed.
    only: str = ""


class HeroOptions(BaseModel):
    """What one `hero` command was told to do."""

    frame_dir: Path | None = None
    # Which hero to raise. None reads the hall and starts nothing, which is the
    # half of the command worth running on its own: what each hero costs next
    # is the number the decision rests on, and reading it spends nothing.
    upgrade: HeroKind | None = None
    # Where the hall is, for a run that already knows.
    at: tuple[int, int] | None = None


class DonateOptions(BaseModel):
    """What one `donate` command was told to do."""

    frame_dir: Path | None = None
    # Walk the whole path and stop before the tap that gives anything away,
    # because the panel confirms nothing.
    dry_run: bool = False
    # 0 gives until the request is full or the village has nothing left.
    rounds: int = 0


class FrameReading(BaseModel):
    """What the parsers a loop leans on make of one frame, for the `read` command.

    A screen the loop mishandled is almost always a screen it misread, and this
    is what says which of the readers disagreed with the eye. It is one model
    rather than a printout so a recorded run can be replayed through it.

    Not literally every function in `parsers/`, and the line is where a loop
    would ask: a reader some loop consults to decide what to do next belongs
    here, while one that only measures something the caller already found — the
    hall's scroll arrows, the name strip, the chat's own panel top — does not.
    That line moved once already, when the builder base's three screens turned
    out to be missing from a command the skills point a stuck night round at.
    """

    # Which village the frame was taken on, and the reason it leads: every other
    # field below is read against one of the two, and `stock` in particular
    # answers on both while meaning something different on each.
    world: World | None = None
    scout: ScoutView | None = None
    stock: VillageStock | None = None
    # The builder base's own two rows, which `stock` misreads: its gems bar
    # sits at the y the dark row is read from, so `stock` answers there with a
    # third number that is not a resource. Which of the two to believe is what
    # `world` says.
    builder_stock: VillageStock | None = None
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
    # The builder base's own three screens. They were missing while the skills
    # were already pointing a session at this command to debug a night round —
    # so a round reporting 畫面不在建築大師基地 could be asked every question
    # except the one that had actually failed. `attack_menu` above answers only
    # for the home village, because the two dialogs share nothing but the corner
    # the button that opens them sits in.
    night_menu: bool = False
    searching: bool = False
    loot_cart: bool = False
    # **The cart's sheet and the cart's button are two questions now**, and the
    # second one is the one a session comes here to ask: `loot_cart` says the
    # sheet is up, which it used to be able to say only when its 收集 was live.
    # Without this the frame that the trip gave up on — sheet open, button
    # greyed — answers every question except why it collected nothing.
    cart_ready: bool = False
    # The result screen, which is what ends a battle for every loop here —
    # `read_scout` answering None does not, and the two were confused once at
    # the cost of five rounds walking out of battles still being fought.
    battle_over: bool = False
    # A battle actually being fought, which `card_groups` was standing in for
    # and could not answer: the game's own panels carry a row of card-shaped
    # patches too, so this is the field that separates one from the other.
    in_battle: bool = False
    # The game's own yes/no panel, and both of its buttons. Which one to press
    # is never this reader's to say: 升級城牆 and 確定退出遊戲嗎 are the same
    # panel in the same pixels.
    dialog: GameDialog | None = None
    # Whether 下一個 is up, which `scout` cannot answer where it matters: it is
    # None both for 正在搜尋對手 and for an opponent whose loot will not read,
    # and this is what tells a run debugging the second one which it is looking at.
    skip_offered: bool = False
    # Whether the scout screen has finished fading in, which is the other thing
    # `scout` cannot say: it answers on a half-painted panel with numbers that
    # are wrong and a `can_skip` that reads like an expired countdown, and the
    # search loop refuses exactly those. Without this a frame the loop declined
    # is reported here as an ordinary reading with nothing marking it.
    panel_drawn: bool = False
    idle_dialog: bool = False
    # 首領，歡迎回來, the raid report whose 確定 used to read as 回營.
    welcome_back: bool = False
    # 正在載入, which every other reader answers None on; this is what says a
    # run that reported no village was in fact waiting on the server.
    loading: bool = False
    # The two settings pages the village export sits behind, and the row itself.
    # `export_row` is the coordinate `export` taps, so a run that reported it
    # could not find the row can be asked the same question about the frame it
    # kept — which is the whole reason a reader belongs in this command.
    settings_menu: bool = False
    more_settings: bool = False
    export_row: tuple[int, int] | None = None
    card_groups: list[list[int]] = Field(default_factory=list)
    counted: list[int] = Field(default_factory=list)
    # The same question at the builder base's own line, which the night loop
    # asks instead: its `1x` is too small to clear `counted`'s.
    night_counted: list[int] = Field(default_factory=list)
    freezes: list[int] = Field(default_factory=list)
    live: list[int] = Field(default_factory=list)
    on_field: list[int] = Field(default_factory=list)
    # Which cards the game is drawing selected. The builder base's second stage
    # opens with the surviving machine's card in that state, and a run that
    # could not read it was offered no ability for the whole stage — so this is
    # a reader whose being wrong has already cost a battle.
    selected: list[int] = Field(default_factory=list)
    counts: dict[int, int | None] = Field(default_factory=dict)
    # Where 確認 sits on the full-screen upgrade sheet, which is what every
    # building but a wall confirms through.
    upgrade_sheet: tuple[int, int] | None = None
    # What the game's own red line encloses, which is the one measurement behind
    # every coordinate a battle uses: a camera left somewhere else puts the whole
    # army where nobody asked for it, and this is what says so.
    village_box: tuple[int, int, int, int] | None = None


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
