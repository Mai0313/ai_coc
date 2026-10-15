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
from datetime import UTC, datetime, timedelta
import threading
from contextlib import suppress, contextmanager

import psutil
from filelock import Timeout, FileLock
from pydantic import BaseModel, PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    Loan,
    Watch,
    World,
    Caller,
    MapEdge,
    ProbeRay,
    AppConfig,
    MapSurvey,
    NightPlan,
    PlateRole,
    StateFile,
    AttackPlan,
    CartReport,
    PlateCount,
    PlayedPlan,
    SheetOffer,
    StopTarget,
    ViewReport,
    AdbEndpoint,
    CartOutcome,
    NamedEntity,
    PressReport,
    RunnerState,
    ShieldState,
    StockReport,
    ViewOutcome,
    WatchReport,
    WorldReport,
    AttackReport,
    AttackSeries,
    DonateReport,
    FrameReading,
    LaunchReport,
    PressOutcome,
    RestartScope,
    StatusReport,
    WorldOutcome,
    AttackOptions,
    AttackOutcome,
    CollectReport,
    ConfirmReport,
    DisplayTarget,
    DonateOptions,
    DonateOutcome,
    ExportOutcome,
    LaunchOutcome,
    SurveyOutcome,
    VillageEntity,
    VillageExport,
    BoundarySurvey,
    CollectOutcome,
    ConfirmOutcome,
    LootThresholds,
    StorageCapacity,
    EmulatorInstance,
    PlateCountOutcome,
)
from ai_coc.constants import STATE_PATH, COC_PACKAGE, ACCOUNT_JSON_DIR
from ai_coc.adapters.ai import GeminiClient

