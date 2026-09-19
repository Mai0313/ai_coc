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
    HeroReport,
    PlayedPlan,
    ViewReport,
    WallReport,
    BuildReport,
    HeroOptions,
    NamedEntity,
    PlateReport,
    RunnerState,
    ShieldState,
    StockReport,
    WallOptions,
    WorldReport,
    AttackReport,
    AttackSeries,
    DonateReport,
    FrameReading,
    LaunchReport,
    RestartScope,
    StatusReport,
    AttackOptions,
    BuilderReport,
    CollectReport,
    DisplayTarget,
    DonateOptions,
    VillageEntity,
    VillageExport,
    BoundarySurvey,
    LootThresholds,
    UpgradeOptions,
    StorageCapacity,
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
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)


def _controller() -> AdbController:
    """The first MuMu instance, with Clash of Clans already up on it."""
    mumu = MuMuAdapter()
    instances = mumu.enumerate_instances()
    if not instances:
        raise RuntimeError("找不到任何 MuMu instance")
    return mumu.controller(mumu.ensure_coc(instances[0].index).adb_serial)


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


# How long to give MuMu to actually take an instance down. `control restart`
# returns as soon as the request is sent, so the state read a moment later is
# still the old one — and `ensure_coc` skips its whole boot wait for anything
# still reporting `android_started`, which would aim a `monkey` launch at an
# emulator on its way down.
SHUTDOWN_POLLS = 15
SHUTDOWN_GAP = 2.0


def _await_shutdown(mumu: MuMuAdapter, index: int) -> None:
    """Wait for a restarting instance to really go down before it comes back up.

    An instance missing from the listing entirely is not treated as down: MuMu
    drops one for a moment while it restarts, and `ensure_coc` cannot start from
    there — it raises on an index it cannot find. So that keeps waiting, and
    only an instance that is listed and no longer started ends the wait.
    """
    for _ in range(SHUTDOWN_POLLS):
        time.sleep(SHUTDOWN_GAP)
        current = mumu.instance(index)
        if current is not None and not current.android_started:
            return
    logger.warning("MuMu instance %s never went down; bringing the game up anyway", index)


def launch(restart: RestartScope) -> LaunchReport:
    """Bring the game up on the first MuMu instance, tearing down as much as asked.

    Every other headless command assumes the game is already running: they go
    through `_controller`, which calls `ensure_coc` and gives up on whatever it
    cannot fix. This is that step on its own, for the two states it cannot reach
    from there — an emulator or a game that is up and no longer answering, which
    from here looks exactly like a working one.
    """
    mumu = MuMuAdapter()
    instances = mumu.enumerate_instances()
    if not instances:
        raise RuntimeError("找不到任何 MuMu instance")
    index = instances[0].index
    was_running = instances[0].coc_running
    if restart == "emulator":
        logger.info("Restarting MuMu instance %s before bringing the game up", index)
        mumu.restart_instance(index)
        _await_shutdown(mumu, index)
    elif restart == "game":
        # The game can only be stopped on an emulator that is already up, which
        # is what the inner call is for. On a cold machine that call is also the
        # whole job and `restart_coc` then costs one relaunch of a game that had
        # only just started, which is cheaper than refusing and naming another
        # command: either way the caller asked to end up with a fresh game.
        mumu.restart_coc(mumu.ensure_coc(index))
    instance = mumu.ensure_coc(index)
    # `ensure_coc` is satisfied by a pid, which says the game is running and
    # nothing about whether it can be driven. Every command after this one aims
    # screen coordinates at it, and those were all measured against a village at
    # the far zoom — so this is where the game is brought to that state rather
    # than in each of them. The position is settled there too, by parking the
    # camera against a map edge — the pinch does not do it, whatever this used
    # to say.
    settled = _settle_game(mumu.controller(instance.adb_serial), RESTART_POLLS)
    if restart == "none":
        # Which of these it was is the only thing this scope can report: it does
        # the same work either way, and on a cold machine that work is the whole
        # job rather than the no-op the name suggests.
        did = "部落衝突已經在跑" if was_running else "已把部落衝突開起來"
    else:
        did = "已重開部落衝突" if restart == "game" else "已重開模擬器與部落衝突"
    logger.info("%s on instance %s (%s)", did, instance.index, instance.adb_serial)
    # A game whose village never painted is reported rather than raised: the
    # process is up, so the caller may still have something to do with it, and
    # the one thing it must not do is assume the screen is ready.
    if settled is None:
        did = f"{did}，但村莊沒有出現，畫面可能還在載入"
    return LaunchReport(
        index=instance.index,
        serial=instance.adb_serial,
        was_running=was_running,
        at_village=settled is not None,
        message=f"{did},模擬器 {instance.index} ({instance.adb_serial})",
    )


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


