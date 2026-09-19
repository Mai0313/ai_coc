from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, get_args
import logging
from pathlib import Path
import argparse
from contextlib import nullcontext

from rich.table import Table
from rich.console import Console
from PyQt5.QtWidgets import QApplication

# PyInstaller runs this file as `__main__`, which has no package context, so these
# stay absolute even for same-layer modules.
from ai_coc import commands, __version__
from ai_coc.models import (
    World,
    RunLog,
    HeroKind,
    HeroOptions,
    WallOptions,
    RestartScope,
    AttackOptions,
    DonateOptions,
    LootOverrides,
    VillageExport,
    UpgradeOptions,
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
# once rather than nine times over, which is what a `--frames <dir>` on each of
# them had become — and the directory it used to take is now the run's own.
RECORDABLE = (
    "attack",
    "walls",
    "collect",
    "builders",
    "stock",
    "worker",
    "lab",
    "status",
    "upgrade",
    "hero",
    "donate",
    "probe",
    "bounds",
    "export",
)

# The two sub-commands that hold no claim on the emulator, listed the way round
# that fails safely: `read` parses a PNG off the disk and `stop` changes one
# field of the state file, so neither ever opens ADB. Everything else claims,
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
# one invocation of it does not.
WITHOUT_CLAIM = ("read", "stop")


def _parser() -> argparse.ArgumentParser:
    """Every argument the entry point takes; no sub-command means open the window."""
    parser = argparse.ArgumentParser(prog="ai_coc", description=APP_NAME)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("attack", help="跑進攻迴圈")
    # Omitted means "whichever village the game is on", which is the honest
    # default: the game reopens on the one it was closed on. Naming one sails
    # there first, which is what farming both in one script wants.
    run.add_argument(
        "--world",
        choices=get_args(World),
        help="打哪個世界,不給就打當下所在的那個;指定的話會先坐船過去",
    )
    run.add_argument("--plan-in", type=Path, help="照這份 JSON 打，完全不呼叫 AI，只適用日世界")
    run.add_argument("--plan-out", type=Path, help="把這一場實際用的計畫寫成 JSON")
    run.add_argument("--repeat", type=int, default=1, help="連打幾輪,0 代表打到手動中止為止")
    run.add_argument(
        "--shot-every",
        type=float,
        default=0.0,
        metavar="秒",
        help="除了迴圈自己讀的畫面之外,每隔這麼多秒再存一張,需要搭配 --record",
    )
    # Omitted means "whatever the config file says", like the loot thresholds
    # below; 0 is how one run turns the restart off without editing the file.
    run.add_argument(
        "--restart-every",
        type=int,
        metavar="場",
        help="每真的打完這麼多場就重開模擬器跟遊戲,蓋過設定檔,0 代表這次不重開",
    )
    # The same shape again: 0 is how a test run attacks a village the farming
    # has already filled, where the file's percentage would stand it down first.
    run.add_argument(
        "--stop-at",
        type=int,
        metavar="%",
        help="每一種倉庫都滿到這個百分比就收工,蓋過設定檔,0 代表這次不管倉庫多滿都照打",
    )
    # Omitted means "whatever the config file says". Three zeros is how a run
    # being studied gets back to attacking the first opponent it is shown.
    for flag, resource in (("gold", "金幣"), ("elixir", "聖水"), ("dark", "黑水")):
        run.add_argument(
            f"--min-{flag}", type=int, help=f"只打{resource}至少這麼多的對手,蓋過設定檔"
        )
    # Its own command rather than a flag on `attack`, because the run being
    # stopped is a different process: whatever put that one in the background
    # cannot send it a Ctrl-C, and killing it leaves the game mid-battle.
    # The ones that take nothing of their own beyond the shared flags below, as
    # a table rather than a statement each: `stock` is deliberately among them,
    # since crossing is `world --go` and a status check that sails a boat is no
    # longer a status check — the two compose.
    for name, note in (
        ("stop", "請正在跑的進攻迴圈打完這一場就收工"),
        ("collect", "把採集器裡的資源全部收起來"),
        ("builders", "每個工人在蓋什麼、還要多久"),
        ("stock", "現在這個世界的倉庫水位跟容量,不切世界"),
        ("worker", "現在這個世界的工人在蓋什麼、還要多久,不切世界"),
        ("lab", "現在這個世界的實驗室在研究什麼、還要多久,不切世界"),
        ("status", "現在這個世界的工人、實驗室、倉庫跟護盾,一次讀完,不切世界"),
        ("probe", "花一場戰鬥實測邊界，對照判讀器說的"),
        ("bounds", "花一場戰鬥實測地圖邊緣，回推村莊範圍"),
    ):
        sub.add_parser(name, help=note)
    upgrade = sub.add_parser("walls", help="把儲量拿去升級城牆")
    upgrade.add_argument("--keep-gold", type=int, default=0, help="留下這麼多金幣不要花")
    upgrade.add_argument("--keep-elixir", type=int, default=0, help="留下這麼多聖水不要花")
    upgrade.add_argument("--rounds", type=int, default=0, help="最多買幾批,0 代表買到資源不夠為止")
    upgrade.add_argument(
        "--at",
        metavar="X,Y",
        action="append",
        help="這個座標上的城牆是候選之一,跳過整個村莊的掃描;可以給很多次,最便宜的那片先買",
    )
    build = sub.add_parser("upgrade", help="把閒著的工人派去升級建築")
    build.add_argument("--keep-gold", type=int, default=0, help="留下這麼多金幣不要花")
    build.add_argument("--keep-elixir", type=int, default=0, help="留下這麼多聖水不要花")
    build.add_argument(
        "--at",
        metavar="X,Y",
        action="append",
        help="這個座標上的建築是候選之一,跳過尋找;可以給很多次,最貴而且買得起的先升",
    )
    build.add_argument(
        "--only",
        metavar="名稱",
        default="",
        help="只升名字含這幾個字的建築,例如 --only 金礦;不給就升最貴而且買得起的",
    )
    champions = sub.add_parser("hero", help="讀英雄殿堂,把閒著的工人派去升級指定的英雄")
    # Reading is the default and starting an upgrade is the exception, because
    # only one of the two spends anything. Which hero is worth a builder is a
    # judgement about the village, so nothing here picks one on its own.
    champions.add_argument(
        "--upgrade",
        metavar="英雄",
        choices=[kind for kind in get_args(HeroKind) if kind != "unknown"],
        help="真的把這個英雄送去升級,不給就只讀不動",
    )
    champions.add_argument("--at", metavar="X,Y", help="直接點這個座標上的建築,跳過整個村莊的掃描")
    # The game reopens on whichever village it was closed on, so no other
    # command can assume which one it is looking at. Reading is the default and
    # crossing is what `--go` asks for.
    where = sub.add_parser("world", help="現在在日世界還是夜世界,也可以切過去")
    where.add_argument(
        "--go", choices=get_args(World), help="切到這個世界,已經在那邊就什麼都不做;不給就只讀不切"
    )
    camera = sub.add_parser("view", help="拉遠或拉近村莊鏡頭")
    camera.add_argument(
        "--zoom",
        choices=("out", "in"),
        default="out",
        help="out 是拉遠回到所有座標量測時的視野,in 是拉近,預設 out",
    )
    camera.add_argument("--times", type=int, default=3, help="做幾次,已經到底的話多做無害")
    # Every other command assumes the game is up and gives up when it is not, so
    # this is the one that puts it there. The scopes exist because neither an
    # emulator nor a game that has stopped answering looks any different from a
    # working one down here; only whoever is watching the screen can tell.
    boot = sub.add_parser("launch", help="開模擬器並啟動部落衝突")
    boot.add_argument(
        "--restart",
        choices=get_args(RestartScope),
        default="none",
        help="要重開到哪一層: none 只確保遊戲在跑,game 重開遊戲但不動模擬器,emulator 連模擬器一起重開,預設 none",
    )
    give = sub.add_parser("donate", help="有人請求增援就捐兵")
    give.add_argument(
        "--dry-run", action="store_true", help="走完流程但不真的捐,只回報畫面上能捐什麼"
    )
    give.add_argument("--rounds", type=int, default=0, help="最多捐幾次,0 代表捐到不能捐為止")
    # No directory argument: it writes into this run's own `frames/` like every
    # other command that saves what it saw. Whoever ran it used to invent a
    # path, and `--label` is what that need becomes.
    shot = sub.add_parser("capture", help="從遊戲連續存畫面")
    shot.add_argument("--count", type=int, default=1)
    shot.add_argument("--gap", type=float, default=1.5)
    frame = sub.add_parser("read", help="把一張畫面丟給每個 parser,印出各自讀到什麼")
    frame.add_argument("png", type=Path)
    # The game's own village export, which is the only complete account of what
    # the village holds and far more than any screen reader can see.
    village = sub.add_parser("export", help="從遊戲裡取得村莊資訊,對照名稱後存起來")
    village.add_argument(
        "--table", action="store_true", help="印成表格給人看,不給的話跟其他指令一樣吐 JSON"
    )
    village.add_argument("--last", action="store_true", help="不碰遊戲,直接讀上一次匯出的結果")
    for name in RECORDABLE:
        sub.choices[name].add_argument(
            "--record", action="store_true", help="把這次讀到的每一張畫面存進這次的紀錄資料夾"
        )
    # Every sub-command, because why a session names a run — to find it again
    # afterwards — has nothing to do with which one it ran.
    for one in sub.choices.values():
        one.add_argument(
            "--label",
            default="",
            metavar="名稱",
            help="在這次的紀錄資料夾名字後面加上這個,方便之後認出是哪一次",
        )
    return parser


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
                plan_in=a.plan_in,
                plan_out=a.plan_out,
                plan_log=run.plan_log,
                minimums=LootOverrides(
                    min_gold=a.min_gold, min_elixir=a.min_elixir, min_dark=a.min_dark
                ),
                world=a.world,
                rounds=a.repeat,
                shot_every=a.shot_every,
                restart_every=a.restart_every,
                stop_at=a.stop_at,
            )
        ),
        "stop": commands.stop,
        "walls": lambda: commands.walls(
            WallOptions(
                frame_dir=run.frames,
                keep_gold=a.keep_gold,
                keep_elixir=a.keep_elixir,
                rounds=a.rounds,
                at=[_spot(one) for one in a.at or ()],
            )
        ),
        "collect": lambda: commands.collect(run.frames),
        "builders": lambda: commands.builders(run.frames),
        "stock": lambda: commands.stock(run.frames),
        "worker": lambda: commands.worker(run.frames),
        "lab": lambda: commands.lab(run.frames),
        "status": lambda: commands.status(run.frames),
        "upgrade": lambda: commands.upgrade(
            UpgradeOptions(
                frame_dir=run.frames,
                keep_gold=a.keep_gold,
                keep_elixir=a.keep_elixir,
                at=[_spot(one) for one in a.at or ()],
                only=a.only,
            )
        ),
        "hero": lambda: commands.hero(
            HeroOptions(frame_dir=run.frames, upgrade=a.upgrade, at=_spot(a.at) if a.at else None)
        ),
        "world": lambda: commands.world(a.go),
        "view": lambda: commands.view(a.zoom, a.times),
        "launch": lambda: commands.launch(a.restart),
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
    # both skills tell a session to start with.
    console.print(commands.export_line(export))