# A runtime import rather than a TYPE_CHECKING one: `FrameTicker` declares it as
# a field, and a model whose field type is only importable to a type checker
# cannot be built at all.
from ai_coc.adapters.adb import ZOOM_PINCHES, AdbController, AdbControlError
from ai_coc.parsers.clan import donatable_cards
from ai_coc.parsers.home import (
    PANEL_CLOSE,
    running_rows,
    free_builders,
    collect_bubbles,
    build_panel_open,
)
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.parsers.scout import (
    NIGHT_COUNT_WHITE_RATIO,
    in_battle,
    card_count,
    live_cards,
    read_scout,
    read_stock,
    star_bonus,
    battle_over,
    card_groups,
    field_units,
    panel_drawn,
    card_drained,
    freeze_cards,
    skip_offered,
    welcome_back,
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
from ai_coc.adapters.config import ConfigStore, gemini_key
from ai_coc.parsers.village import clock_tower, upgrade_timers, parse_village_text
from ai_coc.adapters.mapping import fetch_entity_mapping
from ai_coc.parsers.boundary import PLAYFIELD, VILLAGE_CENTRE, village_box, boundary_reach
from ai_coc.parsers.building import (
    MENU_ROW_BOX,
    SHEET_CLOSE_BOX,
    sheet_open,
    game_dialog,
    sheet_offer,
    green_buttons,
    upgrade_sheet,
    panel_wall_menu,
    green_button_row,
    menu_spends_gems,
)
from ai_coc.parsers.settings import export_row, window_open, settings_open, more_settings_open
from ai_coc.adapters.emulator import Emulator, EmulatorError
from ai_coc.adapters.ldplayer import LDPlayerAdapter
from ai_coc.adapters.clipboard import read_clipboard, clear_clipboard, write_clipboard

from .ui.clan import ClanRunner
from .ui.world import cross, uncovered, park_camera, collect_cart
from .ui.attack import CARD_ROW_Y, DROP_SETTLE, BATTLE_TIMEOUT, SINGLE_DROP_DELAY, AttackRunner
from .ui.plates import PlateRunner
from .ui.runner import (
    BUY_SETTLE,
    BACK_SETTLE,
    SESSION_TAKEN,
    ScreenRunner,
    SessionTakenError,
    on_hud,
    relogin,
    spell_out,
    refuse_taken,
)
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


def _attached() -> AdbController:
    """The configured instance as it is, for a command that only looks.

    `_controller` goes through `ensure_coc`, which relaunches the game or the
    whole instance when it finds them down. That is driving the emulator, and a
    command that takes no claim must not do it under a loop that holds one.
    """
    emulator, instance = chosen(emulators())
    if instance.endpoint.port == 0:
        raise RuntimeError(
            f"模擬器 {instance.adb_serial} 沒開,這個指令只看不開;要開用 ai_coc launch"
        )
    return emulator.controller(instance.adb_serial)


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

# How the wait for the village ended; reaching it needs no words of its own.
LAUNCH_OUTCOME_LINES: dict[LaunchOutcome, str] = {
    "at_village": "",
    "no_village": ",但村莊沒有出現,畫面可能還在載入",
    "stopped": ",收到停止要求,沒有等村莊出現",
}


def launch_line(report: LaunchReport) -> str:
    """The one line a person reads off a launch."""
    did = LAUNCH_LINES.get(report.restart) or (
        "部落衝突已經在跑" if report.was_running else "已把部落衝突開起來"
    )
    return f"{did}{LAUNCH_OUTCOME_LINES[report.outcome]},模擬器 {report.index} ({report.serial})"


def _keep_frame(path: Path, png: bytes) -> None:
    """Keep the frame a command could not find a village on, for whoever debugs it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    logger.info("Kept the frame no village read on: %s", path)


def _keep_screen(adb: AdbController, path: Path) -> None:
    """`_keep_frame` for what is on screen now, when a wait has already given up."""
    try:
        _keep_frame(path, adb.screenshot(adb.display_for(COC_PACKAGE)))
    except AdbControlError:
        logger.warning("The game is not on a display; there is no frame to keep")


def launch(
    restart: RestartScope, should_stop: Callable[[], bool], keep: Path | None = None
) -> LaunchReport:
    """Bring the game up on the configured instance, tearing down as much as asked.

    Every other headless command assumes the game is already running: they go
    through `_controller`, which calls `ensure_coc` and gives up on whatever it
    cannot fix. This is that step on its own, for the two states it cannot reach
    from there — an emulator or a game that is up and no longer answering, which
    from here looks exactly like a working one. `keep` is where the frame goes
    when the village never appears.
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
    adb = emulator.controller(instance.adb_serial)
    settled = _settle_game(adb, RESTART_POLLS, should_stop)
    # A game whose village never painted is reported rather than raised: the
    # process is up, so the caller may still have something to do with it, and
    # the one thing it must not do is assume the screen is ready.
    if settled is not None:
        outcome: LaunchOutcome = "at_village"
    elif should_stop():
        outcome = "stopped"
    else:
        outcome = "no_village"
        if keep is not None:
            _keep_screen(adb, keep)
    report = LaunchReport(
        index=instance.index,
        serial=instance.adb_serial,
        was_running=was_running,
        restart=restart,
        outcome=outcome,
    )
    logger.info("Launch: %s", launch_line(report))
    return report


def _planner(config: AppConfig) -> GeminiClient | None:
    """A client for the main tier, or None so the attack falls back to its own way.

    None is not an error anywhere: the attack uses its fixed flank. That is
    what keeps a run with no key working exactly as it did before any of this
    existed.
    """
    key = gemini_key(config)
    if not key:
        logger.info("No API key is saved; the loops will fall back to reading the screen alone")
        return None
    return GeminiClient(api_key=key, settings=config.gemini.main)


class FrameTicker(BaseModel):
    """Saves a frame every few seconds for as long as a run lasts.

    The loop's own captures are the ones it reads, and it only looks where it has
    a question; between them a battle can go badly with nothing recorded at all.
    This is the other view — a fixed heartbeat, each frame named by how far into
    the run it was taken and named in the log as it is saved, so a recording
    lines up against the log afterwards.

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
                name = f"tick_{elapsed:07.1f}s.png"
                (self.out_dir / name).write_bytes(png)
                logger.info("Saved frame %s", name)
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

# How many rounds in a row may fail to open the attack menu before the series
# gives up. Measured 2026-10-08: an event's story dialog that `back` does not
# close covered the home village, and a `--repeat 0` series came back
# `no_attack_menu` eleven times in a row, an `IDLE_REST` between each, for about
# 25 minutes until the agent driving it noticed and stopped it. The floor is a
# battle left running, which `uncovered` will not press at and which ends by
# itself: a run in #344 met one, came back `no_attack_menu` twice and attacked on
# its third round. So the rounds have to outlast a whole battle, rests included,
# rather than borrow the three `ADAPTER_FAILURES` uses.
MENU_FAILURES = BATTLE_TIMEOUT // IDLE_REST + 1


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

# How long a command that needs the emulator waits for the run holding it to
# stand down before ending that run itself. A loop stands down only at a seam,
# and the longest way to one is a battle already under way: the builder base
# plays up to two stages, each a plan, a drop and up to `BATTLE_TIMEOUT` of
# waiting, then the result screens and the cart. Three of them covers that with
# a stage to spare, so a holder still up past it is not reaching a seam at all.
TAKEOVER_WAIT = 3 * BATTLE_TIMEOUT
# How many times a claim reads the file again when it will not parse, and how
# far apart. `read_state` answers a torn read as "running, and ours", which at
# claim time would pass for a free emulator while another process is halfway
# through writing its own record.
TORN_READS = 3
TORN_GAP = 0.2
# What a holder's process is called: `uv run` starts Python, and the release
# build is its own executable. Windows reuses pids and ignores their two low
# bits, so a record with nothing else to go on must not read as held by
# whatever process sits at that number now.
HOLDER_NAMES = ("python", "ai_coc")
# How long to wait for another process's turn at the file. A turn is a read and
# a write, milliseconds long, so running out means that process died holding
# the lock, and the write goes ahead unguarded rather than failing the run.
LOCK_WAIT = 10
# The window's own name on the file. It is never ended by a takeover: that
# would close the program somebody may be looking at.
WINDOW_AGENT = "window"
# How long a loan outlives the borrower's last command before the lender may
# start again. Long enough for the thinking and editing between one live test
# and the next, each of which would otherwise borrow afresh and wait out a
# battle; short enough that a borrower who forgot `giveback`, or died, costs
# half an hour of farming. Longer than `TAKEOVER_WAIT`, so a loan does not lapse
# while its borrower is still waiting for the lender's battle to end.
LOAN_TIME = timedelta(minutes=30)

# How often a write tries again when Windows refuses to replace the file, and
# how far apart. It refuses while any other process has the file open, and the
# loops and the watch each open it to read every `STOP_POLL`; a read is open for
# a moment, so a few tries a moment apart get past one.
REPLACE_TRIES = 10
REPLACE_GAP = 0.05

# The loan this process wrote when it stood a run down. A borrower with no
# session has nothing else to recognise its own loan by.
_borrowed: datetime | None = None


class LoanCancelledError(RuntimeError):
    """`ai_coc stop` cancelled the loan a claim was queued behind."""


class NoStateFileError(RuntimeError):
    """`ai_coc watch` found no state file to put itself on.

    Deleting the file by hand is a stop to the emulator's holder, which reads it
    only at its next seam, and a watch that wrote the file back in between would
    hide that stop. Any command that drives the emulator writes it afresh.
    """


@contextmanager
def _turn() -> Iterator[None]:
    """One process at a time through a read and a write of the state file.

    Without it two commands waiting on one holder both read `idle` in the same
    instant and both claim, and a `stopping` written from a stale read lands on
    whichever record replaced it — both of them two processes driving one
    display, which is what the file exists to prevent.
    """
    lock = FileLock(str(STATE_PATH.with_name(f"{STATE_PATH.name}.lock")))
    try:
        lock.acquire(timeout=LOCK_WAIT)
    except Timeout:
        logger.warning("%s stayed locked for %ds; writing without it", STATE_PATH, LOCK_WAIT)
        yield
        return
    try:
        yield
    finally:
        lock.release()


def _started_at(pid: int) -> float | None:
    """When process `pid` started, or None where that cannot be told."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _alive(state: RunnerState | Watch) -> bool:
    """Whether the process a record names is still the one that wrote it.

    Never `os.kill(pid, 0)`: on Windows signal 0 is `CTRL_C_EVENT`, so it sends
    a Ctrl+C to that console group, can kill the very run it asked about, and
    still reads as alive. A process this one cannot inspect is not an `ai_coc`
    run, which is always the same user's.
    """
    if state.pid <= 0 or state.pid == os.getpid():
        return False
    try:
        process = psutil.Process(state.pid)
        name = process.name().lower()
        created = process.create_time()
    except psutil.Error:
        return False
    if not any(part in name for part in HOLDER_NAMES):
        return False
    return state.pid_created is None or created == state.pid_created


def holder() -> RunnerState | None:
    """The live run of another process holding the emulator, if there is one."""
    torn = RunnerState(status="running", pid=os.getpid())
    state = read_state()
    for _ in range(TORN_READS):
        if state != torn or _held is not None:
            break
        time.sleep(TORN_GAP)
        state = read_state()
    if state is None or state.status == "idle" or not _alive(state):
        return None
    return state


def _same(one: Caller, other: Caller) -> bool:
    """Whether two callers are one session. A blank session is nobody's."""
    return bool(one.session) and one.session == other.session


def _borrows(caller: Caller, loan: Loan) -> bool:
    """Whether `caller` is the one this loan was made to."""
    return _same(caller, loan.borrower) or (_borrowed is not None and loan.since == _borrowed)


def _standing(state: RunnerState | None) -> Loan | None:
    """The loan on this record while it still holds the emulator.

    One the borrower is driving on holds for as long as that process lives, so
    a long test is not taken back halfway; an idle one until `until`.
    """
    loan = state.loan if state else None
    if state is None or loan is None or loan.ended is not None:
        return None
    if loan.until is None:
        return loan if _alive(state) else None
    return loan if datetime.now().astimezone() < loan.until else None


def _queue() -> datetime | None:
    """Wait while the emulator is lent out, standing nobody down.

    Answers when the loan it waited behind began, or None when there was none.
    """
    waited: datetime | None = None
    while (loan := _standing(read_state())) is not None:
        if waited is None:
            logger.info(
                "The emulator is lent to %s (%s); waiting for it to come back. "
                "`ai_coc giveback` returns it",
                loan.borrower.agent or "a command with no agent named",
                loan.borrower.mission or "no mission given",
            )
            waited = loan.since
        time.sleep(STOP_POLL)
    return waited


def _carried(state: RunnerState | None, caller: Caller, *, yields: bool) -> Loan | None:
    """The loan the record a claim writes goes on with.

    The borrower's own claim marks it as driven on. A yielding claim only gets
    this far once no loan stands, and drops it, the lender being back at work.
    Anyone else's passes it along.

    One still marked as driven on lost its driver without a release, or this
    claim would not have got through; it lapses from now, as that release
    would have had it. Passed along as it was, whatever process this record
    names would stand in for the dead borrower and hold the loan open.
    """
    loan = state.loan if state else None
    if loan is None or yields:
        return None
    now = datetime.now().astimezone()
    until = loan.until or now + LOAN_TIME
    if loan.ended is None and now < until and _borrows(caller, loan):
        return loan.model_copy(update={"until": None})
    return loan.model_copy(update={"until": until})


def take_over(caller: Caller, wait: float | None = None) -> None:
    """Have the run holding the emulator stand down, and return once it has.

    **The file is a lock**, the user's call of 2026-09-27. A command that needs
    the emulator asks the holder to stand down through the same `stopping` that
    `ai_coc stop` writes, naming who asked, and waits for it to reach its seam
    and release — a battle under way is played out, never abandoned. A holder
    still up after `wait` is ended here and that is logged, rather than left for
    somebody to kill by hand or to delete the file under. The wait starts over
    for each holder, so one that claimed while this waited on another gets its
    own, rather than the rest of its predecessor's.
    """
    wait = TAKEOVER_WAIT if wait is None else wait
    deadline = 0.0
    asked: tuple[int, datetime | None] | None = None
    while (current := holder()) is not None:
        if asked != (current.pid, current.started):
            deadline = time.monotonic() + wait
            ask_to_stand_down(current, caller)
            logger.info(
                "%s (pid %d, %s) holds the emulator; asked it to stand down, waiting up to %.0fs",
                current.command,
                current.pid,
                current.caller.mission or "no mission given",
                wait,
            )
            asked = (current.pid, current.started)
        if time.monotonic() >= deadline:
            _end(current, wait)
            return
        time.sleep(STOP_POLL)


def ask_to_stand_down(held: RunnerState, caller: Caller) -> None:
    """Write `stopping` onto this holder's record, naming who asked.

    Only onto that same record, and only while it still reads `running`: one
    another process wrote in the meantime is not this holder, and a `stopping`
    already there keeps the name of whoever asked first.

    **A yielding run is lent, not stopped**: the record says so before the
    holder stands down, so its agent reads a loan the moment its run ends and
    starts it again with `--yield`, which queues until the loan is handed back.
    Any other run is stopped as `ai_coc stop` would, since nobody is lined up
    to start it again.
    """
    global _borrowed  # noqa: PLW0603 - process-wide by nature; see `_borrowed`
    with _turn():
        current = read_state()
        if (
            current is not None
            and current.status == "running"
            and (current.pid, current.started) == (held.pid, held.started)
        ):
            update: dict[str, object] = {"status": "stopping", "stop_by": caller}
            if current.yields:
                now = datetime.now().astimezone()
                update["loan"] = Loan(
                    lender=current.caller, borrower=caller, since=now, until=now + LOAN_TIME
                )
                _borrowed = now
                logger.info(
                    "Borrowed the emulator from %s (%s); `ai_coc giveback` hands it back",
                    current.caller.agent or "an agent with no name given",
                    current.caller.mission or "no mission given",
                )
            _write_state(current.model_copy(update=update))


def _end(holder: RunnerState, waited: float) -> None:
    """End a holder that never reached its seam, and say who it was."""
    if holder.caller.agent == WINDOW_AGENT:
        raise RuntimeError(
            f"視窗佔著模擬器,請它停了 {waited / 60:.0f} 分鐘還沒放手;請在視窗按停止"
        )
    # A record written before `pid_created` existed was matched on the process
    # name alone, which a Python process that reused the pid also passes.
    if holder.pid_created is None:
        raise RuntimeError(
            f"pid {holder.pid} 佔著模擬器,請它停了 {waited / 60:.0f} 分鐘還沒放手;"
            "它的紀錄是舊版寫的,分不出是不是原本那個程序,沒有結束它"
        )
    logger.warning(
        "%s (pid %d, %s: %s) did not stand down in %.0fs; ending it",
        holder.command,
        holder.pid,
        holder.caller.agent or "no agent named",
        holder.caller.mission or "no mission given",
        waited,
    )
    try:
        process = psutil.Process(holder.pid)
        process.terminate()
    except psutil.NoSuchProcess:
        return
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()


def read_state() -> RunnerState | None:
    """The emulator's section of the state file; None when there is no file at all."""
    found = _read_file()
    return found.emulator if found else None


def read_watch() -> Watch | None:
    """The upgrade watch's section of the state file; None when there is no file at all."""
    found = _read_file()
    return found.watch if found else None


def _torn() -> StateFile:
    """What a read that failed answers: both sections running, and this process's."""
    return StateFile(
        emulator=RunnerState(status="running", pid=os.getpid()),
        watch=Watch(status="running", pid=os.getpid()),
    )


def _read_file() -> StateFile | None:
    """What the state file says; None when there is no file at all.

    **Nothing in here raises, which is the whole point.** `should_stop` is
    called from inside a battle — `AttackRunner` polls it between opponents —
    and the only thing catching anything up there is a `KeyboardInterrupt`
    handler, so an exception raised on a read
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
        return _torn()
    try:
        return StateFile.model_validate_json(text)
    except ValueError:
        logger.warning("%s will not parse; carrying on as if nobody had asked", STATE_PATH)
        return _torn()


def _kept() -> StateFile:
    """The file as it stands, so a write of one section carries the other over.

    A torn read is read again, since writing it back would hand the other
    section to this process; one that stays torn puts that section back to idle,
    which a live holder of it rewrites at its next claim or release.
    """
    for _ in range(TORN_READS):
        found = _read_file()
        if found != _torn():
            return found or StateFile()
        time.sleep(TORN_GAP)
    logger.warning("%s would not read; the other section of it goes back to idle", STATE_PATH)
    return StateFile()


def _write_state(state: RunnerState) -> None:
    """Write the emulator's section, keeping the watch's as the file holds it."""
    _write_file(_kept().model_copy(update={"emulator": state}))


def _write_watch(watch: Watch) -> None:
    """Write the watch's section, keeping the emulator's as the file holds it."""
    _write_file(_kept().model_copy(update={"watch": watch}))


def _write_file(state: StateFile) -> None:
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
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(scratch, STATE_PATH)
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_GAP)
        else:
            return


@contextmanager
def claim(
    command: str, log: Path | None = None, caller: Caller | None = None, *, yields: bool = False
) -> Iterator[None]:
    """Say that this process is driving the emulator, and who asked, and hand it back after.

    Every command that drives the emulator takes one, short ones included: a
    `collect` that runs for eight seconds still holds the screen for those
    eight, and a second session that reads the file wants the truth rather than
    only being told about the long runs. `read` takes none, since it parses a
    PNG and never opens ADB, and neither do `capture` and a plain `world`,
    which only look.

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

    **Another live run holding it is asked to stand down first**, and waited out;
    see `take_over`. The decision to claim is taken again inside the lock,
    because two commands can finish waiting on one holder in the same instant
    and only one of them may win — the other goes back to asking.

    **A yielding claim** (`--yield`, the farming loops) is background work: it
    stands nobody down, and waits until nothing holds the emulator and no loan
    stands (`_queue`), so starting again after a loan, or a `world --go`
    between rounds, never takes the emulator back off whoever borrowed it. Any
    other claim may borrow from it, and never queues behind a loan, so a
    borrower's own next command is not locked out. `ai_coc stop` cancelling the
    loan it queued behind ends it with `LoanCancelledError`, since a stop means
    nothing lined up behind it starts.
    """
    global _held  # noqa: PLW0603 - process-wide by nature; see `_held`
    if _held is not None:
        yield
        return
    who = caller or Caller()
    queued: datetime | None = None
    while True:
        if yields:
            queued = _queue() or queued
            waited: tuple[int, datetime | None] | None = None
            while (current := holder()) is not None:
                if waited != (current.pid, current.started):
                    logger.info(
                        "%s (%s) holds the emulator; waiting for it to finish",
                        current.command,
                        current.caller.mission or "no mission given",
                    )
                    waited = (current.pid, current.started)
                time.sleep(STOP_POLL)
        else:
            take_over(who)
        with _turn():
            state = read_state()
            loan = state.loan if state else None
            if queued and loan and (loan.since, loan.ended) == (queued, "cancelled"):
                raise LoanCancelledError("借用被 ai_coc stop 取消了,這個指令沒有開跑")
            if holder() is None and not (yields and _standing(state)):
                mine = RunnerState(
                    status="running",
                    pid=os.getpid(),
                    pid_created=_started_at(os.getpid()),
                    command=command,
                    started=datetime.now().astimezone(),
                    log=log,
                    caller=who,
                    yields=yields,
                    loan=_carried(state, who, yields=yields),
                )
                _write_state(mine)
                _held = mine
                break
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

    The loan is the file's, not this process's copy, so one handed back while
    this ran stays handed back. A borrower's own release starts its lapse
    clock, or, with no session to borrow again under, hands it straight back.
    """
    global _held, _borrowed  # noqa: PLW0603 - process-wide by nature; see `_held`
    with _turn():
        held = read_state()
        if held is not None and held.pid not in (0, os.getpid()):
            _held = _borrowed = None
            return
        loan = held.loan if held else None
        if (
            loan is not None
            and loan.ended is None
            and loan.until is None
            and _held is not None
            and _borrows(_held.caller, loan)
        ):
            now = datetime.now().astimezone()
            loan = loan.model_copy(
                update={"until": now + LOAN_TIME}
                if _held.caller.session
                else {"ended": "returned"}
            )
        elif loan is not None and loan.ended is None and _held is not None and _held.yields:
            logger.info(
                "Lent to %s (%s); start again with --yield, which waits for it to come back",
                loan.borrower.agent or "a command with no agent named",
                loan.borrower.mission or "no mission given",
            )
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
                # Kept on `idle` so the last run stays attributable after it ends,
                # and so is who stopped it, for the agent whose run it was.
                caller=_held.caller if _held else Caller(),
                stop_by=held.stop_by if held else None,
                loan=loan,
            )
        )
    _held = _borrowed = None