# A round that fought nothing is nearly always an army still training, which
# takes minutes rather than seconds. Coming straight round again would walk the
# same menus to read the same half-full camp, so a round that did not attack
# waits before the next one is started.
IDLE_REST = 60

# How long to give the game to paint a village before the world is decided.
# Sized for a launch rather than for a game already up: `ensure_coc` returns on
# a pid and the village follows about twenty seconds later, so a run started
# right after one used to read no village and end before round one.
WORLD_SETTLE_POLLS = 8

# How many builder base battles go by between trips to the loot cart. Three is
# the player's own pacing rather than a measurement: the cart accumulates, so
# the only cost of waiting is the risk of it capping out.
CART_EVERY = 3
# How often the barracks wait looks up to see whether it has been stood down.
# Sleeping through the whole minute in one go would leave a stop unnoticed for
# most of it, which reads as a stop that did nothing.
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
    """Wait out the barracks, answering whether the wait was cut short.

    A stop that lands here is said out loud, because the state file goes back to
    `idle` on the way out and this line is then its only trace in `run.log`.
    """
    try:
        for _ in range(int(seconds / STOP_POLL)):
            if should_stop():
                logger.info("Stop requested while waiting for the army; ending the series")
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
# after the `monkey` while the village is not on screen for much longer than
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
            # landed on is settled by whoever asked for it: every loop's own
            # way home crosses if it has to, and `launch` reports `at_village`
            # rather than promising the home one.
            #
            # It is the better test of the two on its own terms as well: a home
            # village with the camera at a map corner can leave the dark elixir
            # row unreadable, which had `read_stock` call an ordinary village no
            # village at all.
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
    time and fire another `monkey` at a game that had only just come up.

    False means the village never appeared. What to do about that is the
    caller's call, since it is a decision about the series rather than about the
    emulator.
    """
    # Wider than it looks, and deliberately so: `launch` raises `RuntimeError`
    # for an instance MuMu has dropped from its listing and `MuMuError` for a
    # game that never came up, both of which are exactly the state this is here
    # to recover from. Letting either escape would take the whole series with it
    # — `cli.py` never reaches `run.answer` and `result.json` is left empty,
    # which is the failure the False path below exists to avoid.
    try:
        launched = launch("emulator")
    except (RuntimeError, KeyboardInterrupt):
        logger.exception("The emulator did not come back up")
        return False
    adb = MuMuAdapter().controller(launched.serial)
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


def _pick_world(adb: AdbController, display: DisplayTarget, wanted: World | None) -> World | None:
    """Which village the series will play, or None when a named one is out of reach.

    Settled once for the whole series rather than per round. Naming one crosses
    to it; not naming one takes whichever is up, because the game reopens on the
    village it was closed on and refusing to play that one would stand half the
    runs down for no reason.

    **An unreadable frame is never fatal**, whether or not a village was named,
    because it is not the same answer as "the other village". The runner has its
    own answers for most of them: `_open_attack_menu` restarts a dropped game,
    leaves a result screen, presses a popup away and sails across, and every
    one of those is a state `current_world` says nothing for. Bailing here ends
    a whole series before round one.

    Measured twice, and the second one is why this covers a named village too.
    Without a name it already fell through here. With one it did not, so a run
    asked for `--world night` while a battle was still on screen — an ordinary
    state, a round abandoned by a stop — ended immediately with
    沒辦法切到夜世界,沒有開打, having done nothing and waited for nothing.

    **What this buys is the rounds, not the round.** A battle is the one state
    neither this nor the runner can shorten: `uncovered` refuses to press at one
    and `_open_attack_menu` spends its five attempts in about ten seconds, so
    that first round still reports 畫面不在建築大師基地. What follows it is the
    difference — `IDLE_REST` between rounds outlasts a battle comfortably, and
    the next round finds the village. A single-round run gets nothing out of
    this, and that is the honest limit of it.

    What is still fatal is a crossing that landed somewhere real and wrong:
    that is a boat this run cannot find, and the runner has no better answer.
    """
    if wanted is None:
        return current_world(adb.screenshot(display)) or "day"
    landed = cross(adb, display, wanted)
    if landed == wanted:
        return wanted
    if landed is None:
        logger.info("No village readable yet; leaving %s to the runner to reach", wanted)
        return wanted
    logger.warning("Wanted the %s village and the game is on %s", wanted, landed)
    return None


def _empty_cart(world: World, battles: int, adb: AdbController, display: DisplayTarget) -> None:
    """Fetch the builder base's elixir every few battles, since it is not paid in.

    **That village pays its elixir into a cart rather than into the storages**,
    so a series that never empties it farms half of what it wins. Every few
    battles rather than every one: the cart accumulates — measured, it holds a
    million — while emptying it costs a camera drag to the far corner and back.
    """
    if world != "night" or not battles or battles % CART_EVERY:
        return
    # **A failed trip to the cart is not a reason to lose the series.** It is a
    # side errand between rounds, and it opens with a capture of its own, so an
    # adapter error here used to unwind all the way out of `attack` with
    # `result.json` unwritten — measured twice, at 17 and 14 battles played and
    # countable only out of `run.log`. The elixir stays in the cart, which
    # holds a million and is emptied every few battles anyway, so the next trip
    # collects what this one did not.
    try:
        collect_cart(adb, display)
    except AdbControlError as exc:
        logger.warning("The loot cart could not be emptied this time: %s", exc)


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
    report = AttackReport(world=runner.world, message=f"模擬器沒有回應：{exc}")
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
        logger.info("Attack finished: %s", report.message)
        logger.error("The emulator has not answered for %d rounds; ending the series", runner.lost)
        return None
    # Handed back as an ordinary round that fought nothing, which is what it
    # was. The caller then records it, rests the barracks wait, and comes round
    # again — and that rest is worth more here than anywhere else, since an
    # emulator that just refused a command is the last thing to fire another
    # one at immediately.
    return report


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

    `plan_in` replaces the AI entirely — the loop plays that file and asks for
    nothing — and `plan_out` writes down whichever plan actually ran, so a battle
    worth repeating can be repeated and one worth arguing with can be edited.

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
    adb = _controller()
    _prepare_frames(options)
    plan = plans.load(options.plan_in) if options.plan_in else None
    config = ConfigStore().load()
    # **Which village to play cannot be asked until one has painted.**
    # `_controller` is satisfied by a pid, so a run started right after a launch
    # reaches here with the loading screen still up — measured, one did so three
    # seconds in, read no village at all, and ended the whole series before round
    # one. This is the same wait a restart already does, and it leaves the camera
    # at the far zoom on the way past, which every coordinate below wants anyway.
    display = _settle_game(adb, WORLD_SETTLE_POLLS, should_stop) or adb.display_for(COC_PACKAGE)
    world = _pick_world(adb, display, options.world)
    if world is None:
        return AttackSeries(
            root=[AttackReport(message=f"沒辦法切到{_WORLDS[options.world]},沒有開打")]
        )
    logger.info("Playing the %s village", world)
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
    # for the reason `AttackOptions.restart_every` gives: a round spent waiting
    # for barracks did not tire the emulator out.
    fought = 0
    # Night battles since the run began, which is what the loot cart is emptied on.
    battles = 0
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
            # the next. Both died in `_empty_cart` below with `result.json`
            # never written, so the 17 and 14 battles they had played were
            # countable only out of `run.log`.
            #
            # **`_restart_emulator` does not already cover this.** It re-points
            # `runner` and `ticker` and says why, but it never touches this
            # function's own pair either — so the scheduled restart has the same
            # `_empty_cart` crash waiting behind it, unreached only because no
            # night series had yet run long enough to hit one. Syncing here
            # rather than at the call covers both restarts and whatever consumer
            # is added to this loop next.
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
            # and the barracks wait both have to ask both ways.
            fighting = report.attacked is not None or report.phases > 0
            if fighting:
                fought += 1
                battles += 1
                # Inside the branch that moved the counter, or a round that
                # matched nobody would pay for the whole trip again against a
                # cart emptied moments earlier.
                _empty_cart(world, battles, adb, display)
            logger.info("Attack finished: %s", report.message)
            _write_plan(options.plan_out, runner.played)
            _log_plan(options.plan_log, len(series.root), runner.played)
            if report.stock_full:
                logger.info("The storages are full; there is nothing left to farm for")
                break
            # A stop that arrived mid-search comes back here having attacked
            # nothing, and a run about to walk away has no reason to wait on
            # barracks first. Falling through to the loop's own check is what
            # logs why the series ended, and `run.log` is the only thing a
            # background run leaves to read while it is still going.
            if not fighting and not should_stop() and (rounds <= 0 or len(series.root) < rounds):
                logger.info("Nothing was attacked; waiting %ds for the army", IDLE_REST)
                if _rest(IDLE_REST, should_stop):
                    break
    return series


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

    survey: BoundarySurvey = BoundarySurvey()

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
    runner = _BoundarySurvey(
        adb=adb, display=display, thresholds=LootThresholds(), frame_dir=frame_dir
    )
    runner.run()
    logger.info("Boundary survey: %s", runner.survey.agreement)
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

    survey: MapSurvey = MapSurvey()
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
    runner = _MapSurvey(adb=adb, display=display, thresholds=LootThresholds(), frame_dir=frame_dir)
    runner.run()
    logger.info("Map survey: %s", runner.survey.summary)
    return runner.survey


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
    # Said here rather than inside the loop: what the runner knows is how many
    # batches it bought, and "已停止" is a fact about this call rather than about
    # the walls. A run stopped before it bought anything is the exception — its
    # own fallback message is a verdict on positions it never tried, and that
    # sentence has been read as "the walls are finished" and taken for it.
    if should_stop():
        report.message = (
            f"已停止，{report.message}" if report.upgrades else "已停止，還沒買成任何一批"
        )
    logger.info(
        "Walls: %s (金幣 %d／聖水 %d)", report.message, report.paid("gold"), report.paid("elixir")
    )
    return report


def builders(frame_dir: Path | None = None) -> BuilderReport:
    """Say who is building what and how much longer, with no window in the way.

    The one thing a village cannot be talked out of is a busy builder, and until
    now nothing here could see past the 1/5 to how long that would last. It is
    read-only and costs three captures, so it is cheap enough to ask before
    deciding whether a run is worth starting at all.
    """
    adb, display = _session(frame_dir)
    report = UpkeepRunner(adb=adb, display=display, frame_dir=frame_dir).builders()
    logger.info("Builders: %s", report.message)
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
    that way.** It goes through `GameRunner._home`, which sails to the day
    village the moment it finds the builder base — so asking it about the
    builder base spends a boat trip and leaves the game on the other village,
    which is a thing the `farm` skill has to warn every session about. This one
    reads whichever village is on screen and reports which that turned out to be.
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
    """
    if shield is None:
        return "這個世界沒有護盾" if world == "night" else "護盾的牌子讀不到,沒辦法說還剩多久"
    if not shield.up:
        return "**沒有護盾**,村莊現在可以被打"
    if shield.remaining is None:
        return "有護盾,但倒數讀不到"
    return f"護盾還有 {spell_out(shield.remaining)}"


