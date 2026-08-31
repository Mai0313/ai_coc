"""The headless side of the application: what `cli.py` runs when given a command.

Nothing here builds a window. Everything below the `ui/` package is already
Qt-free, so the only thing the window was ever providing was the wiring — find
the emulator, resolve the display it put the game on, hand over the API key —
and that is what these functions are. A feature that can only be reached through
a widget cannot be run against the live game while it is being worked on, which
is the whole reason the attack loop grew a `frame_dir` at the same time.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Self
import logging
from pathlib import Path
import threading

from pydantic import BaseModel, PrivateAttr

from ai_coc import plans
from ai_coc.models import (
    World,
    MapEdge,
    HeroKind,
    ProbeRay,
    AppConfig,
    MapSurvey,
    NightPlan,
    AttackPlan,
    HeroReport,
    PlayedPlan,
    ViewReport,
    WallReport,
    BuildReport,
    WallOptions,
    WorldReport,
    AttackReport,
    AttackSeries,
    DonateReport,
    FrameReading,
    LaunchReport,
    OnlineReport,
    RestartScope,
    AttackOptions,
    BuilderReport,
    CollectReport,
    DisplayTarget,
    BoundarySurvey,
    GeminiSettings,
    LootThresholds,
    StorageCapacity,
)
from ai_coc.constants import NUDGE_MS, NUDGE_TO, NUDGE_ROW, STOP_FLAG, NUDGE_FROM, COC_PACKAGE
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
    card_count,
    live_cards,
    read_scout,
    read_stock,
    card_groups,
    field_units,
    card_drained,
    freeze_cards,
    skip_offered,
    army_strength,
    counted_cards,
    attack_menu_open,
    storage_capacity,
    idle_disconnected,
)
from ai_coc.parsers.world import current_world
from ai_coc.adapters.config import ConfigStore
from ai_coc.adapters.secrets import SecretStore
from ai_coc.parsers.boundary import PLAYFIELD, VILLAGE_CENTRE, boundary_reach
from ai_coc.parsers.building import wall_menu, upgrade_buttons

from .ui.clan import ClanRunner
from .ui.hero import HeroRunner
from .ui.walls import WallRunner
from .ui.world import cross, collect_cart
from .ui.attack import CARD_ROW_Y, DROP_SETTLE, SINGLE_DROP_DELAY, AttackRunner
from .ui.upkeep import UpkeepRunner

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _controller() -> AdbController:
    """The first MuMu instance, with Clash of Clans already up on it."""
    mumu = MuMuAdapter()
    instances = mumu.enumerate_instances()
    if not instances:
        raise RuntimeError("找不到任何 MuMu instance")
    return mumu.controller(mumu.ensure_coc(instances[0].index).adb_serial)


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
        current = next((item for item in mumu.enumerate_instances() if item.index == index), None)
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
    # than in each of them. Zooming out is also what centres the village, since
    # the map clamps the camera at its own edges.
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


def _planner(config: AppConfig) -> GeminiClient | None:
    """The saved key, or None so the attack falls back to its fixed flank."""
    try:
        key = SecretStore().load()
    except Exception:
        logger.warning("No saved API key could be read", exc_info=True)
        return None
    if not key:
        logger.info("No API key is saved; the attack will use the fixed flank and spell grid")
        return None
    return GeminiClient(
        settings=GeminiSettings(
            api_key=key,
            model=config.gemini_model,
            base_url=config.gemini_endpoint,
            thinking_level=config.gemini_thinking,
        )
    )


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
# How long the camera takes to settle after a pinch.
PINCH_SETTLE = 1.5
# How often the barracks wait looks up to see whether it has been stood down.
# Sleeping through the whole minute in one go would leave a stop unnoticed for
# most of it, which reads as a stop that did nothing.
STOP_POLL = 2.0


def stop() -> str:
    """Ask whichever long loop is running to stand down, without waiting for it.

    Nothing here touches the game or looks for a process: this writes the flag
    and ends. What actually stops is the loop, when it next looks, and where
    that is belongs to each of them — `attack` between rounds and between
    opponents, `walls` between batches and during the opening scan. Never
    mid-battle or mid-batch, because either one abandoned halfway leaves the
    game on a screen the next run does not know how to get home from.
    """
    STOP_FLAG.write_text("", encoding="utf-8")
    return f"已要求停止,旗標寫在 {STOP_FLAG}。正在跑的迴圈會做完手上這一件事才收工。"


def stop_requested() -> bool:
    """Whether somebody has asked the loop that is running now to stand down."""
    return STOP_FLAG.exists()


def _clear_stop() -> None:
    """Take the flag, and call this at both ends of every loop that reads it.

    At the start because a process killed outright never reaches the other end,
    and a flag left behind that way would stand the next run down before it had
    done anything — reported as a stop nobody asked for. At the end because a
    request that has been served should stop looking like one still waiting: a
    flag that is still there means somebody asked and no loop has taken it yet,
    which is what makes the file worth looking at to tell whether a stop landed.
    """
    STOP_FLAG.unlink(missing_ok=True)


def _rest(seconds: float) -> bool:
    """Wait out the barracks, answering whether the wait was cut short."""
    try:
        for _ in range(int(seconds / STOP_POLL)):
            if stop_requested():
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


# How many pinches to spend putting the camera back. `view`'s docstring measured
# one gesture as covering the whole range and a second as changing nothing, so
# two is that plus a spare — the cost is a second and the alternative is every
# battle of the rest of the run landing nothing.
RESTART_ZOOM_PINCHES = 2


def _settle_game(
    adb: AdbController, polls: int, should_stop: Callable[[], bool] = lambda: False
) -> DisplayTarget | None:
    """Wait for a village that can be tapped, then put the camera where the coordinates are.

    Two steps that always belong together, because every coordinate in this
    project was measured against one particular view: a village on screen, at
    the game's far zoom limit. A caller that has one without the other has a
    game that answers and misses everything it aims at.

    **Zooming out is also how the village gets centred.** The map clamps the
    camera at its own edges, so at the far limit the village diamond fills the
    frame on its own — measured, its middle lands within about 20 px of the
    screen's. There is nothing else to do, and nothing here has to find the
    village to do it, which is what makes this safe on a frame nobody has read.

    None means the village never appeared. Whether that is worth giving up over
    is the caller's decision, not this one's.
    """
    waiting = "nothing was tried"
    for _ in range(polls):
        # Checked inside the wait rather than only around it: this is the
        # longest stretch of a run where nothing else looks at the flag, and a
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
            if current_world(adb.screenshot(display)) is not None:
                adb.zoom("out", RESTART_ZOOM_PINCHES, COC_PACKAGE, display)
                return display
            waiting = "the village has not painted yet"
        logger.debug("Still waiting for the game: %s", waiting)
        time.sleep(RESTART_POLL_GAP)
    logger.warning(
        "Gave up after %.0fs waiting for the game: %s", polls * RESTART_POLL_GAP, waiting
    )
    return None


def _restart_emulator(runner: AttackRunner, ticker: FrameTicker) -> bool:
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
    display = _settle_game(adb, RESTART_POLLS, stop_requested)
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


def _restarted(runner: AttackRunner, ticker: FrameTicker, fought: int, every: int) -> int | None:
    """How many battles to carry forward, or None when the emulator never returned.

    `fought` unchanged where no restart was due, and zero after one. The
    decision, the restart and the reset are one thought, and keeping them in one
    place is also what keeps `attack` under the complexity this repo lints for.
    """
    if not every or fought < every:
        return fought
    logger.info("%d battle(s) fought; restarting the emulator", fought)
    if _restart_emulator(runner, ticker):
        return 0
    # The series ends either way, but only one of these is an alarm: a stop
    # asked for mid-restart comes back the same False as an emulator that never
    # returned, and an error line that cries wolf on an ordinary `ai_coc stop`
    # is worth less than no error line at all.
    if not stop_requested():
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

    **An unreadable frame is only fatal when a village was named.** Without one
    this falls through to the home village and lets the runner sort it out:
    `_open_attack_menu` waits, restarts the game, leaves a result screen and
    sails home, and every one of those is a state `current_world` answers None
    for. Bailing on them ended a whole series before round one.
    """
    if wanted is None:
        return current_world(adb.screenshot(display)) or "day"
    landed = cross(adb, display, wanted)
    if landed == wanted:
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
    if world == "night" and battles and battles % CART_EVERY == 0:
        collect_cart(adb, display)


