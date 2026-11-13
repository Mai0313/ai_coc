"""The headless side of the application: what `cli.py` runs when given a command.

These functions are the wiring around the loops under `ui/`: find the emulator,
resolve the display it put the game on, hand over the API key.
"""

from __future__ import annotations

import os
import re
import math
import time
from typing import TYPE_CHECKING, Self, Literal
import logging
from datetime import UTC, date, datetime, timedelta
import itertools
import threading
from contextlib import suppress, contextmanager
import statistics
from collections import Counter

import psutil
from filelock import Timeout, FileLock
from pydantic import BaseModel, PrivateAttr

from ai_coc import plans, logging_setup
from ai_coc.models import (
    Hold,
    Loan,
    Notes,
    Watch,
    World,
    Caller,
    MapEdge,
    FrameLog,
    HoldStop,
    LoanStop,
    ProbeRay,
    AppConfig,
    AttackRun,
    ExportRow,
    LockEvent,
    LostLogin,
    MapSurvey,
    NightPlan,
    PaidShare,
    PlateRole,
    SearchHit,
    StateFile,
    WatchStop,
    AttackPlan,
    CartReport,
    HoldReport,
    NoteAction,
    NoteReport,
    PlateCount,
    RoundEvent,
    SearchNext,
    SheetOffer,
    StopReport,
    StopTarget,
    ViewReport,
    WaitReport,
    AdbEndpoint,
    BoostReport,
    CartOutcome,
    GoblinOffer,
    JournalLine,
    LevelChange,
    NextUpgrade,
    NoteOutcome,
    PressReport,
    RunnerState,
    ShieldState,
    StockReport,
    UpgradeRole,
    ViewOutcome,
    WatchReport,
    WorldReport,
    AttackReport,
    AttackSeries,
    BoostOutcome,
    DonateReport,
    EmulatorStop,
    ExportReport,
    FarmedReport,
    FrameReading,
    GameEntities,
    GrowthReport,
    JournalRound,
    LaunchReport,
    LegacyExport,
    PressOutcome,
    RestartScope,
    SearchReport,
    SeriesEnding,
    StatusReport,
    TendedReport,
    UpgradeTimer,
    VillageStock,
    WorldOutcome,
    AttackOptions,
    AttackOutcome,
    CollectReport,
    ConfirmReport,
    DisplayTarget,
    DonateOptions,
    DonateOutcome,
    ExportOutcome,
    FarmedVillage,
    GrowthOutcome,
    JournalTimers,
    LaunchOutcome,
    SearchOptions,
    SearchOutcome,
    SearchSection,
    SurveyOutcome,
    TendedUpgrade,
    VillageExport,
    BoundarySurvey,
    CollectOutcome,
    ConfirmOutcome,
    GameDataReport,
    GivebackReport,
    JournalConfirm,
    LootThresholds,
    GivebackOutcome,
    ListPriceReport,
    StandDownReport,
    StorageCapacity,
    TurnaroundGroup,
    EmulatorInstance,
    StandDownOutcome,
    TurnaroundReport,
    TurnaroundSample,
    PlateCountOutcome,
    TurnaroundOutcome,
    labelled,
)
from ai_coc.constants import STATE_PATH, COC_PACKAGE, ACCOUNT_JSON_DIR
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import ZOOM_PINCHES, AdbController, AdbControlError
from ai_coc.parsers.clan import donatable_cards
from ai_coc.parsers.home import (
    PANEL_CLOSE,
    CLOCK_TOWER_BELOW,
    reward_chest,
    free_builders,
    collect_bubbles,
    build_panel_open,
    clock_tower_marker,
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
    event_ended,
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
from ai_coc.parsers.village import (
    SPEED_UPS,
    merge,
    clock_tower,
    from_legacy,
    upgrade_timers,
    helper_cooldowns,
    parse_village_text,
)
from ai_coc.parsers.village import growth as village_growth
from ai_coc.parsers.village import search as village_search
from ai_coc.parsers.village import closest as closest_names
from ai_coc.parsers.boundary import PLAYFIELD, VILLAGE_CENTRE, village_box, boundary_reach
from ai_coc.parsers.building import (
    GOBLIN_X_BOX,
    MENU_ROW_BOX,
    SHEET_CLOSE_BOX,
    free_boost,
    sheet_open,
    boost_sheet,
    game_dialog,
    sheet_offer,
    goblin_offer,
    goblin_prompt,
    green_buttons,
    selection_row,
    sheet_running,
    upgrade_sheet,
    panel_wall_menu,
    green_button_row,
    menu_spends_gems,
    research_running,
)
from ai_coc.parsers.settings import export_row, window_open, settings_open, more_settings_open
from ai_coc.adapters.emulator import Emulator, EmulatorError
from ai_coc.adapters.gamedata import game_data, newest_kept
from ai_coc.adapters.ldplayer import LDPlayerAdapter
from ai_coc.adapters.clipboard import read_clipboard, clear_clipboard, write_clipboard

from .ui.clan import ClanRunner
from .ui.world import cross, uncovered, open_chest, park_camera, collect_cart
from .ui.attack import CARD_ROW_Y, DROP_SETTLE, BATTLE_TIMEOUT, SINGLE_DROP_DELAY, AttackRunner
from .ui.plates import PlateRunner
from .ui.runner import (
    BUY_SETTLE,
    BACK_SETTLE,
    ScreenRunner,
    SessionTakenError,
    on_hud,
    relogin,
    spell_out,
    refuse_taken,
)
from .ui.upkeep import UpkeepRunner

if TYPE_CHECKING:
    from pathlib import Path
    from collections.abc import Callable, Iterable, Iterator, Generator

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


def _recording(adb: AdbController, frames: FrameLog) -> AdbController:
    """The controller, capturing into this run's frames from here on."""
    adb.frames = frames
    return adb


def _controller(frames: FrameLog) -> AdbController:
    """The configured instance, with Clash of Clans already up on it."""
    emulator, instance = chosen(emulators())
    return _recording(emulator.controller(emulator.ensure_coc(instance.index).adb_serial), frames)


def _attached(frames: FrameLog) -> AdbController:
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
    return _recording(emulator.controller(instance.adb_serial), frames)


def _session(frames: FrameLog) -> tuple[AdbController, DisplayTarget]:
    """What every headless loop starts from: the game's controller and its display."""
    adb = _controller(frames)
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


def launch(
    restart: RestartScope, should_stop: Callable[[], bool], frames: FrameLog
) -> LaunchReport:
    """Bring the game up on the configured instance, tearing down as much as asked.

    Every other headless command assumes the game is already running: they go
    through `_controller`, which calls `ensure_coc` and gives up on whatever it
    cannot fix. This is that step on its own, for the two states it cannot reach
    from there — an emulator or a game that is up and no longer answering, which
    from here looks exactly like a working one. When the village never appears,
    the screen it gave up on is kept as `no_village`.
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
    adb = _recording(emulator.controller(instance.adb_serial), frames)
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
        try:
            frames.write("no_village", adb.screenshot(adb.display_for(COC_PACKAGE), None))
        except AdbControlError:
            logger.warning("The game is not on a display; there is no frame to keep")
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
    key = gemini_key(config.gemini.main)
    if not key:
        logger.info("No API key is saved; the loops will fall back to reading the screen alone")
        return None
    return GeminiClient(api_key=key, settings=config.gemini.main)


class FrameTicker(BaseModel):
    """Saves a frame every few seconds for as long as a command runs.

    A command's own captures are the ones it reads, and it only looks where it
    has a question; between them a battle can go badly with nothing recorded at
    all. This is the other view — a fixed heartbeat, numbered among the run's
    other frames (`0007_tick`) so the directory sorts in time order, and named in
    the log as it is saved.

    It shoots wherever the command last captured from (`FrameLog.source`), so it
    takes nothing before the command's first capture and follows a restart onto
    the display MuMu moves the game to, where a copy of its own would go on
    asking the display the game left, which answers `Status: -2`.

    It costs the emulator a PNG encode every tick and competes with the command
    for the same ADB connection, which is why it is off unless asked for.
    """

    frames: FrameLog
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
        failing = False
        while not self._stop.wait(self.seconds):
            source = self.frames.source
            if source is None:
                continue
            adb, display = source
            try:
                # A copy capturing for no run, so the tick is not taken for
                # where the command last looked.
                png = adb.model_copy(update={"frames": None}).screenshot(display, None)
            except Exception:
                # Loud, but never fatal: this is a recording of the run, not
                # part of it, and losing the battle to a missed frame is worse
                # than the gap in the recording. Once per streak, since every
                # warning lands in `runs.jsonl` with its trace.
                if not failing:
                    logger.warning(
                        "A heartbeat capture failed; skipping ticks until one lands", exc_info=True
                    )
                failing = True
                continue
            if failing:
                logger.info("Heartbeat captures are landing again")
                failing = False
            self.frames.number("tick", png)


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
# is the line `takeover` already draws for whoever is reading the log, rather than a
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
# How long a loan outlives the borrower's last command before the lender may
# start again. Long enough for the thinking and editing between one live test
# and the next, each of which would otherwise borrow afresh and wait out a
# battle; short enough that a borrower who forgot `giveback`, or died, costs
# half an hour of farming. Longer than `TAKEOVER_WAIT`, so a loan does not lapse
# while its borrower is still waiting for the lender's battle to end.
LOAN_TIME = timedelta(minutes=30)
# How long a session's hold outlives its last command, which is the gap one
# session leaves between two of its commands. Over 2026-10-25 to 11-12 the
# journal held 494 gaps between one session's command and its next `tap`,
# `swipe` or `confirm` with no watch slept in between: p50 9 s, p95 22 s, p99
# 42 s, three over 60 s and two over 120 s.
HOLD_TIME = timedelta(seconds=120)
# The longest any hold is set ahead, by `hold --for` or `watch --hold`. The
# takeover readies a sheet five minutes before it is due, and the game drops an
# idle session somewhere between 5 min 20 s and 12 min 48 s, so a sheet held
# open longer than this is not still open.
HOLD_MAX = timedelta(minutes=10)
# The commands that press where the agent read off an earlier frame, and so run
# only inside their own session's standing hold.
HANDS = ("tap", "swipe", "confirm")

# How often a write tries again when Windows refuses to replace the file, and
# how far apart. It refuses while any other process has the file open, and the
# loops and the watch each open it to read every `STOP_POLL`; a read is open for
# a moment, so a few tries a moment apart get past one.
REPLACE_TRIES = 10
REPLACE_GAP = 0.05
# How many copies of the state file `_write_file` keeps in `backups/` beside
# it. The newest is what a write carries on from when the file will not read;
# the rest cover a write that went wrong.
STATE_BACKUPS = 20

# The loan this process wrote when it stood a run down. A borrower with no
# session has nothing else to recognise its own loan by.
_borrowed: datetime | None = None
# The record this process's claim replaced, which `hold` reports as who drove last.
_before: RunnerState | None = None
# Numbers this process's state file copies, so two in one clock tick differ.
_copies = itertools.count()


class LoanCancelledError(RuntimeError):
    """`ai_coc stop` cancelled the loan a claim was queued behind."""


class NoStateFileError(RuntimeError):
    """`ai_coc watch` or `ai_coc note` found no state file to write into.

    Deleting the file by hand is a stop to the emulator's holder, which reads it
    only at its next seam, and a write that put the file back in between would
    hide that stop. Any command that drives the emulator writes it afresh.
    """


class HoldError(RuntimeError):
    """A command that stood down over a hold, carrying whose it was."""

    def __init__(
        self, holder: Caller | None = None, command: str = "", until: datetime | None = None
    ) -> None:
        super().__init__(holder, command, until)
        self.holder = holder
        self.command = command
        self.until = until


class HeldError(HoldError):
    """Another session holds the emulator between its commands."""


class NoHoldError(HoldError):
    """A hand command, or `watch --hold`, whose own session's hold no longer stands."""


class HoldCancelledError(HoldError):
    """`ai_coc stop` cancelled the hold a claim was queued behind."""


class StaleExportError(RuntimeError):
    """`watch --upgrades` found the newest saved export taken before the newest confirmed press.

    That export still lists the job the press finished, with a `done_at` gone
    by, so the watch would wake at once: three times on 2026-11-12.
    """

    def __init__(self, exported_at: str | None = None, confirm_run: str = "") -> None:
        super().__init__(exported_at, confirm_run)
        self.exported_at = exported_at
        self.confirm_run = confirm_run


# The exceptions a command stands down with, which `cli` answers as a
# `StandDownReport` rather than a crash, and the line `run.log` gets for each.
STAND_DOWNS: dict[type[RuntimeError], StandDownOutcome] = {
    LoanCancelledError: "loan_cancelled",
    SessionTakenError: "session_taken",
    NoStateFileError: "no_state_file",
    HeldError: "held",
    NoHoldError: "no_hold",
    HoldCancelledError: "hold_cancelled",
    StaleExportError: "stale_export",
}
STAND_DOWN_LINES: dict[StandDownOutcome, str] = {
    "loan_cancelled": "借用被 ai_coc stop 取消了,這個指令沒有開跑",
    "session_taken": "另一部裝置登入了這個帳號,沒有把登入搶回來",
    "no_state_file": "狀態檔不在(手動刪掉是叫停),什麼都沒改",
    "held": "另一個 session 在兩個指令之間保留著模擬器,這個指令沒有開跑",
    "no_hold": "這個 session 的保留已經不在(過期、被 stop 取消,或別人寫過狀態檔),什麼都沒按",
    "hold_cancelled": "排在後面等的那個保留被 ai_coc stop 取消了,這個指令沒有開跑",
    "stale_export": "最新那份村莊匯出比最後一次按成功的確認還舊,沒有睡;重讀 status 再掛",
}


def stand_down(error: RuntimeError) -> StandDownReport:
    """The answer a command that stood down with `error` gives, whose hold or which export included."""
    report = StandDownReport(outcome=STAND_DOWNS[type(error)])
    if isinstance(error, HoldError):
        return report.model_copy(
            update={"holder": error.holder, "command": error.command, "until": error.until}
        )
    if isinstance(error, StaleExportError):
        return report.model_copy(
            update={"exported_at": error.exported_at, "confirm_run": error.confirm_run}
        )
    return report


@contextmanager
def _turn() -> Generator[None]:
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


def started_at(pid: int) -> float | None:
    """When process `pid` started, or None where that cannot be told."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _alive(state: RunnerState | Watch | JournalLine) -> bool:
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
        if state != torn:
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
                extra={"journal": LockEvent(what="queued", other=loan.borrower)},
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


def _hold(state: RunnerState | None) -> Hold | None:
    """The hold on this record while it still stands.

    One whose holder's command is driving stands while that command runs and
    has not been asked to stop; one between commands until `until`.
    """
    hold = state.hold if state else None
    if state is None or hold is None or hold.ended is not None:
        return None
    if hold.until is None:
        return hold if state.status == "running" and _alive(state) else None
    return hold if datetime.now().astimezone() < hold.until else None


def _blocking(state: RunnerState | None, caller: Caller) -> Hold | None:
    """The standing hold of a session other than `caller`, which a blank session never owns."""
    hold = _hold(state)
    return None if hold is None or _same(caller, hold.holder) else hold


def _mine(state: RunnerState | None, caller: Caller) -> bool:
    """Whether `caller`'s own session holds the emulator."""
    hold = _hold(state)
    return hold is not None and _same(caller, hold.holder)


def _no_hold(state: RunnerState | None) -> NoHoldError:
    """The refusal of a command whose hold is gone, naming whoever holds now or drove last."""
    hold = _hold(state)
    last = hold.holder if hold else state.caller if state else None
    return NoHoldError(last, state.command if state else "", hold.until if hold else None)


def _behind_hold(caller: Caller, deadline: float | None) -> Hold | None:
    """Wait while another session holds the emulator, standing nobody down.

    Answers the hold it waited behind, or None when there was none. Past
    `deadline` (a `time.monotonic` instant) it stands down `held`; None waits
    for as long as the hold stands, which is a yielding claim's way.
    """
    waited: Hold | None = None
    while (hold := _blocking(state := read_state(), caller)) is not None:
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            raise HeldError(hold.holder, state.command if state else "", hold.until)
        # Another session's hold may take over from the one first waited behind.
        if waited is None or waited.since != hold.since:
            logger.info(
                "%s (%s) holds the emulator between its commands until %s; waiting",
                hold.holder.agent or "an agent with no name given",
                hold.holder.mission or "no mission given",
                "its command ends" if hold.until is None else f"{hold.until:%H:%M:%S}",
                extra={
                    "journal": LockEvent(
                        what="queued",
                        other=hold.holder,
                        other_command=state.command if state else "",
                    )
                },
            )
            waited = hold
        time.sleep(STOP_POLL if left is None else min(STOP_POLL, left))
    return waited


def take_over(caller: Caller) -> None:
    """Have the run holding the emulator stand down, and return once it has.

    **The file is a lock**, the user's call of 2026-09-27. A command that needs
    the emulator asks the holder to stand down through the same `stopping` that
    `ai_coc stop` writes, naming who asked, and waits for it to reach its seam
    and release — a battle under way is played out, never abandoned. A holder
    still up after `TAKEOVER_WAIT` is ended here and that is logged, rather
    than left for somebody to kill by hand or to delete the file under. The
    wait starts over for each holder, so one that claimed while this waited on
    another gets its own, rather than the rest of its predecessor's.
    """
    wait = TAKEOVER_WAIT
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
                extra={
                    "journal": LockEvent(
                        what="asked_to_stand_down",
                        other=current.caller,
                        other_command=current.command,
                    )
                },
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
                    extra={
                        "journal": LockEvent(
                            what="borrowed", other=current.caller, other_command=current.command
                        )
                    },
                )
            _write_state(current.model_copy(update=update))


