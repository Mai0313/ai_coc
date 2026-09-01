from __future__ import annotations

import sys
from typing import Any, get_args
import logging
from pathlib import Path
import argparse

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

# PyInstaller runs this file as `__main__`, which has no package context, so these
# stay absolute even for same-layer modules.
from ai_coc import commands, __version__
from ai_coc.models import (
    World,
    RunLog,
    HeroKind,
    WallOptions,
    RestartScope,
    AttackOptions,
    LootOverrides,
)
from ai_coc.constants import APP_NAME
from ai_coc.logging_setup import configure_logging
from ai_coc.ui.main_window import MainWindow, migrate_settings

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
    "upgrade",
    "hero",
    "donate",
    "probe",
    "bounds",
)


def _parser() -> argparse.ArgumentParser:
    """Every argument the entry point takes; no sub-command means open the window.

    The two GUI switches are declared here as well, rather than left to a scan of
    raw argv. Otherwise anything starting with a dash has to bypass argparse,
    which takes `--help` down with it.
    """
    parser = argparse.ArgumentParser(prog="ai_coc", description=APP_NAME)
    parser.add_argument("--live-test", action="store_true", help="開視窗後對目前畫面問 AI 一次")
    parser.add_argument("--agent-command", default="", help="開視窗後把這句話送進 AI 助手執行")
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
    # Omitted means "whatever the config file says". Three zeros is how a run
    # being studied gets back to attacking the first opponent it is shown.
    for flag, resource in (("gold", "金幣"), ("elixir", "聖水"), ("dark", "黑水")):
        run.add_argument(
            f"--min-{flag}", type=int, help=f"只打{resource}至少這麼多的對手,蓋過設定檔"
        )
    # Its own command rather than a flag on `attack`, because the run being
    # stopped is a different process: whatever put that one in the background
    # cannot send it a Ctrl-C, and killing it leaves the game mid-battle.
    sub.add_parser("stop", help="請正在跑的進攻迴圈打完這一場就收工")
    # No rounds and no limit: this one runs until it is stopped by design, since
    # what it is for is the stretch between one night's farming and the next.
    alive = sub.add_parser("online", help="留在遊戲裡保持上線,別人就打不了這個村莊")
    alive.add_argument(
        "--every", type=float, metavar="秒", help="每隔這麼多秒動一次畫面,蓋過設定檔"
    )
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
    sub.add_parser("collect", help="把採集器裡的資源全部收起來")
    sub.add_parser("builders", help="每個工人在蓋什麼、還要多久")
    build = sub.add_parser("upgrade", help="把閒著的工人派去升級建築")
    build.add_argument("--keep-gold", type=int, default=0, help="留下這麼多金幣不要花")
    build.add_argument("--keep-elixir", type=int, default=0, help="留下這麼多聖水不要花")
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
    sub.add_parser("probe", help="花一場戰鬥實測邊界，對照判讀器說的")
    sub.add_parser("bounds", help="花一場戰鬥實測地圖邊緣，回推村莊範圍")
    shot = sub.add_parser("capture", help="從遊戲連續存畫面")
    shot.add_argument("out", type=Path)
    shot.add_argument("--count", type=int, default=1)
    shot.add_argument("--gap", type=float, default=1.5)
    frame = sub.add_parser("read", help="把一張畫面丟給每個 parser,印出各自讀到什麼")
    frame.add_argument("png", type=Path)
    for name in RECORDABLE:
        sub.choices[name].add_argument(
            "--record", action="store_true", help="把這次讀到的每一張畫面存進這次的紀錄資料夾"
        )
    return parser