def attack(options: AttackOptions) -> AttackSeries:
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
    the rounds already played are still reported. The flag is the one that
    reaches a run put in the background, which nothing can send a Ctrl-C to.
    """
    _clear_stop()
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
    display = _settle_game(adb, WORLD_SETTLE_POLLS) or adb.display_for(COC_PACKAGE)
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
        stop_at=config.stop_at,
        ai=None if plan else _planner(config),
        plan=plan,
        should_stop=stop_requested,
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
            if stop_requested():
                logger.info(
                    "Stop requested; ending the series after %d round(s)", len(series.root)
                )
                break
            # And the cheapest place to restart, for the same reasons. After the
            # stop check rather than before it, because a run being stood down
            # has no use for a fresh emulator.
            carried = _restarted(runner, ticker, fought, restart_every)
            if carried is None:
                # Everything after this would be aimed at an emulator that never
                # came back, so the series ends here holding the rounds it really
                # played rather than raising and taking them with it. Why it
                # ended is logged where the two reasons can still be told apart.
                break
            fought = carried
            logger.info("Round %d of %s", len(series.root) + 1, rounds or "no limit")
            try:
                report = runner.run()
            except KeyboardInterrupt:
                logger.info("Interrupted; stopping after %d round(s)", len(series.root))
                break
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
            if (
                not fighting
                and not stop_requested()
                and (rounds <= 0 or len(series.root) < rounds)
            ):
                logger.info("Nothing was attacked; waiting %ds for the army", IDLE_REST)
                if _rest(IDLE_REST):
                    break
    _clear_stop()
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
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    runner = _BoundarySurvey(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        thresholds=LootThresholds(),
        frame_dir=frame_dir,
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

    `VILLAGE_GRID` was calibrated by overlaying candidates on live frames until
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
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    runner = _MapSurvey(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        thresholds=LootThresholds(),
        frame_dir=frame_dir,
    )
    runner.run()
    logger.info("Map survey: %s", runner.survey.summary)
    return runner.survey


def walls(options: WallOptions) -> WallReport:
    """Spend the storages on wall upgrades, with no window in the way.

    Walls upgrade the instant they are paid for and tie up no builder, so this is
    what a village does with loot it has nowhere else to put — every builder busy
    and both storages filling towards the point where the attack loop stands
    itself down.

    `ai_coc stop` ends this one too, between batches. A run with `--rounds 0`
    against a village full of walls is the other loop here that goes on long
    enough to be worth interrupting, and it answers the same flag rather than a
    second mechanism of its own.
    """
    _clear_stop()
    adb = _controller()
    if options.frame_dir is not None:
        options.frame_dir.mkdir(parents=True, exist_ok=True)
    runner = WallRunner(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        keep_gold=options.keep_gold,
        keep_elixir=options.keep_elixir,
        rounds=options.rounds,
        at=options.at,
        should_stop=stop_requested,
        frame_dir=options.frame_dir,
    )
    report = runner.run()
    # Said here rather than inside the loop: what the runner knows is how many
    # batches it bought, and "已停止" is a fact about this call rather than about
    # the walls. A run stopped before it bought anything is the exception — its
    # own fallback message is a verdict on positions it never tried, and that
    # sentence has been read as "the walls are finished" and taken for it.
    if stop_requested():
        report.message = (
            f"已停止，{report.message}" if report.upgrades else "已停止，還沒買成任何一批"
        )
    _clear_stop()
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
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    report = UpkeepRunner(
        adb=adb, display=adb.display_for(COC_PACKAGE), frame_dir=frame_dir
    ).builders()
    logger.info("Builders: %s", report.message)
    return report


def collect(frame_dir: Path | None = None) -> CollectReport:
    """Tap every collector the village has left standing, with no window in the way.

    Collectors stop once they are full, so a village nobody has emptied has spent
    most of its time doing nothing. This is the cheapest thing in the project to
    run and the one worth running most often.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    display = adb.display_for(COC_PACKAGE)
    # **The builder base has no collectors to sweep and one cart instead.** Its
    # elixir is paid into that cart rather than into the storages, so this is
    # the same job on that village even though it shares none of the machinery:
    # one tap at a known spot rather than a colour-and-size search over the map.
    if current_world(adb.screenshot(display)) == "night":
        gained = collect_cart(adb, display)
        return CollectReport(
            markers=1 if gained else 0,
            elixir=gained,
            message=f"建築大師基地的推車收到聖水 {gained}" if gained else "推車裡沒有東西可以收",
        )
    report = UpkeepRunner(adb=adb, display=display, frame_dir=frame_dir).collect()
    logger.info("Collect: %s", report.message)
    return report