def collect(frame_dir: Path | None = None) -> CollectReport:
    """Tap every collector the village has left standing, with no window in the way.

    Collectors stop once they are full, so a village nobody has emptied has spent
    most of its time doing nothing. This is the cheapest thing in the project to
    run and the one worth running most often.
    """
    adb, display = _session(frame_dir)
    # **The builder base has no collectors to sweep and one cart instead.** Its
    # elixir is paid into that cart rather than into the storages, so this is
    # the same job on that village even though it shares none of the machinery:
    # one tap at a known spot rather than a colour-and-size search over the map.
    if current_world(adb.screenshot(display)) == "night":
        gained = collect_cart(adb, display)
        if gained is None:
            return CollectReport(message="鏡頭沒辦法停回定位,這一趟沒有去找聖水車")
        return CollectReport(
            markers=1 if gained else 0,
            elixir=gained,
            message=f"建築大師基地的推車收到聖水 {gained}" if gained else "推車裡沒有東西可以收",
        )
    report = UpkeepRunner(adb=adb, display=display, frame_dir=frame_dir).collect()
    logger.info("Collect: %s", report.message)
    return report


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
    logger.info("Upgrade: %s", report.message)
    return report


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
    logger.info("Hero: %s", report.message)
    return report


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
    logger.info("Donate: %s", report.message)
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
        return WorldReport(found=None, world=None, message="遊戲還沒有畫面,無法判斷世界")
    found = current_world(adb.screenshot(display))
    if go is None or go == found:
        report = WorldReport(found=found, world=found, message=f"目前在{_WORLDS[found]}")
    else:
        report = WorldReport(
            found=found,
            world=(landed := cross(adb, display, go)),
            crossed=landed == go,
            message=f"從{_WORLDS[found]}切到{_WORLDS[landed]}"
            if landed == go
            else f"想切到{_WORLDS[go]},但畫面還停在{_WORLDS[landed]}",
        )
    logger.info("World: %s", report.message)
    return report


