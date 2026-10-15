from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, get_args
import logging
from pathlib import Path
import argparse
from datetime import datetime
from contextlib import nullcontext

from rich.table import Table
from rich.console import Console
from PyQt5.QtWidgets import QApplication

# PyInstaller runs this file as `__main__`, which has no package context, so these
# stay absolute even for same-layer modules.
from ai_coc import commands, __version__
from ai_coc.models import (
    World,
    Caller,
    RunLog,
    StopTarget,
    RestartScope,
    AttackOptions,
    DonateOptions,
    LootOverrides,
    VillageExport,
)
from ai_coc.constants import APP_NAME
from ai_coc.logging_setup import configure_logging
from ai_coc.ui.main_window import MainWindow

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from collections.abc import Callable

    from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _log_uncaught(kind: type[BaseException], value: BaseException, trace: Any) -> None:  # noqa: ANN401 - matches sys.excepthook
    """Qt swallows slot exceptions; without this the packaged EXE loses them."""
    logger.critical("未處理的例外", exc_info=(kind, value, trace))


# The sub-commands that drive a loop, and so have frames worth keeping. Declared
# once rather than eight times over, which is what a `--frames <dir>` on each of
# them had become — and the directory it used to take is now the run's own.
RECORDABLE = ("attack", "collect", "stock", "status", "donate", "probe", "bounds", "export")

# The sub-commands that hold no claim on the emulator, listed the way round
# that fails safely: `read` parses a PNG off the disk, and `stop` and
# `giveback` each change one field of the state file, so none of them ever
# opens ADB. Everything else claims,
# short ones included — a `collect` holds the display for the eight seconds it
# takes, and a session reading the state file to find out whether the screen is
# free wants that as much as it wants to know about a farming run.
#
# Naming the exceptions rather than the claimers is what keeps a command added
# later honest: forgotten here it claims, which costs two writes it did not
# need, where a positive list forgotten would have it drive the emulator while
# the file says nobody is.
#
# `export --last` is the same exception reached by a flag rather than by a name,
# and `_claim_for` is where the two meet: the command drives the emulator, that
# one invocation of it does not. So is `world` without `--go`.
#
# `capture` only looks, and so does a plain `world`: both take screenshots and
# nothing else, and the file is a lock now, so a claim there would stand a
# farming loop down for one screenshot (#290).
WITHOUT_CLAIM = ("read", "stop", "giveback", "capture", "watch")


# Every sub-command and the line `--help` gives it, in the order a person
# reaches for them, which is the order the help lists them in: farming, the
# readings, the rest of the village, the emulator and its camera, spending, the
# export, then the tools for working on the parsers.
COMMANDS = (
    ("attack", "進攻遊戲當下所在的村莊，預設打一輪"),
    # Its own command rather than a flag on `attack`, because the run being
    # stopped is a different process: whatever put that one in the background
    # cannot send it a Ctrl-C, and killing it leaves the game mid-battle.
    (
        "stop",
        "請正在用模擬器的指令做完手上這件事就收工，盯升級的也一起停；後面加 emulator 或 watch 只停一個",
    ),
    ("giveback", "把借來的模擬器還回去，借出的那一邊會自己接著跑"),
    (
        "watch",
        "睡到 --next 那個時間才結束，期間在狀態檔記下是誰在盯升級；丟到背景跑，結束時叫醒你",
    ),
    (
        "status",
        "兩個世界每件升級實際幾點好（藥水和鐘塔已算進去），加上這個村莊的閒置人數、倉庫和護盾",
    ),
    ("stock", "這個村莊的倉庫存量和容量"),
    ("world", "看遊戲停在日世界還是夜世界，加 --go 就開船過去"),
    ("collect", "收掉採集器裡的資源；在夜世界收的是聖水車"),
    ("donate", "部落有人要兵就捐"),
    ("launch", "開模擬器和部落衝突，停在村莊畫面"),
    ("view", "把村莊鏡頭拉遠或拉近"),
    ("tap", "點畫面上一個點，存下點完的畫面；升級確認視窗只能點它的 X"),
    ("swipe", "從一點滑到另一點，例如捲動建造面板，存下滑完的畫面"),
    (
        "confirm",
        "確認鈕上的價格跟 --price 一樣、而且是金幣聖水黑水，才按下確認；加 --pay 是從城牆選單的升級鈕開始付",
    ),
    ("export", "從遊戲匯出整個村莊的資料，對上名稱後存起來"),
    ("capture", "連續截圖，存進這次的紀錄資料夾"),
    ("read", "不碰遊戲，把一張截圖交給每個判讀器，印出各自讀到什麼"),
    ("probe", "花一場戰鬥實測部署邊界，對照判讀器的結果"),
    ("bounds", "花一場戰鬥實測地圖邊緣，推回村莊範圍"),
)