def stop(caller: Caller | None = None, target: StopTarget = "all") -> str:
    """Ask the command driving the emulator, and the upgrade watch, to stand down.

    Nothing here touches the game or looks for a process: this changes fields
    and ends. What actually stops is the loop, when it next looks, and where
    that is belongs to each of them — `attack` between rounds and between
    opponents. Never mid-battle, because one abandoned halfway leaves the
    game on a screen the next run does not know how to get home from.

    Saying so when there is nothing to stop is the half the old flag could not
    do: it wrote a file whether or not anything was listening, so a stop that
    landed and one that fell on an idle machine read identically.

    **One stop reaches both sections**, the user's call of 2026-10-12: a stop
    is everything this project has running. `target` narrows it to one, which
    is how the skills stop their own loop to cross or to pick up a fix without
    ending the watch.
    """
    who = caller or Caller()
    said: list[str] = []
    with _turn():
        if target != "watch":
            said.append(_stop_emulator(read_state(), who))
        if target != "emulator":
            said.append(_stop_watch(read_watch(), who))
    return "".join(said)


def _stop_emulator(state: RunnerState | None, who: Caller) -> str:
    """Mark the emulator's holder `stopping`, and settle its loan.

    **A loan nobody is driving on is cancelled**, so the lender does not start
    again and nothing queued behind it starts either: a stop is a stop, not a
    turn of the emulator. A borrower's command still running is stopped like
    any other and its loan kept, so a test can be stopped without ending the
    farming it borrowed from; a second stop, with nothing running, cancels it.
    """
    loan = _standing(state)
    if state is None or (state.status == "idle" and loan is None):
        return "現在沒有指令在跑,沒有東西要停。"
    driven = loan is not None and loan.until is None
    update: dict[str, object] = {}
    if loan is not None and not driven:
        update["loan"] = loan.model_copy(update={"ended": "cancelled"})
    if state.status != "idle":
        update |= {"status": "stopping", "stop_by": who}
    _write_state(state.model_copy(update=update))
    after = ""
    if loan is not None:
        lender = loan.lender.agent or "借出模擬器的那一邊"
        after = (
            f"借用還在,{lender}之後會自己開回去;要連它一起停,等這個指令停了再下一次 ai_coc stop。"
            if driven
            else f"借用也取消了,{lender}不會自己開回去。"
        )
    if state.status == "idle":
        return f"現在沒有指令在跑;{after}"
    return (
        f"已要求 {state.command}(pid {state.pid})收工,狀態寫在 {STATE_PATH}。"
        f"它會做完手上這一件事才停。{after}"
    )


