"""The headless side of the application: what `cli.py` runs when given a command.

Nothing here builds a window. Everything below the `ui/` package is already
Qt-free, so the only thing the window was ever providing was the wiring — find
the emulator, resolve the display it put the game on, hand over the API key —
and that is what these functions are. A feature that can only be reached through
a widget cannot be run against the live game while it is being worked on, which
is the whole reason the attack loop grew a `frame_dir` at the same time.
"""

from __future__ import annotations

import os
import math
import time
from typing import TYPE_CHECKING, Self, Literal
import logging
from pathlib import Path
from datetime import UTC, datetime
import threading
from contextlib import contextmanager

from pydantic import BaseModel, PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    World,
    MapEdge,
    ProbeRay,
    AppConfig,
    MapSurvey,
    NightPlan,
    PlateRole,
    AttackPlan,
    CartReport,
    HeroReport,
    PlayedPlan,
    ViewReport,
    WallReport,
    AdbEndpoint,
    BuildReport,
    CartOutcome,
    HeroOptions,
    HeroOutcome,
    NamedEntity,
    PlateReport,
    RunnerState,
    ShieldState,
    StockReport,
    ViewOutcome,
    WallOptions,
    WallOutcome,
    WorldReport,
    AttackReport,
    AttackSeries,
    BuildOutcome,
    DonateReport,
    FrameReading,
    LaunchReport,
    RestartScope,
    StatusReport,
    WorldOutcome,
    AttackOptions,
    AttackOutcome,
    BuilderReport,
    CollectReport,
    DisplayTarget,
    DonateOptions,
    DonateOutcome,
    ExportOutcome,
    SurveyOutcome,
    VillageEntity,
    VillageExport,
    BoundarySurvey,
    BuilderOutcome,
    CollectOutcome,
    LootThresholds,
    UpgradeOptions,
    StorageCapacity,
    EmulatorInstance,
)
from ai_coc.constants import STATE_PATH, COC_PACKAGE, ACCOUNT_JSON_DIR
from ai_coc.adapters.ai import GeminiClient

# A runtime import rather than a TYPE_CHECKING one: `FrameTicker` declares it as
# a field, and a model whose field type is only importable to a type checker
# cannot be built at all.
from ai_coc.adapters.adb import AdbController, AdbControlError
from ai_coc.parsers.clan import donatable_cards
from ai_coc.parsers.hero import hero_cards
from ai_coc.parsers.home import builder_jobs, free_builders, collect_bubbles
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.parsers.scout import (
    in_battle,
    card_count,
    live_cards,
    read_scout,
    read_stock,
    battle_over,
    card_groups,
    field_units,
    panel_drawn,
    card_drained,
    freeze_cards,
    skip_offered,
    army_strength,
    counted_cards,
    loading_screen,
    loot_cart_open,
    selected_cards,
    loot_cart_ready,
    attack_menu_open,
    storage_capacity,
    idle_disconnected,
    night_attack_menu,
    read_builder_stock,
    searching_opponent,
)
from ai_coc.parsers.world import current_world
from ai_coc.adapters.config import ConfigStore
from ai_coc.parsers.village import parse_village_text
from ai_coc.adapters.mapping import fetch_entity_mapping
from ai_coc.adapters.secrets import SecretStore
from ai_coc.parsers.boundary import PLAYFIELD, VILLAGE_CENTRE, village_box, boundary_reach
from ai_coc.parsers.building import wall_menu, game_dialog, upgrade_sheet, upgrade_buttons
from ai_coc.parsers.settings import export_row, settings_open, more_settings_open
from ai_coc.adapters.emulator import Emulator, EmulatorError
from ai_coc.adapters.ldplayer import LDPlayerAdapter
from ai_coc.adapters.clipboard import read_clipboard, clear_clipboard, write_clipboard

from .ui.clan import ClanRunner
from .ui.hero import HeroRunner
from .ui.walls import WallRunner
from .ui.world import cross, uncovered, park_camera, collect_cart
from .ui.attack import CARD_ROW_Y, DROP_SETTLE, SINGLE_DROP_DELAY, AttackRunner
from .ui.plates import PlateRunner
from .ui.runner import ScreenRunner, spell_out, restart_game
from .ui.upkeep import UpkeepRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)


def emulators() -> Iterator[tuple[Emulator, EmulatorInstance]]:
    """Every instance of every emulator installed here, MuMu's first.

    Lazy, so a caller that finds what it wants among MuMu's instances never
    asks LDPlayer at all: every command starts here, and an emulator it is not
    driving should not be able to slow it down or fail it. One that is not
    installed is left out quietly, and one whose CLI will not list its
    instances is left out with a warning.
    """
    for kind in (MuMuAdapter, LDPlayerAdapter):
        try:
            emulator = kind()
        except EmulatorError as exc:
            logger.debug("%s is not installed here: %s", kind.label, exc)
            continue
        try:
            instances = emulator.enumerate_instances()
        except (EmulatorError, ValueError):
            logger.warning("%s did not list its instances", kind.label, exc_info=True)
            continue
        for instance in instances:
            yield emulator, instance


def chosen(
    listed: Iterable[tuple[Emulator, EmulatorInstance]],
) -> tuple[Emulator, EmulatorInstance]:
    """The instance `config.json`'s `adb_serial` names, picking and writing one if it names none.

    A serial is matched against what the emulator reports and against where
    the instance will listen, which is what finds one that is down: MuMu
    reports port 0 until it is up, and `ensure_coc` is what brings it up.

    The pick is the first instance with the game running, else the first
    listed, so MuMu wins wherever both emulators have it.
    """
    store = ConfigStore()
    config = store.load()
    if config.adb_serial:
        wanted = AdbEndpoint.parse(config.adb_serial)
        for emulator, instance in listed:
            if wanted in (
                instance.endpoint,
                AdbEndpoint.parse(emulator.serial_for(instance.index)),
            ):
                return emulator, instance
        raise RuntimeError(
            f"設定的模擬器 {config.adb_serial} 不在任何模擬器列出的 instance 裡"
            ",有模擬器列不出 instance 的話,原因在 log 前面的 warning"
        )
    pairs = iter(listed)
    first = next(pairs, None)
    if first is None:
        raise RuntimeError("找不到任何模擬器 instance")
    emulator, instance = (
        first if first[1].coc_running else next((p for p in pairs if p[1].coc_running), first)
    )
    serial = emulator.serial_of(instance)
    logger.info("Driving %s instance %s at %s from now on", emulator.label, instance.index, serial)
    store.save(config.model_copy(update={"adb_serial": serial}))
    return emulator, instance


def _controller() -> AdbController:
    """The configured instance, with Clash of Clans already up on it."""
    emulator, instance = chosen(emulators())
    return emulator.controller(emulator.ensure_coc(instance.index).adb_serial)


