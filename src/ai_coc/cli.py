from __future__ import annotations

import sys
from typing import Any
import logging
from pathlib import Path
import argparse

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

# PyInstaller runs this file as `__main__`, which has no package context, so these
# stay absolute even for same-layer modules.
from ai_coc import commands, __version__
from ai_coc.constants import APP_NAME
from ai_coc.logging_setup import configure_logging
from ai_coc.ui.main_window import MainWindow

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
    run = sub.add_parser("attack", help="跑一輪進攻迴圈,不開視窗")
    run.add_argument("--frames", type=Path, help="把迴圈讀到的每一張畫面存進這個資料夾")
    run.add_argument("--plan-in", type=Path, help="照這份 JSON 打，完全不呼叫 AI")
    run.add_argument("--plan-out", type=Path, help="把這一場實際用的計畫寫成 JSON")
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
    if arguments.command == "attack":
        report = commands.attack(arguments.frames, arguments.plan_in, arguments.plan_out)
        result = report.model_dump_json(indent=2)
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