def _stop_watch(watching: Watch | None, who: Caller) -> str:
    """Mark a live upgrade watch `stopping`; it wakes within a `STOP_POLL`."""
    if watching is None or watching.status == "idle" or not _alive(watching):
        return "沒有人在盯升級。"
    _write_watch(watching.model_copy(update={"status": "stopping", "stop_by": who}))
    return f"已要求{watching.caller.agent or '盯升級的那一邊'}的盯升級收工。"


def giveback() -> str:
    """Hand a borrowed emulator back, so the run it was taken from starts again.

    Anyone may, not only the borrower: one whose agent has died would otherwise
    keep the lender waiting out `LOAN_TIME`. A borrower still driving finishes
    what it is doing first, and the lender waits for that like any other run.
    """
    with _turn():
        state = read_state()
        loan = _standing(state)
        if state is None or loan is None:
            return "現在沒有借出去的模擬器,沒有東西要還。"
        _write_state(
            state.model_copy(update={"loan": loan.model_copy(update={"ended": "returned"})})
        )
    return f"還了,{loan.lender.agent or '借出模擬器的那一邊'}會自己開回去。"


# How long a new watch gives the one it replaces to let go before ending it. A
# watch reads the file every `STOP_POLL` while it sleeps, so one still holding
# on after this many polls is not reading it at all.
WATCH_TAKEOVER = 15 * STOP_POLL


def watch(
    caller: Caller | None, next_at: datetime, about: str, log: Path | None = None
) -> WatchReport:
    """Sleep until `next_at` as the one process watching the upgrade timers.

    The agent runs this in the background and is woken by it ending, so the
    record of a watch and the wake are one thing: a record whose process is
    gone is nobody watching, read off its pid like the emulator's holder. It
    used to be a record and nothing else, and an agent wrote one with nothing
    scheduled to wake it while every other agent took the timers for watched.
    Nothing here touches the game.

    **A live watch already there is stopped and taken over**, this session's
    own included, the user's call of 2026-10-12: the newest request comes from
    a session the user has just been talking to, and the one replaced wakes,
    reads who stopped it and why off its own answer, and stands down.
    """
    who = caller or Caller()
    wake = next_at if next_at.tzinfo else next_at.astimezone()
    mine = _take_watch(who, wake, about, log)
    logger.info("Watching the upgrade timers until %s: %s", f"{wake:%m-%d %H:%M}", about)
    try:
        while (left := (wake - datetime.now().astimezone()).total_seconds()) > 0:
            current = read_watch()
            if current is None or current.status == "stopping" or current.pid != mine.pid:
                break
            time.sleep(min(STOP_POLL, left))
    except KeyboardInterrupt:
        logger.info("Interrupted; letting the watch go")
    return _let_watch_go(mine, wake)


def _take_watch(who: Caller, wake: datetime, about: str, log: Path | None) -> Watch:
    """Stand any live watch down, naming `who`, and put this process in its place.

    The decision is taken inside the turn, as a claim's is, so two newcomers
    finishing their wait together cannot both write themselves in.
    """
    asked: tuple[int, datetime | None] | None = None
    deadline = 0.0
    while True:
        with _turn():
            current = read_watch()
            if current is None:
                raise NoStateFileError(
                    f"{STATE_PATH} 不在(手動刪掉是叫停),這次沒有開始盯升級;"
                    "先跑一個會操作模擬器的指令(例如 ai_coc status)再開"
                )
            if current.status == "idle" or not _alive(current):
                mine = Watch(
                    status="running",
                    pid=os.getpid(),
                    pid_created=_started_at(os.getpid()),
                    started=datetime.now().astimezone(),
                    log=log,
                    caller=who,
                    next_at=wake,
                    about=about,
                )
                _write_watch(mine)
                return mine
            if asked != (current.pid, current.started):
                _write_watch(current.model_copy(update={"status": "stopping", "stop_by": who}))
                logger.info(
                    "%s is watching (%s); asked it to stand down",
                    current.caller.agent or "an agent with no name given",
                    current.about,
                )
                asked = (current.pid, current.started)
                deadline = time.monotonic() + WATCH_TAKEOVER
        # Only a record that names its start time, since a pid alone may have
        # been handed to another process since.
        if time.monotonic() >= deadline and current.pid_created is not None:
            logger.warning(
                "The watch at pid %d did not stand down in %.0fs; ending it",
                current.pid,
                WATCH_TAKEOVER,
            )
            with suppress(psutil.Error):
                psutil.Process(current.pid).terminate()
        time.sleep(STOP_POLL)


def _let_watch_go(mine: Watch, wake: datetime) -> WatchReport:
    """Put this watch back to idle, unless another has replaced it, and say how it ended.

    A file deleted by hand is not written back: that deletion is a stop to the
    emulator's holder as well, and a file back in place before it looks would
    hide it.
    """
    now = datetime.now().astimezone()
    with _turn():
        current = read_watch()
        ours = current is not None and current.pid == mine.pid
        stop_by = None if current is None else current.stop_by if ours else current.caller
        if ours:
            _write_watch(
                mine.model_copy(update={"status": "idle", "ended": now, "stop_by": stop_by})
            )
    stopped = not ours or (current is not None and current.status == "stopping") or now < wake
    report = WatchReport(
        caller=mine.caller,
        next_at=wake,
        about=mine.about,
        ended=now,
        stop_by=stop_by,
        outcome="stopped" if stopped else "due",
    )
    logger.info(
        "The watch ended %s%s",
        report.outcome,
        f", stopped by {stop_by.agent or 'nobody named'} ({stop_by.mission})" if stop_by else "",
    )
    return report


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