def upgrade(
    frame_dir: Path | None = None, keep_gold: int = 0, keep_elixir: int = 0
) -> BuildReport:
    """Put the village's idle builders to work, with no window in the way.

    A builder standing around is the one thing a village cannot buy its way out
    of, so this is worth running whenever an upgrade finishes. Walls are left to
    `walls`, which needs no builder at all.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    report = UpkeepRunner(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        frame_dir=frame_dir,
        keep_gold=keep_gold,
        keep_elixir=keep_elixir,
    ).upgrade()
    logger.info("Upgrade: %s", report.message)
    return report


def hero(
    frame_dir: Path | None = None, which: HeroKind | None = None, at: tuple[int, int] | None = None
) -> HeroReport:
    """Read the 英雄殿堂, and raise the hero named, with no window in the way.

    Reading is the default and spends nothing: what each hero costs next is the
    number the decision rests on, and a hall nobody has looked at is the most
    expensive kind of idle builder — a hero level runs for the better part of a
    day, so one not started this evening is one not finished tomorrow.

    `which` is what turns it into a purchase, and only ever for the hero named:
    which hero is worth raising is a judgement about how the village plays, not
    something a price can settle.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    report = HeroRunner(
        adb=adb, display=adb.display_for(COC_PACKAGE), frame_dir=frame_dir, hero=which, at=at
    ).run()
    logger.info("Hero: %s", report.message)
    return report