def _run_command(arguments: argparse.Namespace, run: RunLog) -> int:
    """Run one headless command; its result goes to stdout so it can be piped.

    Written rather than logged: the log already carries the running commentary
    on stderr, and this is the answer. It goes into the run's own directory as
    well, so the answer and the log explaining it are found together instead of
    that depending on whoever started the run having redirected stdout.
    """
    # The commands whose whole argument list is a frame directory, dispatched
    # through a table rather than a branch each. They are the same shape, and
    # four copies of that shape is most of what this function's branching budget
    # was being spent on.
    plain = {
        "collect": commands.collect,
        "builders": commands.builders,
        "probe": commands.probe,
        "bounds": commands.bounds,
    }
    # The same again for the ones whose arguments are a couple of plain scalars
    # instead of a frame directory. Two is already worth a table: a branch each
    # is what tipped this function past the complexity limit.
    scalar = {
        "view": lambda: commands.view(arguments.zoom, arguments.times),
        "world": lambda: commands.world(arguments.go),
        "launch": lambda: commands.launch(arguments.restart),
        "online": lambda: commands.online(arguments.every),
    }
    if arguments.command in plain:
        result = plain[arguments.command](run.frames).model_dump_json(indent=2)
    elif arguments.command in scalar:
        result = scalar[arguments.command]().model_dump_json(indent=2)
    elif arguments.command == "attack":
        result = commands.attack(
            AttackOptions(
                frame_dir=run.frames,
                plan_in=arguments.plan_in,
                plan_out=arguments.plan_out,
                plan_log=run.plan_log,
                minimums=LootOverrides(
                    min_gold=arguments.min_gold,
                    min_elixir=arguments.min_elixir,
                    min_dark=arguments.min_dark,
                ),
                world=arguments.world,
                rounds=arguments.repeat,
                shot_every=arguments.shot_every,
                restart_every=arguments.restart_every,
            )
        ).model_dump_json(indent=2)
    elif arguments.command == "stop":
        result = commands.stop()
    elif arguments.command == "walls":
        result = commands.walls(
            WallOptions(
                frame_dir=run.frames,
                keep_gold=arguments.keep_gold,
                keep_elixir=arguments.keep_elixir,
                rounds=arguments.rounds,
                at=[_spot(one) for one in arguments.at or ()],
            )
        ).model_dump_json(indent=2)
    elif arguments.command == "upgrade":
        result = commands.upgrade(
            run.frames, arguments.keep_gold, arguments.keep_elixir
        ).model_dump_json(indent=2)
    elif arguments.command == "hero":
        result = commands.hero(
            run.frames, arguments.upgrade, _spot(arguments.at) if arguments.at else None
        ).model_dump_json(indent=2)
    elif arguments.command == "donate":
        result = commands.donate(run.frames, arguments.dry_run, arguments.rounds).model_dump_json(
            indent=2
        )
    elif arguments.command == "capture":
        saved = commands.capture(arguments.out, arguments.count, arguments.gap)
        result = "\n".join(str(path) for path in saved)
    else:
        result = commands.read(arguments.png.read_bytes()).model_dump_json(indent=2)
    run.answer(result)
    sys.stdout.write(f"{result}\n")
    return 0


def _spot(text: str) -> tuple[int, int]:
    """One `X,Y` argument as a point on the screen."""
    x, _, y = text.partition(",")
    return int(x), int(y)


def main() -> int:
    sys.excepthook = _log_uncaught
    arguments = _parser().parse_args()
    # Opened after parsing, so `--help` and a rejected flag leave no empty
    # directory behind, and so its name can say which command it holds.
    run = RunLog.open(arguments.command or "app", recording=getattr(arguments, "record", False))
    configure_logging(run)
    # First line of every run, because a directory nobody can name is one nobody
    # goes back to: this is what a session reads to find the frames afterwards.
    logger.info("This run is being kept in %s", run.directory)
    migrate_settings()
    if arguments.command:
        return _run_command(arguments, run)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # Qt's own metadata slot, which takes the bare version, not a display label.
    app.setApplicationVersion(__version__)
    window = MainWindow(run)
    window.show()
    if arguments.live_test:
        QTimer.singleShot(2500, window.live_ai_test)
    if arguments.agent_command:

        def run_command(text: str = arguments.agent_command) -> None:
            window.tabs.setCurrentIndex(2)
            window.chat_input.setText(text)
            window.send_chat()

        QTimer.singleShot(2500, run_command)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