# How long `launch` gives a started emulator before deciding it is not coming.
#
# **The wait is not for the launch, it is for a frame a loop could play from.**
# `ensure_coc` returns as soon as it has seen the game's process, and that check
# is a `pidof` — the process exists a couple of seconds after the launch while
# the village is not on screen for much longer than that. `current_world` is
# what settles it, because the corner's + buttons are how everything here tests
# for a village being up at all.
#
# Much longer than `restart_game`'s flat 15 seconds, which only reopens the
# package on an emulator that never went down; this sits through a cold boot.
#
# **And a cold boot is not a fixed cost.** Measured on the same machine within
# one run: the first restart had the village up 22 seconds after `launch`
# returned, and the second one had not got there in 120. So the patience is
# sized for the bad case rather than the good one; a caller that reaches the
# end of it has no village, while one that waits an extra minute has lost a
# minute.
RESTART_POLLS = 45
RESTART_POLL_GAP = 4.0


def _place_camera(adb: AdbController, display: DisplayTarget, world: World, park: bool) -> None:
    """Park the camera, or only take it to the far zoom when nothing will tap the map."""
    if park:
        park_camera(adb, display, world)
    else:
        adb.zoom("out", ZOOM_PINCHES, COC_PACKAGE, display)


def _settle_game(
    adb: AdbController,
    polls: int,
    should_stop: Callable[[], bool] = lambda: False,
    park: bool = True,
) -> DisplayTarget | None:
    """Wait for a village that can be tapped, then put the camera where the coordinates are.

    Two steps that always belong together, because every coordinate in this
    project was measured against one particular view: a village on screen, at
    a free scenery's far zoom limit, which the park brings a bought one back
    to. A caller that has one without the other has a game that answers and
    misses everything it aims at.

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

    `park` False leaves the camera where it is at the far zoom: the attack taps
    no map coordinate before its battle, which measures its own camera, and a
    park there only came before the cart visit's own on the builder base.
    """
    waiting = "nothing was tried"
    relogged = False
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
                # this function means no village appeared, which `launch` reports
                # as `no_village`; a park that fell short is a worse view
                # rather than no village, and nothing this hands back depends on
                # it — the attack menu is a fixed screen corner and the battle
                # camera is measured per battle by `_settle_camera`. The park
                # logs its own warning, which is where that belongs.
                _place_camera(adb, display, world, park)
                return display
            # A session the server dropped would otherwise sit under its dialog
            # for the whole wait, since nothing else here answers one. Once,
            # because a login that lands back on the dialog is the server
            # still down, and the loading screen after it is the state to wait
            # in rather than restart out of: measured across two outages,
            # neither a game nor an emulator restart shortened one.
            if idle_disconnected(png):
                refuse_taken(png)
                waiting = "the session is dropped and the one login did not clear it"
                if not relogged:
                    logger.warning("The session was dropped; logging in again")
                    relogin(adb, display)
                    relogged = True
                    waiting = "the game is logging in again"
            elif (pressed := _press_away(adb, display, png)) is not None:
                waiting = pressed
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


def _press_away(adb: AdbController, display: DisplayTarget, png: bytes) -> str | None:
    """Press away a cover the game came up under, and say which; None where there is none.

    The raid report the game opens on after the village was attacked gets
    `back`, and so does the 勝利之星 reward sheet. The build panel shifts the
    corner column, so the village under it never reads; `back` leaves it up,
    and its own X shuts it.
    """
    if welcome_back(png):
        logger.info("The raid report is covering the village; pressing back")
        adb.back(display)
        return "the raid report was covering the village"
    if star_bonus(png):
        logger.info("The star bonus sheet is covering the village; pressing back")
        adb.back(display)
        return "the star bonus sheet was covering the village"
    if build_panel_open(png):
        logger.info("The build panel is covering the village; pressing its X")
        adb.tap(*PANEL_CLOSE, display)
        return "the build panel was covering the village"
    return None


def _prepare_frames(options: AttackOptions) -> None:
    """Make room for whatever this run was told to keep, and refuse what it cannot.

    A heartbeat with nowhere to write is the one combination that has to fail
    loudly rather than quietly recording nothing: `--shot-every` is asked for by
    somebody who wants to look at the frames afterwards.
    """
    if options.frame_dir is not None:
        options.frame_dir.mkdir(parents=True, exist_ok=True)
    elif options.shot_every > 0:
        raise ValueError("--shot-every 要搭配 --debug，不然心跳畫面沒有地方放")


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
    "builder_free": "這個村莊有工人閒著,停止刷資源",
    "lab_free": "這個村莊的實驗室有空檔,停止刷資源",
    "no_opponent": "等不到對手,已放棄這一輪搜尋",
    "all_skipped": "跳過的對手都未達門檻,已結束搜尋",
    "stopped": "收到停止要求,未開打就離開",
    "no_attack_menu": "沒有開啟攻擊選單就停手",
    "server_loading": "遊戲卡在載入畫面,伺服器可能連不上,這一輪停手",
    "server_flapping": "遊戲又回到載入畫面,伺服器可能還連不上,這一輪停手",
    "emulator_silent": "模擬器沒有回應",
    "other_village": "遊戲停在另一個村莊,這一批不打那邊;換村莊用 ai_coc world --go",
    "session_taken": "另一部裝置登入了這個帳號,沒有把登入搶回來,停止刷資源",
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
    """One round, with the ways it can end badly folded in. None ends the series.

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
    # Read before the battle; one read during it the runner reports itself, so
    # the round keeps the battle it fought. `_series_over` ends on either.
    except SessionTakenError:
        return AttackReport(world=runner.world, outcome="session_taken")
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

    Six outcomes do. A full storage is the goal being met, and `farm` owns where
    that leads; there is nothing left for the round loop to farm for either
    way. An idle builder or research slot on a run asked to watch them is the
    other goal. The game
    standing on the other village is the third: nothing here sails, so every
    round after would find it there too. Another device logging in, the one of
    these that can come mid-battle, is the fourth: the player is on, and the
    next round would log them out.

    **An army short of the camp is the last one, and it is a fault rather than
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
    if report.outcome in ("builder_free", "lab_free", "other_village", "session_taken"):
        return True
    if report.outcome == "army_short":
        logger.error(
            "The army is under half the camp and training is instant, so this will not clear "
            "on its own; ending the series after %d round(s)",
            played,
        )
        return True
    return False


def _menu_out_of_reach(series: AttackSeries) -> bool:
    """Whether the last few rounds all failed to open the attack menu, which ends the series.

    Whatever stood in the way outlasted a whole battle, so every round after
    would walk into it again. It can be a screen `back` will not close, a session
    that keeps dropping or somebody playing on a phone; this cannot tell which,
    so the line says only that the series ended, and each round's own lines say
    what it met. The last round keeps its `no_attack_menu`, as `emulator_silent`
    does when its own run ends a series.
    """
    last = series.root[-MENU_FAILURES:]
    if len(last) < MENU_FAILURES or any(report.outcome != "no_attack_menu" for report in last):
        return False
    logger.error(
        "The attack menu has not opened for %d rounds in a row; ending the series", MENU_FAILURES
    )
    return True


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
    # one. This is the same wait a restart already does, at the far zoom the
    # storage bars read best at, and without the park: nothing below taps the map.
    display = _settle_game(adb, WORLD_SETTLE_POLLS, should_stop, park=False) or adb.display_for(
        COC_PACKAGE
    )
    runner = AttackRunner(
        adb=adb,
        display=display,
        world=world,
        thresholds=options.minimums.over(config.thresholds),
        # One setting for both villages, because it is a share of whatever the
        # storages hold rather than an amount: the runner reads each village's
        # own ceilings off its bars, so the same 90% means one thing here and
        # another on the builder base without anybody typing either number.
        # The flag overrides it for one run, like the loot thresholds above.
        stop_at=config.stop_at if options.stop_at is None else options.stop_at,
        until_idle=options.until_idle,
        ai=None if plan else _planner(config),
        plan=plan,
        should_stop=should_stop,
        frame_dir=options.frame_dir,
    )
    series = AttackSeries()
    rounds = options.rounds
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
            # `run.log`. Syncing here covers whatever consumer is added to this
            # loop next.
            #
            # Not every `Status: -2` is this: one recorded series lost a display
            # with no restart at all and died in `uncovered`, where the runner's
            # own copy is the stale one. That one is still open.
            adb, display = runner.adb, runner.display
            ticker.adb, ticker.display = adb, display
            series.root.append(report)
            # A builder base round reports no `attacked` — there is no scout
            # screen to have advertised any loot — so the two villages answer
            # "did this round really fight" differently and the wait between
            # rounds has to ask both ways.
            fighting = report.attacked is not None or report.phases > 0
            logger.info("Attack finished: %s", round_line(report))
            _write_plan(options.plan_out, runner.played)
            _log_plan(options.plan_log, len(series.root), runner.played)
            # A stop is let through first, so the series ends on its own line
            # and a loan reads as one rather than as a run that gave up.
            if _series_over(report, len(series.root)) or (
                not should_stop() and _menu_out_of_reach(series)
            ):
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


def _survey_battle(runner: AttackRunner) -> None:
    """Spend a survey's battle, standing down like any other command if another device takes it.

    The round reports a takeover as an outcome, which a survey would otherwise
    ignore: it handed back rays measured under the dialog and exited 0, leaving
    a borrower nothing that says to run `ai_coc stop` rather than give back.
    """
    if runner.run().outcome == "session_taken":
        raise SessionTakenError(SESSION_TAKEN)


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
    _survey_battle(runner)
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
    _survey_battle(runner)
    logger.info("Map survey: %s", SURVEY_LINES["surveyed"].format(result=runner.survey.summary))
    return runner.survey


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


def _entity_names() -> dict[int, str]:
    """The community mapping's names, or none at all where it cannot be had.

    `status` wants the names to make its timers readable and nothing more, so a
    machine offline with no cached copy still gets every timer, unnamed, rather
    than losing the whole reading to the download.
    """
    try:
        return fetch_entity_mapping().names()
    except OSError:
        logger.warning("No name mapping to be had; the timers come back unnamed", exc_info=True)
        return {}


def status(frame_dir: Path | None = None) -> StatusReport:
    """When every upgrade on both villages finishes, and what the village on screen holds.

    The timers come off the game's own export, worked through whatever
    speed-ups are running, so both villages are read without sailing and no
    model is asked anything. The plates' idle counts, the storages and the
    shield come off the village on screen, which is the only one that draws
    them.
    """
    names = _entity_names()
    adb = _controller()
    settled = _settle_game(adb, WORLD_SETTLE_POLLS)
    display = settled or adb.display_for(COC_PACKAGE)
    runner = PlateRunner(adb=adb, display=display, frame_dir=frame_dir)
    # **The first reading is the check**, so there is no capture to take ahead
    # of it: the plates answer `not_a_village` for a panel over the village, and
    # clearing one then costs one retry rather than a capture on every run.
    builder, lab = runner.counts()
    if builder.outcome == "not_a_village" and uncovered(adb, display) is not None:
        builder, lab = runner.counts()
    shield = runner.shield()
    # **Only on a village**, because every tap of the walk aims at a fixed place
    # and `uncovered` leaves a battle alone on purpose: one of those taps on a
    # battle can deploy the card selected there. And **ahead of the storages**,
    # whose ceilings are read by opening a tooltip on each bar that can be left
    # standing over the gear.
    exported = (
        _exported(adb, display, frame_dir, names)
        if settled is not None and builder.world is not None
        else VillageExport(tag="", exported_at="", outcome="no_village")
    )
    report = StatusReport(
        builder=builder,
        lab=lab,
        stock=stock_of(runner, adb, display),
        shield=shield,
        export=exported.outcome,
    )
    if exported.outcome == "exported":
        report.exported_at = exported.exported_at
        report.boosts = exported.boosts
        report.timers = upgrade_timers(exported)
        report.clock_tower = clock_tower(exported)
    report.world = builder.world or lab.world or report.stock.world
    logger.info(
        "Status: %s；%s；%s；%s",
        _count_line(builder),
        _count_line(lab),
        _timers_line(report),
        _shield_line(report.shield, report.world),
    )
    return report


# One line per way a plate's count can come back, for `run.log`.
COUNT_LINES: dict[PlateCountOutcome, str] = {
    "counted": "{held} {free}/{total}",
    "unread": "{held}的數字讀不到",
    "not_a_village": "畫面不在村莊,讀不到右下的數字",
}


def _count_line(count: PlateCount) -> str:
    """One plate's count as a line for a person."""
    return COUNT_LINES[count.outcome].format(
        held=PLATE_NAMES[count.role], free=count.free, total=count.total
    )