def donate(frame_dir: Path | None = None, dry_run: bool = False, rounds: int = 0) -> DonateReport:
    """Give troops to whoever in the clan is asking, with no window in the way.

    Cheap to run and cheap to find nothing: a clan with no request open costs one
    tap on the chat tab and one capture. `dry_run` walks the whole path and stops
    before the tap that gives something away, because the panel does not confirm.
    """
    adb = _controller()
    if frame_dir is not None:
        frame_dir.mkdir(parents=True, exist_ok=True)
    report = ClanRunner(
        adb=adb,
        display=adb.display_for(COC_PACKAGE),
        frame_dir=frame_dir,
        dry_run=dry_run,
        rounds=rounds,
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
    """Zoom the village camera, with no window in the way.

    **The game has no zoom control to tap and reports no zoom level**, so this
    writes the two-finger gesture straight to the touch device — see
    `AdbController.pinch` for why `input` cannot. Zooming out past the far limit
    does nothing at all, which is what makes `--zoom out` safe to run blind: it
    is how a session that zoomed in to look at something gets back to the view
    every coordinate in this project was measured against.

    Measured live, one pinch covers the whole range: from fully zoomed in, a
    single gesture came back to the far limit and a second changed nothing.
    """
    adb = _controller()
    adb.zoom(zoom, times, COC_PACKAGE, adb.display_for(COC_PACKAGE))
    report = ViewReport(message=f"鏡頭{'拉遠' if zoom == 'out' else '拉近'}了 {times} 次")
    logger.info("View: %s", report.message)
    return report


def online(seconds: float | None = None) -> OnlineReport:
    """Hold the session open so nobody can attack the village, until stopped.

    Clash of Clans will not let anyone raid a village whose owner is online, so
    a run that has finished farming is safer sitting in the game than leaving
    it. **What keeps a session alive is input rather than a connection** — the
    game drops an idle session whatever the socket is doing — so this sends the
    smallest gesture that counts as one: a short drag over the middle of the
    screen, which on a village pans the camera and does nothing else, reversed
    each time so it does not walk the view anywhere over several hours.

    It ends only on `ai_coc stop`, which is the same flag every other long loop
    here reads: this one has no natural end, so a run that could not be stopped
    would have to be killed, and killing it is what leaves the game somewhere
    the next run cannot start from.
    """
    _clear_stop()
    adb = _controller()
    display = adb.display_for(COC_PACKAGE)
    gap = ConfigStore().load().keepalive_seconds if seconds is None else seconds
    report = OnlineReport()
    started = time.monotonic()
    logger.info("Holding the session open, nudging every %.0fs", gap)
    while not stop_requested():
        near, far = (NUDGE_FROM, NUDGE_TO) if report.nudges % 2 == 0 else (NUDGE_TO, NUDGE_FROM)
        adb.swipe((near, NUDGE_ROW), (far, NUDGE_ROW), NUDGE_MS, display)
        report.nudges += 1
        # Through `_rest` rather than a plain sleep, so a stop asked for two
        # minutes into a wait is answered in two seconds rather than at the end
        # of it — the same reason the barracks wait goes through it.
        if _rest(gap):
            break
    _clear_stop()
    report.seconds = time.monotonic() - started
    report.message = f"保持上線 {report.seconds / 60:.0f} 分鐘,動了 {report.nudges} 次畫面"
    logger.info("Online: %s", report.message)
    return report


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
        skip_offered=skip_offered(png),
        idle_dialog=idle_disconnected(png),
        card_groups=groups,
        counted=counted_cards(png, slots),
        freezes=freeze_cards(png, slots),
        live=live_cards(png, slots),
        on_field=field_units(png, slots),
        counts={slot: card_count(png, slot) for slot in slots},
    )