def _watch_flags(watcher: argparse.ArgumentParser, stopper: argparse.ArgumentParser) -> None:
    """`watch` never claims and never opens ADB: it sleeps on its own section of
    the state file, where a second agent sees somebody is watching and `stop`
    reaches it. `stop` reaches both sections unless told which.

    Its own function only because `_parser` is at Ruff's statement limit.
    """
    watcher.add_argument(
        "--next",
        type=datetime.fromisoformat,
        required=True,
        metavar="時間",
        help="睡到幾點醒來，ISO 格式",
    )
    watcher.add_argument("--about", default="", metavar="說明", help="在等什麼，一句話")
    stopper.add_argument(
        "target",
        nargs="?",
        choices=get_args(StopTarget),
        default="all",
        help="只停模擬器上的指令（emulator）或只停盯升級（watch）；不給就兩個都停",
    )


def _press_flags(
    press: argparse.ArgumentParser, drag: argparse.ArgumentParser, buy: argparse.ArgumentParser
) -> None:
    """The hands the agent drives the build panel with.

    They press where they are told and refuse only where a press would spend;
    reading the screen is the agent's, off the frame each one leaves. Their own
    function for the reason `_watch_flags` is.
    """
    press.add_argument("x", type=int, help="橫座標，1600x900 畫面上")
    press.add_argument("y", type=int, help="縱座標")
    for name in ("x1", "y1", "x2", "y2"):
        drag.add_argument(name, type=int, help="起點到終點的座標")
    drag.add_argument("--ms", type=int, default=commands.SWIPE_TIME, help="滑多久，毫秒")
    for one in (press, drag):
        one.add_argument(
            "--wait", type=float, default=commands.PRESS_SETTLE, metavar="秒", help="等多久再截圖"
        )
    buy.add_argument("--price", type=int, required=True, help="在面板那一列讀到的價格")
    buy.add_argument(
        "--pay",
        choices=("gold", "elixir"),
        help="城牆:從牆的選單按這種資源的升級鈕,再付跳出來的確認",
    )