# Only ever used to build a message, which is why it is here rather than beside
# the type: nothing in the loops cares what these are called in Chinese.
_WORLDS: dict[World | None, str] = {"day": "日世界", "night": "夜世界", None: "不明的畫面"}


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
    scaled = f"鏡頭{'拉遠' if zoom == 'out' else '拉近'}了 {times} 次"
    if zoom != "out":
        # **A camera that has just been zoomed in cannot be parked**, so asking
        # would undo the thing that was asked for and claim a position it never
        # reached. Measured two pinches in, the camera runs off the village into
        # the map's dark border and was still walking hundreds of pixels per
        # swipe after fourteen of them; `park_camera` pinches back out for
        # exactly that reason, which on this path is the opposite of the request.
        report = ViewReport(message=f"{scaled}，拉近的鏡頭停不住，沒有把鏡頭停回定位")
    elif (world := current_world(adb.screenshot(display))) is None:
        report = ViewReport(message=f"{scaled}，但畫面不是村莊，沒有把鏡頭停回定位")
    elif park_camera(adb, display, world):
        report = ViewReport(message=f"{scaled}，並把鏡頭停回定位")
    else:
        # The one message that used to be a claim rather than a reading. A park
        # that never arrived leaves every remembered coordinate off by however
        # far it was short, and this is the command somebody runs precisely to
        # put those back.
        report = ViewReport(message=f"{scaled}，但鏡頭一直沒有停下來，定位失敗")
    logger.info("View: %s", report.message)
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
# How long to wait for MuMu to mirror the Android clipboard onto Windows.
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
        return VillageExport(tag="", exported_at="", message="還沒有匯出過任何村莊資訊")
    try:
        export = VillageExport.model_validate_json(saved[-1].read_text(encoding="utf-8"))
    except ValueError:
        return VillageExport(
            tag="",
            exported_at="",
            message=f"{saved[-1].name} 不是這個指令存的格式,請重新執行一次 ai_coc export",
        )
    return export.model_copy(
        update={"message": f"{export.tag} 共 {len(export.entities)} 筆,讀自 {saved[-1]}"}
    )


