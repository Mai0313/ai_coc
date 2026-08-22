from __future__ import annotations

import sys
from typing import Any
import logging

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from .constants import APP_NAME, VERSION_LABEL
from .logging_setup import configure_logging
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _log_uncaught(kind: type[BaseException], value: BaseException, trace: Any) -> None:  # noqa: ANN401 - matches sys.excepthook
    """Qt swallows slot exceptions; without this the packaged EXE loses them."""
    logger.critical("未處理的例外", exc_info=(kind, value, trace))


def main() -> int:
    configure_logging()
    sys.excepthook = _log_uncaught
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION_LABEL)
    window = MainWindow()
    window.show()
    if "--live-test" in sys.argv:
        QTimer.singleShot(2500, window.live_ai_test)
    for argument in sys.argv:
        if argument.startswith("--agent-command="):
            command = argument.split("=", 1)[1]

            def run_command(text: str = command) -> None:
                window.tabs.setCurrentIndex(2)
                window.chat_input.setText(text)
                window.send_chat()

            QTimer.singleShot(2500, run_command)
    return app.exec_()