def _end(holder: RunnerState, waited: float) -> None:
    """End a holder that never reached its seam, and say who it was."""
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
    """The file as it stands, so a write of one section carries the others over.

    A torn read is read again, since writing it back would hand the other
    sections to this process. One that stays torn carries on from the newest
    copy in `backups/` rather than from an empty file: the emulator and the
    watch would be rewritten by their own processes, but nothing rewrites the
    notes. A file deleted by hand is a stop, so its emulator and watch start
    afresh, and only its notes come back from the copies.
    """
    for _ in range(TORN_READS):
        found = _read_file()
        if found is None:
            return StateFile(notes=(_newest_backup() or StateFile()).notes)
        if found != _torn():
            return found
        time.sleep(TORN_GAP)
    logger.warning("%s would not read; carrying on from its newest backup", STATE_PATH)
    return _newest_backup() or StateFile()


def _backups() -> Path:
    """Where the state file's copies go: beside it, so a test's own directory holds them."""
    return STATE_PATH.with_name("backups")


def _newest_backup() -> StateFile | None:
    """The newest copy in `backups/` that still parses; None when there is none."""
    for copy in sorted(_backups().glob("state-*.json"), reverse=True):
        try:
            return StateFile.model_validate_json(copy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _back_up(text: str) -> None:
    """Keep a copy of what was just written, and only the newest `STATE_BACKUPS`.

    It never fails the write it follows, since one of the processes writing
    here is in a battle.
    """
    folder = _backups()
    try:
        folder.mkdir(exist_ok=True)
        # The count as well, because the clock can stand still between two writes.
        stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}-{next(_copies):06d}"
        (folder / f"state-{stamp}.json").write_text(text, encoding="utf-8")
        for old in sorted(folder.glob("state-*.json"))[:-STATE_BACKUPS]:
            old.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not keep a copy of %s in %s", STATE_PATH, folder)


def _write_state(state: RunnerState) -> None:
    """Write the emulator's section, keeping the others as the file holds them."""
    _write_file(_kept().model_copy(update={"emulator": state}))


def _write_watch(watch: Watch) -> None:
    """Write the watch's section, keeping the others as the file holds them."""
    _write_file(_kept().model_copy(update={"watch": watch}))


def _write_notes(notes: Notes) -> None:
    """Write the notes, keeping the others as the file holds them."""
    _write_file(_kept().model_copy(update={"notes": notes}))


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
    `takeover` uses to tell a clean stop from a killed process.
    """
    scratch = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    text = state.model_dump_json(indent=2)
    scratch.write_text(text, encoding="utf-8")
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(scratch, STATE_PATH)
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_GAP)
        else:
            _back_up(text)
            return


@contextmanager
def claim(  # noqa: PLR0913 - keyword-only past `caller`, each one a flag of the claiming commands
    command: str,
    log: Path | None = None,
    caller: Caller | None = None,
    *,
    yields: bool = False,
    queue: float = 0.0,
    linger: timedelta = HOLD_TIME,
) -> Generator[None]:
    """Say that this process is driving the emulator, and who asked, and hand it back after.

    Every command that drives the emulator takes one, short ones included: a
    `collect` that runs for eight seconds still holds the screen for those
    eight, and a second session that reads the file wants the truth rather than
    only being told about the long runs. `read` takes none, since it parses a
    PNG and never opens ADB, and neither do `capture` and a plain `world`,
    which only look.

    **Another live run holding it is asked to stand down first**, and waited out;
    see `take_over`. The decision to claim is taken again inside the lock,
    because two commands can finish waiting on one holder in the same instant
    and only one of them may win — the other goes back to asking.

    **A yielding claim** (`--yield`, the farming loops) is background work: it
    stands nobody down, and waits until nothing holds the emulator and no loan
    stands (`_queue`), so starting again after a loan, or a crossing
    between rounds, never takes the emulator back off whoever borrowed it. Any
    other claim may borrow from it, and never queues behind a loan, so a
    borrower's own next command is not locked out. `ai_coc stop` cancelling the
    loan it queued behind ends it with `LoanCancelledError`, since a stop means
    nothing lined up behind it starts. One queued behind a holder that lent
    nothing is not stopped: it starts once a stop has ended that holder.
    Lending is keyed on `--yield` rather than on two sessions differing: keyed
    on sessions, a farming command claiming between two of a tester's commands
    would borrow from the tester, whose next command would then queue behind it
    for hours.

    **A session holds the emulator across its commands** (`Hold`): a claim
    without `--yield` that names a session holds it while it runs and for
    `linger` after (`_release`), and its session's next claim renews it. While
    another session's hold stands, a claim stands nobody down: a yielding one
    queues, and any other waits up to `queue` seconds and then stands down
    `held`. `tap`, `swipe` and `confirm` with a session run only inside their
    own session's hold (`no_hold` otherwise), so a sequence that was cut into
    never goes on pressing off a frame read before. A hold `ai_coc stop`
    cancelled while this claim queued behind it ends it with
    `HoldCancelledError`, as a cancelled loan does.
    """
    global _held, _before
    who = caller or Caller()
    holds = bool(who.session) and not yields
    hands = command in HANDS and bool(who.session)
    deadline = time.monotonic() + queue
    queued: datetime | None = None
    behind: Hold | None = None
    while True:
        if hands and not _mine(state := read_state(), who):
            raise _no_hold(state)
        if yields:
            behind = _behind_hold(who, None) or behind
            queued = _queue() or queued
            waited: tuple[int, datetime | None] | None = None
            while (current := holder()) is not None:
                if waited != (current.pid, current.started):
                    logger.info(
                        "%s (%s) holds the emulator; waiting for it to finish",
                        current.command,
                        current.caller.mission or "no mission given",
                        extra={
                            "journal": LockEvent(
                                what="queued", other=current.caller, other_command=current.command
                            )
                        },
                    )
                    waited = (current.pid, current.started)
                time.sleep(STOP_POLL)
        else:
            behind = _behind_hold(who, deadline) or behind
            take_over(who)
        with _turn():
            state = read_state()
            loan = state.loan if state else None
            if queued and loan and (loan.since, loan.ended) == (queued, "cancelled"):
                raise LoanCancelledError
            hold = state.hold if state else None
            if behind and hold and (hold.since, hold.ended) == (behind.since, "cancelled"):
                raise HoldCancelledError(hold.holder, state.command if state else "")
            if hands and not _mine(state, who):
                raise _no_hold(state)
            if (
                _blocking(state, who) is None
                and holder() is None
                and not (yields and _standing(state))
            ):
                now = datetime.now().astimezone()
                # Past `_blocking`, a hold still standing is this session's own.
                own = _hold(state)
                mine = RunnerState(
                    status="running",
                    pid=os.getpid(),
                    pid_created=started_at(os.getpid()),
                    command=command,
                    started=now,
                    log=log,
                    caller=who,
                    yields=yields,
                    loan=_carried(state, who, yields=yields),
                    hold=Hold(holder=who, since=own.since if own else now) if holds else own,
                )
                _write_state(mine)
                _held, _before = mine, state
                # The moment the run really starts, which after a queue or a
                # stand-down can be minutes past the moment it was started.
                logger.info("Claimed the emulator", extra={"journal": LockEvent(what="claimed")})
                break
    try:
        yield
    finally:
        _release(command, log, linger)


def _release(command: str, log: Path | None, linger: timedelta = HOLD_TIME) -> None:
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
    The hold is the file's too: one given back or cancelled while this ran
    stays ended, and this run's own starts lapsing `linger` from now.
    """
    global _held, _borrowed  # noqa: PLW0603 - process-wide by nature; see `_held`
    with _turn():
        held = read_state()
        if held is not None and held.pid not in (0, os.getpid()):
            _held = _borrowed = None
            return
        hold = held.hold if held else None
        if (
            hold is not None
            and hold.ended is None
            and _held is not None
            and _held.hold is not None
            and _same(hold.holder, _held.caller)
        ):
            hold = hold.model_copy(update={"until": datetime.now().astimezone() + linger})
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
                extra={"journal": LockEvent(what="lent", other=loan.borrower)},
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
                hold=hold,
            )
        )
    _held = _borrowed = None


# One line per way `ai_coc stop` can leave each section, for `run.log`.
STOP_LINES: dict[EmulatorStop, str] = {
    "asked": "已要求 {command}(pid {pid})收工,它會做完手上這一件事才停",
    "nothing_running": "現在沒有指令在跑",
}
LOAN_LINES: dict[LoanStop, str] = {
    "kept": "借用還在,{lender}之後會自己開回去",
    "cancelled": "借用取消了,{lender}不會自己開回去",
}
HOLD_LINES: dict[HoldStop, str] = {
    "kept": "{holder}的保留還在,它的指令停了之後再 stop 一次才取消",
    "cancelled": "{holder}的保留取消了,它之後的點擊和排在它後面的指令都不會開跑",
}
WATCH_LINES: dict[WatchStop, str] = {
    "asked": "已要求{watcher}的盯升級收工",
    "nothing_watching": "沒有人在盯升級",
}


def stop_line(report: StopReport) -> str:
    """What one stop did, each section it reached in turn."""
    said: list[str] = []
    if report.emulator is not None:
        said.append(STOP_LINES[report.emulator].format(command=report.command, pid=report.pid))
    if report.loan is not None:
        lender = (report.lender.agent if report.lender else "") or "借出模擬器的那一邊"
        said.append(LOAN_LINES[report.loan].format(lender=lender))
    if report.hold is not None:
        holder = (report.holder.agent if report.holder else "") or "保留著模擬器的那一邊"
        said.append(HOLD_LINES[report.hold].format(holder=holder))
    if report.watch is not None:
        watcher = (report.watcher.agent if report.watcher else "") or "盯升級的那一邊"
        said.append(WATCH_LINES[report.watch].format(watcher=watcher))
    return ";".join(said)


def stop(caller: Caller | None = None, target: StopTarget = "all") -> StopReport:
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
    report = StopReport(target=target)
    with _turn():
        if target != "watch":
            report = _stop_emulator(report, read_state(), who)
        if target != "emulator":
            report = _stop_watch(report, read_watch(), who)
    logger.info("%s", stop_line(report))
    return report


def _stop_emulator(report: StopReport, state: RunnerState | None, who: Caller) -> StopReport:
    """Mark the emulator's holder `stopping`, and settle its loan and its hold.

    **A loan nobody is driving on is cancelled**, so the lender does not start
    again and nothing queued behind it starts either: a stop is a stop, not a
    turn of the emulator. A borrower's command still running is stopped like
    any other and its loan kept, so a test can be stopped without ending the
    farming it borrowed from; a second stop, with nothing running, cancels it.

    **A hold is settled the same way.** One nobody is driving on is cancelled,
    so that session's next `tap`, `swipe` or `confirm` stands down `no_hold`
    and whatever queued behind it `hold_cancelled`. One whose command is still
    running is kept with it: cancelled there, the lender queued behind both
    would end `hold_cancelled` once the loan came back, and the test stopped
    would have ended the farming after all.
    """
    loan = _standing(state)
    hold = _hold(state)
    if state is None or (state.status == "idle" and loan is None and hold is None):
        return report.model_copy(update={"emulator": "nothing_running"})
    driven = loan is not None and loan.until is None
    # `_hold` answers one with no `until` only while its command is running.
    held = hold is not None and hold.until is None
    asked = state.status != "idle"
    update: dict[str, object] = {}
    if loan is not None and not driven:
        update["loan"] = loan.model_copy(update={"ended": "cancelled"})
    if hold is not None and not held:
        update["hold"] = hold.model_copy(update={"ended": "cancelled"})
    if asked:
        update |= {"status": "stopping", "stop_by": who}
    _write_state(state.model_copy(update=update))
    return report.model_copy(
        update={
            "emulator": "asked" if asked else "nothing_running",
            "command": state.command if asked else "",
            "pid": state.pid if asked else None,
            "loan": None if loan is None else "kept" if driven else "cancelled",
            "lender": None if loan is None else loan.lender,
            "hold": None if hold is None else "kept" if held else "cancelled",
            "holder": None if hold is None else hold.holder,
        }
    )


def _stop_watch(report: StopReport, watching: Watch | None, who: Caller) -> StopReport:
    """Mark a live upgrade watch `stopping`; it wakes within a `STOP_POLL`."""
    if watching is None or watching.status == "idle" or not _alive(watching):
        return report.model_copy(update={"watch": "nothing_watching"})
    _write_watch(watching.model_copy(update={"status": "stopping", "stop_by": who}))
    return report.model_copy(update={"watch": "asked", "watcher": watching.caller})


# One line per way `ai_coc giveback` can end, for `run.log`.
GIVEBACK_LINES: dict[GivebackOutcome, str] = {
    "returned": "還了,{lender}會自己開回去",
    "released": "放掉了這個 session 的保留,沒有借來的模擬器要還",
    "no_loan": "這個 session 沒有保留,也沒有借出去的模擬器,沒有東西要還",
    "not_yours": "模擬器是{holder}保留著的,不是這個 session 的,什麼都沒改",
}


def giveback(caller: Caller | None = None) -> GivebackReport:
    """Hand back the caller's hold and a borrowed emulator, so the run it was taken from starts again.

    **Another session's standing hold is left alone** (`not_yours`): a test
    ending with the routine `giveback` used to return the takeover's loan
    from under its open sheet. Once no hold stands, anyone may hand a loan
    back, not only the borrower: one whose agent has died would otherwise keep
    the lender waiting out `LOAN_TIME`. A borrower still driving finishes what
    it is doing first, and the lender waits for that like any other run.
    """
    who = caller or Caller()
    with _turn():
        state = read_state()
        loan = _standing(state)
        hold = _hold(state)
        if state is None or (loan is None and hold is None):
            report = GivebackReport(outcome="no_loan")
        elif hold is not None and not _same(who, hold.holder):
            report = GivebackReport(outcome="not_yours", holder=hold.holder)
        else:
            update: dict[str, object] = {}
            if loan is not None:
                update["loan"] = loan.model_copy(update={"ended": "returned"})
            if hold is not None:
                update["hold"] = hold.model_copy(update={"ended": "returned"})
            _write_state(state.model_copy(update=update))
            report = GivebackReport(
                outcome="released" if loan is None else "returned",
                lender=None if loan is None else loan.lender,
                holder=None if hold is None else hold.holder,
            )
    lender = (report.lender.agent if report.lender else "") or "借出模擬器的那一邊"
    holder = (report.holder.agent if report.holder else "") or "另一個 session"
    logger.info("%s", GIVEBACK_LINES[report.outcome].format(lender=lender, holder=holder))
    return report


def wait(run: Path, seconds: float = 0.0) -> WaitReport:
    """Where one run stands, waiting up to `seconds` for it to finish or die.

    `run` is its directory, the directory's name, or the `--label` it was
    started with, the newest run carrying it. Read off the journal, that run's
    process and its log alone, so it claims nothing and starts nothing: an
    agent waiting on a background run makes one call that ends by itself in any
    runtime or shell, where a run killed outright writes nothing and only its
    process being gone says so.
    """
    deadline = time.monotonic() + seconds
    while (report := _where(run)).outcome not in ("finished", "gone") and (
        left := deadline - time.monotonic()
    ) > 0:
        time.sleep(min(STOP_POLL, left))
    logger.info("%s is %s", report.log.name, report.outcome)
    return report


def _where(run: Path) -> WaitReport:
    """Where the newest run named `run` stands now, this process's own run never among them."""
    # A label reaches the directory name as `RunLog.open` cuts it down; a
    # directory's own name is matched as it is.
    name = labelled(run.name)
    lines: list[JournalLine] = []
    with suppress(FileNotFoundError), logging_setup.JOURNAL_PATH.open(encoding="utf-8") as journal:
        for text in journal:
            # Most lines are not this run's, and parsing every one of a journal
            # that runs to megabytes on every poll is the cost worth skipping.
            if name in text:
                with suppress(ValueError):
                    lines.append(JournalLine.model_validate_json(text))
    starts = [
        line
        for line in lines
        if line.event == "start"
        and line.pid != os.getpid()
        and (line.run in (run.name, name) or line.run.endswith(f"-{name}"))
    ]
    if not starts:
        return WaitReport(log=run, outcome="no_such_run")
    start = starts[-1]
    log = start.log or run
    mine = [line for line in lines if line.run == start.run]
    try:
        quiet: int | None = int(time.time() - (log / "run.log").stat().st_mtime)
    except OSError:
        quiet = None
    report = WaitReport(
        log=log, outcome="running", command=start.command, pid=start.pid, quiet=quiet
    )
    if end := next((line for line in mine if line.event == "end"), None):
        return report.model_copy(
            update={"outcome": "finished", "exit": end.exit, "run_outcome": end.outcome}
        )
    if _alive(start):
        locks = {line.what for line in mine if line.event == "lock"}
        if "claimed" not in locks and locks & {"queued", "asked_to_stand_down"}:
            held = holder()
            return report.model_copy(
                update={"outcome": "queued", "holder": held.log if held else None}
            )
        return report
    # Its `end` line is the one write that can be dropped (`Journal`).
    return report.model_copy(
        update={"outcome": "finished" if (log / "result.json").exists() else "gone"}
    )


# One line per way `ai_coc note` can end, for `run.log` and over what it prints.
NOTE_LINES: dict[NoteOutcome, str] = {
    "listed": "列出交代",
    "added": "記下這一條交代",
    "done": "刪掉做完的那一條",
    "incomplete": "add 要給世界跟內容, done 要給世界跟第幾條, 什麼都沒改",
    "no_such_note": "那個世界沒有這一條, 什麼都沒改",
}