def _copy_village(adb: AdbController, display: DisplayTarget, frame_dir: Path | None) -> str:
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
        raise RuntimeError("點了設定齒輪,但設定視窗沒有打開")
    adb.tap(*MORE_SETTINGS, display)
    time.sleep(PAGE_SETTLE)
    if not more_settings_open(look("more_settings")):
        raise RuntimeError("點了更多設定,但那一頁沒有打開")
    for attempt in range(SCROLL_TRIES):
        spot = export_row(look(f"scroll_{attempt}"))
        if spot is not None:
            break
        adb.swipe(*SETTINGS_SCROLL, SCROLL_MS, display)
        time.sleep(PAGE_SETTLE)
    else:
        raise RuntimeError("捲到底了還是找不到「以 JSON 格式匯出村莊數據」那一列")
    held = read_clipboard()
    clear_clipboard()
    adb.tap(*spot, display)
    try:
        for _ in range(CLIPBOARD_POLLS):
            time.sleep(CLIPBOARD_GAP)
            if payload := read_clipboard():
                return payload
        look("nothing_copied")
        raise RuntimeError(
            "點了複製,但剪貼簿沒有東西。可能是遊戲沒吃到那一下,也可能是 MuMu 沒把 Android 的剪貼簿同步過來"
        )
    finally:
        write_clipboard(held)


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
        return VillageExport(tag="", exported_at="", message="遊戲沒有回到村莊畫面,沒有匯出")
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = _copy_village(adb, display, frame_dir)
        # Parsed inside the same guard, because the clipboard is the one input
        # here that comes from outside: anything else copied during the poll is
        # read as the payload, and a `ValidationError` reaching the top would
        # leave `result.json` unwritten with only the log to reconstruct from.
        snapshot = parse_village_text(payload)
    except RuntimeError as exc:
        return VillageExport(tag="", exported_at="", message=str(exc))
    except ValueError as exc:
        logger.warning("The clipboard payload was not a village: %s", exc)
        return VillageExport(
            tag="",
            exported_at="",
            message="剪貼簿裡的不是村莊資料,複製的當下可能被別的東西蓋過去了",
        )
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
        message=f"{snapshot.tag} 共 {len(named)} 筆,其中 {unnamed} 筆還沒有名字,存到 {path}",
    )
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Export: %s", result.message)
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