def _run_command(arguments: argparse.Namespace, run: RunLog) -> int:
    """Run one headless command; its result goes to stdout so it can be piped.

    Written rather than logged: the log already carries the running commentary
    on stderr, and this is the answer. It goes into the run's own directory as
    well, so the answer and the log explaining it are found together instead of
    that depending on whoever started the run having redirected stdout.
    """
    with _claim_for(arguments, run):
        answer = _answer(arguments, run)
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
    return 0


def _claim_for(arguments: argparse.Namespace, run: RunLog) -> AbstractContextManager[None]:
    """Hold the emulator for this command, or nothing at all for one that never touches it.

    Mostly that is a property of the command, but `export --last` only reads a
    file this machine already has. A claim there would overwrite whatever a
    farming run had written, and release it as `idle` — which reads from
    outside as an emulator nobody is driving, while a battle is still going on.
    """
    if arguments.command in WITHOUT_CLAIM or getattr(arguments, "last", False):
        return nullcontext()
    return commands.claim(arguments.command, run.directory)


def _spot(text: str) -> tuple[int, int]:
    """One `X,Y` argument as a point on the screen."""
    x, _, y = text.partition(",")
    return int(x), int(y)


def main() -> int:
    sys.excepthook = _log_uncaught
    arguments = _parser().parse_args()
    # Opened after parsing, so `--help` and a rejected flag leave no empty
    # directory behind, and so its name can say which command it holds.
    # `capture` records by definition — saving frames is the command — so it
    # carries no `--record` of its own and turns it on here instead.
    run = RunLog.open(
        arguments.command or "app",
        recording=getattr(arguments, "record", False) or arguments.command == "capture",
        label=getattr(arguments, "label", ""),
    )
    configure_logging(run)
    # First line of every run, because a directory nobody can name is one nobody
    # goes back to: this is what a session reads to find the frames afterwards.
    logger.info("This run is being kept in %s", run.directory)
    if arguments.command:
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