def note(action: NoteAction, world: World | None = None, item: str = "") -> NoteReport:
    """List the user's notes, add one to a village, or drop a done one by its number.

    `item` is the text to add, or the number `done` drops, counted from 1 the
    way the list prints them.
    """
    text = item.strip()
    with _turn():
        notes = _kept().notes
        outcome: NoteOutcome = "listed"
        if action != "list" and (world is None or not text):
            outcome = "incomplete"
        elif action != "list" and world is not None:
            if _read_file() is None:
                raise NoStateFileError
            held = getattr(notes, world)
            index = int(text) if text.isdigit() else 0
            if action == "add":
                outcome, held = "added", [*held, text]
            elif 1 <= index <= len(held):
                outcome, held = "done", held[: index - 1] + held[index:]
            else:
                outcome = "no_such_note"
            if outcome != "no_such_note":
                notes = notes.model_copy(update={world: held})
                _write_notes(notes)
    logger.info(
        "%s；日世界 %d 條，夜世界 %d 條", NOTE_LINES[outcome], len(notes.day), len(notes.night)
    )
    return NoteReport(notes=notes, outcome=outcome)


def note_text(report: NoteReport) -> str:
    """Each village's notes numbered from 1, the number `note done` takes, under what this run changed."""
    shown = [] if report.outcome == "listed" else [NOTE_LINES[report.outcome], ""]
    for world in ("day", "night"):
        held = getattr(report.notes, world)
        shown += [WORLD_NAMES[world], *(f"{at}. {item}" for at, item in enumerate(held, 1))]
        shown += [] if held else ["沒有交代"]
        shown.append("")
    return "\n".join(shown).rstrip()


# How long a new watch gives the one it replaces to let go before ending it. A
# watch reads the file every `STOP_POLL` while it sleeps, so one still holding
# on after this many polls is not reading it at all.
WATCH_TAKEOVER = 15 * STOP_POLL


def wake_at(
    nexts: list[datetime],
    upgrades: list[UpgradeRole] | None = None,
    lead: int = 0,
    minutes: int | None = None,
    tower: bool = False,
) -> datetime | None:
    """The earliest of the instants a watch was asked to wake at, or None when there is none.

    `upgrades` adds every running upgrade of the newest saved export (what
    `status --last` prints), or the named slots' alone, `lead` minutes ahead of
    its `done_at`; one whose lead has run out wakes at its `done_at`, since the
    session prepares for those before it sleeps (`takeover`), and one already
    done wakes it now. `tower` adds the clock tower's `ready_at`, except a tower
    ready now, which the `status` before this read and which would otherwise
    wake the session at once on every watch when it will not boost.

    **`upgrades` stands down on an export older than the newest confirmed
    press** (`StaleExportError`), any session's: that export still lists the
    job the press finished, due already, so the watch would wake at once. A
    press leaves the clock tower's `ready_at` as it was, so `tower` alone does not.
    """
    now = datetime.now().astimezone()
    found = [(at if at.tzinfo else at.astimezone(), "--next") for at in nexts]
    if minutes is not None:
        found.append((now + timedelta(minutes=minutes), f"{minutes} min from now"))
    read = _last_export() if upgrades is not None or tower else None
    if read is not None and read.export is None:
        logger.warning("No saved export to read the upgrades from: %s", read.outcome)
    village = None if read is None else read.export
    if (
        village is not None
        and upgrades is not None
        and (pressed := _last_confirm()) is not None
        and pressed.at.timestamp() > village.timestamp
    ):
        raise StaleExportError(village.exported_at, pressed.run)
    if village is not None:
        for timer in upgrade_timers(village):
            if upgrades is None or (upgrades and timer.role not in upgrades):
                continue
            done = datetime.fromisoformat(timer.done_at)
            early = done - timedelta(minutes=lead)
            at = early if early > now else max(done, now)
            found.append((at, f"{timer.zh or timer.en or timer.data_id}, {lead} min early"))
        ready = clock_tower(village)
        if tower and ready is not None and (at := datetime.fromisoformat(ready.ready_at)) > now:
            found.append((at, "the clock tower"))
    if not found:
        return None
    wake, why = min(found, key=lambda one: one[0])
    logger.info("Waking at %s for %s", f"{wake:%m-%d %H:%M:%S}", why)
    return wake


def lingering(seconds: float | None) -> timedelta:
    """How long `hold --for` keeps the hold after it: `HOLD_TIME` by default, never past `HOLD_MAX`."""
    return HOLD_TIME if seconds is None else min(timedelta(seconds=seconds), HOLD_MAX)


def hold(seconds: float | None = None) -> HoldReport:
    """Hold the emulator for this session and do nothing with it; the claim around it is the work.

    It is how a sequence that starts with a `tap` opens, since a hand command
    runs only inside its own session's hold, and how an agent that needs longer
    than `HOLD_TIME` with a frame says so. `until` is when the release that
    follows this answer sets it, give or take that moment.
    """
    mine = _held.hold if _held else None
    report = HoldReport(
        holder=_held.caller if _held else Caller(),
        since=mine.since if mine else None,
        until=datetime.now().astimezone() + lingering(seconds),
        last=_before.caller if _before else None,
        last_command=_before.command if _before else "",
    )
    logger.info(
        "Holding the emulator until %s; the last to drive it was %s (%s)",
        f"{report.until:%H:%M:%S}",
        (report.last.agent if report.last else "") or "nobody named",
        report.last_command or "no command",
    )
    return report


def _keep_hold(who: Caller, until: datetime) -> datetime:
    """Move `who`'s standing hold on to `until`, never past `HOLD_MAX` from now, and say where it went.

    With no standing hold of its own, it stands down `no_hold` and writes nothing.
    """
    with _turn():
        state = read_state()
        hold = _hold(state)
        if state is None or hold is None or not _same(who, hold.holder):
            raise _no_hold(state)
        until = min(until, datetime.now().astimezone() + HOLD_MAX)
        _write_state(state.model_copy(update={"hold": hold.model_copy(update={"until": until})}))
    return until


def watch(  # noqa: PLR0913 - each is one flag of `ai_coc watch`
    caller: Caller | None,
    next_at: datetime | None,
    about: str,
    log: Path | None = None,
    *,
    farms: bool = False,
    hold: bool = False,
) -> WatchReport:
    """Sleep until `next_at` as the one process watching the upgrade timers.

    The agent runs this in the background and is woken by it ending, so the
    record of a watch and the wake are one thing: a record whose process is
    gone is nobody watching, read off its pid like the emulator's holder. It
    used to be a record and nothing else, and an agent wrote one with nothing
    scheduled to wake it while every other agent took the timers for watched.
    Nothing here touches the game. With no `next_at` (`wake_at` found nothing
    to wait for) it answers at once and leaves a live watch alone.

    **A live watch already there is stopped and taken over**, this session's
    own included, the user's call of 2026-10-12: the newest request comes from
    a session the user has just been talking to, and the one replaced wakes,
    reads who stopped it and why off its own answer, and stands down.

    **`hold` keeps this session's hold through the sleep**, which is where a
    sheet stands open waiting for its upgrade to come due: it is moved on to
    the wake plus `HOLD_TIME` before the watch is taken (standing down
    `no_hold`, the watch untouched, when the session holds nothing), and to
    `HOLD_TIME` from the moment it wakes, never past `HOLD_MAX` either time.
    """
    who = caller or Caller()
    if next_at is None:
        logger.info("Nothing to wait for; not watching")
        now = datetime.now().astimezone()
        return WatchReport(caller=who, next_at=None, about=about, ended=now, outcome="nothing_due")
    wake = next_at if next_at.tzinfo else next_at.astimezone()
    if hold:
        logger.info(
            "Holding the emulator until %s", f"{_keep_hold(who, wake + HOLD_TIME):%H:%M:%S}"
        )
    mine = _take_watch(who, wake, about, log, farms)
    logger.info("Watching the upgrade timers until %s: %s", f"{wake:%m-%d %H:%M}", about)
    try:
        while (left := (wake - datetime.now().astimezone()).total_seconds()) > 0:
            current = read_watch()
            if current is None or current.status == "stopping" or current.pid != mine.pid:
                break
            time.sleep(min(STOP_POLL, left))
    except KeyboardInterrupt:
        logger.info("Interrupted; letting the watch go")
    report = _let_watch_go(mine, wake)
    if not hold:
        return report
    try:
        held = _keep_hold(who, datetime.now().astimezone() + HOLD_TIME)
    except NoHoldError:
        logger.info("The hold no longer stands; nothing held after the watch")
        return report
    logger.info("Holding the emulator until %s", f"{held:%H:%M:%S}")
    return report.model_copy(update={"held_until": held})


def _take_watch(
    who: Caller, wake: datetime, about: str, log: Path | None, farms: bool = False
) -> Watch:
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
                raise NoStateFileError
            if current.status == "idle" or not _alive(current):
                mine = Watch(
                    status="running",
                    pid=os.getpid(),
                    pid_created=started_at(os.getpid()),
                    started=datetime.now().astimezone(),
                    log=log,
                    caller=who,
                    next_at=wake,
                    about=about,
                    farms=farms,
                )
                _write_watch(mine)
                return mine
            if asked != (current.pid, current.started):
                _write_watch(current.model_copy(update={"status": "stopping", "stop_by": who}))
                logger.info(
                    "%s is watching (%s); asked it to stand down",
                    current.caller.agent or "an agent with no name given",
                    current.about,
                    extra={"journal": LockEvent(what="watch_taken", other=current.caller)},
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


def _rest(seconds: float, should_stop: Callable[[], bool] = stop_requested) -> SeriesEnding | None:
    """Wait between rounds, answering how the series ends if the wait was cut short.

    A stop that lands here is said out loud, because the state file goes back to
    `idle` on the way out and this line is then its only trace in `run.log`.
    """
    try:
        for _ in range(int(seconds / STOP_POLL)):
            if should_stop():
                logger.info("Stop requested between rounds; ending the series")
                return "stopped"
            time.sleep(STOP_POLL)
    except KeyboardInterrupt:
        return "interrupted"
    return None


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

    `park` False leaves the camera where it is at the far zoom, for a caller
    that taps no map coordinate: the attack, whose battle measures its own
    camera and whose cart visit on the builder base parks for itself, and the
    reads that tap only fixed HUD places (`status`, `stock`, `export`). The far
    zoom is still taken, since a zoomed-in camera can fuse a storage bar's
    digits (`GameRunner._settle_zoom`).
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
            png = adb.screenshot(display, "settle")
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
    `back`, and so do the 勝利之星 reward sheet and an event's end notice. The build panel shifts the
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
    if event_ended(png):
        logger.info("An event's end notice is covering the village; pressing back")
        adb.back(display)
        return "an event's end notice was covering the village"
    if build_panel_open(png):
        logger.info("The build panel is covering the village; pressing its X")
        adb.tap(*PANEL_CLOSE, display)
        return "the build panel was covering the village"
    if reward_chest(png):
        logger.info("A reward chest is covering the village; tapping it open")
        open_chest(adb, display)
        return "a reward chest was covering the village"
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
    "other_village": "遊戲停在另一個村莊,這一批不打那邊;換村莊用 ai_coc world --day 或 --night",
    "session_taken": "另一部裝置登入了這個帳號,沒有把登入搶回來,停止刷資源",
}

# The round outcomes that end a series on their own, and the ending each gives it.
ROUND_ENDINGS: dict[AttackOutcome, SeriesEnding] = {
    "stock_full": "stock_full",
    "builder_free": "builder_free",
    "lab_free": "lab_free",
    "other_village": "other_village",
    "session_taken": "session_taken",
    "army_short": "army_short",
}