def _timers_line(report: StatusReport) -> str:
    """The soonest upgrade to finish, or why there is none to name."""
    if report.export != "exported":
        return EXPORT_LINES[report.export].format(tag="", count=0)
    if not report.timers:
        return "兩個世界都沒有在升的"
    soonest = report.timers[0]
    return (
        f"最快好的是 {soonest.world} 的 {soonest.name or soonest.section},{soonest.done_at[11:16]}"
    )


# Only ever used to build a log line, which is why these live here rather than
# with the reader: nothing under `ui/` cares what a plate is called, and these
# were the last Chinese *values* `ui/plates.py` held — what is left there quotes
# the game's own screen in a comment.
PLATE_NAMES: dict[PlateRole, str] = {"lab": "實驗室", "builder": "建築工人"}


def _shield_line(shield: ShieldState | None, world: World | None) -> str:
    """What the shield plate is worth saying, including that there is none to say.

    **No shield is the loud answer rather than the quiet one**: it means the
    village is open to being raided right now, which is the state a full one
    should never be left in.

    **`world` is here because the runner's None covers two things and they are
    not the same news.** The builder base has no shield plate at all, which is
    structural; on a home village it is a bar this frame could not read, and
    the line says so rather than guessing.

    **There are two Nones here and they sit one level apart.** No `ShieldState`
    is a plate this frame could not read; a `ShieldState` with no `remaining` is
    the plate read and saying 無. A third line used to sit between them for a
    shield up with an unreadable countdown, and `shield_state` could not produce
    it — see `ShieldState.up` for why that combination is not a state.
    """
    if shield is None:
        return "這個世界沒有護盾" if world == "night" else "護盾那一條讀不到,沒辦法說還剩多久"
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
    "stock_unread": "{markers} 個採集器的標記點掉了,但之後讀不到儲量",
}


def collect_line(report: CollectReport) -> str:
    """The one line a person reads, from whichever village the run was on.

    The builder base's trip has its own answers and they are not a subset
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

    **The builder base's cart has answers of its own, and this used to squash them into two.**
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


def world(go: World | None = None, keep: Path | None = None) -> WorldReport:
    """Which village the game is on, and sail to the other one when asked for it.

    Reading is the default and crossing is the exception: this is the question
    every other command has to ask itself now that the game reopens on whichever
    village it was closed on, and the answer alone is worth having.

    **Reading really is one capture and nothing else** — no swipe, no tap, and
    no pinch, which is what lets a session ask it at any moment without first
    working out what it would disturb. It went through `_settle_game` for a
    while, and that zooms the camera back out on its way past: harmless by this
    project's own measurement, and still enough to make the claim false. The
    patience that call also brings is not this command's to spend: a game still
    on its loading screen honestly has no village on it, and `ai_coc launch` is
    the one that waits a cold start out. `keep` is where the frame goes when no
    village reads on it.

    **Reading takes no claim and starts nothing** (`_attached`): it runs beside a
    loop that holds the emulator without disturbing it. Crossing drives the game,
    so it goes through `_controller` and the claim like any other command.
    """
    adb = _controller() if go is not None else _attached()
    try:
        display = adb.display_for(COC_PACKAGE)
    except AdbControlError:
        logger.warning("The game is not on a display yet; neither village can be confirmed")
        return WorldReport(found=None, world=None, outcome="no_display")
    png = adb.screenshot(display)
    found = current_world(png)
    if found is None and keep is not None:
        _keep_frame(keep, png)
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


# The village export lives behind the avatar → 設定 tab → 更多設定 → scroll →
# 複製, and every one of these was measured on the live game at 1600x900 after
# the 2026-10 overhaul moved settings into the profile window. The copy button
# is not here because `export_row` returns it: it is the one step that must not
# be tapped on an unverified screen, so its coordinate travels with the reader
# that confirms the row.
PROFILE_AVATAR = (85, 100)
SETTINGS_TAB = (1265, 78)
MORE_SETTINGS = (802, 797)
CLOSE_SETTINGS = (1398, 70)
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
            logger.info("Saved frame %s.png", label)
        return png

    adb.tap(*PROFILE_AVATAR, display)
    time.sleep(PAGE_SETTLE)
    if not window_open(look("profile")):
        return "", "settings_shut"
    adb.tap(*SETTINGS_TAB, display)
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
    "settings_shut": "點了頭像跟「設定」分頁,但設定頁沒有打開",
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
    return _exported(adb, display, frame_dir, names)


def _exported(
    adb: AdbController, display: DisplayTarget, frame_dir: Path | None, names: dict[int, str]
) -> VillageExport:
    """The walk through the settings menus and everything after it, on a village already up.

    Shared with `status`, which reads its timers off the same payload. The
    caller is the one that knows a village is on screen: every tap here aims at
    a fixed place, and one aimed at a battle can deploy whatever card is
    selected there.
    """
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
        # Whatever happened, the profile window is left covering the village,
        # and every other command starts by assuming one is on screen. Only
        # where it is up: with none, that place is the gold bar.
        if window_open(adb.screenshot(display)):
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

    It only looks, so it takes no claim and starts nothing (`_attached`): a game
    that is down is `ai_coc launch`'s to bring up.
    """
    adb = _attached()
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