def _session(frame_dir: Path | None = None) -> tuple[AdbController, DisplayTarget]:
    """What every headless loop starts from: the game's controller, its display, and a frame directory that exists.

    The directory is made here rather than left to the loop because every loop
    takes `frame_dir: Path | None` and reads None as "do not save"; one that
    is not None is expected to be writable by the first capture.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    return adb, adb.display_for(COC_PACKAGE)


# How long to give the emulator to actually take an instance down. MuMu's
# `control restart` returns as soon as the request is sent, so the state read a moment later is
# still the old one — and `ensure_coc` skips its whole boot wait for anything
# still reporting `android_started`, which would aim a launch at an
# emulator on its way down.
SHUTDOWN_POLLS = 15
SHUTDOWN_GAP = 2.0


def _await_shutdown(emulator: Emulator, index: int) -> None:
    """Wait for a restarting instance to really go down before it comes back up.

    An instance missing from the listing entirely is not treated as down: MuMu
    drops one for a moment while it restarts, and `ensure_coc` cannot start from
    there — it raises on an index it cannot find. So that keeps waiting, and
    only an instance that is listed and no longer started ends the wait.
    """
    for _ in range(SHUTDOWN_POLLS):
        time.sleep(SHUTDOWN_GAP)
        current = emulator.instance(index)
        if current is not None and not current.android_started:
            return
    logger.warning(
        "%s instance %s never went down; bringing the game up anyway", emulator.label, index
    )


# What this call did, for the two scopes that say it outright. `none` is left
# out because it reads off `was_running` instead, which is the only thing that
# scope can report: it does the same work either way, and on a cold machine that
# work is the whole job rather than the no-op the name suggests.
LAUNCH_LINES: dict[RestartScope, str] = {
    "game": "已重開部落衝突",
    "emulator": "已重開模擬器與部落衝突",
}


def launch_line(report: LaunchReport) -> str:
    """The one line a person reads off a launch."""
    did = LAUNCH_LINES.get(report.restart) or (
        "部落衝突已經在跑" if report.was_running else "已把部落衝突開起來"
    )
    if not report.at_village:
        did = f"{did},但村莊沒有出現,畫面可能還在載入"
    return f"{did},模擬器 {report.index} ({report.serial})"


def launch(restart: RestartScope) -> LaunchReport:
    """Bring the game up on the configured instance, tearing down as much as asked.

    Every other headless command assumes the game is already running: they go
    through `_controller`, which calls `ensure_coc` and gives up on whatever it
    cannot fix. This is that step on its own, for the two states it cannot reach
    from there — an emulator or a game that is up and no longer answering, which
    from here looks exactly like a working one.
    """
    emulator, found = chosen(emulators())
    index = found.index
    was_running = found.coc_running
    if restart == "emulator":
        logger.info("Restarting %s instance %s before bringing the game up", emulator.label, index)
        emulator.restart_instance(index)
        _await_shutdown(emulator, index)
    elif restart == "game":
        # The game can only be stopped on an emulator that is already up, which
        # is what the inner call is for. On a cold machine that call is also the
        # whole job and `restart_coc` then costs one relaunch of a game that had
        # only just started, which is cheaper than refusing and naming another
        # command: either way the caller asked to end up with a fresh game.
        emulator.restart_coc(emulator.ensure_coc(index))
    instance = emulator.ensure_coc(index)
    # `ensure_coc` is satisfied by a pid, which says the game is running and
    # nothing about whether it can be driven. Every command after this one aims
    # screen coordinates at it, and those were all measured against a village at
    # the far zoom — so this is where the game is brought to that state rather
    # than in each of them. The position is settled there too, by parking the
    # camera against a map edge — the pinch does not do it, whatever this used
    # to say.
    settled = _settle_game(emulator.controller(instance.adb_serial), RESTART_POLLS)
    # A game whose village never painted is reported rather than raised: the
    # process is up, so the caller may still have something to do with it, and
    # the one thing it must not do is assume the screen is ready.
    report = LaunchReport(
        index=instance.index,
        serial=instance.adb_serial,
        was_running=was_running,
        restart=restart,
        at_village=settled is not None,
    )
    logger.info("Launch: %s", launch_line(report))
    return report


def _planner(config: AppConfig, tier: Literal["main", "lite"] = "main") -> GeminiClient | None:
    """A client for one tier, or None so every caller falls back to its own way.

    None is not an error anywhere: the attack uses its fixed flank, the finders
    sweep, and the building namer leaves the name unread. That is what keeps a
    run with no key working exactly as it did before any of this existed.
    """
    try:
        key = SecretStore().load()
    except Exception:
        logger.warning("No saved API key could be read", exc_info=True)
        return None
    if not key:
        logger.info("No API key is saved; the loops will fall back to reading the screen alone")
        return None
    return GeminiClient(api_key=key, settings=getattr(config.gemini, tier))


class FrameTicker(BaseModel):
    """Saves a frame every few seconds for as long as a run lasts.

    The loop's own captures are the ones it reads, and it only looks where it has
    a question; between them a battle can go badly with nothing recorded at all.
    This is the other view — a fixed heartbeat, each frame named by how far into
    the run it was taken, so a recording lines up against the log afterwards.

    It costs the emulator a PNG encode every tick and competes with the loop for
    the same ADB connection, which is why it is off unless asked for.
    """

    adb: AdbController
    display: DisplayTarget
    out_dir: Path
    seconds: float

    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def __enter__(self) -> Self:
        """Start ticking, or do nothing at all when no interval was asked for."""
        if self.seconds > 0:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stop ticking and wait for a capture already in flight to be written."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.seconds + 30)

    def _tick(self) -> None:
        started = time.monotonic()
        while not self._stop.is_set():
            elapsed = time.monotonic() - started
            try:
                png = self.adb.screenshot(self.display)
            except Exception:
                # Loud, but never fatal: this is a recording of the run, not
                # part of it, and losing the battle to a missed frame is worse
                # than the gap in the recording.
                logger.warning("A heartbeat capture failed", exc_info=True)
            else:
                (self.out_dir / f"tick_{elapsed:07.1f}s.png").write_bytes(png)
            self._stop.wait(self.seconds)


# How long a round that attacked nothing waits before the next one is started.
# **The army is no longer one of the reasons.** Training is instant in the
# current game, so a camp under the threshold never fills and that round ends
# the series instead. What is left are the screens a fresh round would read the
# same way a minute sooner: no opponent above the thresholds, a popup over the
# village, an emulator that has just refused a command.
IDLE_REST = 60

# How long to give the game to paint a village before the world is decided.
# Sized for a launch rather than for a game already up: `ensure_coc` returns on
# a pid and the village follows about twenty seconds later, so a run started
# right after one used to read no village and end before round one.
WORLD_SETTLE_POLLS = 8

# Which village a handed-in plan was written for, by its kind.
_PLAN_WORLD: dict[type[AttackPlan | NightPlan], World] = {AttackPlan: "day", NightPlan: "night"}

# How often the wait between rounds looks up to see whether it has been stood
# down. Sleeping through the whole minute in one go would leave a stop unnoticed
# for most of it, which reads as a stop that did nothing.
STOP_POLL = 2.0

# How many rounds in a row may die on the emulator before the series gives up.
# One is a blip an overnight run should survive; three in a row is an emulator
# that has gone, and there is nothing to gain by aiming more rounds at it. Three
# is the line `farm` already draws for whoever is reading the log, rather than a
# number measured here.
ADAPTER_FAILURES = 3


# The claim this process wrote, or None while it holds none. Two things need it.
#
# A **missing** file means "somebody deleted it, stand down" only to a process
# that wrote one: a fresh machine has no state file at all, and without this
# every first run on it would read its own absence as a stop and end at zero
# rounds. And it carries `started` across exactly that deletion — the record
# that held it is gone by then, so the `idle` written on the way out would
# otherwise lose how long the run took, measured live on the first deletion
# this ever answered.
_held: RunnerState | None = None


def read_state() -> RunnerState | None:
    """What the state file says; None when there is no file at all.

    **Nothing in here raises, which is the whole point.** `should_stop` is
    called from inside a battle — `AttackRunner` polls it between opponents and
    `GameRunner._opened` after every tap — and the only thing catching anything
    up there is a `KeyboardInterrupt` handler, so an exception raised on a read
    would end the series with the army still on the field. The old flag was a
    `Path.exists()` and could not fail; this one parses JSON off a file two
    processes may be writing, so every way that can go wrong lands on "carry
    on" instead.

    **A missing file and an unreadable one are not the same answer.** Missing
    is a stop, because deleting it by hand is the escape hatch for somebody
    whose agent has died mid-run and whose only other move is the task manager.
    Unreadable is not: a write interrupted halfway leaves exactly that, and so
    does a read landing between another process's truncate and its write, so
    standing a farming run down over one is worse than ignoring it — the next
    claim rewrites the file anyway.
    """
    try:
        text = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        # Windows holds a file open against another process's write, so this is
        # a moment rather than a fault. Answering "no stop" costs one more poll.
        logger.warning("%s could not be read; carrying on", STATE_PATH)
        return RunnerState(status="running", pid=os.getpid())
    try:
        return RunnerState.model_validate_json(text)
    except ValueError:
        logger.warning("%s will not parse; carrying on as if nobody had asked", STATE_PATH)
        return RunnerState(status="running", pid=os.getpid())


def _write_state(state: RunnerState) -> None:
    """Replace the file rather than rewrite it, so no reader sees it half-built.

    `write_text` truncates before it writes, and something reads this every few
    seconds from another process; landing in that window returns zero bytes,
    which is a parse failure on a file that was never actually broken. The
    rename is atomic on Windows, so a reader gets the old state or the new one.

    **The scratch file carries this process's pid**, because the writers here
    are concurrent by design: `ai_coc stop` exists to be run against a loop that
    is itself writing at its claim and its release. One shared scratch name has
    them truncating each other's half-built file, and on Windows `os.replace`
    wants its source unopened — so the loser raises `PermissionError` out of
    `_release`, inside `claim`'s `finally`, which propagates past `run.answer`
    and leaves the run with no `result.json` at all. That file is the signal
    `farm` uses to tell a clean stop from a killed process.
    """
    scratch = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    scratch.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    os.replace(scratch, STATE_PATH)


@contextmanager
def claim(command: str, log: Path | None = None) -> Iterator[None]:
    """Say that this process is driving the emulator, and hand it back after.

    Every command that touches the emulator takes one, short ones included: a
    `collect` that runs for eight seconds still holds the screen for those
    eight, and a second session that reads the file wants the truth rather than
    only being told about the long runs. `read` takes none, since it parses a
    PNG and never opens ADB.

    **A claim this process already holds is left alone, and the outermost one
    owns the release.** The window runs every pass as its own `commands.*` call
    while the automation cycle carries on around them, so a release at the end
    of each pass would publish `idle` between passes and read, from outside, as
    an emulator nobody is using.

    **That test reads this process's own record and never the file**, because
    the file cannot answer it. A read that fails or comes back torn recovers as
    "running, and it is ours", which is the answer that keeps a battle from
    standing down over a moment — and taking it for a claim already held would
    have this yield without writing anything, leaving the run unrecorded and
    stoppable by nothing at all: `stop` would read the last run's `idle` and say
    there was nothing to stop, and deleting the file would not stop it either.

    **It never refuses.** A second run started while one is going overwrites
    this record and both then drive the same display, which is the thing the
    file exists to let a session avoid rather than a thing it prevents: the
    check belongs to whoever is about to start, and a lock that could refuse
    would need to tell a live run from a killed one, which is a pid liveness
    call this deliberately does not make.
    """
    global _held  # noqa: PLW0603 - process-wide by nature; see `_held`
    if _held is not None:
        yield
        return
    mine = RunnerState(
        status="running",
        pid=os.getpid(),
        command=command,
        started=datetime.now().astimezone(),
        log=log,
    )
    _write_state(mine)
    _held = mine
    try:
        yield
    finally:
        _release(command, log)


def _release(command: str, log: Path | None) -> None:
    """Write down that this run has finished, unless somebody else has claimed.

    The pid check is what keeps a run that outlived its own record from wiping
    out the one after it. A file deleted by hand is written back, because that
    deletion was a stop request and the `idle` landing here is its receipt: the
    file coming back is how whoever deleted it sees that the loop really stood
    down rather than ignored them.

    `stopping` becomes `idle` like any other ending. The request has been served
    by the time this runs, and leaving it standing would have the next reader
    believe a run is still winding down hours after it finished.
    """
    global _held  # noqa: PLW0603 - process-wide by nature; see `_held`
    held = read_state()
    if held is not None and held.pid not in (0, os.getpid()):
        return
    _write_state(
        RunnerState(
            status="idle",
            pid=os.getpid(),
            command=command,
            # This process's own copy rather than the file's, because the file
            # is gone whenever the run was stopped by deleting it, and that is
            # the case where the record would otherwise lose its start time.
            started=_held.started if _held else None,
            ended=datetime.now().astimezone(),
            log=log,
        )
    )
    _held = None


def stop() -> str:
    """Ask whichever command is driving the emulator to finish and stand down.

    Nothing here touches the game or looks for a process: this changes one field
    and ends. What actually stops is the loop, when it next looks, and where
    that is belongs to each of them — `attack` between rounds and between
    opponents, `walls` between batches and during the opening scan. Never
    mid-battle or mid-batch, because either one abandoned halfway leaves the
    game on a screen the next run does not know how to get home from.

    Saying so when there is nothing to stop is the half the old flag could not
    do: it wrote a file whether or not anything was listening, so a stop that
    landed and one that fell on an idle machine read identically.
    """
    state = read_state()
    if state is None or state.status == "idle":
        return "現在沒有指令在跑,沒有東西要停。"
    _write_state(state.model_copy(update={"status": "stopping"}))
    return (
        f"已要求 {state.command}(pid {state.pid})收工,狀態寫在 {STATE_PATH}。"
        "它會做完手上這一件事才停。"
    )


def stop_requested() -> bool:
    """Whether the command driving the emulator now should stand down.

    A file that is not there is only a stop **to a process that wrote one**.
    Otherwise a machine that has never run this — or a caller reaching `attack`
    without going through a claim, which every test does — reads its own
    absence as a stop and ends before it starts.
    """
    state = read_state()
    if state is None:
        return _held is not None
    return state.status == "stopping"


def _rest(seconds: float, should_stop: Callable[[], bool] = stop_requested) -> bool:
    """Wait between rounds, answering whether the wait was cut short.

    A stop that lands here is said out loud, because the state file goes back to
    `idle` on the way out and this line is then its only trace in `run.log`.
    """
    try:
        for _ in range(int(seconds / STOP_POLL)):
            if should_stop():
                logger.info("Stop requested between rounds; ending the series")
                return True
            time.sleep(STOP_POLL)
    except KeyboardInterrupt:
        return True
    return False


# How long to give a restarted emulator before deciding it is not coming back.
#
# **The wait is not for the launch, it is for a frame the loop could play from.**
# `launch("emulator")` returns as soon as `ensure_coc` has seen the game's
# process, and that check is a `pidof` — the process exists a couple of seconds
# after the launch while the village is not on screen for much longer than
# that. `current_world` is what settles it, because counting the plate row is
# how everything here tests for a village being up at all.
#
# Much longer than `restart_game`'s flat 15 seconds, which only reopens the
# package on an emulator that never went down; this sits through a cold boot.
#
# **And a cold boot is not a fixed cost.** Measured on the same machine within
# one run: the first restart had the village up 22 seconds after `launch`
# returned, and the second one had not got there in 120 — which is the whole
# reason this feature exists, since an emulator slow enough to need restarting
# is also slow to come back. So the patience is sized for the bad case rather
# than the good one; a run that reaches the end of it has lost the series, while
# one that waits an extra minute has lost a minute.
RESTART_POLLS = 45
RESTART_POLL_GAP = 4.0


def _settle_game(
    adb: AdbController, polls: int, should_stop: Callable[[], bool] = lambda: False
) -> DisplayTarget | None:
    """Wait for a village that can be tapped, then put the camera where the coordinates are.

    Two steps that always belong together, because every coordinate in this
    project was measured against one particular view: a village on screen, at
    the game's far zoom limit. A caller that has one without the other has a
    game that answers and misses everything it aims at.

    **The scale and the position are two different things, and the park owns
    both.** This file used to say the far limit centred the village too, on the
    reasoning that the map clamps the camera at its own edges; measured, a pinch
    does not move the camera at all. The clamp is real and the pinch simply
    never reaches it, so `park_camera` runs the camera into a corner as well —
    and it pinches on the way in rather than leaving that here, because its own
    walk can only be read at the far zoom. The village this landed on is already
    read by then, which is what the park needs to know, since the two maps clamp
    in opposite corners.

    None means the village never appeared. Whether that is worth giving up over
    is the caller's decision, not this one's.
    """
    waiting = "nothing was tried"
    restarted = False
    for _ in range(polls):
        # Checked inside the wait rather than only around it: this is the
        # longest stretch of a run where nothing else reads the state, and a
        # stop that takes two minutes to show reads as one that did nothing.
        if should_stop():
            logger.info("Stop requested while the game was coming up")
            return None
        # Which of the two it is waiting on gets logged, because the two mean
        # different things and a run that gives up says neither: no display is a
        # game with no window yet, while a display whose frame will not read is
        # a game that is up and still on its loading screen. Silence here left
        # one real failure — the second restart of a live run — with nothing to
        # tell those apart afterwards.
        try:
            display = adb.display_for(COC_PACKAGE)
        except AdbControlError:
            waiting = "the game is not on a display yet"
        else:
            # `current_world` rather than `read_stock`, and **either village
            # counts** — what this is waiting for is a game that has finished
            # painting, not a particular one of the two. Which village a restart
            # landed on is not this wait's to settle: nothing crosses on its own,
            # a loop that finds the other village says so, and `launch` reports
            # `at_village` rather than promising the home one.
            png = adb.screenshot(display)
            if (world := current_world(png)) is not None:
                # The scale and the position together, and the pinch that used
                # to be written here is inside the park now: the walk it does is
                # only bounded at the far zoom, so it establishes that itself
                # rather than trusting whoever called it. The village this
                # landed on is already in hand, which is both things the park
                # needs — that this is a village at all, and which of the two,
                # since the maps clamp in opposite corners.
                #
                # **Its answer is deliberately not acted on here.** None from
                # this function means no village appeared, and `attack` ends the
                # whole series on it; a park that fell short is a worse view
                # rather than no village, and nothing this hands back depends on
                # it — the attack menu is a fixed screen corner and the battle
                # camera is measured per battle by `_settle_camera`. The park
                # logs its own warning, which is where that belongs.
                park_camera(adb, display, world)
                return display
            # A session the server dropped would otherwise sit under its dialog
            # for the whole wait, since nothing else here answers one. Once,
            # because a restart that lands back on the dialog is the server
            # still down, and the loading screen after it is the state to wait
            # in rather than restart out of: measured across two outages,
            # neither a game nor an emulator restart shortened one.
            if idle_disconnected(png):
                waiting = "the session is dropped and the one restart did not clear it"
                if not restarted:
                    logger.warning("The session was dropped; restarting the game")
                    restart_game(adb, display)
                    restarted = True
                    waiting = "the game is being restarted"
            elif loading_screen(png):
                waiting = "the game is on its loading screen"
            else:
                waiting = "the village has not painted yet"
        logger.debug("Still waiting for the game: %s", waiting)
        time.sleep(RESTART_POLL_GAP)
    logger.warning(
        "Gave up after %.0fs waiting for the game: %s", polls * RESTART_POLL_GAP, waiting
    )
    return None


def _restart_emulator(
    runner: AttackRunner, ticker: FrameTicker, should_stop: Callable[[], bool] = stop_requested
) -> bool:
    """Restart the emulator and the game, and point the run at what came back.

    MuMu drops frames after running for a while and nothing short of this clears
    it. What makes it more than one `launch` call is that everything still
    holding the old emulator has to be told — the display above all, because
    MuMu opens the game on a display of its own choosing and nothing promises it
    picks the same one. An `input tap` aimed at the old one lands silently on
    the launcher, which is the exact failure the `-d` flags exist to prevent.

    The controller is built from the serial `launch` already resolved rather
    than through `_controller()`, which would enumerate the instances a second
    time and fire another launch at a game that had only just come up.

    False means the village never appeared. What to do about that is the
    caller's call, since it is a decision about the series rather than about the
    emulator.
    """
    # Wider than it looks, and deliberately so: `launch` raises `RuntimeError`
    # for an instance MuMu has dropped from its listing and `EmulatorError` for
    # a game that never came up, both of which are exactly the state this is here
    # to recover from. Letting either escape would take the whole series with it
    # — `cli.py` never reaches `run.answer` and `result.json` is left empty,
    # which is the failure the False path below exists to avoid.
    try:
        launched = launch("emulator")
    except (RuntimeError, KeyboardInterrupt):
        logger.exception("The emulator did not come back up")
        return False
    adb = AdbController(endpoint=AdbEndpoint.parse(launched.serial))
    # The camera comes back out in here too, and that is not a courtesy. A
    # restarted game does not return at the zoom everything was measured at:
    # observed live, the restart succeeded, the village read, and every battle
    # afterwards deployed nothing at all — two rounds of `0 of 4 hero card(s)
    # landed` before a recorded frame showed a battlefield zoomed most of the
    # way in.
    display = _settle_game(adb, RESTART_POLLS, should_stop)
    if display is None:
        return False
    # Both objects. The ticker captures from its own thread and would otherwise
    # spend the rest of the night timing out against a display that no longer
    # exists, logged as warnings nobody is reading.
    runner.adb = adb
    runner.display = display
    ticker.adb = adb
    ticker.display = display
    # That the village reads at all is the signal; what it reads is not, and
    # logging the number would present it as one. Measured on a live restart,
    # the storage bars animate up from zero while the game loads and the first
    # frame that resolved came back at 12.4M gold against a real 19.7M —
    # printed here, that reads as a village raided overnight.
    logger.info("The emulator is back and the village is on display %s", display.logical_id)
    return True


def _prepare_frames(options: AttackOptions) -> None:
    """Make room for whatever this run was told to keep, and refuse what it cannot.

    A heartbeat with nowhere to write is the one combination that has to fail
    loudly rather than quietly recording nothing: `--shot-every` is asked for by
    somebody who wants to look at the frames afterwards.
    """
    if options.frame_dir is not None:
        options.frame_dir.mkdir(parents=True, exist_ok=True)
    elif options.shot_every > 0:
        raise ValueError("--shot-every 要搭配 --record，不然心跳畫面沒有地方放")


def _restarted(
    runner: AttackRunner,
    ticker: FrameTicker,
    fought: int,
    every: int,
    should_stop: Callable[[], bool] = stop_requested,
) -> int | None:
    """How many battles to carry forward, or None when the emulator never returned.

    `fought` unchanged where no restart was due, and zero after one. The
    decision, the restart and the reset are one thought, and keeping them in one
    place is also what keeps `attack` under the complexity this repo lints for.
    """
    if not every or fought < every:
        return fought
    logger.info("%d battle(s) fought; restarting the emulator", fought)
    # **The restart is the other place an adapter error can take the series
    # down with the process**, and it is not covered by the guard around the
    # round itself: `_restart_emulator` guards the launch, while the
    # `_settle_game` that follows captures, pinches and parks, and any of those
    # can raise. Ending the series is already what this function says with
    # None, so the error needs no new answer — only somewhere to be caught, so
    # that `result.json` is still written with the rounds already played.
    try:
        restarted = _restart_emulator(runner, ticker, should_stop)
    except AdbControlError as exc:
        logger.error("The emulator did not come back: %s", exc)
        return None
    if restarted:
        return 0
    # The series ends either way, but only one of these is an alarm: a stop
    # asked for mid-restart comes back the same False as an emulator that never
    # returned, and an error line that cries wolf on an ordinary `ai_coc stop`
    # is worth less than no error line at all.
    if not should_stop():
        logger.error("The village never came back after the restart; stopping here")
    return None


def _write_plan(path: Path | None, plan: AttackPlan | NightPlan | None) -> None:
    """Write down the plan that actually ran, so the battle can be repeated.

    Both halves are optional and neither is an error: nobody asked for a copy,
    or the round ended before there was a plan to copy. Answering that here
    rather than at the call site keeps the round's own code to what it does.
    """
    if path is None or plan is None:
        return
    # A path the caller chose is a path they meant, so make room for it rather
    # than failing on a directory they have not made yet.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Wrote the plan that ran to %s", path)


def _log_plan(path: Path | None, played: int, plan: AttackPlan | NightPlan | None) -> None:
    """Append one round's tactic to the run's plan log.

    Appended rather than rewritten, and one file rather than one per round:
    `--repeat 0` runs until it is stopped, so a night of farming is a hundred
    rounds or more and a hundred small files is a directory nobody opens.

    A round that ended before there was a plan writes nothing, which is not an
    error — no opponent above the thresholds, an army under `MIN_ARMY_RATIO`,
    the attack menu not opening. Its round number is simply missing from the
    log, and `result.json` is where what happened instead is recorded.
    """
    if path is None or plan is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(PlayedPlan(round=played, plan=plan).model_dump_json() + "\n")


# Only ever used to build the `Attack finished:` line a person reads; the field
# a caller decides on is `AttackReport.outcome`. **The four that fought are kept
# distinguishable in the wording too**, because `run.log` is where a farming
# session looks first and the two that used to read alike sent readers to
# opposite halves of the code.
ROUND_LINES: dict[AttackOutcome, str] = {
    "took_loot": "已進攻並回營",
    "deployed": "兵出去了並回營;這個世界沒有戰利品面板可以判斷搶到多少",
    "loot_unread": "打完回營了,但整場都讀不到戰利品面板,成果無從判斷",
    "no_loot": "打完回營了,但整場戰利品沒有變化 —— 部隊有出去,這一場沒搶到東西",
    "nothing_deployed": "沒有任何一張卡片出得去,部隊沒有成功部署",
    "army_short": "兵力不足,沒付搜尋費就退出來",
    "stock_full": "倉庫都滿過設定的百分比,停止刷資源",
    "no_opponent": "等不到對手,已放棄這一輪搜尋",
    "all_skipped": "跳過的對手都未達門檻,已結束搜尋",
    "stopped": "收到停止要求,未開打就離開",
    "no_attack_menu": "沒有開啟攻擊選單就停手",
    "server_loading": "遊戲卡在載入畫面,伺服器可能連不上,這一輪停手",
    "server_flapping": "遊戲又回到載入畫面,伺服器可能還連不上,這一輪停手",
    "emulator_silent": "模擬器沒有回應",
    "other_village": "遊戲停在另一個村莊,這一批不打那邊;換村莊用 ai_coc world --go",
}


def round_line(report: AttackReport) -> str:
    """One round as a line for a person, built from the report's own fields.

    Here rather than carried on the model, for the reason `stock_of` gives. The
    numbers are appended rather than written into each sentence because they are
    orthogonal to how the round ended: a forced battle can take loot or deploy
    nothing, and a round that skipped forty opponents can still end any of the
    ways `AttackOutcome` lists.
    """
    line = ROUND_LINES[report.outcome]
    if report.forced:
        line = f"倒數結束被強制開戰,{line}"
    if report.skipped:
        line = f"{line}(跳過 {report.skipped} 個對手)"
    if report.phases > 1:
        line = f"{line}(共出兵 {report.phases} 次)"
    return line


def _round(runner: AttackRunner, series: AttackSeries) -> AttackReport | None:
    """One round, with both of the ways it can end badly folded in. None ends the series.

    The two are not the same kind of thing and they are here together because
    the caller does the same thing with either: a `KeyboardInterrupt` is a
    person asking for the run to stop, while an `AdbControlError` is the
    emulator having stopped answering. What they share is that neither may
    reach `main()`, because nothing up there catches one and the series and its
    `result.json` go down with the process.
    """
    try:
        report = runner.run()
    except KeyboardInterrupt:
        logger.info("Interrupted; stopping after %d round(s)", len(series.root))
        return None
    except AdbControlError as exc:
        return _lost_round(runner, series, exc)
    runner.lost = 0
    return report


def _lost_round(
    runner: AttackRunner, series: AttackSeries, exc: AdbControlError
) -> AttackReport | None:
    """Record a round that died on the emulator, or None once the series should end.

    **An adapter error used to end the process rather than the series, and
    losing the record was the expensive half.** Nothing above `main()` catches
    one, so `result.json` was never written and the rounds already played were
    countable only out of `run.log`: seven runs died that way between 2026-09-13
    and 2026-09-15, one of them on a round it had just won outright. The trigger
    behind those seven is guarded now, inside `tap_many`, but the next one will
    have a different cause — a screencap that timed out, a display the game was
    moved off — so what is worth fixing is the loss rather than any one cause.
    The round goes into the series as a round that happened and failed, which is
    what a reader counting rounds afterwards needs it to be.

    **Three in a row rather than one.** A single timeout is a blip an overnight
    run should survive, and the next round opens with `_settle_game`, which is
    built to pick a game up from wherever the last one left it. An emulator that
    has really gone answers nothing, and there is no reason to keep aiming
    rounds at it. Three is the line `farm` already draws for whoever is reading
    the log rather than a number measured here, so the code draws it in the same
    place.
    """
    runner.lost += 1
    logger.error(
        "Round %d died on the emulator (%d in a row): %s", len(series.root) + 1, runner.lost, exc
    )
    report = AttackReport(world=runner.world, outcome="emulator_silent")
    if runner.lost >= ADAPTER_FAILURES:
        # Recorded here rather than handed back, because the caller records
        # what it is given and this one ends the series instead of reaching it.
        # It is still a round that happened, and leaving it out would take one
        # round off the count this whole function exists to preserve.
        series.root.append(report)
        # The caller breaks before its own `Attack finished:` line, and
        # `references/running.md` tells a session there is one of those per
        # round — so without this the last round is in `result.json` and
        # missing from the log, an undercount of exactly what this preserves.
        logger.info("Attack finished: %s", round_line(report))
        logger.error("The emulator has not answered for %d rounds; ending the series", runner.lost)
        return None
    # Handed back as an ordinary round that fought nothing, which is what it
    # was. The caller then records it, rests between rounds, and comes round
    # again — and that rest is worth more here than anywhere else, since an
    # emulator that just refused a command is the last thing to fire another
    # one at immediately.
    return report


def _series_over(report: AttackReport, played: int) -> bool:
    """Whether this round's outcome stands the whole series down, and says why.

    Three outcomes do, and all are read off the village before the search fee
    is charged. A full storage is the goal being met, and `farm` owns where that
    leads; there is nothing left for the round loop to farm for either way. The
    game standing on the other village is the third: nothing here sails, so
    every round after would find it there too.

    **An army short of the camp is the other one, and it is a fault rather than
    a goal.** The wait between rounds was written for an army still training,
    where a round that backed out would have been over the line a few minutes
    later. Training is instant in the current game, so nothing about the camp
    changes between rounds: measured 2026-09-22, a limited-time event ended and
    took its own troops out of the saved army, leaving 20 of 340 against a
    threshold of 306, and a `--repeat 0` run then walked the same menus once a
    minute and attacked nothing at all. Only the player can re-arm that army, so
    the run says so and stands down rather than filling the night with rounds
    that cannot fight.
    """
    if report.stock_full:
        logger.info("The storages are full; there is nothing left to farm for")
        return True
    if report.outcome == "other_village":
        return True
    if report.outcome == "army_short":
        logger.error(
            "The army is under half the camp and training is instant, so this will not clear "
            "on its own; ending the series after %d round(s)",
            played,
        )
        return True
    return False


def _handed_plan(options: AttackOptions) -> tuple[AttackPlan | NightPlan | None, World | None]:
    """The plan `--plan` handed in, and the village it was written for, which the series plays."""
    if options.plan is None:
        return None, None
    plan = plans.load_any(options.plan)
    return plan, _PLAN_WORLD[type(plan)]


def attack(
    options: AttackOptions, should_stop: Callable[[], bool] = stop_requested
) -> AttackSeries:
    """The attack loop, with no window in the way, for as many rounds as asked.

    Thresholds, storage limits and the model to plan with all come from the
    shared config file, so a run started here plays the same way as one from
    the window — the storage limits included, which means a full village stands
    this down before it searches. `minimums` overrides the thresholds alone,
    which is how a loop being studied gets the old behaviour back: all three at
    zero is "attack the first opponent shown", and skipping nothing is what puts
    the code worth watching on screen.

    `plan` replaces the AI entirely — the loop plays that file and asks for
    nothing — and `plan_out` writes down whichever plan actually ran, so a battle
    worth repeating can be repeated and one worth arguing with can be edited.
    A plan is written for one village, so the series plays that one and ends
    at once on the other. Without a plan it plays whichever the game is on, and
    in neither case does it sail: crossing is `ai_coc world --go`.

    `rounds` of 0 keeps going until it is stopped, which is what watching the
    loop play needs: a tactic is judged over a run of battles rather than one,
    and the interesting ones are the battles nobody was sitting there to start.
    Either `ai_coc stop` or a Ctrl-C ends the series rather than the process, so
    the rounds already played are still reported. The state file is the one that
    reaches a run put in the background, which nothing can send a Ctrl-C to.

    **`should_stop` is what lets the window run this same function.** It has a
    stop button rather than a terminal, and the button has to reach a battle
    already under way; a run started there passes a condition that answers to
    both, so `ai_coc stop` from a terminal still ends a window's round. Nothing
    replaces the state file — the default reads it, and the `claim` around this
    call is what marks the run as under way for anybody looking from outside.
    """
    plan, world = _handed_plan(options)
    adb = _controller()
    _prepare_frames(options)
    config = ConfigStore().load()
    # **Which village to play cannot be asked until one has painted.**
    # `_controller` is satisfied by a pid, so a run started right after a launch
    # reaches here with the loading screen still up — measured, one did so three
    # seconds in, read no village at all, and ended the whole series before round
    # one. This is the same wait a restart already does, and it leaves the camera
    # at the far zoom on the way past, which every coordinate below wants anyway.
    display = _settle_game(adb, WORLD_SETTLE_POLLS, should_stop) or adb.display_for(COC_PACKAGE)
    runner = AttackRunner(
        adb=adb,
        display=display,
        world=world,
        thresholds=options.minimums.over(config.thresholds),
        # One setting for both villages, because it is a share of whatever the
        # storages hold rather than an amount: the runner reads each village's
        # own ceilings off its bars, so the same 90% means one thing here and
        # another on the builder base without anybody typing either number.
        # The flag overrides it for one run, like `restart_every` below.
        stop_at=config.stop_at if options.stop_at is None else options.stop_at,
        ai=None if plan else _planner(config),
        plan=plan,
        should_stop=should_stop,
        frame_dir=options.frame_dir,
    )
    series = AttackSeries()
    rounds = options.rounds
    # The file is the default and the flag overrides it for one run, the same
    # shape the loot thresholds already have.
    restart_every = (
        config.restart_every if options.restart_every is None else options.restart_every
    )
    # Battles since the last restart rather than rounds over the whole series,
    # for the reason `AttackOptions.restart_every` gives: a round that found
    # nobody to fight did not tire the emulator out.
    fought = 0
    with FrameTicker(
        adb=adb, display=display, out_dir=options.frame_dir or Path(), seconds=options.shot_every
    ) as ticker:
        while rounds <= 0 or len(series.root) < rounds:
            # Between rounds, which is the cheapest place to stop: the village is
            # on screen, nothing is deployed, and the rounds already played are
            # in the series either way.
            if should_stop():
                logger.info(
                    "Stop requested; ending the series after %d round(s)", len(series.root)
                )
                break
            # And the cheapest place to restart, for the same reasons. After the
            # stop check rather than before it, because a run being stood down
            # has no use for a fresh emulator.
            carried = _restarted(runner, ticker, fought, restart_every, should_stop)
            if carried is None:
                # Everything after this would be aimed at an emulator that never
                # came back, so the series ends here holding the rounds it really
                # played rather than raising and taking them with it. Why it
                # ended is logged where the two reasons can still be told apart.
                break
            fought = carried
            logger.info("Round %d of %s", len(series.root) + 1, rounds or "no limit")
            report = _round(runner, series)
            if report is None:
                break
            # **A restart moves the game to a display of MuMu's choosing, and
            # this function is the last to hear about it.** `AttackRunner`
            # reassigns its own `self.display` when the idle-disconnect dialog
            # sends it through `restart_game`, so it keeps playing; the local
            # below and the ticker's copy go on naming a display the game has
            # left, and `screencap -d` against one answers `Failed to take
            # screenshot. Status: -2`.
            #
            # Measured on two consecutive night series, both a few battles after
            # an idle-disconnect restart — display 2 to 3 on one and 3 to 4 on
            # the next. Both died on the trip to the loot cart this loop used to
            # make between rounds, with `result.json` never written, so the 17
            # and 14 battles they had played were countable only out of
            # `run.log`.
            #
            # **`_restart_emulator` does not already cover this.** It re-points
            # `runner` and `ticker` and says why, but it never touches this
            # function's own pair either. Syncing here rather than at the call
            # covers both restarts and whatever consumer is added to this loop
            # next.
            #
            # Not every `Status: -2` is this: one recorded series lost a display
            # with no restart at all and died in `uncovered`, where the runner's
            # own copy is the stale one. That one is still open.
            adb, display = runner.adb, runner.display
            ticker.adb, ticker.display = adb, display
            series.root.append(report)
            # A builder base round reports no `attacked` — there is no scout
            # screen to have advertised any loot — so the two villages answer
            # "did this round really fight" differently and the restart counter
            # and the wait between rounds both have to ask both ways.
            fighting = report.attacked is not None or report.phases > 0
            if fighting:
                fought += 1
            logger.info("Attack finished: %s", round_line(report))
            _write_plan(options.plan_out, runner.played)
            _log_plan(options.plan_log, len(series.root), runner.played)
            if _series_over(report, len(series.root)):
                break
            # A stop that arrived mid-search comes back here having attacked
            # nothing, and a run about to walk away has no reason to wait
            # first. Falling through to the loop's own check is what logs why
            # the series ended, and `run.log` is the only thing a background run
            # leaves to read while it is still going.
            if not fighting and not should_stop() and (rounds <= 0 or len(series.root) < rounds):
                logger.info("Nothing was attacked; waiting %ds before the next round", IDLE_REST)
                if _rest(IDLE_REST, should_stop):
                    break
    return series


# Only ever used to build the line a person reads; `outcome` is what a caller
# decides on. Both surveys measure the home village's battlefield and nothing
# here sails, so on the builder base they stop before spending anything —
# run there, one would walk the menus of a village it cannot fight on.
SURVEY_LINES: dict[SurveyOutcome, str] = {
    "surveyed": "量完了:{result}",
    "builder_base": "遊戲停在夜世界,量的是主村的戰場;要量先跑 ai_coc world --go day",
}


# Twelve rays is the whole village at 30 degree steps, and two drops on each is
# what fits: a refused drop costs no troop but does cost the capture that reads
# the card afterwards, so a wider sweep runs past the end of the battle.
SURVEY_RAYS = tuple(angle * 30 for angle in range(12))
# How far either side of the predicted line the two drops go. Wider than the
# margin `fitted_line` already adds, so a ray that agrees really does straddle it.
SURVEY_MARGIN = 60


class _BoundarySurvey(AttackRunner):
    """An attack that surveys the deployment boundary instead of fighting.

    The boundary reader is the least certain thing in the loop: its colour
    thresholds were measured on four village themes and the map diamond was
    calibrated by eye. This is how to find out whether it still agrees with the
    game — on a new theme, after a game update, or when a battle goes wrong for
    reasons the recorded frames do not explain. It costs a battle that is thrown
    away, which costs nothing but the time.
    """

    survey: BoundarySurvey = BoundarySurvey(outcome="surveyed")

    def _deploy(self, frame: bytes) -> None:
        card = card_groups(frame)[0][0]
        battle = self._wait_for_battle()
        if battle is None:
            logger.warning("Never reached the battle; nothing to survey")
            return
        for degrees in SURVEY_RAYS:
            reach = boundary_reach(battle, degrees)
            if reach is None:
                self.survey.unread.append(degrees)
                logger.info("Ray %.0f: the reader found no boundary", degrees)
                continue
            radius = math.hypot(reach[0] - VILLAGE_CENTRE[0], reach[1] - VILLAGE_CENTRE[1])
            dx, dy = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
            verdicts: list[bool] = []
            for offset in (-SURVEY_MARGIN, SURVEY_MARGIN):
                point = (
                    round(VILLAGE_CENTRE[0] + dx * (radius + offset)),
                    round(VILLAGE_CENTRE[1] + dy * (radius + offset)),
                )
                before = self._frame(f"{degrees:03.0f}deg-{offset:+d}-before")
                self.adb.tap_many([(card, CARD_ROW_Y), point], self.display, gap=SINGLE_DROP_DELAY)
                time.sleep(DROP_SETTLE)
                after = self._frame(f"{degrees:03.0f}deg-{offset:+d}")
                # The card is what the survey measures, because the warning
                # banner it used to read is raised by four different things and
                # not raised at all by a tap the game simply swallows.
                verdicts.append(not card_drained(before, after, [card]))
            ray = ProbeRay(
                degrees=degrees,
                predicted=round(radius),
                inside_refused=verdicts[0],
                outside_refused=verdicts[1],
            )
            self.survey.rays.append(ray)
            logger.info(
                "Ray %.0f: predicted %d, inside %s, outside %s -> %s",
                degrees,
                ray.predicted,
                "refused" if ray.inside_refused else "ACCEPTED",
                "refused" if ray.outside_refused else "accepted",
                "agrees" if ray.agrees else "DISAGREES",
            )


def probe(frame_dir: Path | None = None) -> BoundarySurvey:
    """Survey where drops are really accepted, and compare it to what the reader says.

    Spends a battle to answer one question the recorded frames cannot: whether
    the red line the parser found is the line the game is enforcing.
    """
    adb, display = _session(frame_dir)
    if _village_now(adb, display) == "night":
        survey = BoundarySurvey(outcome="builder_base")
        logger.warning("Boundary survey: %s", SURVEY_LINES[survey.outcome])
        return survey
    runner = _BoundarySurvey(
        adb=adb, display=display, thresholds=LootThresholds(), frame_dir=frame_dir
    )
    runner.run()
    logger.info(
        "Boundary survey: %s", SURVEY_LINES["surveyed"].format(result=runner.survey.agreement)
    )
    return runner.survey


# Six rays, picked for where the map's own edge falls inside the playfield. The
# vertical pair runs off the top and bottom of the screen long before it reaches
# the diamond, so it would measure the UI rather than the map.
MAP_RAYS = (0.0, 45.0, 135.0, 180.0, 225.0, 315.0)
# How far each probe steps back in from the screen edge, and how many it may take
# before the ray is given up on. A refused probe costs no troop, only the capture
# that reads the card, so the limit is the battle's own three minutes.
MAP_STEP = 45
MAP_PROBES = 8


class _MapSurvey(AttackRunner):
    """An attack spent measuring how far out the game will still take a drop.

    `DEPLOY_BOUND` was calibrated by overlaying candidates on live frames until
    they sat on the ground's own edge, which is the sort of number a screenshot
    cannot argue with. This is the argument: walk inwards along a ray until a
    troop lands, and that is where the map really ends. It is worth re-running
    after a game update, a theme that repaints the ground, or any change to the
    emulator's resolution.
    """

    survey: MapSurvey = MapSurvey(outcome="surveyed")
    # Which cards the probes may spend, read once off the full row.
    _troops: list[int] = PrivateAttr(default_factory=list)

    def _edge(self, degrees: float, shot: bytes) -> tuple[MapEdge | None, bytes]:
        """Walk one ray inwards from the screen edge until a drop lands."""
        left, top, right, bottom = PLAYFIELD
        dx, dy = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        # The furthest this ray gets before the playfield stops it.
        limit = min(
            (edge - VILLAGE_CENTRE[axis]) / step
            for axis, step, edge in (
                (0, dx, right if dx > 0 else left),
                (1, dy, bottom if dy > 0 else top),
            )
            if abs(step) > 1e-9
        )
        for probe in range(MAP_PROBES):
            radius = limit - probe * MAP_STEP
            if radius <= 0:
                break
            point = (
                round(VILLAGE_CENTRE[0] + dx * radius),
                round(VILLAGE_CENTRE[1] + dy * radius),
            )
            card = next(iter(live_cards(shot, self._troops)), None)
            if card is None:
                logger.info("Every troop card is spent; the survey stops here")
                return None, shot
            self.adb.tap_many([(card, CARD_ROW_Y), point], self.display, gap=SINGLE_DROP_DELAY)
            time.sleep(DROP_SETTLE)
            before, shot = shot, self._frame(f"{degrees:03.0f}deg-{round(radius):04d}")
            if card_drained(before, shot, [card]):
                logger.info("Ray %.0f: accepted at %d of %d", degrees, radius, limit)
                # Accepted at the very first probe means the screen ran out
                # before the map did, so this ray measured the playfield.
                return MapEdge(
                    degrees=degrees,
                    reached=None if probe == 0 else round(radius),
                    predicted=round(limit),
                ), shot
        logger.info("Ray %.0f: nothing landed anywhere along it", degrees)
        return None, shot

    def _deploy(self, frame: bytes) -> None:
        self._troops = card_groups(frame)[0]
        battle = self._wait_for_battle()
        if battle is None:
            logger.warning("Never reached the battle; nothing to survey")
            return
        shot = battle
        for degrees in MAP_RAYS:
            edge, shot = self._edge(degrees, shot)
            if edge is not None:
                self.survey.edges.append(edge)


def bounds(frame_dir: Path | None = None) -> MapSurvey:
    """Spend a battle finding where the map really ends, and fit a diamond to it."""
    adb, display = _session(frame_dir)
    if _village_now(adb, display) == "night":
        survey = MapSurvey(outcome="builder_base")
        logger.warning("Map survey: %s", SURVEY_LINES[survey.outcome])
        return survey
    runner = _MapSurvey(adb=adb, display=display, thresholds=LootThresholds(), frame_dir=frame_dir)
    runner.run()
    logger.info("Map survey: %s", SURVEY_LINES["surveyed"].format(result=runner.survey.summary))
    return runner.survey


# Only ever used to build a log line; `WallReport.outcome` is what a caller
# reads. **`nothing_bought` is the one that must not name a cause**, because it
# is by definition the one the loop cannot explain — and it has two routes, not
# one. The town hall capping every wall found is the likeliest, and the other is
# a builder counter that would not read: `WallRunner` only takes the
# builders-busy exit on a count it could resolve, so an unread one drops every
# candidate and lands here. Those two want opposite next steps — wait for a
# workman against stop running `walls` at all — so the line points at the record
# rather than at a reason.
WALL_LINES: dict[WallOutcome, str] = {
    "bought": "升級了 {walls} 面城牆",
    "nothing_bought": "每一個位置都沒有買成,而這個迴圈說不出原因;紀錄裡有各自停在哪一步",
    "cannot_afford": "剩下的資源買不起下一批城牆",
    "builders_busy": "工人都在忙,遊戲不讓升級城牆;跑 ai_coc builders 看最快的還要多久",
    "no_walls_found": "找不到任何城牆",
    "no_village": "畫面沒辦法回到村莊,城牆升級沒有開始",
    "builder_base": "遊戲停在夜世界,城牆只在主村升;要升先跑 ai_coc world --go day",
    "no_stock": "看不到村莊的儲量,先停下來",
    "stopped": "收到停止要求,已經買成 {walls} 面城牆",
}


def walls(options: WallOptions, should_stop: Callable[[], bool] = stop_requested) -> WallReport:
    """Spend the storages on wall upgrades, with no window in the way.

    Walls upgrade the instant they are paid for and tie up no builder, so this is
    what a village does with loot it has nowhere else to put — every builder busy
    and both storages filling towards the point where the attack loop stands
    itself down.

    `ai_coc stop` ends this one too, between batches. A run with `--rounds 0`
    against a village full of walls is the other loop here that goes on long
    enough to be worth interrupting, and it answers the same file rather than a
    second mechanism of its own. `should_stop` is what the window passes so its
    button reaches the scan as well; see `attack` for why the state file stays
    the default rather than being replaced.
    """
    adb, display = _session(options.frame_dir)
    runner = WallRunner(
        adb=adb,
        display=display,
        keep_gold=options.keep_gold,
        keep_elixir=options.keep_elixir,
        rounds=options.rounds,
        at=options.at,
        # Skipped when the run was told where the walls are: the whole point of
        # asking is to find them, and a caller that named them has already
        # looked at the screen.
        ai=None if options.at else _planner(ConfigStore().load()),
        should_stop=should_stop,
        frame_dir=options.frame_dir,
    )
    report = runner.run()
    # **The runner names the stop itself now**, which is what took a rewrite
    # out of here: this used to prefix 已停止 onto whatever sentence the loop had
    # written, and the exception it needed — a run stopped before it bought
    # anything, whose own fallback reads as a verdict on positions it never
    # tried — is exactly the ambiguity `WallOutcome` exists to remove.
    logger.info(
        "Walls: %s (金幣 %d／聖水 %d)",
        WALL_LINES[report.outcome].format(walls=report.walls),
        report.paid("gold"),
        report.paid("elixir"),
    )
    return report


BUILDER_LINES: dict[BuilderOutcome, str] = {
    "read": "工人 {free}/{total},{running} 個升級在跑,最快的還要 {soonest}",
    "idle": "工人 {free}/{total},沒有在跑的升級",
    "no_village": "畫面沒辦法回到村莊,讀不到工人",
    "builder_base": "遊戲停在夜世界,這裡讀的是主村的工人;夜世界的用 ai_coc worker",
    "count_unread": "讀不到工人數量,先停下來",
    "panel_shut": "工人 {free}/{total},但工人面板打不開",
}


def builder_line(report: BuilderReport) -> str:
    """The one line a person reads off a builder panel."""
    remaining = report.queue.remaining
    return BUILDER_LINES[report.outcome].format(
        free=report.free,
        total=report.total,
        running=report.queue.running,
        soonest=spell_out(remaining[0]) if remaining else "",
    )


def builders(frame_dir: Path | None = None) -> BuilderReport:
    """Say who is building what and how much longer, with no window in the way.

    The one thing a village cannot be talked out of is a busy builder, and until
    now nothing here could see past the 1/5 to how long that would last. It is
    read-only and costs three captures, so it is cheap enough to ask before
    deciding whether a run is worth starting at all.
    """
    adb, display = _session(frame_dir)
    report = UpkeepRunner(adb=adb, display=display, frame_dir=frame_dir).builders()
    logger.info("Builders: %s", builder_line(report))
    return report


def stock(frame_dir: Path | None = None) -> StockReport:
    """What the village on screen is holding, and how close that is to full.

    **The one thing nothing here could answer without starting a run.** Every
    loop that farms or spends reads the storages on its way past, but those
    numbers only ever reached a caller as a line in that run's log — so a
    session that wanted to know where a village stood had to start something
    that drives the game for minutes, or take a capture and read it by hand at
    whatever camera happened to be up. That is how a session ends up saying a
    village is full when its loot was spent an hour ago.

    **About the village on screen, and it does not cross.** `world` reports
    which one that turned out to be rather than promising either; sailing is
    `ai_coc world --go` and belongs to whoever asked, since a status check that
    moves the game is no longer one. The two are meant to be used together.

    The ceilings cost six taps and three captures, which is what makes the
    answer a percentage rather than a number nobody can size. That is the form
    every decision here is made in: `stop_at` is a share, and the storages grow
    when a builder finishes one, so reading them beats writing them down.
    """
    adb = _controller()
    display = _settle_game(adb, WORLD_SETTLE_POLLS) or adb.display_for(COC_PACKAGE)
    return stock_of(ScreenRunner(adb=adb, display=display, frame_dir=frame_dir), adb, display)


def stock_of(runner: ScreenRunner, adb: AdbController, display: DisplayTarget) -> StockReport:
    """The storage half of `stock`, for a caller that already has a runner going.

    `status` asks three things of one village, and settling the game once for
    all three is the whole of what it saves over running the three commands.
    """
    world, held = runner.read_storages()
    if world is None:
        # **A panel over the village is the ordinary way to arrive here**, and
        # nothing above this line clears one: `ScreenRunner` has no `_home`, and
        # `_settle_game` only waits. The skills now ask for this command before
        # every run, which makes it the one most likely to land on a screen the
        # last command left behind — measured elsewhere, a `collect` run leaves
        # an 8級聖水收集器 panel up. `uncovered` is the crossing's own answer to
        # exactly that, and it refuses to press at a battle.
        world = uncovered(adb, display)
        if world is not None:
            world, held = runner.read_storages()
    # The log line is built here rather than carried on the model, because a
    # person reading `run.log` wants a sentence and a caller reading the report
    # wants fields — and the sentence is derivable from the fields, which is
    # what made it a duplicate when the model held one.
    if world is None:
        logger.info("Stock: the screen is not a village, so there are no bars to read")
        return StockReport()
    if held is None:
        logger.info("Stock: on %s, but this frame would not resolve the bars", world)
        return StockReport(world=world)
    capacity = runner.read_ceilings(world) or StorageCapacity()
    rows = (
        ("gold", capacity.gold, held.gold),
        ("elixir", capacity.elixir, held.elixir),
        ("dark", capacity.dark, held.dark),
    )
    filled = {name: f"{now * 100 // ceiling}%" for name, ceiling, now in rows if ceiling}
    report = StockReport(world=world, held=held, capacity=capacity, filled=filled)
    logger.info(
        "Stock: %s holds %s of %s",
        world,
        filled or "an unknown share",
        capacity.model_dump(exclude_none=True) or "ceilings that would not read",
    )
    return report


def _namer() -> GeminiClient | None:
    """Who reads what is being raised, or nothing where there is no key.

    The lite tier, for the reason `upgrade` uses it: this is a label off a crop
    rather than a judgement about a whole screen. Without one the countdowns
    still read and the names come back blank, which every caller treats as the
    ordinary answer it is.
    """
    return _planner(ConfigStore().load(), "lite")


def _plate(role: PlateRole, frame_dir: Path | None) -> PlateReport:
    """One plate on whichever village is up, and never the other one.

    Same shape as `stock` and for the same reason: crossing is `ai_coc world
    --go`, so a status check that sails a boat is no longer one. `uncovered` is
    what clears a panel the last command left standing, which the skills now make
    the ordinary way to arrive here.
    """
    adb = _controller()
    display = _settle_game(adb, WORLD_SETTLE_POLLS) or adb.display_for(COC_PACKAGE)
    runner = PlateRunner(adb=adb, display=display, frame_dir=frame_dir, namer=_namer())
    report = runner.read(role)
    # The outcome rather than `world is None`, which is the same state encoded
    # twice: only this branch of the reader leaves the world unset, and reading
    # the field that says so is what keeps the two from drifting apart.
    if report.outcome == "not_a_village" and uncovered(adb, display) is not None:
        report = runner.read(role)
    logger.info("Plate %s: %s", role, _plate_line(report))
    return report


def worker(frame_dir: Path | None = None) -> PlateReport:
    """Who is building what on the village that is up, and how long each has left.

    **`ai_coc builders` is the home village's own version of this and stays
    that way.** It goes through `GameRunner._home`, which answers the builder
    base with `builder_base` and reads nothing there. This one reads whichever
    village is on screen and reports which that turned out to be.
    """
    return _plate("builder", frame_dir)


def lab(frame_dir: Path | None = None) -> PlateReport:
    """What is being researched on the village that is up, and how long it has left.

    The home village counts two slots on this plate — the laboratory and the pet
    house beside it — and the builder base one. Both open the same panel as the
    builders' plate does, so this is `worker` pointed one plate to the left.
    """
    return _plate("lab", frame_dir)


def status(frame_dir: Path | None = None) -> StatusReport:
    """Everything one village says about itself, in one pass and without crossing.

    The three readings a session takes before deciding anything — who is
    building, what is being researched, how full the storages are — plus the
    shield, which is the one countdown here that costs something when it runs
    out. Settling the game once for all four is the whole of what this saves
    over running the commands separately.
    """
    adb = _controller()
    display = _settle_game(adb, WORLD_SETTLE_POLLS) or adb.display_for(COC_PACKAGE)
    runner = PlateRunner(adb=adb, display=display, frame_dir=frame_dir, namer=_namer())
    # **The first reading is the check**, so there is no capture to take ahead
    # of it: `read` already answers `not_a_village` for a panel over the village,
    # and clearing one then costs one retry rather than a capture on every run.
    # It only has to happen once — whatever was covering the village is gone by
    # the time the other three look.
    builder = runner.read("builder")
    if builder.outcome == "not_a_village" and uncovered(adb, display) is not None:
        builder = runner.read("builder")
    report = StatusReport(
        builder=builder,
        lab=runner.read("lab"),
        stock=stock_of(runner, adb, display),
        shield=runner.shield(),
    )
    report.world = report.builder.world or report.lab.world or report.stock.world
    logger.info(
        "Status: %s；%s；%s",
        _plate_line(report.builder),
        _plate_line(report.lab),
        _shield_line(report.shield, report.world),
    )
    return report


# Only ever used to build a log line, which is why these live here rather than
# with the reader: nothing under `ui/` cares what a plate is called, and these
# were the last Chinese *values* `ui/plates.py` held — what is left there quotes
# the game's own screen in a comment.
PLATE_NAMES: dict[PlateRole, str] = {"lab": "實驗室", "builder": "建築工人", "shield": "護盾"}


def _plate_line(report: PlateReport) -> str:
    """One plate as a line for a person, built from the report's own fields.

    Here rather than carried on the model, for the reason `stock_of` gives: a
    person reading `run.log` wants a sentence and a caller reading the report
    wants fields, and a sentence derivable from the fields is the same answer
    twice. `outcome` is what the fields could not say on their own — a plate
    whose badge was not on the frame, against a panel that would not open on a
    frame whose count would not read either.
    """
    held = PLATE_NAMES[report.role]
    if report.outcome == "not_a_village":
        return "畫面不在村莊,讀不到上排的牌子"
    if report.outcome == "no_badge":
        return f"這個畫面上看不到{held}的牌子"
    if report.outcome == "panel_shut":
        return f"{held}的面板打不開,只讀到牌子上的數字"
    # Only the plate's own count, because the running count is on every line
    # below this one — carrying it here as well printed it twice.
    counted = f"{report.free}/{report.total}" if report.free is not None else "數字讀不到"
    if report.outcome == "idle":
        return f"{held} {counted},沒有在跑的項目"
    timed = [job for job in report.jobs if job.remaining is not None]
    if not timed:
        return f"{held} {counted},{len(report.jobs)} 個在跑,但每一個的倒數都讀不到"
    next_up = min(timed, key=lambda job: job.remaining or 0)
    named = f"是{next_up.name}," if next_up.name else ""
    return (
        f"{held} {counted},{len(report.jobs)} 個在跑,"
        f"最快的{named}還要 {spell_out(next_up.remaining or 0)}"
    )


def _shield_line(shield: ShieldState | None, world: World | None) -> str:
    """What the shield plate is worth saying, including that there is none to say.

    **No shield is the loud answer rather than the quiet one**: it means the
    village is open to being raided right now, which is the state a full one
    should never be left in.

    **`world` is here because the runner's None covers two things and they are
    not the same news.** The builder base has no shield plate at all, which is
    structural and needs no following up; a home village whose plate this frame
    could not place is a reading to take again, and reported as the first it
    would stop a session watching the one countdown that costs loot when it
    lapses.

    **There are two Nones here and they sit one level apart.** No `ShieldState`
    is a plate this frame could not read; a `ShieldState` with no `remaining` is
    the plate read and saying 無. A third line used to sit between them for a
    shield up with an unreadable countdown, and `shield_state` could not produce
    it — see `ShieldState.up` for why that combination is not a state.
    """
    if shield is None:
        return "這個世界沒有護盾" if world == "night" else "護盾的牌子讀不到,沒辦法說還剩多久"
    if shield.remaining is None:
        return "**沒有護盾**,村莊現在可以被打"
    return f"護盾還有 {spell_out(shield.remaining)}"


# Only ever used to build a line for a person; `CartReport.outcome` is what a
# caller reads. **`locked` is the one worth spelling out**, because it is the
# ordinary state of a farmed village and it reads nothing like the
# 推車裡沒有東西可以收 this reported for it: the cart is standing full and the
# game will not open its gate.
CART_LINES: dict[CartOutcome, str] = {
    "collected": "建築大師基地的推車收到聖水 {elixir}",
    "empty": "按了收集,但儲量沒有變,推車應該是空的",
    "locked": "推車是開的,收集鈕是灰的,而車上的數字讀不到",
    "locked_holding": "推車裡存著 {held}／{capacity} 聖水,收集鈕是灰的 —— 聖水倉庫滿了,倉庫有空間再收",
    "locked_empty": "推車是空的,收集鈕是灰的,沒有東西可以收",
    "not_found": "三個候選點都沒有打開推車",
    "not_parked": "鏡頭沒辦法停回定位,這一趟沒有去找聖水車",
    "wrong_world": "現在不在建築大師基地,沒有推車可以收",
    "unreadable": "按了收集,但有一邊的儲量條讀不到,不知道進帳多少",
}


def cart_line(cart: CartReport) -> str:
    """The one line a person reads off a trip to the loot cart."""
    return CART_LINES[cart.outcome].format(
        elixir=cart.elixir, held=cart.held, capacity=cart.capacity
    )


COLLECT_LINES: dict[CollectOutcome, str] = {
    "collected": "收了 {markers} 個採集器,金幣 +{gold}／聖水 +{elixir}／黑水 +{dark}",
    "nothing_to_collect": "沒有採集器等著收",
    "no_village": "畫面沒辦法回到村莊,收集沒有開始",
    "builder_base": "遊戲停在夜世界,主村的採集器沒有收",
    "stock_unread": "點了 {markers} 個採集標記,但收完之後讀不到儲量",
}


def collect_line(report: CollectReport) -> str:
    """The one line a person reads, from whichever village the run was on.

    The builder base's trip has its own seven answers and they are not a subset
    of these, so the cart speaks for itself rather than being flattened into a
    vocabulary of collector markers.
    """
    if report.cart is not None:
        return cart_line(report.cart)
    return COLLECT_LINES[report.outcome].format(
        markers=report.markers, gold=report.gold, elixir=report.elixir, dark=report.dark
    )


def _village_now(adb: AdbController, display: DisplayTarget) -> World | None:
    """Which village the game is on, once one has painted; None when none does in time."""
    for _ in range(WORLD_SETTLE_POLLS):
        world = current_world(adb.screenshot(display))
        if world is not None:
            return world
        time.sleep(RESTART_POLL_GAP)
    return None


def collect(frame_dir: Path | None = None) -> CollectReport:
    """Tap every collector the village has left standing, with no window in the way.

    Collectors stop once they are full, so a village nobody has emptied has spent
    most of its time doing nothing. This is the cheapest thing in the project to
    run and the one worth running most often.

    **The builder base's cart has six answers and this used to give it two.**
    See `CartReport`: a cart the game had locked was reported as an empty one,
    which is the opposite instruction.
    """
    adb, display = _session(frame_dir)
    # **Decided on a village that has painted, not on the first frame.** Every
    # branch but the cart's is the home village's, and a frame caught between a
    # battle and its village used to send a run meant for the cart across the
    # water instead, measured live 13 s after a builder base attack stood down.
    # Only the world is waited for: both branches park the camera themselves.
    report = None
    if _village_now(adb, display) != "night":
        report = UpkeepRunner(adb=adb, display=display, frame_dir=frame_dir).collect()
    # **The builder base has no collectors to sweep and one cart instead.** Its
    # elixir is paid into that cart rather than into the storages, so this is
    # the same job on that village even though it shares none of the machinery:
    # one tap at a known spot rather than a colour-and-size search over the map.
    # A village slower to paint than the wait above goes down the home path,
    # which stops rather than sails on the builder base, so its cart is taken
    # here as well.
    if report is None or report.outcome == "builder_base":
        cart = collect_cart(adb, display)
        report = CollectReport(outcome="cart", elixir=cart.elixir, cart=cart)
    logger.info("Collect: %s", collect_line(report))
    return report


# What stopped the run putting builders on things. `started` adds nothing,
# because the count and the names are the line's own opening either way.
BUILD_LINES: dict[BuildOutcome, str] = {
    "started": "",
    "nothing_found": "掃過村莊都沒有找到可以升級的建築",
    "cannot_afford": "剩下的資源買不起任何升級,最便宜的要 {cheapest}",
    "only_no_match": "沒有找到名字含「{only}」而且買得起的建築",
    "builders_busy": "工人都在忙,沒有可以派的",
    "count_unread": "讀不到工人數量,先停下來",
    "no_village": "畫面沒辦法回到村莊,建築升級沒有開始",
    "builder_base": "遊戲停在夜世界,這裡只升主村的建築;要升先跑 ai_coc world --go day",
    "village_lost": "升級之後讀不到村莊,先停下來",
}


def build_line(report: BuildReport) -> str:
    """What one `upgrade` run started, and what stopped it starting more.

    Named where a name was read, because "開始了 1 個升級" says nothing about
    which builder went where — and that is the whole point of asking.
    """
    named = "、".join(str(job.building) for job in report.started if job.building.name)
    did = f"開始了 {len(report.started)} 個升級" + (f":{named}" if named else "")
    reason = BUILD_LINES[report.outcome].format(cheapest=report.cheapest, only=report.only)
    return f"{did};{reason}" if reason else did


def upgrade(
    options: UpgradeOptions, should_stop: Callable[[], bool] = stop_requested
) -> BuildReport:
    """Put the village's idle builders to work, with no window in the way.

    A builder standing around is the one thing a village cannot buy its way out
    of, so this is worth running whenever an upgrade finishes. Walls are left to
    `walls`, which needs no builder at all.

    **This is the one upkeep command long enough to be worth stopping.** The
    other three are seconds, but a run with no key to ask with, or one whose
    finder came back empty, falls through to `_sweep` and spends a couple of
    minutes tapping a grid. The check itself is in `GameRunner._opened`, which
    is the walk all three finders share.
    """
    adb, display = _session(options.frame_dir)
    config = ConfigStore().load()
    report = UpkeepRunner(
        adb=adb,
        display=display,
        frame_dir=options.frame_dir,
        keep_gold=options.keep_gold,
        keep_elixir=options.keep_elixir,
        at=options.at,
        should_stop=should_stop,
        # Skipped when the run was told where to look, for the reason the wall
        # loop skips it: the whole point of asking is to find them.
        ai=None if options.at else _planner(config),
        # Always, because it answers the one question the parsers cannot: which
        # building this is. `--only` needs it, and without it the report is a
        # coordinate.
        namer=_planner(config, "lite"),
        only=options.only,
    ).upgrade()
    logger.info("Upgrade: %s", build_line(report))
    return report


HERO_LINES: dict[HeroOutcome, str] = {
    "read": "英雄殿堂裡讀到 {count} 個英雄",
    "started": "{hero} 開始升級,花了 {price} {resource}",
    "hall_not_found": "掃過村莊都沒有找到英雄殿堂",
    "hero_absent": "英雄殿堂裡沒有看到 {hero}",
    "already_upgrading": "{hero} 現在沒有升級按鈕,多半正在升級中",
    "button_dead": "{hero} 的升級按鈕是停用的,多半是英雄殿堂的等級擋住了(要 {price} {resource})",
    "cannot_afford": "資源不夠,{hero} 要 {price} {resource}",
    "builders_busy": "工人都在忙,{hero} 現在派不出去",
    "count_unread": "讀不到工人數量,先停下來",
    "card_moved": "{hero} 不在畫面上了,這一輪先不動它",
    "no_confirmation": "點了 {hero} 的升級,但沒有出現確認畫面",
    "undercharged": "確認了 {hero} 的升級,但儲量沒有少那麼多",
    "no_village": "畫面沒辦法回到村莊,英雄升級沒有開始",
    "builder_base": "遊戲停在夜世界,英雄殿堂在主村;要升先跑 ai_coc world --go day",
    "village_lost": "開始升級 {hero} 之後讀不到村莊,先停下來",
}


def hero_line(report: HeroReport) -> str:
    """The one line a person reads off a hall reading or a hero upgrade.

    The price comes off whichever card this run was about — the one it started
    where it started something, and the one it read where it refused — because
    every refusal that names a number is naming that card's own.
    """
    card = report.started or next((one for one in report.cards if one.hero == report.hero), None)
    return HERO_LINES[report.outcome].format(
        count=len(report.cards),
        hero=report.hero,
        price=card.price if card else "",
        resource=card.resource if card else "",
    )


def hero(options: HeroOptions) -> HeroReport:
    """Read the 英雄殿堂, and raise the hero named, with no window in the way.

    Reading is the default and spends nothing: what each hero costs next is the
    number the decision rests on, and a hall nobody has looked at is the most
    expensive kind of idle builder — a hero level runs for the better part of a
    day, so one not started this evening is one not finished tomorrow.

    `upgrade` is what turns it into a purchase, and only ever for the hero
    named: which hero is worth raising is a judgement about how the village
    plays, not something a price can settle.
    """
    adb, display = _session(options.frame_dir)
    report = HeroRunner(
        ai=_planner(ConfigStore().load()),
        adb=adb,
        display=display,
        frame_dir=options.frame_dir,
        hero=options.upgrade,
        at=options.at,
    ).run()
    logger.info("Hero: %s", hero_line(report))
    return report


DONATE_LINES: dict[DonateOutcome, str] = {
    "donated": "捐了 {gifts} 次",
    "dry_run": "試跑,畫面上有 {offered} 種可以捐,一種都沒有捐出去",
    "nothing_given": "有人在請求,但沒有捐出去(畫面上有 {offered} 種可捐)",
    "nobody_asking": "部落聊天裡目前沒有人在請求增援",
    "panel_shut": "點了增援,但捐贈畫面沒有打開",
    "no_village": "畫面沒辦法回到村莊,捐兵沒有開始",
    "builder_base": "遊戲停在夜世界,捐兵只在主村;要捐先跑 ai_coc world --go day",
}


def donate(options: DonateOptions) -> DonateReport:
    """Give troops to whoever in the clan is asking, with no window in the way.

    Cheap to run and cheap to find nothing: a clan with no request open costs one
    tap on the chat tab and one capture. `dry_run` walks the whole path and stops
    before the tap that gives something away, because the panel does not confirm.
    """
    adb, display = _session(options.frame_dir)
    report = ClanRunner(
        adb=adb,
        display=display,
        frame_dir=options.frame_dir,
        dry_run=options.dry_run,
        rounds=options.rounds,
    ).donate()
    logger.info(
        "Donate: %s",
        DONATE_LINES[report.outcome].format(gifts=len(report.gifts), offered=report.offered),
    )
    return report


def world(go: World | None = None) -> WorldReport:
    """Which village the game is on, and sail to the other one when asked for it.

    Reading is the default and crossing is the exception, for the same reason
    `hero` reads unless told to spend a builder: this is the question every
    other command has to ask itself now that the game reopens on whichever
    village it was closed on, and the answer alone is worth having.

    **Reading really is one capture and nothing else** — no swipe, no tap, and
    no pinch, which is what lets a session ask it at any moment without first
    working out what it would disturb. It went through `_settle_game` for a
    while, and that zooms the camera back out on its way past: harmless by this
    project's own measurement, and still enough to make the claim false. The
    patience that call also brings is not this command's to spend: a game still
    on its loading screen honestly has no village on it, and `ai_coc launch` is
    the one that waits a cold start out.
    """
    adb = _controller()
    try:
        display = adb.display_for(COC_PACKAGE)
    except AdbControlError:
        logger.warning("The game is not on a display yet; neither village can be confirmed")
        return WorldReport(found=None, world=None, outcome="no_display")
    found = current_world(adb.screenshot(display))
    if go is None or go == found:
        report = WorldReport(found=found, world=found, outcome="here" if found else "no_village")
    else:
        landed = cross(adb, display, go)
        report = WorldReport(
            found=found,
            world=landed,
            crossed=landed == go,
            outcome="crossed" if landed == go else "crossing_failed",
        )
    logger.info("World: %s", world_line(report))
    return report


# Only ever used to build a line somebody reads, which is why these are here
# rather than beside the type: nothing in the loops cares what the two villages
# are called in Chinese.
_WORLDS: dict[World | None, str] = {"day": "日世界", "night": "夜世界", None: "不明的畫面"}

WORLD_LINES: dict[WorldOutcome, str] = {
    "here": "目前在{world}",
    "crossed": "從{found}切到{world}",
    "crossing_failed": "想切過去,但畫面還停在{world}",
    "no_village": "畫面不是村莊,無法判斷世界",
    "no_display": "遊戲還沒有畫面,無法判斷世界",
}


def world_line(report: WorldReport) -> str:
    """The one line a person reads off a world check or a crossing."""
    return WORLD_LINES[report.outcome].format(
        found=_WORLDS[report.found], world=_WORLDS[report.world]
    )


VIEW_LINES: dict[ViewOutcome, str] = {
    "parked": "並把鏡頭停回定位",
    "not_a_village": "但畫面不是村莊,沒有把鏡頭停回定位",
    "zoomed_in": "拉近的鏡頭停不住,沒有把鏡頭停回定位",
    "never_settled": "但鏡頭一直沒有停下來,定位失敗",
}


def view_line(report: ViewReport) -> str:
    """What this command asked of the camera, and what it settled."""
    scaled = f"鏡頭{'拉遠' if report.zoom == 'out' else '拉近'}了 {report.times} 次"
    return f"{scaled},{VIEW_LINES[report.outcome]}"


def view(zoom: str = "out", times: int = 3) -> ViewReport:
    """Put the village camera back where every coordinate here was measured.

    **The game has no zoom control to tap and reports no zoom level**, so this
    writes the two-finger gesture straight to the touch device — see
    `AdbController.pinch` for why `input` cannot. Zooming out past the far limit
    does nothing at all, which is what makes `--zoom out` safe to run blind.

    **One pinch does not cover the whole range, and this said it did.** Swept
    from a camera zoomed fully in, one `out` takes the bare-ground share of the
    frame from 0.19 to 0.40 and a second to 0.507, where a third moves it by
    0.002 — and a longer finger travel does not buy the difference either; see
    `ZOOM_PINCHES`. So `--times` defaults to what it takes rather than to one.

    **Scale is only half of it, and the half that was missing is the one that
    breaks things.** This used to zoom and stop, on the documented understanding
    that the far limit also centres the village. It does not: shove the camera
    off centre, pinch, and the view has not moved at all. So the position is
    settled separately, by running the camera into a map corner where it clamps —
    see `park_camera` for the measurement. `--zoom` stays the knob for scale.

    **The park is gated on a village and this command's own pinch is not**,
    which is the difference between a gesture that is safe on any screen and one
    that is not. A swipe with a card selected deploys troops along its path
    instead of panning, and this command's whole reputation is that it can be run
    blind at whatever the game is showing — a killed run leaves the game
    mid-battle, and that is exactly when somebody reaches for it. The one capture
    answers both questions the park has: whether this is a village at all, and
    which of the two, since the maps clamp in opposite corners. Anything else
    keeps the zoom and says the camera was left where it was.

    **And `--zoom in` is refused a park before the capture is even taken**, for
    the reason `park_camera` pinches at all: its walk is only bounded at the far
    zoom, and measured two pinches in the camera runs off the village and was
    still moving after fourteen swipes. So a park asked for there would undo the
    zoom just requested and then claim a position it never reached. The cost on
    the ordinary path is that `--zoom out` pinches twice — `--times` here and
    `ZOOM_PINCHES` inside the park — which past the far limit does nothing at
    all and is why the flag is still the knob it says it is.
    """
    adb = _controller()
    display = adb.display_for(COC_PACKAGE)
    adb.zoom(zoom, times, COC_PACKAGE, display)
    asked: Literal["in", "out"] = "out" if zoom == "out" else "in"
    world = None
    if zoom != "out":
        # **A camera that has just been zoomed in cannot be parked**, so asking
        # would undo the thing that was asked for and claim a position it never
        # reached. Measured two pinches in, the camera runs off the village into
        # the map's dark border and was still walking hundreds of pixels per
        # swipe after fourteen of them; `park_camera` pinches back out for
        # exactly that reason, which on this path is the opposite of the request.
        outcome: ViewOutcome = "zoomed_in"
    elif (world := current_world(adb.screenshot(display))) is None:
        outcome = "not_a_village"
    elif park_camera(adb, display, world):
        outcome = "parked"
    else:
        # The one answer that used to be a claim rather than a reading. A park
        # that never arrived leaves every remembered coordinate off by however
        # far it was short, and this is the command somebody runs precisely to
        # put those back.
        outcome = "never_settled"
    report = ViewReport(zoom=asked, times=times, world=world, outcome=outcome)
    logger.info("View: %s", view_line(report))
    return report


# The village export lives behind 設定 → 更多設定 → scroll → 複製, and every one
# of these was measured on the live game at 1600x900. The copy button is not
# here because `export_row` returns it: it is the one step that must not be
# tapped on an unverified screen, so its coordinate travels with the reader that
# confirms the row.
SETTINGS_GEAR = (1540, 652)
MORE_SETTINGS = (793, 770)
CLOSE_SETTINGS = (1288, 99)
SETTINGS_SCROLL = ((800, 650), (800, 250))
SCROLL_MS = 400
# The list is about four swipes deep; the extra ones cost nothing because the
# loop stops as soon as the row reads, and the game clamps at the bottom.
SCROLL_TRIES = 6
# Each menu is a full-screen page with an animation on it.
PAGE_SETTLE = 1.2
# How long to wait for the emulator to mirror the Android clipboard onto Windows.
# Measured: the payload was already there on the first read after the tap, and a
# cleared clipboard stayed empty for ten seconds with nothing copied — so a poll
# that runs out really is "nothing was copied" rather than "not yet".
CLIPBOARD_POLLS = 15
CLIPBOARD_GAP = 0.4


def _named(entities: list[VillageEntity], names: dict[int, str]) -> list[NamedEntity]:
    """Every entity with whatever the community mapping calls it, None where it has nothing."""
    return [
        NamedEntity.model_validate({**entity.model_dump(), "name": names.get(entity.data_id)})
        for entity in entities
    ]


def _export_path(tag: str) -> Path:
    """Where one account's export is kept, with the tag made safe to be a filename.

    The tag comes off the clipboard, so it is outside data: anything that parses
    as JSON can put a `/` or a trailing dot in it, which is a path escape rather
    than a filename.
    """
    safe = "".join(ch for ch in tag if ch.isalnum() or ch in "-_#") or "UNKNOWN"
    return ACCOUNT_JSON_DIR / f"{safe}.json"


def _last_export() -> VillageExport:
    """The most recent export on this machine, without touching the emulator.

    This is what the window shows when it opens and what `--last` prints. A file
    written before this command existed holds the game's raw payload rather than
    an export, and says so rather than coming back as an empty village.
    """
    saved = sorted(ACCOUNT_JSON_DIR.glob("*.json"), key=lambda one: one.stat().st_mtime)
    if not saved:
        return VillageExport(tag="", exported_at="", outcome="never_exported")
    try:
        export = VillageExport.model_validate_json(saved[-1].read_text(encoding="utf-8"))
    except ValueError:
        logger.warning("%s does not read back as an export", saved[-1])
        return VillageExport(tag="", exported_at="", outcome="wrong_format")
    logger.info("Read %s back off %s", export.tag, saved[-1])
    return export.model_copy(update={"outcome": "read_back"})


def _copy_village(
    adb: AdbController, display: DisplayTarget, frame_dir: Path | None
) -> tuple[str, ExportOutcome]:
    """Walk the settings menus and come back with whatever the game copied.

    Every step is verified before the next tap, because these are fixed
    coordinates on full-screen pages: a menu that did not open leaves the next
    tap somewhere nobody chose. On the scrolled page that is the 更高幀數 toggle,
    which flips a game setting and reports nothing at all.

    The clipboard is emptied before the copy is tapped, so what is read
    afterwards is known to be new. Without that, a tap that missed reads back
    the payload from the last run and the whole command reports success on stale
    data. **And whatever was on it goes back afterwards**, because it belongs to
    whoever is at the keyboard: this command is worth running while somebody is
    in the middle of something else, and leaving 7 KB of village JSON where
    their own copy used to be is a cost they did not agree to.
    """

    def look(label: str) -> bytes:
        png = adb.screenshot(display)
        if frame_dir is not None:
            (frame_dir / f"{label}.png").write_bytes(png)
        return png

    adb.tap(*SETTINGS_GEAR, display)
    time.sleep(PAGE_SETTLE)
    if not settings_open(look("settings")):
        return "", "settings_shut"
    adb.tap(*MORE_SETTINGS, display)
    time.sleep(PAGE_SETTLE)
    if not more_settings_open(look("more_settings")):
        return "", "more_settings_shut"
    for attempt in range(SCROLL_TRIES):
        spot = export_row(look(f"scroll_{attempt}"))
        if spot is not None:
            break
        adb.swipe(*SETTINGS_SCROLL, SCROLL_MS, display)
        time.sleep(PAGE_SETTLE)
    else:
        return "", "row_not_found"
    held = read_clipboard()
    clear_clipboard()
    adb.tap(*spot, display)
    try:
        for _ in range(CLIPBOARD_POLLS):
            time.sleep(CLIPBOARD_GAP)
            if payload := read_clipboard():
                return payload, "exported"
        look("nothing_copied")
        return "", "nothing_copied"
    finally:
        write_clipboard(held)


EXPORT_LINES: dict[ExportOutcome, str] = {
    "exported": "{tag} 共 {count} 筆,剛從遊戲裡匯出",
    "read_back": "{tag} 共 {count} 筆,讀自上一次匯出",
    "never_exported": "還沒有匯出過任何村莊資訊",
    "wrong_format": "存檔讀不成村莊匯出,請重新執行一次 ai_coc export",
    "no_village": "遊戲沒有回到村莊畫面,沒有匯出",
    "settings_shut": "點了設定齒輪,但設定視窗沒有打開",
    "more_settings_shut": "點了更多設定,但那一頁沒有打開",
    "row_not_found": "捲到底了還是找不到「以 JSON 格式匯出村莊數據」那一列",
    "nothing_copied": (
        "點了複製,但剪貼簿沒有東西。"
        "可能是遊戲沒吃到那一下,也可能是模擬器沒把 Android 的剪貼簿同步過來"
    ),
    "not_a_village": "剪貼簿裡的不是村莊資料,複製的當下可能被別的東西蓋過去了",
}


def export_line(export: VillageExport) -> str:
    """The one line a person reads off an export, whether it was taken or read back."""
    return EXPORT_LINES[export.outcome].format(tag=export.tag, count=len(export.entities))


def export(frame_dir: Path | None = None, last: bool = False) -> VillageExport:
    """Get the village out of the game and name everything in it.

    The game will write the whole village out as JSON, which is the only
    complete account of what a village holds — every building, every level,
    every troop — and far more than the screen readers can see. It used to be
    driven by asking Gemini to find each of the three buttons on a screenshot,
    once per step, which is both unreliable and an AI call for something that
    never moves. The coordinates are measured now and the reading is what
    decides whether to tap them.

    Only the named result is kept. The game's own payload is not thrown away in
    the process: every section it carries becomes entities, every field of every
    row survives on them, and the export time and boosts come across whole.
    """
    if last:
        return _last_export()
    # Fetched before the emulator is touched: it reaches the network, and a run
    # that fails on it after walking the menus has spent the taps and thrown the
    # answer away.
    names = fetch_entity_mapping().names()
    adb = _controller()
    display = _settle_game(adb, WORLD_SETTLE_POLLS)
    if display is None:
        return VillageExport(tag="", exported_at="", outcome="no_village")
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload, copied = _copy_village(adb, display, frame_dir)
        if copied != "exported":
            return VillageExport(tag="", exported_at="", outcome=copied)
        # Parsed inside the same guard, because the clipboard is the one input
        # here that comes from outside: anything else copied during the poll is
        # read as the payload, and a `ValidationError` reaching the top would
        # leave `result.json` unwritten with only the log to reconstruct from.
        snapshot = parse_village_text(payload)
    except ValueError as exc:
        logger.warning("The clipboard payload was not a village: %s", exc)
        return VillageExport(tag="", exported_at="", outcome="not_a_village")
    finally:
        # Whatever happened, the settings window is left covering the village,
        # and every other command starts by assuming one is on screen.
        adb.tap(*CLOSE_SETTINGS, display)
    stamp = snapshot.raw.timestamp
    when = (
        datetime.fromtimestamp(stamp, UTC).isoformat() if stamp else datetime.now(UTC).isoformat()
    )
    named = _named(snapshot.entities, names)
    unnamed = sum(1 for entity in named if entity.name is None)
    path = _export_path(snapshot.tag)
    result = VillageExport(
        tag=snapshot.tag,
        exported_at=when,
        timestamp=stamp,
        entities=named,
        boosts=snapshot.raw.boosts,
        outcome="exported",
    )
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Export: %s,其中 %d 筆還沒有名字,存到 %s", export_line(result), unnamed, path)
    return result


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
        world=current_world(png),
        scout=read_scout(png),
        stock=read_stock(png),
        builder_stock=read_builder_stock(png),
        capacity=StorageCapacity(
            gold=storage_capacity(png, 0),
            elixir=storage_capacity(png, 1),
            dark=storage_capacity(png, 2),
        ),
        army=army_strength(png),
        wall_menu=wall_menu(png),
        bubbles=collect_bubbles(png),
        heroes=hero_cards(png),
        builders=free_builders(png),
        queue=builder_jobs(png),
        upgrades=upgrade_buttons(png),
        donatable=len(donatable_cards(png)),
        attack_menu=attack_menu_open(png),
        night_menu=night_attack_menu(png),
        searching=searching_opponent(png),
        loot_cart=loot_cart_open(png),
        cart_ready=loot_cart_ready(png),
        battle_over=battle_over(png),
        in_battle=in_battle(png),
        dialog=game_dialog(png),
        skip_offered=skip_offered(png),
        panel_drawn=panel_drawn(png),
        idle_dialog=idle_disconnected(png),
        loading=loading_screen(png),
        settings_menu=settings_open(png),
        more_settings=more_settings_open(png),
        export_row=export_row(png),
        card_groups=groups,
        counted=counted_cards(png, slots),
        freezes=freeze_cards(png, slots),
        live=live_cards(png, slots),
        on_field=field_units(png, slots),
        selected=selected_cards(png, slots),
        counts={slot: card_count(png, slot) for slot in slots},
        upgrade_sheet=upgrade_sheet(png),
        village_box=village_box(png),
    )