# How a series ended, as `attack` and `farmed` print it after its battles.
ENDING_LINES: dict[SeriesEnding, str] = {
    "repeat_done": "打完交代的輪數",
    "stopped": "被叫停",
    "interrupted": "被 Ctrl-C 中斷",
    "stock_full": "倉庫都滿過設定的百分比",
    "builder_free": "這個村莊有工人閒著",
    "lab_free": "這個村莊的實驗室有空檔",
    "army_short": "兵力不到兵營的一半",
    "other_village": "遊戲停在這一批沒在打的另一個村莊",
    "session_taken": "另一部裝置登入了這個帳號",
    "emulator_silent": f"模擬器連續 {ADAPTER_FAILURES} 輪沒有回應",
    "no_attack_menu": f"攻擊選單連續 {MENU_FAILURES} 輪打不開",
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


def farmed(series: AttackSeries, ending: SeriesEnding | None) -> FarmedVillage | None:
    """What a series that ended `ending` brought home on the village it played; None for one that never read which."""
    played = [one for one in series.root if one.world is not None]
    world = played[0].world if played else None
    if world is None:
        return None
    fought = [at for at, one in enumerate(played) if one.attacked is not None or one.phases > 0]
    read = [(at, one.holding) for at, one in enumerate(played) if one.holding is not None]
    carts = [one.cart for one in played if one.cart is not None]
    start = _cart_level(carts[0]) if carts else None
    now = _cart_level(carts[-1]) if carts else None
    village = FarmedVillage(
        world=world,
        battles=len(fought),
        uncounted=len(fought),
        capacity=next((one.capacity for one in reversed(played) if one.capacity), None),
        cart_gained=None if start is None or now is None else now - start,
        cart_now=now,
        cart_capacity=next((cart.capacity for cart in reversed(carts) if cart.capacity), None),
        cart_unread=played[-1].since_cart or 0,
        ending=ending,
    )
    if read:
        (first, before), (last, after) = read[0], read[-1]
        village.gained = VillageStock(
            gold=after.gold - before.gold,
            elixir=after.elixir - before.elixir,
            dark=after.dark - before.dark,
        )
        village.held = after
        village.uncounted = sum(1 for at in fought if at < first or at >= last)
    return village


def _cart_level(cart: CartReport) -> int | None:
    """What the cart held when a trip left it, where the trip says."""
    if cart.outcome in ("collected", "empty", "locked_empty"):
        return 0
    return cart.held if cart.outcome == "locked_holding" else None


def attack_text(run: AttackRun) -> str:
    """What `attack` prints: its series as the farming report writes it, or how it ended without a village."""
    village = farmed(run.rounds, run.ending)
    if village is None:
        return f"一輪都沒有讀到是哪個村莊, 一場也沒打; {ENDING_LINES[run.ending]}"
    return farmed_text(village)


def farmed_text(village: FarmedVillage) -> str:
    """A series as the farming report writes it: battles and how it ended, then the loot table."""
    names = ("gold", "elixir", "dark") if village.world == "day" else ("gold", "elixir")
    rows = [
        (
            _RESOURCE_NAMES[one],
            "讀不到" if village.gained is None else _signed(getattr(village.gained, one)),
            _share(
                getattr(village.held, one) if village.held else None,
                getattr(village.capacity, one) if village.capacity else None,
            ),
        )
        for one in names
    ]
    if village.world == "night":
        now = village.cart_now
        held = "讀不到" if now is None else _wan(now)
        if now is not None and village.cart_capacity:
            held += f" / {_wan(village.cart_capacity)}"
        gain = "讀不到" if village.cart_gained is None else _signed(village.cart_gained)
        rows.append(("聖水車", gain, held))
    ended = "" if village.ending is None else f", {ENDING_LINES[village.ending]}"
    shown = [
        f"{WORLD_NAMES[village.world]}: {village.battles} 場{ended}",
        "",
        "| 資源 | 進帳 | 現在 |",
        "| --- | --- | --- |",
        *(f"| {name} | {gain} | {now} |" for name, gain, now in rows),
    ]
    if village.uncounted:
        shown += ["", f"有 {village.uncounted} 場打完之後沒讀到倉庫, 沒算進進帳"]
    if village.cart_unread:
        shown += [f"有 {village.cart_unread} 場的聖水在最後一次去聖水車之後才進車, 沒算進進帳"]
    return "\n".join(shown)


def farmed_runs(by: str | None = None, since: datetime | None = None) -> FarmedReport:
    """What a session's farming came to since `since`, every attack run summed, off `runs.jsonl` alone.

    `by` takes the session's own runs and its farm subagent's (`<id>-farm`).
    Each run is summed as `farmed` sums a series, so what was spent between
    runs is not taken off the loot; the cart's last battles in each run stay
    unread, since the next run's first trip empties them into the storages
    before its first reading. A round line written before the rounds carried
    their own storages has them beside its report.
    """
    since = since.astimezone() if since is not None else None
    lines: list[JournalLine] = []
    rounds: list[JournalRound] = []
    with suppress(FileNotFoundError), logging_setup.JOURNAL_PATH.open(encoding="utf-8") as journal:
        for text in journal:
            with suppress(ValueError):
                line = JournalLine.model_validate_json(text)
                lines.append(line)
                if line.event == "round":
                    rounds.append(JournalRound.model_validate_json(text))
    starts = {
        line.run: line
        for line in lines
        if line.event == "start"
        and line.command == "attack"
        and (by is None or line.caller.session in (by, f"{by}-farm"))
        and (since is None or line.at >= since)
    }
    ends = {line.run: line for line in lines if line.event == "end" and line.run in starts}
    played: dict[str, list[JournalRound]] = {}
    for one in rounds:
        if one.run in starts:
            played.setdefault(one.run, []).append(one)
    runs = sorted(played, key=lambda run: starts[run].at)
    report = FarmedReport(by=by, since=since)
    villages = [
        village
        for run in runs
        if (
            village := farmed(
                AttackSeries([_with_storages(one) for one in played[run]]),
                _series_ending(ends.get(run), played[run][-1]),
            )
        )
    ]
    for world in ("day", "night"):
        if mine := [village for village in villages if village.world == world]:
            report.villages.append(_summed(mine))
    if runs:
        report.started = starts[runs[0]].at
        report.ended = max(
            end.at if (end := ends.get(run)) else played[run][-1].at for run in runs
        )
    # Any run of the session holding the emulator, one still before its first
    # round included: the loop starts again after every loan.
    held = read_state()
    report.running = (
        held is not None
        and held.status != "idle"
        and held.log is not None
        and held.log.name in starts
    )
    logger.info("Farmed: %d run(s) of %d round(s)", len(runs), sum(map(len, played.values())))
    return report


def _series_ending(end: JournalLine | None, last: JournalRound) -> SeriesEnding | None:
    """How an attack run's series ended, off its `end` line; None for one with no `end`.

    An `end` written before series named their endings carries its last round
    instead, which says how the series ended only where that round ends one.
    """
    if end is None:
        return None
    with suppress(ValueError):
        return AttackRun.model_validate(end.result).ending
    return ROUND_ENDINGS.get(last.report.outcome)


def _with_storages(line: JournalRound) -> AttackReport:
    """A journal round's report with the storages it was written beside."""
    return line.report.model_copy(
        update={"holding": line.holding, "capacity": line.capacity, "cart": line.cart}
    )


def _summed(villages: list[FarmedVillage]) -> FarmedVillage:
    """One village's runs as one: counts and loot added, the readings the last run's.

    The cart's gain is unread as a whole where a run that fought could not read it.
    """
    gained = [one.gained for one in villages if one.gained is not None]
    carts = [one.cart_gained for one in villages if one.battles or one.cart_gained is not None]
    last = villages[-1]
    return last.model_copy(
        update={
            "battles": sum(one.battles for one in villages),
            "gained": VillageStock(
                gold=sum(one.gold for one in gained),
                elixir=sum(one.elixir for one in gained),
                dark=sum(one.dark for one in gained),
            )
            if gained
            else None,
            "uncounted": sum(one.uncounted for one in villages),
            "held": next((one.held for one in reversed(villages) if one.held), None),
            "capacity": next((one.capacity for one in reversed(villages) if one.capacity), None),
            "cart_gained": None if None in carts else sum(c for c in carts if c is not None),
            "cart_now": next(
                (one.cart_now for one in reversed(villages) if one.cart_now is not None), None
            ),
            "cart_unread": sum(one.cart_unread for one in villages),
        }
    )


def farmed_report_text(report: FarmedReport, now: datetime | None = None) -> str:
    """A session's farming as the farming report writes it: when and how long, then each village."""
    if report.started is None or report.ended is None:
        return "這段時間沒有打資源的紀錄"
    now = now or datetime.now().astimezone()
    span = spell_out(int((report.ended - report.started).total_seconds()))
    head = (
        f"{_moment(report.started.isoformat(), now)} 到 {_moment(report.ended.isoformat(), now)},"
        f" {span}{', 還在打' if report.running else ''}"
    )
    return "\n\n".join([head, *(farmed_text(village) for village in report.villages)])


def _share(now: int | None, ceiling: int | None) -> str:
    """An amount held, with its share of the ceiling where one was read."""
    if now is None:
        return "讀不到"
    return f"{_wan(now)} ({now * 100 // ceiling}%)" if ceiling else _wan(now)


def _signed(amount: int) -> str:
    return f"+{_wan(amount)}" if amount >= 0 else f"-{_wan(-amount)}"


def _round(runner: AttackRunner, series: AttackSeries) -> AttackReport | SeriesEnding:
    """One round, with the ways it can end badly folded in. An ending ends the series.

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
        return "interrupted"
    except AdbControlError as exc:
        return _lost_round(runner, series, exc)
    # Read before the battle; one read during it the runner reports itself, so
    # the round keeps the battle it fought. `_series_over` ends on either.
    except SessionTakenError:
        return _seen(AttackReport(world=runner.world, outcome="session_taken"), runner)
    runner.lost = 0
    return _seen(report, runner)


def _seen(report: AttackReport, runner: AttackRunner) -> AttackReport:
    """The round's report with what the runner read around it: the storages before it, the ceilings, the cart."""
    return report.model_copy(
        update={
            "holding": runner.holding,
            "capacity": runner.capacity,
            "cart": runner.cart,
            "since_cart": runner.since_cart,
        }
    )


def _lost_round(
    runner: AttackRunner, series: AttackSeries, exc: AdbControlError
) -> AttackReport | SeriesEnding:
    """Record a round that died on the emulator, or `emulator_silent` once the series should end.

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
    rounds at it. Three is the line `takeover` already draws for whoever is reading
    the log rather than a number measured here, so the code draws it in the same
    place.
    """
    runner.lost += 1
    logger.error(
        "Round %d died on the emulator (%d in a row): %s", len(series.root) + 1, runner.lost, exc
    )
    report = _seen(AttackReport(world=runner.world, outcome="emulator_silent"), runner)
    if runner.lost >= ADAPTER_FAILURES:
        # Recorded here rather than handed back, because the caller records
        # what it is given and this one ends the series instead of reaching it.
        # It is still a round that happened, and leaving it out would take one
        # round off the count this whole function exists to preserve.
        series.root.append(report)
        # The caller breaks before its own `Attack finished:` line, so without
        # this the last round is in `result.json` and missing from the log and
        # the journal, an undercount of exactly what this preserves.
        logger.info("Attack finished: %s", round_line(report), extra=_round_event(runner, series))
        logger.error("The emulator has not answered for %d rounds; ending the series", runner.lost)
        return "emulator_silent"
    # Handed back as an ordinary round that fought nothing, which is what it
    # was. The caller then records it, rests between rounds, and comes round
    # again — and that rest is worth more here than anywhere else, since an
    # emulator that just refused a command is the last thing to fire another
    # one at immediately.
    return report


def _round_event(runner: AttackRunner, series: AttackSeries) -> dict[str, RoundEvent]:
    """The journal's line for the round just added to `series`, as a log call's `extra`."""
    return {
        "journal": RoundEvent(
            round=len(series.root),
            report=series.root[-1],
            plan=runner.played,
            holding=runner.holding,
            capacity=runner.capacity,
            cart=runner.cart,
        )
    }


def _series_over(report: AttackReport, played: int) -> SeriesEnding | None:
    """How this round's outcome ends the whole series, saying why; None where it does not.

    Six outcomes do (`ROUND_ENDINGS`). A full storage is the goal being met, and `takeover` owns where
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
    if report.outcome == "army_short":
        logger.error(
            "The army is under half the camp and training is instant, so this will not clear "
            "on its own; ending the series after %d round(s)",
            played,
        )
    return ROUND_ENDINGS.get(report.outcome)


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
    options: AttackOptions, frames: FrameLog, should_stop: Callable[[], bool] = stop_requested
) -> AttackRun:
    """The attack loop, for as many rounds as asked, answering every round and how the series ended.

    Thresholds, storage limits and the model to plan with all come from the
    settings file — the storage limits included, which means a full village
    stands this down before it searches. `minimums` overrides the thresholds
    alone, which is how a loop being studied gets the old behaviour back: all
    three at zero is "attack the first opponent shown", and skipping nothing is
    what puts the code worth watching on screen.

    `plan` replaces the AI entirely — the loop plays that file and asks for
    nothing — and `plan_out` writes down whichever plan actually ran, so a battle
    worth repeating can be repeated and one worth arguing with can be edited.
    A plan is written for one village, so the series plays that one and ends
    at once on the other; `plan_name` is a plan shipped for both, loaded for
    whichever village the series plays. Without a plan it plays whichever the game is on, and
    in neither case does it sail: crossing is `ai_coc world --day` / `--night`.

    `rounds` of 0 keeps going until it is stopped, which is what watching the
    loop play needs: a tactic is judged over a run of battles rather than one,
    and the interesting ones are the battles nobody was sitting there to start.
    Either `ai_coc stop` or a Ctrl-C ends the series rather than the process, so
    the rounds already played are still reported. The state file is the one that
    reaches a run put in the background, which nothing can send a Ctrl-C to.

    **`should_stop` is the one condition the series stops on**, between rounds
    and, handed to the runner, between opponents. The default reads the state
    file that `ai_coc stop` writes, and the `claim` around this call is what
    marks the run as under way for anybody looking from outside.
    """
    plan, world = _handed_plan(options)
    adb = _controller(frames)
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
    # The runner keeps its own display from here on and moves it through its
    # own restarts. Known and open: one recorded series lost its display with
    # no restart at all and died in `uncovered` on the runner's stale copy
    # (`Status: -2`).
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
        ai=None if plan or options.plan_name else _planner(config),
        plan=plan,
        plan_name=options.plan_name,
        should_stop=should_stop,
    )
    series = AttackSeries()
    rounds = options.rounds
    # What the loop ends on when nothing below breaks out of it first.
    ending: SeriesEnding = "repeat_done"
    while rounds <= 0 or len(series.root) < rounds:
        # Between rounds, which is the cheapest place to stop: the village is
        # on screen, nothing is deployed, and the rounds already played are
        # in the series either way.
        if should_stop():
            logger.info("Stop requested; ending the series after %d round(s)", len(series.root))
            ending = "stopped"
            break
        logger.info("Round %d of %s", len(series.root) + 1, rounds or "no limit")
        report = _round(runner, series)
        if isinstance(report, str):
            ending = report
            break
        series.root.append(report)
        # A builder base round reports no `attacked` — there is no scout
        # screen to have advertised any loot — so the two villages answer
        # "did this round really fight" differently and the wait between
        # rounds has to ask both ways.
        fighting = report.attacked is not None or report.phases > 0
        logger.info("Attack finished: %s", round_line(report), extra=_round_event(runner, series))
        _write_plan(options.plan_out, runner.played)
        if (over := _series_over(report, len(series.root))) is not None:
            ending = over
            break
        # A stop is let through first, so the series ends `stopped` and a loan
        # reads as one rather than as a run that gave up.
        if not should_stop() and _menu_out_of_reach(series):
            ending = "no_attack_menu"
            break
        # A stop that arrived mid-search comes back here having attacked
        # nothing, and a run about to walk away has no reason to wait
        # first. Falling through to the loop's own check is what logs why
        # the series ended, and `run.log` is the only thing a background run
        # leaves to read while it is still going.
        if not fighting and not should_stop() and (rounds <= 0 or len(series.root) < rounds):
            logger.info("Nothing was attacked; waiting %ds before the next round", IDLE_REST)
            if (cut := _rest(IDLE_REST, should_stop)) is not None:
                ending = cut
                break
    # A last round cut short by a stop ends the rounds asked for too, and the
    # stop is what the caller has to act on.
    if ending == "repeat_done" and should_stop():
        logger.info("Stop requested; ending the series after %d round(s)", len(series.root))
        ending = "stopped"
    return AttackRun(ending=ending, rounds=series)


# Only ever used to build the line a person reads; `outcome` is what a caller
# decides on. Both surveys measure the home village's battlefield and nothing
# here sails, so on the builder base they stop before spending anything —
# run there, one would walk the menus of a village it cannot fight on.
SURVEY_LINES: dict[SurveyOutcome, str] = {
    "surveyed": "量完了:{result}",
    "on_night": "遊戲停在夜世界,量的是日世界的戰場;要量先跑 ai_coc world --day",
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
        raise SessionTakenError


def probe(frames: FrameLog) -> BoundarySurvey:
    """Survey where drops are really accepted, and compare it to what the reader says.

    Spends a battle to answer one question the recorded frames cannot: whether
    the red line the parser found is the line the game is enforcing.
    """
    adb, display = _session(frames)
    if _village_now(adb, display) == "night":
        survey = BoundarySurvey(outcome="on_night")
        logger.warning("Boundary survey: %s", SURVEY_LINES[survey.outcome])
        return survey
    runner = _BoundarySurvey(adb=adb, display=display, thresholds=LootThresholds())
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


def bounds(frames: FrameLog) -> MapSurvey:
    """Spend a battle finding where the map really ends, and fit a diamond to it."""
    adb, display = _session(frames)
    if _village_now(adb, display) == "night":
        survey = MapSurvey(outcome="on_night")
        logger.warning("Map survey: %s", SURVEY_LINES[survey.outcome])
        return survey
    runner = _MapSurvey(adb=adb, display=display, thresholds=LootThresholds())
    _survey_battle(runner)
    logger.info("Map survey: %s", SURVEY_LINES["surveyed"].format(result=runner.survey.summary))
    return runner.survey


def stock(frames: FrameLog) -> StockReport:
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
    `ai_coc world --day` / `--night` and belongs to whoever asked, since a status check that
    moves the game is no longer one. The two are meant to be used together.

    The ceilings cost six taps and three captures, which is what makes the
    answer a percentage rather than a number nobody can size. That is the form
    every decision here is made in: `stop_at` is a share, and the storages grow
    when a builder finishes one, so reading them beats writing them down.
    """
    adb = _controller(frames)
    display = _settle_game(adb, WORLD_SETTLE_POLLS, park=False) or adb.display_for(COC_PACKAGE)
    return stock_of(ScreenRunner(adb=adb, display=display), adb, display)


def stock_of(runner: ScreenRunner, adb: AdbController, display: DisplayTarget) -> StockReport:
    """The storage half of `stock`, for a caller that already has a runner going.

    `status` asks three things of one village, and settling the game once for
    all three is the whole of what it saves over running the three commands.
    """
    world, held, gems = runner.read_storages()
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
            world, held, gems = runner.read_storages()
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
    report = StockReport(world=world, held=held, capacity=capacity, filled=filled, gems=gems)
    logger.info(
        "Stock: %s holds %s of %s, gems %s",
        world,
        filled or "an unknown share",
        capacity.model_dump(exclude_none=True) or "ceilings that would not read",
        gems,
    )
    return report


def status(frames: FrameLog, last: bool = False) -> StatusReport:
    """When every upgrade on both villages finishes, and what the village on screen holds.

    The timers come off the game's own export, worked through whatever
    speed-ups are running, so both villages are read without sailing and no
    model is asked anything. The plates' idle counts, the storages and the
    shield come off the village on screen, which is the only one that draws
    them. `last` reads the newest saved export instead and touches nothing,
    which is how a report is written after a stop, when a new run would log
    the player out.
    """
    if last:
        read = _last_export()
        return _timed(StatusReport(export=read.outcome), read.export)
    adb = _controller(frames)
    # Not parked: everything below taps fixed HUD places, and the park cost
    # about 14 of a status run's 50 seconds (the user's call of 2026-11-07).
    settled = _settle_game(adb, WORLD_SETTLE_POLLS, park=False)
    display = settled or adb.display_for(COC_PACKAGE)
    runner = PlateRunner(adb=adb, display=display)
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
        _exported(adb, display)
        if settled is not None and builder.world is not None
        else ExportReport(outcome="no_village")
    )
    stock = stock_of(runner, adb, display)
    report = _timed(
        StatusReport(
            world=builder.world or lab.world or stock.world,
            builder=builder,
            lab=lab,
            stock=stock,
            shield=shield,
            export=exported.outcome,
        ),
        exported.export,
    )
    logger.info(
        "Status: %s；%s；%s；%s",
        _count_line(builder),
        _count_line(lab),
        _timers_line(report),
        _shield_line(report.shield, report.world),
    )
    return report


def _timed(report: StatusReport, village: VillageExport | None) -> StatusReport:
    """The report with what the export says filled in, and what the newest purchases paid against list."""
    if village is not None:
        report.exported_at = village.exported_at
        report.boosts = village.boosts
        report.timers = upgrade_timers(village)
        report.clock_tower = clock_tower(village)
        report.helpers = helper_cooldowns(village)
        # Its own timers too: the `status` after a purchase is the first to list it,
        # and its line is not on the journal yet.
        own = (datetime.fromisoformat(village.exported_at), report.timers)
        report.paid, report.timed = _paid_shares(_kept_tables(), datetime.now().astimezone(), own)
    return report


# `confirm --item` as the `upgrades` skill writes it: the game's name, then the level bought.
ITEM = re.compile(r"(.+?)\s+(\d+)(?:\s*級)?")
# How old a purchase can be and still say what the village's upgrades cost
# today. An event or a gold pass season can end at any hour and nothing on the
# export or in the tables says when (checked 2026-11-13), and a price guessed
# too low has the takeover skip farming while the upgrade waits. A day is the
# user's call of 2026-11-13, not a measurement: a takeover confirms most days
# often enough to renew it (23 purchases on 11-12), and on a quiet one (1 on
# 11-09) the estimate falls back to list.
DISCOUNT_FRESH = timedelta(hours=24)
# Seconds either side of a recorded charge that a list time, discounted or
# not, still explains it. The press is on this machine's clock and the
# export's moment on the game's, a few seconds apart, and under a speed-up
# that gap counts at its rate: 烈焰熔爐 3, bought at 0.4 under a lab potion on
# 2026-11-12, fell outside a slack that did not. Replayed over the 41
# purchases of 11-12 and 11-13 that resolve, 39 time at their price's share
# and 2, with a status minutes later under a potion, at the low end.
CHARGE_SLACK = 60


def _paid_shares(
    tables: GameEntities | None,
    now: datetime,
    own: tuple[datetime, list[UpgradeTimer]] | None = None,
) -> tuple[dict[World, PaidShare], dict[World, PaidShare]]:
    """What the newest purchases on each village paid against list, in price and in time, off the journal.

    A `confirm` counts whatever it went on to do once the button asked exactly
    the price the agent read off the row, since that is the price today; a
    wall's is several walls and is left out. Its item has to name one level of
    one village at one list price: 12 names are on both villages and 19 on
    several entities of one. Only purchases within `DISCOUNT_FRESH` of `now`
    count, and a `listprice` line ends every older one on its village, as a
    purchase at list price would. The time share is the newest such purchase
    whose job a later `status` timed (`_time_share`), `own` being the timers
    of the `status` asking, not yet on the journal.
    """
    if tables is None:
        return {}, {}
    listed = _listed(tables)
    lines = _journal()
    starts = {line.run: line for line in lines if line.event == "start"}
    statuses = sorted(
        [*_status_timers(lines, starts), *([own] if own else [])], key=lambda one: one[0]
    )
    paid: dict[World, PaidShare] = {}
    timed: dict[World, PaidShare] = {}
    ended: set[World] = set()
    for end in reversed(lines):
        start = starts.get(end.run)
        if end.event != "end" or start is None or end.at < now - DISCOUNT_FRESH:
            continue
        if start.command == "listprice":
            with suppress(ValueError):
                ended.add(ListPriceReport.model_validate(end.result).world)
            continue
        bought = _purchase(end, listed) if start.command == "confirm" else None
        if bought is None or bought[0] in ended:
            continue
        world, share, item, level, report, found = bought
        paid.setdefault(world, PaidShare(share=share, at=end.at, item=item))
        # The run's end, after the press, only shortens the work counted since.
        pressed = _press_time(start, report.pressed) or end.at
        times = {data_id: time for _, _, data_id, time in found if time}
        if world not in timed:
            spent = _time_share(times, level, pressed, statuses, share)
            if spent is not None:
                timed[world] = PaidShare(share=spent, at=end.at, item=item)
    return paid, timed