# The build panel's rows, its tabs and every sheet a row opens are driven by the
# agent reading the frames these leave behind, so these are the hands and not
# the eyes: they press where they are told and report what the screen became.
# What they refuse is the only judgement they make, and it is about money.
PRESS_SETTLE = 1.5
SWIPE_TIME = 1200
# A tap may press a dialog's 取消 and nothing else on it. The two buttons are
# measured at x 476-771 and 825-1122, so this reach around 取消's middle covers
# that button and stops well short of 確定.
CANCEL_REACH = (140, 35)
# Where the build panel starts across the screen; its rows sit right of this.
PANEL_LEFT = 1170
# The bars and their + buttons, which the open panel pushes left by its own width.
PANEL_HUD_BOX = (810, 0, 1170, 330)
# How many times `confirm` looks after its press, this far apart, and how many
# of those looks in a row must show the panel or the village back before it
# calls the purchase done. More than one, because the wall loop measured the
# first capture after a purchase missing a dialog that was still sliding in.
CONFIRM_LOOKS = 4
CONFIRM_GAP = 0.8
CONFIRM_SETTLED = 2

PRESS_LINES: dict[PressOutcome, str] = {
    "pressed": "已經按下去",
    "sheet_up": "升級確認視窗開著,只能按它的 X;要買用 ai_coc confirm",
    "dialog_up": "遊戲的對話框開著,只能按取消",
    "on_hud": "那裡是遊戲自己的按鈕(商店、加號、頭像那一排),不按",
    "menu_gems": "建築選單那一排有寶石或道具的按鈕(立即完成、取消升級、加速),不按",
    "green_button": "那是綠色的按鈕(確認、立即完成、寶石補位都是綠的),要買用 ai_coc confirm",
    "running_row": "那一列正在升級,點下去是立即完成(寶石)或那棟的選單,不按",
    "in_battle": "畫面在戰鬥中,按下去會出兵,不按",
}


def press_line(report: PressReport) -> str:
    """What one tap or swipe was asked to do, and whether it did."""
    gesture = "點" if report.gesture == "tap" else "滑"
    points = " → ".join(f"({x}, {y})" for x, y in report.points)
    return f"{gesture} {points}:{PRESS_LINES[report.outcome]}"