def _parser() -> argparse.ArgumentParser:
    """Every argument the entry point takes; no sub-command means open the window."""
    parser = argparse.ArgumentParser(
        prog="ai_coc",
        description=(
            "在模擬器上自動玩部落衝突。不帶指令就開視窗。"
            "每個指令只處理遊戲當下停著的那個村莊，換村莊用 world --go。"
        ),
    )
    # A metavar instead of the default brace list, which printed every name
    # twice over on one unbroken line.
    sub = parser.add_subparsers(dest="command", title="指令", metavar="<指令>")
    for name, note in COMMANDS:
        sub.add_parser(name, help=note)
    # It plays whichever village the game is on and never sails there itself:
    # crossing is `world --go`, which is the one command that moves the game.
    run = sub.choices["attack"]
    run.add_argument(
        "--plan",
        type=Path,
        help="照這份戰術 JSON 打，完全不問 AI；戰術寫明是哪個村莊的，遊戲停在另一邊就不打",
    )
    run.add_argument("--plan-out", type=Path, help="把實際用的戰術寫成 JSON")
    run.add_argument("--repeat", type=int, default=1, help="打幾輪，0 代表打到叫停為止")
    run.add_argument(
        "--shot-every",
        type=float,
        default=0.0,
        metavar="秒",
        help="除了迴圈自己讀的畫面，每隔這麼多秒再存一張（要搭配 --debug）",
    )
    # Omitted means "whatever the config file says", like the loot thresholds
    # below; 0 is how a test run attacks a village the farming has already
    # filled, where the file's percentage would stand it down first.
    run.add_argument(
        "--stop-at",
        type=int,
        metavar="%",
        help="每種倉庫都滿到這個百分比就收工，蓋過設定檔；0 代表不管倉庫多滿都照打",
    )
    run.add_argument(
        "--until-idle",
        nargs="*",
        choices=("builder", "lab"),
        metavar="builder|lab",
        help="這個村莊有工人或實驗室閒著也收工；只給旗標就兩個都看，倉庫的收工條件照舊",
    )
    # Omitted means "whatever the config file says". Three zeros is how a run
    # being studied gets back to attacking the first opponent it is shown.
    for flag, resource in (("gold", "金幣"), ("elixir", "聖水"), ("dark", "黑水")):
        run.add_argument(
            f"--min-{flag}", type=int, help=f"只打{resource}至少這麼多的對手，蓋過設定檔"
        )
    # The game reopens on whichever village it was closed on, so no other
    # command can assume which one it is looking at. Reading is the default and
    # crossing is what `--go` asks for.
    where = sub.choices["world"]
    where.add_argument(
        "--go", choices=get_args(World), help="開船到這個世界，已經在那邊就什麼都不做"
    )
    _watch_flags(sub.choices["watch"], sub.choices["stop"])
    give = sub.choices["donate"]
    give.add_argument("--dry-run", action="store_true", help="走完流程但不真的捐，只回報能捐什麼")
    give.add_argument("--rounds", type=int, default=0, help="最多捐幾次，0 代表捐到不能捐為止")
    # Every other command assumes the game is up and gives up when it is not, so
    # this is the one that puts it there. The scopes exist because neither an
    # emulator nor a game that has stopped answering looks any different from a
    # working one down here; only whoever is watching the screen can tell.
    boot = sub.choices["launch"]
    boot.add_argument(
        "--restart",
        choices=get_args(RestartScope),
        default="none",
        help="要重開到哪一層：none 只確保遊戲在跑，game 只重開遊戲，emulator 連模擬器一起重開；預設 none",
    )
    camera = sub.choices["view"]
    camera.add_argument(
        "--zoom",
        choices=("out", "in"),
        default="out",
        help="out 拉遠到所有座標量測時的視野，in 拉近；預設 out",
    )
    camera.add_argument("--times", type=int, default=3, help="做幾次，已經到底的話多做無害")
    _press_flags(sub.choices["tap"], sub.choices["swipe"], sub.choices["confirm"])
    # The game's own village export, which is the only complete account of what
    # the village holds and far more than any screen reader can see.
    village = sub.choices["export"]
    village.add_argument(
        "--table", action="store_true", help="印成表格給人看；不給就跟其他指令一樣輸出 JSON"
    )
    village.add_argument("--last", action="store_true", help="不碰遊戲，直接讀上一次匯出的結果")
    # No directory argument: it writes into this run's own `frames/` like every
    # other command that saves what it saw. Whoever ran it used to invent a
    # path, and `--label` is what that need becomes.
    shot = sub.choices["capture"]
    shot.add_argument("--count", type=int, default=1, help="存幾張")
    shot.add_argument("--gap", type=float, default=1.5, metavar="秒", help="每張間隔幾秒")
    sub.choices["read"].add_argument("png", type=Path, help="要判讀的截圖")
    for name in RECORDABLE:
        sub.choices[name].add_argument(
            "--debug",
            action="store_true",
            help="把這次讀到的每張畫面存進紀錄資料夾，log 也降到 DEBUG",
        )
    # Every sub-command, because why a session names a run — to find it again
    # afterwards — has nothing to do with which one it ran.
    for one in sub.choices.values():
        one.add_argument(
            "--label",
            default="",
            metavar="名稱",
            help="加在這次紀錄資料夾名字的後面，方便之後認出是哪一次",
        )
        # Who asked, for the same reason on every sub-command: `stop` and `read`
        # included, since the log is where a stop somebody issued gets traced.
        # Optional so a person can still type a command bare; the skills are
        # what hold an agent to them.
        one.add_argument(
            "--agent",
            default="",
            metavar="名稱",
            help="是誰叫的，例如 claude-code、antigravity、codex",
        )
        one.add_argument(
            "--session", default="", metavar="ID", help="叫它的 agent 自己的 session id"
        )
        one.add_argument("--mission", default="", metavar="任務", help="這次在做什麼，一句話")
        # Background work says so itself, since only its caller knows whether it
        # will be started again: farming is, a live test is not.
        one.add_argument(
            "--yield",
            dest="yields",
            action="store_true",
            help="背景工作：不停掉別人，等模擬器空出來才開始；別人要用時借出去，還回來再接著跑",
        )
    return parser