# Each priced level by the game's name, the level and its resource, with the
# village, list price, data_id and list time of every entity that matches.
Listed = dict[tuple[str, int, str], list[tuple[World, int, int, int | None]]]


def _listed(tables: GameEntities) -> Listed:
    """The tables' levels keyed the way `confirm --item` names one."""
    listed: Listed = {}
    for entity in tables.entities:
        for level in entity.levels:
            for resource, amount in level.cost.items():
                key = (entity.zh or "", level.lvl, resource)
                listed.setdefault(key, []).append((
                    entity.world,
                    amount,
                    entity.data_id,
                    level.time,
                ))
    return listed


def _purchase(
    end: JournalLine, listed: Listed
) -> tuple[World, float, str, int, ConfirmReport, list[tuple[World, int, int, int | None]]] | None:
    """A `confirm`'s village, the share of list it paid, its item and level, its report and the table rows the item names; None where it says nothing of today's price."""
    try:
        report = ConfirmReport.model_validate(end.result)
    except ValueError:
        return None
    named = ITEM.fullmatch(report.item or "")
    offer = report.offer
    if named is None or offer is None or report.wall is not None:
        return None
    found = listed.get((named[1], int(named[2]), offer.resource), [])
    priced = {(world, amount) for world, amount, _, _ in found}
    if offer.price != report.asked or len(priced) != 1:
        return None
    [(world, amount)] = priced
    if not 0 < offer.price <= amount:
        return None
    return world, offer.price / amount, named[0], int(named[2]), report, found


def _status_timers(
    lines: list[JournalLine], starts: dict[str, JournalLine]
) -> list[tuple[datetime, list[UpgradeTimer]]]:
    """Every `status` run's timers with the moment its export was taken."""
    statuses: list[tuple[datetime, list[UpgradeTimer]]] = []
    for end in lines:
        if end.event == "end" and (start := starts.get(end.run)) and start.command == "status":
            with suppress(ValueError):
                read = JournalTimers.model_validate(end.result)
                if read.exported_at is not None:
                    statuses.append((read.exported_at, read.timers))
    return statuses


def _time_share(
    times: dict[int, int],
    level: int,
    pressed: datetime,
    statuses: list[tuple[datetime, list[UpgradeTimer]]],
    price: float,
) -> float | None:
    """What a purchase's job was charged against its list time, off the first `status` after it that lists the job.

    The charge is the work done between the press and that export plus the
    work it still had (`timer`). The work done lies between the seconds gone
    and those seconds at the speed-up running at the export, since a potion
    may have started after the press, and a helper's is not counted. The game
    floors a discounted time (`_panel_floor`), so where the price's share
    floored lands inside that range, widened by `CHARGE_SLACK`, it is the
    time's share too, and where the list time does the time was not
    discounted; otherwise the lower end, which errs short. None where no
    `status` listed the job.
    """
    for at, timers in statuses:
        jobs = [one for one in timers if one.data_id in times and one.level == level - 1]
        if at < pressed or not jobs:
            continue
        job = max(jobs, key=lambda one: one.timer)
        listed = times[job.data_id]
        gone = (at - pressed).total_seconds()
        rate = SPEED_UPS.get((job.world, job.role), ("", 1))[1] if job.boost and job.role else 1
        low = job.timer + gone - CHARGE_SLACK
        high = job.timer + (gone + CHARGE_SLACK) * rate
        for share in (price, 1.0):
            if low <= _panel_floor(listed * share) <= high:
                return share
        return min((job.timer + gone) / listed, 1.0)
    return None


def listprice(world: World) -> ListPriceReport:
    """Record that the village's build panel asks list price now, which ends the discount `status` estimates with."""
    logger.info("List price on %s from now on", world)
    return ListPriceReport(world=world)


# Rows of one kind, level and slot whose upgrades end this close together are one row
# of the report, with a count: `takeover` wakes five minutes ahead of a finish and handles
# whatever ends inside that window in the same wake.
MERGE_WITHIN = 5 * 60
# The multiplication sign the reports count merged rows with, escaped for Ruff.
TIMES = "\u00d7"
# The counters as the reports and the log name them.
COUNT_NAMES: dict[PlateRole, str] = {"builder": "工人", "lab": "實驗室"}
# Whose slot each row of the table holds, which the names alone leave to guessing.
ROLE_NAMES: dict[UpgradeRole, str] = {"builder": "工人", "lab": "實驗室", "pet": "戰寵"}
# The Builder's Apprentice and the Lab Assistant, the two helpers that speed up
# the upgrades the report lists; the Alchemist and the Prospector do not, and an
# unused one reads as ready on every export.
UPGRADE_HELPERS = (93000000, 93000001)
# How many hours ahead the shortfall lines sum the next levels: the takeover's
# farm check interval (`watch --minutes 180`), the longest it goes without
# reading again. Replayed over the 60 home village `status` runs of 2026-11-11
# and 11-12 at the share each would have read, it fired on 6: the three dark
# elixir runs of 18:19-18:47, before five dark purchases in a row, and three
# of 11-11 before any purchase named its item, at list price. At list price
# throughout it fired on 20.
SHORT_WITHIN = 3
# The resources each village's storages hold; the builder base's third bar is its gems.
STORED: dict[World, tuple[str, ...]] = {
    "day": ("gold", "elixir", "dark"),
    "night": ("gold", "elixir"),
}
# The running speed-ups as the report names them.
BOOST_NAMES = {
    "builder_boost": "工人藥水",
    "lab_boost": "實驗室藥水",
    "pet_boost": "戰寵藥水",
    "clocktower_boost": "鐘塔加速",
}


def status_text(
    report: StatusReport, now: datetime | None = None, within: int | None = None
) -> str:
    """The report as `takeover` and `upgrades` hand it to the user: each village, then its upgrades as a table.

    **This, not the JSON, is what `status` prints**, because whoever reads its
    stdout is an agent about to write this report; the fields stay in
    `result.json` and behind `--json`. Each row is facts only: when, whose slot
    (a goblin's marked), the item with its levels, and what its next level
    asks. What follows a row (continue it, switch, ask) is the agent's to
    decide and add. Times are read against `now`, since `--last`
    prints an export taken a while ago. `within` keeps the rows finishing in
    that many hours and folds the rest into one line, as the report a stop
    hands over lists them.

    The next level's price and time are the list's scaled by the village's
    newest purchases (`paid`, `timed`), and the head line names them, since a
    list price cries wolf all through an event. Under the village whose
    storages were read come a line per resource they do not cover if every
    row finishing within `SHORT_WITHIN` hours continued, and nothing when they
    do; then the speed-ups running and when each ends.
    """
    now = now or datetime.now().astimezone()
    if report.exported_at is None:
        head = f"村莊匯出沒讀到: {EXPORT_LINES[report.export].format(tag='', count=0)}"
    else:
        head = f"村莊匯出: {_moment(report.exported_at, now)}"
        if report.stock is None:
            head += " (存下的那份, 倉庫跟計數這次沒讀)"
        head += f"; {_paid_line(report.paid, report.timed, now)}"
    shown = [head]
    for world in ("day", "night"):
        shown += ["", _world_line(report, world)]
        if report.exported_at is None:
            continue
        rows = _timer_rows(report, world, now)
        near = [row for row in rows if within is None or row[0] <= now + timedelta(hours=within)]
        if near:
            shown += ["", "| 時間 | 目標 | 項目 |", "| --- | --- | --- |"]
            shown += [f"| {when} | {slot} | {item}{cost} |" for _, when, slot, item, cost in near]
        if later := rows[len(near) :]:
            names = "、".join(item for _, _, _, item, _ in later)
            shown += ["", f"之後還有 {names}, 最晚 {later[-1][1]}"]
        if not rows:
            shown.append("沒有在升的")
        for block in (_short_lines(report, world, now), _boost_lines(report, world, now)):
            shown += ["", *block] if block else []
        helpers = [
            f"{helper.zh or helper.en or f'#{helper.data_id}'} "
            + (
                "現在能派"
                if datetime.fromisoformat(helper.ready_at) <= now
                else f"{_moment(helper.ready_at, now)} 能派"
            )
            for helper in report.helpers
            if helper.data_id in UPGRADE_HELPERS
        ]
        if world == "day" and helpers:
            shown += ["", f"助手: {'、'.join(helpers)}"]
    return "\n".join(shown)


def _world_line(report: StatusReport, world: World) -> str:
    """A village's heading, with its storages and counters where the game was standing on it."""
    name = WORLD_NAMES[world]
    if report.stock is None:
        return name
    if report.world is None:
        return f"{name}: 畫面不是村莊, 倉庫跟計數沒讀到"
    if report.world != world:
        return f"{name}: 遊戲停在{WORLD_NAMES[report.world]}, 倉庫跟計數這次沒讀"
    stock = report.stock
    if stock.held is None:
        held = "倉庫讀不到"
    else:
        held = "、".join(
            f"{_RESOURCE_NAMES[one]} {_wan(getattr(stock.held, one))}"
            + (f" ({stock.filled[one]})" if one in stock.filled else "")
            for one in STORED[world]
        )
        if stock.gems is not None:
            held += f"、寶石 {stock.gems:,}"
    counts = "、".join(
        f"{COUNT_NAMES[count.role]} "
        + (f"{count.free}/{count.total}" if count.outcome == "counted" else "讀不到")
        for count in (report.builder, report.lab)
        if count is not None
    )
    if world == "night":
        return f"{name}: {held}; {counts}"
    shield = report.shield
    if shield is None:
        guard = "護盾讀不到"
    else:
        guard = "沒有護盾" if shield.remaining is None else f"護盾 {spell_out(shield.remaining)}"
    return f"{name}: {held}; {counts}; {guard}"


def _timer_rows(
    report: StatusReport, world: World, now: datetime
) -> list[tuple[datetime, str, str, str, str]]:
    """One village's rows of the table, soonest first, the clock tower among them.

    Each is the instant it finishes, its three cells, and what its next level
    asks as the tail of the item cell. A helper working on a row is named in
    its slot with the time its work there ends, and one an old export could
    not count is said beside the time.
    """
    share = report.paid[world].share if world in report.paid else 1.0
    spent = report.timed[world].share if world in report.timed else 1.0
    merged: list[tuple[UpgradeTimer, int]] = []
    for timer in (one for one in report.timers if one.world == world):
        same = next(
            (
                index
                for index, (first, _) in enumerate(merged)
                if (first.data_id, first.level, first.extra, first.helper_until)
                == (timer.data_id, timer.level, timer.extra, timer.helper_until)
                and timer.seconds - first.seconds <= MERGE_WITHIN
            ),
            None,
        )
        if same is None:
            merged.append((timer, 1))
        else:
            first, count = merged[same]
            merged[same] = (first, count + 1)
    helpers = {helper.data_id: helper.zh or helper.en for helper in report.helpers}
    rows = [
        (
            done := datetime.fromisoformat(first.done_at),
            _moment(first.done_at, now)
            + ("" if first.role else " (沒算加速)")
            + (" (沒算助手)" if first.helper_until and first.helper is None else "")
            + (" (已過)" if done <= now else ""),
            (ROLE_NAMES[first.role] if first.role else "—")
            + (" (哥布林)" if first.extra else "")
            + (
                f" ({helpers.get(first.helper) or f'#{first.helper}'}幫到"
                f"{_moment(first.helper_until, now)})"
                if first.helper is not None and first.helper_until
                else ""
            ),
            (first.zh or first.en or f"#{first.data_id}")
            + (f" {TIMES}{count}" if count > 1 else "")
            + _levels(first),
            _next_text(_next_cost(first, share), _next_time(first, spent), count),
        )
        for first, count in merged
    ]
    tower = report.clock_tower
    if world == "night" and tower is not None:
        ready = datetime.fromisoformat(tower.ready_at)
        when = "現在" if ready <= now else _moment(tower.ready_at, now)
        rows.append((ready, when, "—", "鐘塔冷卻結束", ""))
    return sorted(rows, key=lambda row: row[0])


def _next_cost(timer: UpgradeTimer, share: float) -> dict[str, int]:
    """What continuing a row asks, its list price scaled by `share`; empty where it cannot continue.

    It cannot past its last level (no `next`), on a level the hall does not
    allow yet, or with no level at all.
    """
    upgrade = timer.next
    if upgrade is None or (timer.limited_lvl is not None and upgrade.lvl > timer.limited_lvl):
        return {}
    return {resource: round(amount * share) for resource, amount in upgrade.cost.items()}


def _next_time(timer: UpgradeTimer, share: float) -> int | None:
    """How long continuing a row takes, its list time scaled by `share` and floored as the game does; None where it cannot continue or the table has no time."""
    upgrade = timer.next
    if (
        upgrade is None
        or upgrade.time is None
        or (timer.limited_lvl is not None and upgrade.lvl > timer.limited_lvl)
    ):
        return None
    return upgrade.time if share >= 1 else _panel_floor(upgrade.time * share)