def _inside(point: tuple[int, int], box: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = box
    return left <= point[0] <= right and top <= point[1] <= bottom


def _refused(png: bytes, point: tuple[int, int]) -> PressOutcome | None:
    """Why a press here must not happen on this frame, or None when it may.

    **Every refusal is a place where a press spends or ruins something.** An
    upgrade sheet's green button is 確認 on one sheet and 立即完成 for gems on
    the next, and its other 確認 pays with a magic item, so a sheet takes its X
    and nothing else: buying goes through `confirm`, which reads the button
    first. A dialog's 確定 is the same green whether it pays for a wall or quits
    the game. The HUD's + buttons and its shop open the gem store. A running
    row on the build panel opens 立即完成, or a menu whose 取消 throws the
    upgrade away. And a press in a battle deploys whatever card is selected.
    """
    if in_battle(png):
        return "in_battle"
    # The sheet before the dialog: the builder base's sheet is pale enough to
    # pass for one, and its X is the only way out of it.
    if (world := sheet_open(png)) is not None:
        return None if _inside(point, SHEET_CLOSE_BOX[world]) else "sheet_up"
    if (dialog := game_dialog(png)) is not None:
        across, down = CANCEL_REACH
        near = (
            abs(point[0] - dialog.cancel[0]) <= across and abs(point[1] - dialog.cancel[1]) <= down
        )
        return None if near else "dialog_up"
    checks: tuple[tuple[PressOutcome, Callable[[], bool]], ...] = (
        (
            "on_hud",
            lambda: (
                (current_world(png) is not None and on_hud(point))
                or (build_panel_open(png) and _inside(point, PANEL_HUD_BOX))
            ),
        ),
        ("green_button", lambda: any(_inside(point, box) for box in green_buttons(png))),
        (
            "running_row",
            lambda: (
                point[0] >= PANEL_LEFT
                and any(top <= point[1] <= bottom for top, bottom in running_rows(png))
            ),
        ),
        (
            "menu_gems",
            lambda: (
                menu_spends_gems(png, point)
                or (_inside(point, MENU_ROW_BOX) and green_button_row(png))
            ),
        ),
    )
    return next((outcome for outcome, refused in checks if refused()), None)


def _pressed(
    gesture: Literal["tap", "swipe"],
    points: list[tuple[int, int]],
    frame_dir: Path,
    press: Callable[[AdbController, DisplayTarget], None],
    wait: float,
) -> PressReport:
    """Refuse or press, and keep the frame that says which.

    `_attached` rather than `_controller`: nothing is relaunched or pressed on
    the way in, so a panel the agent opened is still open when it gets here.
    A swipe is refused on a sheet even from its X, since only a tap closes one.
    """
    adb = _attached()
    display = adb.display_for(COC_PACKAGE)
    frame_dir.mkdir(parents=True, exist_ok=True)
    before = adb.screenshot(display)
    refusal = _refused(before, points[0])
    if refusal is None and gesture == "swipe" and sheet_open(before):
        refusal = "sheet_up"
    if refusal is not None:
        frame = frame_dir / "refused.png"
        frame.write_bytes(before)
        report = PressReport(gesture=gesture, points=points, outcome=refusal, frame=frame)
    else:
        press(adb, display)
        time.sleep(wait)
        frame = frame_dir / "after.png"
        frame.write_bytes(adb.screenshot(display))
        report = PressReport(gesture=gesture, points=points, outcome="pressed", frame=frame)
    logger.info("Press: %s, frame %s", press_line(report), frame)
    return report


def tap(point: tuple[int, int], frame_dir: Path, wait: float = PRESS_SETTLE) -> PressReport:
    """Tap one point, unless the screen makes that a purchase, and show what followed."""
    return _pressed("tap", [point], frame_dir, lambda adb, display: adb.tap(*point, display), wait)


def swipe(
    start: tuple[int, int],
    end: tuple[int, int],
    frame_dir: Path,
    duration: int = SWIPE_TIME,
    wait: float = PRESS_SETTLE,
) -> PressReport:
    """Drag from one point to another, the way the build panel scrolls.

    Slow by default, because a fast fling keeps the panel coasting past the
    frame taken after it: at 1200 ms three fresh opens scrolled four times each
    stopped within 4 px of one another, against rows about 71 px tall.
    """
    return _pressed(
        "swipe",
        [start, end],
        frame_dir,
        lambda adb, display: adb.swipe(start, end, duration, display),
        wait,
    )


_RESOURCE_NAMES = {"gold": "金幣", "elixir": "聖水", "dark": "黑水"}

CONFIRM_LINES: dict[ConfirmOutcome, str] = {
    "confirmed": "按了確認,視窗關了",
    "no_sheet": "畫面上沒有升級確認視窗,什麼都沒按",
    "not_a_resource": "確認鈕上不是金幣、聖水或黑水的價格(寶石或道具),什麼都沒按",
    "price_mismatch": "確認鈕上的價格跟給的不一樣,什麼都沒按",
    "cannot_afford": "價格是紅字,資源不夠;按下去會跳寶石補差額,什麼都沒按",
    "refused_by_game": "按了確認,遊戲跳出對話框(多半是資源不夠或工人都在忙),已經按取消",
    "sheet_stayed": "按了確認,但視窗還開著,沒有再按",
    "unexpected": "按了確認,但出現認不得的畫面,沒有再按",
    "no_wall_menu": "旁邊沒有開著的城牆選單(或已經有視窗、對話框蓋著),什麼都沒按;已經自己按了升級鈕的話,用不帶 --pay 的 confirm",
    "sheet_disagrees": "按了城牆的升級鈕,跳出的確認視窗跟按鈕對不上,視窗留著沒按",
    "no_confirmation": "按了城牆的升級鈕,沒有跳出確認視窗或對話框;城牆到頂時會跳灰色確認鈕的視窗,看圖再按 X",
}


def confirm_line(report: ConfirmReport) -> str:
    """What the sheet asked, what was expected, and what came of it."""
    read = (
        "讀不到"
        if report.offer is None
        else f"{report.offer.price:,} {_RESOURCE_NAMES[report.offer.resource]}"
    )
    if report.wall is not None:
        wall = f"{report.wall.price:,} {_RESOURCE_NAMES[report.wall.resource]}"
        return f"城牆升級鈕 {wall},確認鈕 {read},預期 {report.asked:,}:{CONFIRM_LINES[report.outcome]}"
    return f"確認鈕 {read},預期 {report.asked:,}:{CONFIRM_LINES[report.outcome]}"


def _after_confirm(adb: AdbController, display: DisplayTarget) -> tuple[ConfirmOutcome, bytes]:
    """What the press on 確認 came to, judged over several looks.

    A dialog at any look gets its 取消 and ends it. Only `CONFIRM_SETTLED`
    looks in a row showing the panel or the village back, with no sheet over
    them, count as done; anything else is left exactly as it stands.
    """
    settled = 0
    outcome: ConfirmOutcome = "unexpected"
    after = b""
    for look in range(CONFIRM_LOOKS):
        time.sleep(BUY_SETTLE if look == 0 else CONFIRM_GAP)
        after = adb.screenshot(display)
        # The sheet first, for the reason `_refused` gives: read as a dialog,
        # a builder base sheet still standing would have its "取消" pressed,
        # and that is wherever the dialog reader thought it saw orange.
        if sheet_open(after):
            settled, outcome = 0, "sheet_stayed"
            continue
        if (dialog := game_dialog(after)) is not None:
            adb.tap(*dialog.cancel, display)
            time.sleep(BACK_SETTLE)
            return "refused_by_game", adb.screenshot(display)
        if build_panel_open(after) or current_world(after) is not None:
            settled += 1
            if settled >= CONFIRM_SETTLED:
                return "confirmed", after
        else:
            settled, outcome = 0, "unexpected"
    return outcome, after


def _raised(
    adb: AdbController, display: DisplayTarget, price: int, pay: Literal["gold", "elixir"]
) -> tuple[ConfirmOutcome, SheetOffer | None, bytes]:
    """Pay what a wall's price button raised: a single wall's sheet, or a batch's dialog.

    **The dialog's 確定 is pressed on the strength of who raised it**: it is
    the 升級城牆 confirmation only because this
    pressed a wall's price button a moment ago, after reading that price as the
    one asked and in white, and with no sheet or dialog standing before it. Its
    own pixels cannot say so: 確定退出遊戲嗎 is the same panel with the same
    buttons, and no dialog priced in gems has ever been recorded to measure
    against. A sheet is held to the price and resource like any other. The
    sheet is looked for first, for the reason `_refused` gives.
    """
    seen: SheetOffer | None = None
    sheet = False
    after = b""
    for look in range(CONFIRM_LOOKS):
        time.sleep(BUY_SETTLE if look == 0 else CONFIRM_GAP)
        after = adb.screenshot(display)
        if sheet_open(after):
            sheet, seen = True, sheet_offer(after)
            if (
                seen is not None
                and seen.price == price
                and seen.resource == pay
                and seen.affordable
            ):
                adb.tap(*seen.point, display)
                outcome, after = _after_confirm(adb, display)
                return outcome, seen, after
            continue
        if (dialog := game_dialog(after)) is not None:
            adb.tap(*dialog.confirm, display)
            outcome, after = _after_confirm(adb, display)
            return outcome, None, after
    return ("sheet_disagrees" if sheet else "no_confirmation"), seen, after


def _pay_wall(
    adb: AdbController,
    display: DisplayTarget,
    before: bytes,
    price: int,
    pay: Literal["gold", "elixir"],
) -> tuple[ConfirmOutcome, SheetOffer | None, SheetOffer | None, bytes]:
    """Read the wall's price button for `pay`, press it if it agrees, and pay what it raised.

    A sheet or a dialog already standing means no wall menu, whatever is drawn
    under it: the dialog `_raised` presses has to be one this raised.
    """
    menu = None if sheet_open(before) or game_dialog(before) else panel_wall_menu(before)
    wall = None if menu is None else menu.gold if pay == "gold" else menu.elixir
    if wall is None:
        return "no_wall_menu", None, None, before
    if wall.price != price:
        return "price_mismatch", None, wall, before
    if not wall.affordable:
        return "cannot_afford", None, wall, before
    adb.tap(*wall.point, display)
    outcome, offer, after = _raised(adb, display, price, pay)
    return outcome, offer, wall, after


def confirm(
    price: int, frame_dir: Path, pay: Literal["gold", "elixir"] | None = None
) -> ConfirmReport:
    """Press the upgrade sheet's 確認 if it asks for exactly this many of a resource.

    **The one command here that spends**, and so the one that reads before it
    presses: the button must carry a gold, elixir or dark icon (立即完成 on a
    running upgrade carries a gem in the same place) and the price written on
    it must be the one the agent read off the panel row, which is what keeps a
    tap on the wrong row from buying the wrong thing. It presses once. Whatever
    the game raises after that — not enough of the resource, every builder
    busy, each of which it answers with a gem offer — gets 取消 or is left
    alone, never a second press.

    **With `pay`, it starts a step earlier, on a wall's menu beside the build
    panel**: that resource's price button is pressed by this rather than by a
    `tap`, only when it asks exactly `price` and in white, and then whatever it
    raised is paid (`_raised`). The same reading-before-pressing holds for the
    button as for the sheet.
    """
    adb = _attached()
    display = adb.display_for(COC_PACKAGE)
    frame_dir.mkdir(parents=True, exist_ok=True)
    before = adb.screenshot(display)
    after = before
    wall: SheetOffer | None = None
    if pay is not None:
        outcome, offer, wall, after = _pay_wall(adb, display, before, price, pay)
    else:
        offer = sheet_offer(before)
        if not sheet_open(before):
            outcome = "no_sheet"
        elif offer is None:
            outcome = "not_a_resource"
        elif offer.price != price:
            outcome = "price_mismatch"
        elif not offer.affordable:
            outcome = "cannot_afford"
        else:
            adb.tap(*offer.point, display)
            outcome, after = _after_confirm(adb, display)
    frame = frame_dir / "after.png"
    frame.write_bytes(after)
    report = ConfirmReport(asked=price, offer=offer, wall=wall, outcome=outcome, frame=frame)
    logger.info("Confirm: %s, frame %s", confirm_line(report), frame)
    return report


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
        panel_wall=panel_wall_menu(png),
        bubbles=collect_bubbles(png),
        builders=free_builders(png),
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
        welcome_back=welcome_back(png),
        loading=loading_screen(png),
        settings_menu=settings_open(png),
        more_settings=more_settings_open(png),
        export_row=export_row(png),
        card_groups=groups,
        counted=counted_cards(png, slots),
        night_counted=counted_cards(png, slots, NIGHT_COUNT_WHITE_RATIO),
        freezes=freeze_cards(png, slots),
        live=live_cards(png, slots),
        on_field=field_units(png, slots),
        selected=selected_cards(png, slots),
        counts={slot: card_count(png, slot) for slot in slots},
        upgrade_sheet=upgrade_sheet(png),
        village_box=village_box(png),
    )