def _caller(arguments: argparse.Namespace) -> Caller:
    return Caller(agent=arguments.agent, session=arguments.session, mission=arguments.mission)


def _answer(arguments: argparse.Namespace, run: RunLog) -> BaseModel | str:
    """What one sub-command answers, as the model it reports with or as plain text.

    One table rather than a branch per command: every entry turns the parsed
    flags into whatever `commands` takes, which for the loops is an options
    model, and the caller prints whatever comes back.
    """
    a = arguments
    handlers: dict[str, Callable[[], BaseModel | str]] = {
        "attack": lambda: commands.attack(
            AttackOptions(
                frame_dir=run.frames,
                plan=a.plan,
                plan_out=a.plan_out,
                plan_log=run.plan_log,
                minimums=LootOverrides(
                    min_gold=a.min_gold, min_elixir=a.min_elixir, min_dark=a.min_dark
                ),
                rounds=a.repeat,
                shot_every=a.shot_every,
                stop_at=a.stop_at,
                until_idle=[] if a.until_idle is None else a.until_idle or ["builder", "lab"],
            )
        ),
        "stop": lambda: commands.stop(_caller(a), a.target),
        "giveback": commands.giveback,
        "watch": lambda: commands.watch(_caller(a), a.next, a.about, run.directory),
        "collect": lambda: commands.collect(run.frames),
        "stock": lambda: commands.stock(run.frames),
        "status": lambda: commands.status(run.frames),
        "world": lambda: commands.world(a.go, run.no_village),
        "view": lambda: commands.view(a.zoom, a.times),
        "tap": lambda: commands.tap((a.x, a.y), run.directory / "frames", a.wait),
        "swipe": lambda: commands.swipe(
            (a.x1, a.y1), (a.x2, a.y2), run.directory / "frames", a.ms, a.wait
        ),
        "confirm": lambda: commands.confirm(a.price, run.directory / "frames", a.pay),
        "launch": lambda: commands.launch(a.restart, commands.stop_requested, run.no_village),
        "donate": lambda: commands.donate(
            DonateOptions(frame_dir=run.frames, dry_run=a.dry_run, rounds=a.rounds)
        ),
        "probe": lambda: commands.probe(run.frames),
        "bounds": lambda: commands.bounds(run.frames),
        # `run.frames` rather than a directory of its own: `main` opens a
        # capture's run with recording already on, because saving frames is the
        # whole of what the command does, so this is never None here.
        "capture": lambda: "\n".join(
            str(path) for path in commands.capture(run.frames, a.count, a.gap)
        ),
        "read": lambda: commands.read(a.png.read_bytes()),
        "export": lambda: commands.export(run.frames, a.last),
    }
    return handlers[a.command]()


def _print_table(export: VillageExport) -> None:
    """The export as a table for a person, which is what `--table` asks for.

    An entity the community mapping has no entry for shows `—` rather than its
    number: the number is already in the column beside it, and this project's
    rule is that a blank says "not known" where an invented value would not.
    """
    table = Table(title=f"{export.tag}  {len(export.entities)} 筆  {export.exported_at}")
    for column in ("Section", "Data ID", "Name", "Level", "Count"):
        table.add_column(column)
    for entity in export.entities:
        table.add_row(
            entity.section,
            str(entity.data_id),
            entity.name or "—",
            "—" if entity.level is None else str(entity.level),
            str(entity.count),
        )
    console = Console()
    if export.entities:
        console.print(table)
    # Always, and it is the whole output when there are no rows: an export that
    # failed says here which step it stopped at — nothing saved yet, a menu that
    # did not open, a clipboard that stayed empty — and the JSON branch that
    # would have shown it is the one `--table` replaced. `--last --table` on a
    # machine that has never run this is exactly that case, and it is the line
    # the `upgrades` skill tells a session to start with.
    console.print(commands.export_line(export))