def _panel_floor(seconds: float) -> int:
    """A discounted duration as the game charges it: down to the hour past a day, to ten minutes past an hour, else to the minute.

    The steps are what the export's timers and the panel showed for seven
    purchases at 0.4 on 2026-11-13: 4天 charged 1天14小時 (38.4 h), 12天
    4天19小時 (115.2 h), 1天 9小時30分 (9 h 36 min), 2天 19小時10分 (19 h 12 min),
    8小時 3小時10分 (3 h 12 min). Under an hour none was seen; the minute is the
    smallest step the panel draws.
    """
    step = 3600 if seconds >= 86400 else 600 if seconds >= 3600 else 60
    return int(seconds // step * step)


def _span(seconds: int) -> str:
    """A duration as the build panel writes it, its two largest units: `2天19小時`, `6天`, `19小時10分`, `45分`."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}天" + (f"{hours}小時" if hours else "")
    if hours:
        return f"{hours}小時" + (f"{minutes}分" if minutes else "")
    return f"{minutes}分"


def _next_text(cost: dict[str, int], time: int | None, count: int) -> str:
    """`, 下一級: 黑水 5.6 萬; 2天19小時`, `下一級各` on a merged row, `—` where it cannot continue."""
    if not cost:
        return ", 下一級: —"
    each = "各" if count > 1 else ""
    asks = "、".join(
        f"{_RESOURCE_NAMES.get(name, name)} {amount / 10_000:g} 萬"
        for name, amount in cost.items()
    )
    takes = "" if time is None else f"; {_span(time)}"
    return f", 下一級{each}: {asks}{takes}"


def _paid_line(paid: dict[World, PaidShare], timed: dict[World, PaidShare], now: datetime) -> str:
    """Where the next levels' prices and times come from: each village's newest purchases, or the list."""

    def part(label: str, one: PaidShare | None, named: bool) -> str:
        if one is None:
            return f"{label}牌價"
        said = f"{label} {round(one.share * 10, 2):g} 折"
        return f"{said} ({one.item}, {_moment(one.at.isoformat(), now)})" if named else said

    sources = []
    for world in ("day", "night"):
        price, time = (
            one if (one := shares.get(world)) is not None and one.share < 1 else None
            for shares in (paid, timed)
        )
        if price is None and time is None:
            sources.append(f"{WORLD_NAMES[world]}牌價")
            continue
        same = (
            price is not None
            and time is not None
            and (price.item, price.at) == (time.item, time.at)
        )
        sources.append(
            f"{WORLD_NAMES[world]}{part('價格', price, not same)}、{part('時間', time, True)}"
        )
    if not any(one.share < 1 for one in (*paid.values(), *timed.values())):
        return "下一級是牌價"
    return f"下一級照最近實付估: {'; '.join(sources)}"


def _short_lines(report: StatusReport, world: World, now: datetime) -> list[str]:
    """One line per resource the village's storages do not cover if every row due within `SHORT_WITHIN` hours continued.

    Only for the village whose storages this run read. A row that cannot
    continue adds nothing, and one the agent will switch adds its own next
    level, since only the agent knows what goes there.
    """
    stock = report.stock
    if stock is None or stock.held is None or stock.world != world:
        return []
    share = report.paid[world].share if world in report.paid else 1.0
    horizon = now + timedelta(hours=SHORT_WITHIN)
    need: Counter[str] = Counter()
    for timer in report.timers:
        if timer.world == world and datetime.fromisoformat(timer.done_at) <= horizon:
            need.update(_next_cost(timer, share))
    return [
        f"資源不足 ({SHORT_WITHIN} 小時內到點的都續升): {_RESOURCE_NAMES[one]}"
        f" 要 {need[one]:,} / 有 {held:,} (差 {need[one] - held:,})"
        for one in STORED[world]
        if need[one] > (held := getattr(stock.held, one))
    ]


def _boost_lines(report: StatusReport, world: World, now: datetime) -> list[str]:
    """A line of the village's speed-ups still running at `now` and when each ends, or none."""
    if report.exported_at is None:
        return []
    taken = datetime.fromisoformat(report.exported_at)
    keys = dict.fromkeys(
        key for (where, _role), (key, _rate) in SPEED_UPS.items() if where == world
    )
    running = [
        f"{BOOST_NAMES.get(key, key)} 到{_moment(end.isoformat(), now)}"
        for key in keys
        if (left := getattr(report.boosts, key)) and (end := taken + timedelta(seconds=left)) > now
    ]
    return [f"加速: {'、'.join(running)}"] if running else []


def _levels(timer: UpgradeTimer) -> str:
    """` (bought/max)`, with the hall's cap where it stops short of the max; nothing for a row with no level."""
    if timer.level is None:
        return ""
    top, cap = timer.max_lvl, timer.limited_lvl
    held = f", 上限 {cap}" if top is not None and cap is not None and cap < top else ""
    return f" ({timer.level + 1}/{'—' if top is None else top}{held})"


def _moment(when: str, now: datetime) -> str:
    """`今天 18:25`, `明天 05:00`, or the date for anything further off."""
    at = datetime.fromisoformat(when).astimezone()
    day = {0: "今天", 1: "明天"}.get((at.date() - now.date()).days, f"{at:%m-%d}")
    return f"{day} {at:%H:%M}"


def _wan(amount: int) -> str:
    """An amount in 萬, with one decimal under ten thousand, as the reports write them."""
    if amount and abs(amount) < 10_000:
        return f"{amount / 10_000:.1f} 萬"
    return f"{round(amount / 10_000)} 萬"


# One line per way a plate's count can come back, for `run.log`.
COUNT_LINES: dict[PlateCountOutcome, str] = {
    "counted": "{held} {free}/{total}",
    "unread": "{held}的數字讀不到",
    "not_a_village": "畫面不在村莊,讀不到右下的數字",
}


def _count_line(count: PlateCount) -> str:
    """One plate's count as a line for a person."""
    return COUNT_LINES[count.outcome].format(
        held=COUNT_NAMES[count.role], free=count.free, total=count.total
    )


def _timers_line(report: StatusReport) -> str:
    """The soonest upgrade to finish, or why there is none to name."""
    if report.export != "exported":
        return EXPORT_LINES[report.export].format(tag="", count=0)
    if not report.timers:
        return "兩個世界都沒有在升的"
    soonest = report.timers[0]
    name = soonest.zh or soonest.en or soonest.section
    return f"最快好的是{WORLD_NAMES[soonest.world]}的 {name},{soonest.done_at[11:16]}"


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
    "collected": "夜世界的推車收到聖水 {elixir}",
    "empty": "按了收集,但儲量沒有變,推車應該是空的",
    "locked": "推車是開的,收集鈕是灰的,而車上的數字讀不到",
    "locked_holding": "推車裡存著 {held}／{capacity} 聖水,收集鈕是灰的 —— 聖水倉庫滿了,倉庫有空間再收",
    "locked_empty": "推車是空的,收集鈕是灰的,沒有東西可以收",
    "not_found": "三個候選點都沒有打開推車",
    "not_parked": "鏡頭沒辦法停回定位,這一趟沒有去找聖水車",
    "not_night": "現在不在夜世界,沒有推車可以收",
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
    "on_night": "遊戲停在夜世界,日世界的採集器沒有收",
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
        world = current_world(adb.screenshot(display, "world"))
        if world is not None:
            return world
        time.sleep(RESTART_POLL_GAP)
    return None


def collect(frames: FrameLog) -> CollectReport:
    """Tap every collector the village has left standing.

    Collectors stop once they are full, so a village nobody has emptied has spent
    most of its time doing nothing. This is the cheapest thing in the project to
    run and the one worth running most often.

    **The builder base's cart has answers of its own, and this used to squash them into two.**
    See `CartReport`: a cart the game had locked was reported as an empty one,
    which is the opposite instruction.
    """
    adb, display = _session(frames)
    # **Decided on a village that has painted, not on the first frame.** Every
    # branch but the cart's is the home village's, and a frame caught between a
    # battle and its village used to send a run meant for the cart across the
    # water instead, measured live 13 s after a builder base attack stood down.
    # Only the world is waited for: both branches park the camera themselves.
    report = None
    if _village_now(adb, display) != "night":
        report = UpkeepRunner(adb=adb, display=display).collect()
    # **The builder base has no collectors to sweep and one cart instead.** Its
    # elixir is paid into that cart rather than into the storages, so this is
    # the same job on that village even though it shares none of the machinery:
    # one tap at a known spot rather than a colour-and-size search over the map.
    # A village slower to paint than the wait above goes down the home path,
    # which stops rather than sails on the builder base, so its cart is taken
    # here as well.
    if report is None or report.outcome == "on_night":
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
    "on_night": "遊戲停在夜世界,捐兵只在日世界;要捐先跑 ai_coc world --day",
}


def donate(options: DonateOptions, frames: FrameLog) -> DonateReport:
    """Give troops to whoever in the clan is asking.

    Cheap to run and cheap to find nothing: a clan with no request open costs one
    tap on the chat tab and one capture. `dry_run` walks the whole path and stops
    before the tap that gives something away, because the panel does not confirm.
    """
    adb, display = _session(frames)
    report = ClanRunner(
        adb=adb, display=display, dry_run=options.dry_run, rounds=options.rounds
    ).donate()
    logger.info(
        "Donate: %s",
        DONATE_LINES[report.outcome].format(gifts=len(report.gifts), offered=report.offered),
    )
    return report


