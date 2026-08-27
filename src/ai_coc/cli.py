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
from ai_coc.models import HeroKind, WallOptions, AttackOptions, LootOverrides
from ai_coc.constants import APP_NAME
from ai_coc.logging_setup import configure_logging
from ai_coc.ui.main_window import MainWindow, migrate_settings

logger = logging.getLogger(__name__)


def _log_uncaught(kind: type[BaseException], value: BaseException, trace: Any) -> None:  # noqa: ANN401 - matches sys.excepthook
    """Qt swallows slot exceptions; without this the packaged EXE loses them."""
    logger.critical("未處理的例外", exc_info=(kind, value, trace))


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
    run = sub.add_parser("attack", help="跑進攻迴圈,不開視窗")
    run.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    run.add_argument("--plan-in", type=Path, help="照這份 JSON 打，完全不呼叫 AI")
    run.add_argument("--plan-out", type=Path, help="把這一場實際用的計畫寫成 JSON")
    run.add_argument("--repeat", type=int, default=1, help="連打幾輪,0 代表打到手動中止為止")
    run.add_argument(
        "--shot-every",
        type=float,
        default=0.0,
        metavar="秒",
        help="除了迴圈自己讀的畫面之外,每隔這麼多秒再存一張,需要搭配 --frames",
    )
    # Omitted means "whatever the config file says". Three zeros is how a run
    # being studied gets back to attacking the first opponent it is shown.
    for flag, resource in (("gold", "金幣"), ("elixir", "聖水"), ("dark", "黑水")):
        run.add_argument(
            f"--min-{flag}", type=int, help=f"只打{resource}至少這麼多的對手,蓋過設定檔"
        )
    upgrade = sub.add_parser("walls", help="把儲量拿去升級城牆,不開視窗")
    upgrade.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    upgrade.add_argument("--keep-gold", type=int, default=0, help="留下這麼多金幣不要花")
    upgrade.add_argument("--keep-elixir", type=int, default=0, help="留下這麼多聖水不要花")
    upgrade.add_argument("--rounds", type=int, default=0, help="最多買幾批,0 代表買到資源不夠為止")
    upgrade.add_argument(
        "--at", metavar="X,Y", help="直接從這個座標上的城牆開始,跳過整個村莊的掃描"
    )
    gather = sub.add_parser("collect", help="把採集器裡的資源全部收起來,不開視窗")
    gather.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    crew = sub.add_parser("builders", help="每個工人在蓋什麼、還要多久,不開視窗")
    crew.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    build = sub.add_parser("upgrade", help="把閒著的工人派去升級建築,不開視窗")
    build.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    build.add_argument("--keep-gold", type=int, default=0, help="留下這麼多金幣不要花")
    build.add_argument("--keep-elixir", type=int, default=0, help="留下這麼多聖水不要花")
    champions = sub.add_parser("hero", help="讀英雄殿堂,把閒著的工人派去升級指定的英雄")
    champions.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
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
    give = sub.add_parser("donate", help="有人請求增援就捐兵,不開視窗")
    give.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    give.add_argument(
        "--dry-run", action="store_true", help="走完流程但不真的捐,只回報畫面上能捐什麼"
    )
    give.add_argument("--rounds", type=int, default=0, help="最多捐幾次,0 代表捐到不能捐為止")
    survey = sub.add_parser("probe", help="花一場戰鬥實測邊界，對照判讀器說的")
    survey.add_argument("--frames", type=Path, help="把每一次探測的畫面存起來")
    edges = sub.add_parser("bounds", help="花一場戰鬥實測地圖邊緣，回推村莊範圍")
    edges.add_argument("--frames", type=Path, help="把每一次探測的畫面存起來")
    shot = sub.add_parser("capture", help="從遊戲連續存畫面")
    shot.add_argument("out", type=Path)
    shot.add_argument("--count", type=int, default=1)
    shot.add_argument("--gap", type=float, default=1.5)
    frame = sub.add_parser("read", help="把一張畫面丟給每個 parser,印出各自讀到什麼")
    frame.add_argument("png", type=Path)
    return parser


def _run_command(arguments: argparse.Namespace) -> int:
    """Run one headless command; its result goes to stdout so it can be piped.

    Written rather than logged: the log already carries the running commentary
    on stderr, and this is the answer.
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
    if arguments.command in plain:
        result = plain[arguments.command](arguments.frames).model_dump_json(indent=2)
    elif arguments.command == "attack":
        result = commands.attack(
            AttackOptions(
                frame_dir=arguments.frames,
                plan_in=arguments.plan_in,
                plan_out=arguments.plan_out,
                minimums=LootOverrides(
                    min_gold=arguments.min_gold,
                    min_elixir=arguments.min_elixir,
                    min_dark=arguments.min_dark,
                ),
                rounds=arguments.repeat,
                shot_every=arguments.shot_every,
            )
        ).model_dump_json(indent=2)
    elif arguments.command == "walls":
        spot = arguments.at.split(",") if arguments.at else None
        result = commands.walls(
            WallOptions(
                frame_dir=arguments.frames,
                keep_gold=arguments.keep_gold,
                keep_elixir=arguments.keep_elixir,
                rounds=arguments.rounds,
                at=(int(spot[0]), int(spot[1])) if spot else None,
            )
        ).model_dump_json(indent=2)
    elif arguments.command == "upgrade":
        result = commands.upgrade(
            arguments.frames, arguments.keep_gold, arguments.keep_elixir
        ).model_dump_json(indent=2)
    elif arguments.command == "hero":
        spot = arguments.at.split(",") if arguments.at else None
        result = commands.hero(
            arguments.frames, arguments.upgrade, (int(spot[0]), int(spot[1])) if spot else None
        ).model_dump_json(indent=2)
    elif arguments.command == "donate":
        result = commands.donate(
            arguments.frames, arguments.dry_run, arguments.rounds
        ).model_dump_json(indent=2)
    elif arguments.command == "capture":
        saved = commands.capture(arguments.out, arguments.count, arguments.gap)
        result = "\n".join(str(path) for path in saved)
    else:
        result = commands.read(arguments.png.read_bytes()).model_dump_json(indent=2)
    sys.stdout.write(f"{result}\n")
    return 0


def main() -> int:
    configure_logging()
    sys.excepthook = _log_uncaught
    migrate_settings()
    arguments = _parser().parse_args()
    if arguments.command:
        return _run_command(arguments)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # Qt's own metadata slot, which takes the bare version, not a display label.
    app.setApplicationVersion(__version__)
    window = MainWindow()
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