def _run_command(arguments: argparse.Namespace, run: RunLog) -> int:
    """Run one headless command; its result goes to stdout so it can be piped.

    Written rather than logged: the log already carries the running commentary
    on stderr, and this is the answer. It goes into the run's own directory as
    well, so the answer and the log explaining it are found together instead of
    that depending on whoever started the run having redirected stdout.
    """
    cancelled = False
    try:
        with _claim_for(arguments, run):
            answer = _answer(arguments, run)
    # A queue that a stop ended is written down like an answer, not left as a
    # crash: `farm` reads a run with no `result.json` as a killed process. The
    # exit below still says it did not run. So is a command that stood down
    # because another device logged in, which can happen to any of them, and a
    # watch that found no state file to sit on.
    except (
        commands.LoanCancelledError,
        commands.SessionTakenError,
        commands.NoStateFileError,
    ) as error:
        answer, cancelled = str(error), True
    # Outside the claim: writing the answer down and printing it touch no
    # emulator, and holding the screen across them would say this run is still
    # driving when it has finished.
    result = answer if isinstance(answer, str) else answer.model_dump_json(indent=2)
    run.answer(result)
    # `result.json` is the answer whatever the terminal is shown, so a run stays
    # machine-readable after a `--table` that was only ever for a person.
    if getattr(arguments, "table", False) and isinstance(answer, VillageExport):
        _print_table(answer)
    else:
        sys.stdout.write(f"{result}\n")
    # Still not zero, though: the stop meant nothing lined up behind the loan
    # runs, and a command chained after this one with `&&` would otherwise
    # start with no loan left to be cancelled.
    return 1 if cancelled else 0


def _claim_for(arguments: argparse.Namespace, run: RunLog) -> AbstractContextManager[None]:
    """Hold the emulator for this command, or nothing at all for one that never touches it.

    Mostly that is a property of the command, but `export --last` only reads a
    file this machine already has. A claim there would overwrite whatever a
    farming run had written, and release it as `idle` — which reads from
    outside as an emulator nobody is driving, while a battle is still going on.
    """
    if (
        arguments.command in WITHOUT_CLAIM
        or getattr(arguments, "last", False)
        or (arguments.command == "world" and arguments.go is None)
    ):
        return nullcontext()
    return commands.claim(
        arguments.command, run.directory, _caller(arguments), yields=arguments.yields
    )


def main() -> int:
    sys.excepthook = _log_uncaught
    arguments = _parser().parse_args()
    # Opened after parsing, so `--help` and a rejected flag leave no empty
    # directory behind, and so its name can say which command it holds.
    # `capture` records by definition — saving frames is the command — so it
    # carries no `--debug` of its own and turns recording on here instead.
    run = RunLog.open(
        arguments.command or "app",
        recording=getattr(arguments, "debug", False) or arguments.command == "capture",
        label=getattr(arguments, "label", ""),
    )
    configure_logging(run)
    # One switch for a run someone means to debug: the frames it reads, and the
    # lines too bulky to keep for every run (each raw ADB call, Gemini's whole
    # prompt and reply).
    if getattr(arguments, "debug", False):
        logging.getLogger().setLevel(logging.DEBUG)
    # First line of every run, because a directory nobody can name is one nobody
    # goes back to: this is what a session reads to find the frames afterwards.
    logger.info("This run is being kept in %s", run.directory)
    if arguments.command:
        # Second line of every run: `state.json` holds only the latest, and the
        # run directories are the history someone greps to find who ran what.
        caller = _caller(arguments)
        logger.info(
            "Called by agent=%r session=%r mission=%r",
            caller.agent,
            caller.session,
            caller.mission,
        )
        if not (caller.agent and caller.session and caller.mission):
            logger.warning("Missing --agent/--session/--mission; this run will not say who ran it")
        return _run_command(arguments, run)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # Qt's own metadata slot, which takes the bare version, not a display label.
    app.setApplicationVersion(__version__)
    window = MainWindow(run)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