def world(go: World | None, frames: FrameLog) -> WorldReport:
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
    the one that waits a cold start out. A frame no village reads on is kept as
    `no_village`.

    **Reading takes no claim and starts nothing** (`_attached`): it runs beside a
    loop that holds the emulator without disturbing it. Crossing drives the game,
    so it goes through `_controller` and the claim like any other command.
    """
    adb = _controller(frames) if go is not None else _attached(frames)
    try:
        display = adb.display_for(COC_PACKAGE)
    except AdbControlError:
        logger.warning("The game is not on a display yet; neither village can be confirmed")
        return WorldReport(found=None, world=None, outcome="no_display")
    png = adb.screenshot(display, "world")
    found = current_world(png)
    if found is None:
        frames.write("no_village", png)
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
    "not_a_village": "畫面不是村莊,沒有把鏡頭停回定位",
    "zoomed_in": "拉近的鏡頭停不住,沒有把鏡頭停回定位",
    "never_settled": "但鏡頭一直沒有停下來,定位失敗",
    "selected": "但有建築或城牆還選著,拖曳可能拖到它,沒有停鏡頭;先點一塊空地取消選取再來",
}


def view_line(report: ViewReport) -> str:
    """What this command asked of the camera, and what it settled."""
    verb = "拉遠" if report.zoom == "out" else "拉近"
    scaled = f"鏡頭{verb}了 {report.times} 次" if report.times else f"鏡頭沒有{verb}"
    return f"{scaled},{VIEW_LINES[report.outcome]}"


def view(frames: FrameLog, zoom: str = "out", times: int = 3) -> ViewReport:
    """Put the village camera back where every coordinate here was measured.

    **The game has no zoom control to tap and reports no zoom level**, so this
    writes the two-finger gesture straight to the touch device — see
    `AdbController.pinch` for why `input` cannot. Zooming out past the far limit
    does nothing at all, which is what makes `--zoom out` safe to send without
    knowing where the camera is.

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

    **Nothing touches the screen until a capture says it is a village**, the
    pinch included. A pinch is two fingers put down on whatever is drawn under
    them, not a gesture only the camera hears: run with the build panel's lab tab
    still open, one finger started on the 亡靈 row and opened its upgrade sheet
    (2026-11-10). A swipe with a card selected deploys troops along its path, and
    a killed run leaves the game mid-battle, which is exactly when somebody
    reaches for this. So any screen `current_world` does not read as a village
    gets no gesture at all and `times` 0.

    **The park is gated a second time, on a capture after the pinch**, which
    answers the three questions the park has: whether this is still a village,
    which of the two, since the maps clamp in opposite corners, and whether
    something on it is still selected (`selection_row`). Anything else keeps the
    zoom and says the camera was left where it was.

    **And `--zoom in` is refused a park**, for the reason `park_camera` pinches
    at all: its walk is only bounded at the far zoom, and measured two pinches in
    the camera runs off the village and was still moving after fourteen swipes.
    So a park asked for there would undo the zoom just requested and then claim a
    position it never reached. The cost on the ordinary path is that
    `--zoom out` pinches twice — `--times` here and `ZOOM_PINCHES` inside the
    park — which past the far limit does nothing at all and is why the flag is
    still the knob it says it is.
    """
    adb = _controller(frames)
    display = adb.display_for(COC_PACKAGE)
    asked: Literal["in", "out"] = "out" if zoom == "out" else "in"
    village = current_world(adb.screenshot(display, "before-zoom")) is not None
    if village:
        adb.zoom(zoom, times, COC_PACKAGE, display)
    else:
        times = 0
    world = None
    if not village:
        outcome: ViewOutcome = "not_a_village"
    elif zoom != "out":
        # **A camera that has just been zoomed in cannot be parked**, so asking
        # would undo the thing that was asked for and claim a position it never
        # reached. Measured two pinches in, the camera runs off the village into
        # the map's dark border and was still walking hundreds of pixels per
        # swipe after fourteen of them; `park_camera` pinches back out for
        # exactly that reason, which on this path is the opposite of the request.
        outcome = "zoomed_in"
    elif (world := current_world(frame := adb.screenshot(display, "view"))) is None:
        outcome = "not_a_village"
    elif selection_row(frame):
        outcome = "selected"
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


def _export_path(tag: str, when: datetime) -> Path:
    """Where one account's export of one day is kept, with the tag made safe to be a filename.

    One file a day, the day's last export, so `growth` has something to compare
    against. The tag comes off the clipboard, so it is outside data: anything
    that parses as JSON can put a `/` or a trailing dot in it, which is a path
    escape rather than a filename.
    """
    safe = "".join(ch for ch in tag if ch.isalnum() or ch in "-_#") or "UNKNOWN"
    return ACCOUNT_JSON_DIR / f"{safe}_{when.astimezone():%Y-%m-%d}.json"


def _tables(adb: AdbController) -> GameEntities | None:
    """The installed game's tables to merge into an export, or None where none can be had.

    The export is saved either way: what it says about the village is the
    game's own, and the tables only add names, levels and prices to it, so a
    first run on a version whose tables do not read still keeps the day's file.
    """
    try:
        return game_data(lambda: adb).tables
    except (RuntimeError, OSError, ValueError):
        logger.warning(
            "No game tables to be had; the export is saved without names, levels or prices",
            exc_info=True,
        )
        return None


def _kept_tables() -> GameEntities | None:
    """The newest game tables kept on disk, for converting an old export without the emulator."""
    newest = newest_kept()
    return newest[1] if newest else None


def _saved_exports(tables: GameEntities | None) -> list[VillageExport]:
    """Every export kept in `account_json/`, oldest first, an old-format one converted on the way.

    A file saved before the merge (`LegacyExport`) is rewritten in place as a
    merged one the first time it is read with the game's tables at hand, so
    the days before the merge keep counting in `growth`. Without tables it is
    left as it is, and out, since converting drops the name it carried. A file
    that reads as neither is left out.
    """
    saved: list[VillageExport] = []
    for path in ACCOUNT_JSON_DIR.glob("*.json"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("%s could not be read; left out", path, exc_info=True)
            continue
        with suppress(ValueError):
            saved.append(VillageExport.model_validate_json(text))
            continue
        try:
            old = LegacyExport.model_validate_json(text)
        except ValueError:
            logger.warning("%s does not read back as an export; left out", path)
            continue
        if tables is None:
            logger.warning(
                "%s is in the shape before the merge and no game tables are kept to convert it; left out",
                path,
            )
            continue
        export = from_legacy(old, tables)
        path.write_text(export.model_dump_json(indent=2), encoding="utf-8")
        logger.info(
            "Converted %s to the merged shape with the tables of %s", path, tables.game_version
        )
        saved.append(export)
    return sorted(saved, key=lambda one: one.timestamp)


def _last_export() -> ExportReport:
    """The most recent export on this machine, by when the game took it, without touching the emulator.

    This is what `--last` prints. Files that are there but none of which reads
    as an export (the game's raw payload, saved before this command existed)
    say so rather than coming back as an empty village.
    """
    saved = _saved_exports(_kept_tables())
    if saved:
        logger.info("Read %s back, taken %s", saved[-1].tag, saved[-1].exported_at)
        return ExportReport(outcome="read_back", export=saved[-1])
    return ExportReport(
        outcome="wrong_format" if any(ACCOUNT_JSON_DIR.glob("*.json")) else "never_exported"
    )


def _copy_village(adb: AdbController, display: DisplayTarget) -> tuple[str, ExportOutcome]:
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
    adb.tap(*PROFILE_AVATAR, display)
    time.sleep(PAGE_SETTLE)
    if not window_open(adb.screenshot(display, "profile")):
        return "", "settings_shut"
    adb.tap(*SETTINGS_TAB, display)
    time.sleep(PAGE_SETTLE)
    if not settings_open(adb.screenshot(display, "settings")):
        return "", "settings_shut"
    adb.tap(*MORE_SETTINGS, display)
    time.sleep(PAGE_SETTLE)
    if not more_settings_open(adb.screenshot(display, "more-settings")):
        return "", "more_settings_shut"
    for attempt in range(SCROLL_TRIES):
        spot = export_row(adb.screenshot(display, f"scroll-{attempt}"))
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
        adb.screenshot(display, "nothing-copied")
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
    "not_an_export": "剪貼簿裡的不是村莊資料,複製的當下可能被別的東西蓋過去了",
}


def export_line(report: ExportReport) -> str:
    """The one line a person reads off an export, whether it was taken or read back."""
    export = report.export
    return EXPORT_LINES[report.outcome].format(
        tag=export.tag if export else "", count=len(list(export.rows())) if export else 0
    )


def export(frames: FrameLog, last: bool = False) -> ExportReport:
    """Get the village out of the game and merge the game's own tables into it.

    The game will write the whole village out as JSON, which is the only
    complete account of what a village holds — every building, every level,
    every troop — and far more than the screen readers can see. It used to be
    driven by asking Gemini to find each of the three buttons on a screenshot,
    once per step, which is both unreliable and an AI call for something that
    never moves. The coordinates are measured now and the reading is what
    decides whether to tap them.

    Only the merged result is kept (`VillageExport`), and it keeps every key
    the game wrote on every row it keeps.
    """
    if last:
        return _last_export()
    adb = _controller(frames)
    display = _settle_game(adb, WORLD_SETTLE_POLLS, park=False)
    if display is None:
        return ExportReport(outcome="no_village")
    return _exported(adb, display)


def _exported(adb: AdbController, display: DisplayTarget) -> ExportReport:
    """The walk through the settings menus and everything after it, on a village already up.

    Shared with `status`, which reads its timers off the same payload. The
    caller is the one that knows a village is on screen: every tap here aims at
    a fixed place, and one aimed at a battle can deploy whatever card is
    selected there.
    """
    try:
        payload, copied = _copy_village(adb, display)
        if copied != "exported":
            return ExportReport(outcome=copied)
        # Parsed inside the same guard, because the clipboard is the one input
        # here that comes from outside: anything else copied during the poll is
        # read as the payload, and a `ValidationError` reaching the top would
        # leave `result.json` unwritten with only the log to reconstruct from.
        document = parse_village_text(payload)
    except ValueError as exc:
        logger.warning("The clipboard payload was not a village: %s", exc)
        return ExportReport(outcome="not_an_export")
    finally:
        # Whatever happened, the profile window is left covering the village,
        # and every other command starts by assuming one is on screen. Only
        # where it is up: with none, that place is the gold bar.
        if window_open(adb.screenshot(display, "profile-left")):
            adb.tap(*CLOSE_SETTINGS, display)
    at = document.timestamp or int(time.time())
    tables = _tables(adb)
    village = merge(document, tables, at)
    path = _export_path(village.tag, datetime.fromtimestamp(at, UTC))
    path.write_text(village.model_dump_json(indent=2), encoding="utf-8")
    report = ExportReport(outcome="exported", export=village)
    unnamed = sum(1 for _, _, row in village.rows() if row.zh is None)
    logger.info("Export: %s,其中 %d 筆還沒有名字,存到 %s", export_line(report), unnamed, path)
    # While the installed game's tables are at hand, any file still in the
    # shape before the merge is converted.
    _saved_exports(tables)
    return report


def _snapshots() -> list[VillageExport]:
    """Every saved export of the account last exported, oldest first."""
    saved = _saved_exports(_kept_tables())
    return [one for one in saved if one.tag == saved[-1].tag] if saved else []


def _day(export: VillageExport) -> date:
    """The local day an export was taken on."""
    return datetime.fromtimestamp(export.timestamp, UTC).astimezone().date()


def growth(start: date | None = None, end: date | None = None) -> GrowthReport:
    """What was upgraded from the start of `start` to the end of `end`, both days included.

    A day's file is its last export, so the baseline is the last one dated
    before `start`, and the first on or after it only when nothing earlier is
    saved. Both default to the ends of what is saved. Reads files alone, so it
    touches neither the game nor the emulator; `status` saves the day's export.
    """
    saved = _snapshots()
    earlier = [one for one in saved if start is not None and _day(one) < start]
    first = (
        earlier[-1]
        if earlier
        else next((one for one in saved if start is None or _day(one) >= start), None)
    )
    last = next((one for one in reversed(saved) if end is None or _day(one) <= end), None)
    if first is None or last is None or first.timestamp >= last.timestamp:
        report = GrowthReport(outcome="too_few")
    else:
        report = GrowthReport(
            tag=last.tag,
            start=first.exported_at,
            end=last.exported_at,
            changes=village_growth(first, last),
            outcome="compared",
        )
    logger.info("%s", GROWTH_LINES[report.outcome].format(count=len(report.changes)))
    return report


# One line per way `ai_coc growth` can end, for `run.log`.
GROWTH_LINES: dict[GrowthOutcome, str] = {
    "compared": "比對了兩份匯出, {count} 種有升級",
    "too_few": "存下的匯出不到兩份可以比, 每跑一次 status 會存當天那一份",
}


def growth_text(report: GrowthReport) -> str:
    """The report as a person reads it: each village, then the builders' upgrades and the laboratory's."""
    if report.outcome != "compared":
        return GROWTH_LINES[report.outcome]
    shown = [f"{report.tag}  {_local(report.start)} → {_local(report.end)}"]
    for world, title in (("day", "日世界"), ("night", "夜世界")):
        shown += ["", title]
        mine = [change for change in report.changes if change.world == world]
        if not mine:
            shown.append("  (沒有升級)")
        for role, label in (("builder", "建築 (含英雄)"), ("lab", "實驗室")):
            rows = [change for change in mine if change.role == role]
            if rows:
                shown.append(f"  {label}")
                shown += [
                    f"    {row.zh or f'#{row.data_id}'}  {_level_change(row)}" for row in rows
                ]
    return "\n".join(shown)


def _local(when: str) -> str:
    return datetime.fromisoformat(when).astimezone().strftime("%Y-%m-%d %H:%M")


def _level_change(change: LevelChange) -> str:
    """`57 → 58` for a single one, `25 個 18 → 19` for some of many, else each level's count."""
    before, after = change.before, change.after
    lost = {
        level: n - after.get(level, 0) for level, n in before.items() if n > after.get(level, 0)
    }
    gained = {
        level: n - before.get(level, 0) for level, n in after.items() if n > before.get(level, 0)
    }

    def counts(levels: dict[int, int]) -> str:
        return ", ".join(f"{level} 級 {n} 個" for level, n in sorted(levels.items())) or "沒有"

    if not before:
        return f"新蓋 {counts(after)}"
    if len(lost) == 1 and len(gained) == 1 and sum(lost.values()) == sum(gained.values()):
        [(old, moved)], [(new, _)] = lost.items(), gained.items()
        return f"{old} → {new}" if sum(after.values()) == 1 else f"{moved} 個 {old} → {new}"
    return f"{counts(before)} → {counts(after)}"


# How long after a timer runs out a confirm still counts as buying its next
# level, and how close before a goblin hire a confirm is taken to have used
# the goblin's slot. Both from the tallies made by hand on 2026-11-07.
TURNAROUND_WINDOW = timedelta(minutes=15)
GOBLIN_GAP = timedelta(minutes=3)
# The first tap a `confirm` run sent, which is the press itself.
TAP_LINE = re.compile(r"^(\d\d:\d\d:\d\d) INFO\s+ai_coc\.adapters\.adb: Tap ")


def _journal() -> list[JournalLine]:
    """Every line of `runs.jsonl` that reads; none when there is no journal yet."""
    lines: list[JournalLine] = []
    with suppress(FileNotFoundError), logging_setup.JOURNAL_PATH.open(encoding="utf-8") as journal:
        for text in journal:
            with suppress(ValueError):
                lines.append(JournalLine.model_validate_json(text))
    return lines


def _last_confirm() -> JournalLine | None:
    """The `end` line of the newest run that ended `confirmed`, which only `confirm` does, any session's; None when there is none."""
    bought = (line for line in _journal() if line.event == "end" and line.outcome == "confirmed")
    return max(bought, key=lambda line: line.at, default=None)


def _press_time(start: JournalLine, pressed: datetime | None) -> datetime | None:
    """When a `confirm` run's first tap landed: what its report recorded, else off its `run.log`; None where neither has it.

    The report has carried it since 2026-11-13; older lines have only the log.
    """
    if pressed is not None:
        return pressed
    if start.log is None:
        return None
    with suppress(OSError), (start.log / "run.log").open(encoding="utf-8") as log:
        for line in log:
            if found := TAP_LINE.match(line):
                clock = datetime.strptime(found[1], "%H:%M:%S").time()
                return datetime.combine(start.at.date(), clock, start.at.tzinfo)
    return None


def _slot(timer: UpgradeTimer) -> TurnaroundGroup:
    # `heroes2` is the builder base's heroes on a journal line written before the merge.
    if timer.section in ("heroes", "heroes2"):
        return "hero"
    if timer.role == "lab":
        return "lab"
    if timer.role == "pet":
        return "pet"
    return "building"


def _journal_upgrades() -> tuple[
    list[tuple[datetime, list[UpgradeTimer]]], list[tuple[datetime, JournalConfirm]]
]:
    """Every `status` that read its timers, and every successful confirm with its press, oldest first."""
    lines = _journal()
    starts = {line.run: line for line in lines if line.event == "start"}
    statuses: list[tuple[datetime, list[UpgradeTimer]]] = []
    confirms: list[tuple[datetime, JournalConfirm]] = []
    for end in (line for line in lines if line.event == "end" and line.run in starts):
        start = starts[end.run]
        with suppress(ValueError):
            if start.command == "status":
                read = JournalTimers.model_validate(end.result)
                if read.export == "exported":
                    statuses.append((end.at, read.timers))
            elif start.command == "confirm" and end.outcome == "confirmed":
                paid = JournalConfirm.model_validate(end.result)
                if (pressed := _press_time(start, paid.pressed)) is not None:
                    confirms.append((pressed, paid))
    return sorted(statuses, key=lambda one: one[0]), sorted(confirms, key=lambda one: one[0])


def turnaround(since: datetime | None = None) -> TurnaroundReport:
    """How long each upgrade waited between its timer running out and the confirm for its next level.

    Off the journal alone: each successful confirm is paired with the latest
    `status` timer that ran out within `TURNAROUND_WINDOW` before its press, and
    a timer goes to the first confirm after it. Goblin hires and walls use up
    their timer but are not counted, and neither is a confirm just before a
    goblin hire, which went on the goblin's slot.
    """
    statuses, confirms = _journal_upgrades()
    since = since.astimezone() if since is not None else None
    goblins = [pressed for pressed, paid in confirms if paid.goblin is not None]
    taken: set[tuple[int, str]] = set()
    samples: list[TurnaroundSample] = []
    for pressed, paid in confirms:
        if paid.wall is not None:
            continue
        timers = next((timers for at, timers in reversed(statuses) if at <= pressed), [])
        due = [
            (datetime.fromisoformat(timer.done_at), timer)
            for timer in timers
            if (timer.data_id, timer.done_at) not in taken
            and timedelta(0)
            <= pressed - datetime.fromisoformat(timer.done_at)
            <= TURNAROUND_WINDOW
        ]
        if not due:
            continue
        done, timer = max(due, key=lambda one: one[0])
        taken.add((timer.data_id, timer.done_at))
        if paid.goblin is not None or any(
            timedelta(0) <= g - pressed <= GOBLIN_GAP for g in goblins
        ):
            continue
        if since is None or pressed >= since:
            samples.append(
                TurnaroundSample(
                    pressed=pressed,
                    group=_slot(timer),
                    world=timer.world,
                    name=timer.zh or timer.en,
                    seconds=(pressed - done).total_seconds(),
                )
            )
    report = TurnaroundReport(samples=samples, outcome="counted" if samples else "none")
    logger.info("%s", TURNAROUND_LINES[report.outcome].format(count=len(samples)))
    return report


# One line per way `ai_coc turnaround` can end, for `run.log`.
TURNAROUND_LINES: dict[TurnaroundOutcome, str] = {
    "counted": "配對到 {count} 次升級",
    "none": "紀錄裡沒有能配對的升級, 哥布林那格跟城牆本來就不算",
}


def turnaround_text(report: TurnaroundReport) -> str:
    """Each slot's count, median and mean, then the latest samples."""
    if report.outcome != "counted":
        return TURNAROUND_LINES[report.outcome]
    labels: dict[TurnaroundGroup, str] = {
        "hero": "王",
        "lab": "實驗室",
        "building": "建築",
        "pet": "戰寵",
    }
    shown = ["從升級完成到按下確認的秒數 (哥布林那格跟城牆不算)"]
    for group, label in labels.items():
        seconds = [sample.seconds for sample in report.samples if sample.group == group]
        if seconds:
            shown.append(
                f"  {label}  {len(seconds)} 次, 中位數 {statistics.median(seconds):.0f} 秒,"
                f" 平均 {statistics.mean(seconds):.0f} 秒"
            )
    shown += ["", "最近 10 次"]
    shown += [
        f"  {sample.pressed.astimezone():%m-%d %H:%M:%S}  {labels[sample.group]}"
        f"  {sample.name or '?'}  {sample.seconds:.0f} 秒"
        for sample in report.samples[-10:]
    ]
    return "\n".join(shown)


def tended(by: str | None = None, since: datetime | None = None) -> TendedReport:
    """What a session did to the village besides farming since `since`, off `runs.jsonl` alone.

    `by` takes the session's own runs and its farm subagent's (`<id>-farm`),
    as `farmed_runs` does. **A goblin hire is one upgrade with the press
    before it**: the `confirm` that pressed the price ends `goblin_offered` on
    the hire prompt (`unexpected` on lines from before that outcome), and the
    `confirm --goblin` after it pays the gems that
    start the upgrade, so a hire within `GOBLIN_GAP` of such a press takes its
    price and its item. **A lost login is counted once** however many runs
    stood down on it, since the farm subagent and the session both run into
    it, and it is back at the end of the next run that claimed the emulator
    and came through.
    """
    since = since.astimezone() if since is not None else None
    lines = _journal()
    starts = {
        line.run: line
        for line in lines
        if line.event == "start"
        and (by is None or line.caller.session in (by, f"{by}-farm"))
        and (since is None or line.at >= since)
    }
    claimed = {line.run for line in lines if line.event == "lock" and line.what == "claimed"}
    report = TendedReport(by=by, since=since)
    pressed: TendedUpgrade | None = None
    for end in (line for line in lines if line.event == "end" and line.run in starts):
        start = starts[end.run]
        # `hold` claims and opens nothing, so it logs nobody back in.
        if _lost_login(report, end, end.run in claimed and start.command != "hold"):
            continue
        if start.command == "boost" and end.outcome == "boosted":
            report.boosts.append(end.at)
        elif start.command == "confirm":
            with suppress(ValueError):
                paid = ConfirmReport.model_validate(end.result)
                pressed = _bought(report, paid, end.at, paid.item or start.caller.mission, pressed)
    logger.info(
        "Tended: %d upgrade(s), %d boost(s), %d lost login(s)",
        len(report.upgrades),
        len(report.boosts),
        len(report.lost),
    )
    return report


def _lost_login(report: TendedReport, end: JournalLine, claimed: bool) -> bool:
    """Whether this run stood down on a lost login, counting it unless one is still open.

    A run that claimed the emulator and came through closes the open one.
    """
    lost = report.lost[-1] if report.lost else None
    if end.outcome == "session_taken":
        if lost is None or lost.back is not None:
            report.lost.append(LostLogin(taken=end.at))
        return True
    if lost is not None and lost.back is None and claimed:
        lost.back = end.at
    return False


def _bought(
    report: TendedReport,
    paid: ConfirmReport,
    at: datetime,
    item: str,
    pressed: TendedUpgrade | None,
) -> TendedUpgrade | None:
    """Add what one `confirm` bought, and hand back the press a goblin hire may follow, if this was one."""
    offer = paid.wall or paid.offer
    if paid.goblin is None or paid.outcome == "goblin_offered":
        bought = TendedUpgrade(
            at=at,
            item=item,
            price=offer.price if offer else None,
            resource=offer.resource if offer else None,
        )
        if paid.outcome == "confirmed":
            report.upgrades.append(bought)
        return bought if paid.outcome in ("unexpected", "goblin_offered") and offer else None
    if paid.outcome != "confirmed":
        return pressed
    if pressed is not None and at - pressed.at <= GOBLIN_GAP:
        report.upgrades.append(pressed.model_copy(update={"gems": paid.goblin.gems}))
    else:
        report.upgrades.append(TendedUpgrade(at=at, item=item, gems=paid.goblin.gems))
    return None


def tended_text(report: TendedReport, now: datetime | None = None) -> str:
    """The upgrade, clock tower and lost login lines of the takeover's stop report."""
    now = now or datetime.now().astimezone()

    def when(at: datetime) -> str:
        return _moment(at.isoformat(), now)

    # No total: the builder base's gold and elixir are other currencies under
    # the same names, and a confirm does not record which village it paid on.
    rows = []
    for one in report.upgrades:
        costs = []
        if one.price is not None and one.resource is not None:
            costs.append(f"{one.price:,} {_RESOURCE_NAMES[one.resource]}")
        if one.gems is not None:
            costs.append(f"哥布林 {one.gems:,} 寶石")
        rows.append(f"  - {when(one.at)} {one.item} ({', '.join(costs)})")
    shown = [f"- 升級: {len(report.upgrades)} 件", *rows]
    boosts = ", ".join(when(at) for at in report.boosts)
    shown.append(f"- 鐘塔: 開了 {len(report.boosts)} 次 ({boosts})" if boosts else "- 鐘塔: 沒開")
    losses = ", ".join(
        f"{when(one.taken)} 被搶, "
        + ("還沒接回" if one.back is None else f"{when(one.back)} 接回")
        for one in report.lost
    )
    shown.append(f"- 被搶登: {len(report.lost)} 次 ({losses})" if losses else "- 被搶登: 沒有")
    return "\n".join(shown)


def capture(frames: FrameLog, count: int = 1, gap: float = 1.5) -> list[Path]:
    """Save frames off the live game, for measuring a screen the parsers cannot read yet.

    A burst rather than a single shot: the screens worth measuring are the ones
    that only exist while something is happening, and those cannot be reached by
    asking for one frame at the right moment.

    It only looks, so it takes no claim and starts nothing (`_attached`): a game
    that is down is `ai_coc launch`'s to bring up.
    """
    adb = _attached(frames)
    display = adb.display_for(COC_PACKAGE)
    saved: list[Path] = []
    for index in range(count):
        saved.append(frames.write(f"frame_{index:03d}", adb.screenshot(display, None)))
        if index + 1 < count:
            time.sleep(gap)
    return saved


def gamedata(frames: FrameLog, refresh: bool = False) -> GameDataReport:
    """The game's own tables for the installed version, read off the emulator once per version.

    It reads files over ADB and never the screen, so it takes no claim and
    starts nothing (`_attached`), like `capture`.
    """
    return game_data(lambda: _attached(frames), refresh)


def search(asked: SearchOptions) -> SearchReport:
    """Look things up in the game's tables and the newest export: by name or data_id, by section, or both.

    Reads files alone, the tables kept on disk and the saved exports, so it
    touches neither the game nor the emulator. A query searches the tables, so
    it finds what the village lacks too; without one it lists what the export
    holds, which needs no tables. Inside one section a query can still be
    answered off the export alone.
    """
    tables = _kept_tables()
    saved = _saved_exports(tables)
    export = saved[-1] if saved else None
    if not asked.query and export is None:
        outcome: SearchOutcome = "no_export"
    elif asked.query and tables is None and (asked.section is None or export is None):
        outcome = "no_tables"
    else:
        outcome = "found"
    hits = village_search(tables, export, asked) if outcome == "found" else []
    if outcome == "found" and not hits:
        outcome = "no_match"
        # Near names answer a name nothing holds, not one the filters emptied.
        if not village_search(tables, export, SearchOptions(query=asked.query)):
            hits = closest_names(tables, export, asked)
            outcome = "close" if hits else "no_match"
    report = SearchReport(
        query=asked.query,
        section=asked.section,
        world=asked.world,
        skip_max_lvl=asked.skip_max_lvl,
        outcome=outcome,
        game_version=tables.game_version if tables else None,
        tag=export.tag if export else None,
        exported_at=export.exported_at if export else None,
        hits=hits,
    )
    logger.info("%s", _search_head(report))
    return report


SEARCH_LINES: dict[SearchOutcome, str] = {
    "found": "找到 {count} 種",
    "close": "沒有名字含這一段的, 下面 {count} 種是名字最接近的, 不是符合的",
    "no_match": "沒有符合的",
    "no_tables": "還沒有存下遊戲的表可以查, 先跑一次 ai_coc gamedata",
    "no_export": "還沒有存下村莊匯出可以列, 跑一次 ai_coc status 或 export 就有",
}
# The tables' prices are the game's list prices; the gold pass and events
# discount upgrades, and the build panel says what one costs today.
LIST_PRICE = "價格跟時間是遊戲表上的牌價, 活動跟金券會打折, 實際要多少照建造面板"
WORLD_NAMES: dict[World, str] = {"day": "日世界", "night": "夜世界"}
# The sections as `search` heads them, in the order it lists them.
SECTION_NAMES: dict[SearchSection, str] = {
    "builder": "工人",
    "lab": "實驗室",
    "pets": "戰寵",
    "equipment": "英雄裝備",
    "helpers": "助手",
}
# Each export section as a hit names its kind, in the words `--section`'s help uses.
KIND_NAMES: dict[str, str] = {
    "heroes": "英雄",
    "buildings": "建築",
    "traps": "陷阱",
    "guardians": "守護者",
    "units": "兵種",
    "spells": "法術",
    "siege_machines": "攻城機器",
    "pets": "戰寵",
    "equipment": "英雄裝備",
    "helpers": "助手",
}


def _search_head(report: SearchReport) -> str:
    asked = " ".join(
        part
        for part in (
            f"「{report.query}」" if report.query else "",
            f"--section {report.section}" if report.section else "",
            f"--{report.world}" if report.world else "",
            "--skip-max-lvl" if report.skip_max_lvl else "",
        )
        if part
    )
    return f"找 {asked}: {SEARCH_LINES[report.outcome].format(count=len(report.hits))}"


def search_text(report: SearchReport) -> str:
    """The report as a person reads it: where it looked, each village's hits by section, and that the prices are list prices.

    Both villages get a block unless one was asked for, and one with nothing
    in it says so.
    """
    shown = [_search_head(report)]
    if report.outcome not in ("found", "close"):
        return shown[0]
    shown.append(
        f"遊戲的表 {report.game_version or '沒有'}, "
        + (
            f"村莊匯出 {report.tag} {_local(report.exported_at)}"
            if report.exported_at
            else "沒有存下村莊匯出, 只查遊戲的表"
        )
    )
    exported = report.exported_at is not None
    worlds: tuple[World, ...] = (report.world,) if report.world else ("day", "night")
    for world in worlds:
        shown += ["", f"== {WORLD_NAMES[world]} =="]
        here = [hit for hit in report.hits if hit.world == world]
        if not here:
            shown += ["", "沒有符合的"]
        # The known sections in their own order, then any the export added.
        for section in dict.fromkeys([*SECTION_NAMES, *(hit.section for hit in here)]):
            listed = [hit for hit in here if hit.section == section]
            if not listed:
                continue
            name = SECTION_NAMES.get(section)
            shown += ["", f"-- {name} ({section}) --" if name else f"-- {section} --"]
            for hit in listed:
                shown += ["", *_hit_lines(hit, exported)]
    return "\n".join([*shown, "", LIST_PRICE])


def _hit_lines(hit: SearchHit, exported: bool) -> list[str]:
    """One hit's lines; with no export read, nothing is said about whether the village has it."""
    names = " / ".join(name for name in (hit.zh, hit.en) if name) or "(遊戲的表沒有名字)"
    top = "—" if hit.max_lvl is None else hit.max_lvl
    lines = [f"{names}  #{hit.data_id}  {KIND_NAMES.get(hit.kind, hit.kind)}  滿級 {top}"]
    if not hit.count:
        return [*lines, f"  村莊裡沒有{_unlock_text(hit)}"] if exported else lines
    if hit.rows is not None:
        return lines + [f"  {_row_text(row)}" for row in hit.rows]
    held = ", ".join(f"{lvl} 級 {n} 個" for lvl, n in hit.owned.items()) or "沒有等級"
    lines.append(f"  有 {hit.count} 個: {held}")
    lines += [
        f"  {_buying(one.lvl)}, {_when(one.done_at)} 好{' (哥布林那格)' if one.extra else ''}"
        for one in hit.upgrading
    ]
    if hit.ready_at:
        lines.append(f"  {_local(hit.ready_at)} 能再派")
    return lines + [
        f"  下一次 {_upgrade_text(one, hit.limited_lvl)}{_open_to(one)}" for one in hit.next
    ]


def _buying(lvl: int | None) -> str:
    """An upgrading copy by the level it is buying, as `status` and the build panel name it."""
    return "在升級" if lvl is None else f"在升到 {lvl + 1} 級"


def _open_to(upgrade: SearchNext) -> str:
    """How many copies can start a next upgrade now, and how many once their running one is done."""
    parts = [f"現在能升 {upgrade.ready} 個"] if upgrade.ready else []
    if upgrade.waiting:
        parts.append(f"{upgrade.waiting} 個要等手上的升級做完")
    return "".join(f", {part}" for part in parts)


def _unlock_text(hit: SearchHit) -> str:
    """What a missing hit's first level needs and where the village stands on each; nothing when the tables say nothing."""
    needs = ", ".join(
        f"{need.zh or need.hall} {need.lvl} 級"
        f" (村莊裡{f' {need.village} 級' if need.village else '還沒有'})"
        for need in hit.unlock
    )
    return f", 解鎖要 {needs}" if needs else ""


def _row_text(row: ExportRow) -> str:
    parts = [f"{_level(row.lvl)} 級 {row.cnt or 1} 個"]
    if row.timer is not None:
        parts.append(
            f"{_buying(row.lvl)}, {_when(row.done_at)} 好{' (哥布林那格)' if row.extra else ''}"
        )
    if row.ready_at:
        parts.append(f"{_local(row.ready_at)} 能再派")
    if row.next is not None:
        parts.append(f"下一次 {_upgrade_text(row.next, row.limited_lvl)}")
    return "  ".join(parts)


def _upgrade_text(upgrade: NextUpgrade, limited: int | None) -> str:
    """A next upgrade's level, list price and time, and the hall's cap where it is below that level."""
    cost = ", ".join(
        f"{amount:,} {_RESOURCE_NAMES.get(name, name)}" for name, amount in upgrade.cost.items()
    )
    took = "" if upgrade.time is None else f", {spell_out(upgrade.time)}"
    blocked = f" (上限 {limited} 級)" if limited is not None and upgrade.lvl > limited else ""
    return f"{upgrade.lvl} 級 {cost or '—'}{took}{blocked}"


def _level(lvl: int | None) -> str:
    return "—" if lvl is None else str(lvl)


def _when(moment: str | None) -> str:
    return "—" if moment is None else _local(moment)


def answer_text(answer: BaseModel, within: int | None = None) -> str | None:
    """The text a command prints for the skill reading it, or None for one printed as its JSON.

    The one place a report becomes text (`cli.TEXT_ANSWERS`); each report's own
    wording sits beside its command. `within` is `status --within`.
    """
    match answer:
        case StatusReport():
            text = status_text(answer, within=within)
        case AttackRun():
            text = attack_text(answer)
        case FarmedReport():
            text = farmed_report_text(answer)
        case TendedReport():
            text = tended_text(answer)
        case GrowthReport():
            text = growth_text(answer)
        case TurnaroundReport():
            text = turnaround_text(answer)
        case SearchReport():
            text = search_text(answer)
        case NoteReport():
            text = note_text(answer)
        case _:
            text = None
    return text


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
    the game. The HUD's + buttons and its shop open the gem store. And a press
    in a battle deploys whatever card is selected.

    **A running row on the build panel is not refused, what it opens is**: a
    hero's, a pet's or a research's 正在將X升至N級 is a sheet, and a building's
    menu carries 立即完成 on the row its 取消 sits in. The agent opens a hero's ahead of
    time, because that sheet turns into the next level's as its countdown ends.
    """
    if in_battle(png):
        return "in_battle"
    # The sheet before the dialog: the builder base's sheet is pale enough to
    # pass for one, and its X is the only way out of it. The lab's 升級部隊
    # window keeps a gem 立即完成 and a research potion beside its X.
    world = sheet_open(png) or ("night" if research_running(png) else None)
    if world is not None:
        return None if _inside(point, SHEET_CLOSE_BOX[world]) else "sheet_up"
    # The goblin builder's prompt hires it for gems; `confirm --goblin` pays it.
    if goblin_prompt(png):
        return None if _inside(point, GOBLIN_X_BOX) else "sheet_up"
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
    frames: FrameLog,
    press: Callable[[AdbController, DisplayTarget], None],
    wait: float,
) -> PressReport:
    """Refuse or press, and keep the frame that says which.

    `_attached` rather than `_controller`: nothing is relaunched or pressed on
    the way in, so a panel the agent opened is still open when it gets here.
    A swipe is refused on a sheet even from its X, since only a tap closes one.
    """
    adb = _attached(frames)
    display = adb.display_for(COC_PACKAGE)
    before = adb.screenshot(display, "before-press")
    refusal = _refused(before, points[0])
    if (
        refusal is None
        and gesture == "swipe"
        and (sheet_open(before) or research_running(before) or goblin_prompt(before))
    ):
        refusal = "sheet_up"
    if refusal is not None:
        frame = frames.write("refused", before)
        report = PressReport(gesture=gesture, points=points, outcome=refusal, frame=frame)
    else:
        press(adb, display)
        time.sleep(wait)
        frame = frames.write("after", adb.screenshot(display, None))
        report = PressReport(gesture=gesture, points=points, outcome="pressed", frame=frame)
    logger.info("Press: %s", press_line(report))
    return report


def tap(point: tuple[int, int], frames: FrameLog, wait: float = PRESS_SETTLE) -> PressReport:
    """Tap one point, unless the screen makes that a purchase, and show what followed."""
    return _pressed("tap", [point], frames, lambda adb, display: adb.tap(*point, display), wait)


def swipe(
    start: tuple[int, int],
    end: tuple[int, int],
    frames: FrameLog,
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
        frames,
        lambda adb, display: adb.swipe(start, end, duration, display),
        wait,
    )


# `search` prints the tables' gems too; the ores keep the tables' own key, since
# nothing here has read the game's Chinese name for them.
_RESOURCE_NAMES = {"gold": "金幣", "elixir": "聖水", "dark": "黑水", "gems": "寶石"}

CONFIRM_LINES: dict[ConfirmOutcome, str] = {
    "confirmed": "按了確認,視窗關了或換成正在升級的畫面",
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
    "no_goblin": "畫面上沒有聘請哥布林工人的視窗,或讀不出它要幾顆寶石,什麼都沒按",
    "goblin_offered": "按了確認,跳出聘請哥布林工人的視窗,沒有再按,視窗還留著",
}


def confirm_line(report: ConfirmReport) -> str:
    """What the sheet asked, what was expected, and what came of it."""
    gems = "讀不到" if report.goblin is None else f"{report.goblin.gems:,} 寶石"
    said = CONFIRM_LINES[report.outcome]
    if report.outcome == "goblin_offered":
        said = f"{said},聘請鈕 {gems}"
    elif report.goblin is not None or report.outcome == "no_goblin":
        return f"哥布林工人 {gems},預期 {report.asked:,}:{said}"
    read = (
        "讀不到"
        if report.offer is None
        else f"{report.offer.price:,} {_RESOURCE_NAMES[report.offer.resource]}"
    )
    if report.wall is not None:
        wall = f"{report.wall.price:,} {_RESOURCE_NAMES[report.wall.resource]}"
        return f"城牆升級鈕 {wall},確認鈕 {read},預期 {report.asked:,}:{said}"
    return f"確認鈕 {read},預期 {report.asked:,}:{said}"


def _after_confirm(adb: AdbController, display: DisplayTarget) -> tuple[ConfirmOutcome, bytes]:
    """What the press on 確認 came to, judged over several looks.

    A dialog at any look gets its 取消 and ends it. Only `CONFIRM_SETTLED`
    looks in a row showing the panel or the village back, with no sheet over
    them, count as done, and so do the two screens a started research leaves
    in the sheet's place: the builder base lab's 升級部隊 window, and on the
    home village the sheet itself turned into the running one, 正在將X升至N級
    (`sheet_running`). Anything else is left exactly as it stands.
    """
    settled = 0
    outcome: ConfirmOutcome = "unexpected"
    after = b""
    for look in range(CONFIRM_LOOKS):
        time.sleep(BUY_SETTLE if look == 0 else CONFIRM_GAP)
        after = adb.screenshot(display, "after-confirm")
        # The sheet first, for the reason `_refused` gives: read as a dialog,
        # a builder base sheet still standing would have its "取消" pressed,
        # and that is wherever the dialog reader thought it saw orange.
        running = sheet_running(after)
        if sheet_open(after) and not running:
            settled, outcome = 0, "sheet_stayed"
            continue
        if not running and (dialog := game_dialog(after)) is not None:
            adb.tap(*dialog.cancel, display)
            time.sleep(BACK_SETTLE)
            return "refused_by_game", adb.screenshot(display, "cancelled")
        if (
            running
            or build_panel_open(after)
            or current_world(after) is not None
            or research_running(after)
        ):
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
        after = adb.screenshot(display, "raised")
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
) -> tuple[ConfirmOutcome, SheetOffer | None, SheetOffer | None, bytes | None]:
    """Read the wall's price button for `pay`, press it if it agrees, and pay what it raised.

    A sheet or a dialog already standing means no wall menu, whatever is drawn
    under it: the dialog `_raised` presses has to be one this raised. The frame
    it ends on is None when it pressed nothing.
    """
    menu = None if sheet_open(before) or game_dialog(before) else panel_wall_menu(before)
    wall = None if menu is None else menu.gold if pay == "gold" else menu.elixir
    if wall is None:
        return "no_wall_menu", None, None, None
    if wall.price != price:
        return "price_mismatch", None, wall, None
    if not wall.affordable:
        return "cannot_afford", None, wall, None
    adb.tap(*wall.point, display)
    outcome, offer, after = _raised(adb, display, price, pay)
    return outcome, offer, wall, after


def confirm(
    price: int,
    frames: FrameLog,
    pay: Literal["gold", "elixir"] | None = None,
    goblin: bool = False,
    item: str | None = None,
) -> ConfirmReport:
    """Press the upgrade sheet's 確認 if it asks for exactly this many of a resource.

    **The one command here that spends**, and so the one that reads before it
    presses: the button must carry a gold, elixir or dark icon (立即完成 on a
    running upgrade carries a gem in the same place) and the price written on
    it must be the one the agent read off the panel row, which is what keeps a
    tap on the wrong row from buying the wrong thing. It presses once. Whatever
    the game raises after that — not enough of the resource, every builder
    busy, each of which it answers with a gem offer — gets 取消 or is left
    alone, never a second press. Of those, the prompt to hire an event's
    goblin builder is named (`goblin_offered`) with the gems it asks, for the
    agent to pay with `goblin` or close.

    **With `pay`, it starts a step earlier, on a wall's menu beside the build
    panel**: that resource's price button is pressed by this rather than by a
    `tap`, only when it asks exactly `price` and in white, and then whatever it
    raised is paid (`_raised`). The same reading-before-pressing holds for the
    button as for the sheet.

    **With `goblin`, it pays gems**, and only on the prompt to hire an event's
    goblin builder, which a press on 確認 raises when every other builder is
    busy: its button is pressed when it asks exactly `price` gems. Where it
    draws two, that is the left one, 哥布林建築工人; the right one, 空出一個建築工人,
    finishes a running job for gems and is never read or pressed. How many gems
    the agent may pass is the user's call, kept in the `upgrades` skill.
    """
    adb = _attached(frames)
    display = adb.display_for(COC_PACKAGE)
    before = adb.screenshot(display, "before-confirm")
    # The first tap follows this frame by no more than the reading of it.
    looked = datetime.now().astimezone().replace(microsecond=0)
    after: bytes | None = None
    wall: SheetOffer | None = None
    hire: GoblinOffer | None = None
    offer: SheetOffer | None = None
    if goblin:
        hire = goblin_offer(before)
        if hire is None:
            outcome = "no_goblin"
        elif hire.gems != price:
            outcome = "price_mismatch"
        else:
            adb.tap(*hire.point, display)
            outcome, after = _after_confirm(adb, display)
    elif pay is not None:
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
            if outcome == "unexpected" and goblin_prompt(after):
                outcome, hire = "goblin_offered", goblin_offer(after)
    # Named the way `tap` names its frame: `after` only when something was pressed.
    frame = frames.write("refused", before) if after is None else frames.write("after", after)
    report = ConfirmReport(
        asked=price,
        offer=offer,
        wall=wall,
        goblin=hire,
        outcome=outcome,
        frame=frame,
        item=item,
        pressed=None if after is None else looked,
    )
    logger.info("Confirm: %s", confirm_line(report))
    return report


BOOST_LINES: dict[BoostOutcome, str] = {
    "boosted": "按了鐘塔的免費加速跟它的確認,鐘塔開始加速",
    "not_night": "遊戲不在夜世界,鐘塔在夜世界,什麼都沒按",
    "not_ready": "畫面上沒有鐘塔能開的時鐘標記(冷卻中,或鐘塔被擋住、選著別的東西),什麼都沒按",
    "no_free_boost": "點了鐘塔,選單上找不到沒標價的免費加速鈕,沒有按;鐘塔還選著",
    "not_taken": "按了但加速沒有生效(確認視窗或免費加速鈕還在),沒有再按",
    "unexpected": "按了免費加速,出現的不是它的確認視窗,沒有再按",
}


def boost_line(report: BoostReport) -> str:
    """Where the marker and the button were, and what came of the press."""
    return f"鐘塔標記 {report.marker},加速鈕 {report.button}:{BOOST_LINES[report.outcome]}"


def boost(frames: FrameLog) -> BoostReport:
    """Start the builder base clock tower's free boost, when its ready marker says it is free.

    **Nothing is pressed without the marker**, which the game floats over the
    tower only while the free boost is ready: off it the same button asks for
    gems. The tower is selected by a tap below the marker, and the button
    pressed is the one carrying the tower's clock with no price on it, once.
    That raises the boost's own sheet, whose 加速 is pressed only because this
    press raised it (`boost_sheet`), as `confirm` answers a dialog it raised; the
    first live run reported a boost on the press alone, with the sheet still
    standing. The tower is left selected either way, for the agent to clear as
    it clears any building it selected.
    """
    adb = _attached(frames)
    display = adb.display_for(COC_PACKAGE)
    before = adb.screenshot(display, "before-boost")
    after: bytes | None = None
    marker = button = None
    outcome: BoostOutcome
    if current_world(before) != "night":
        outcome = "not_night"
    elif (marker := clock_tower_marker(before)) is None:
        outcome = "not_ready"
    else:
        adb.tap(marker[0], marker[1] + CLOCK_TOWER_BELOW, display)
        time.sleep(PRESS_SETTLE)
        after = adb.screenshot(display, "tower-menu")
        if (button := free_boost(after)) is None:
            outcome = "no_free_boost"
        else:
            adb.tap(*button, display)
            time.sleep(PRESS_SETTLE)
            after = adb.screenshot(display, "boost-sheet")
            outcome = "not_taken" if free_boost(after) is not None else "unexpected"
            if (start := boost_sheet(after)) is not None:
                adb.tap(*start, display)
                time.sleep(PRESS_SETTLE)
                after = adb.screenshot(display, "boosted")
                standing = (
                    boost_sheet(after) is not None
                    or sheet_open(after) is not None
                    or game_dialog(after) is not None
                    or free_boost(after) is not None
                )
                outcome = "not_taken" if standing else "boosted"
    # Named the way `tap` names its frame: `after` only when something was pressed.
    frame = frames.write("refused", before) if after is None else frames.write("after", after)
    report = BoostReport(marker=marker, button=button, outcome=outcome, frame=frame)
    logger.info("Boost: %s", boost_line(report))
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
