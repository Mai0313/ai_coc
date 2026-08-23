"""Background work and log plumbing; nothing here touches widgets directly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import logging

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal

from .render import LogHtmlRenderer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    delta = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class Worker(QRunnable):
    def __init__(self, fn: Callable[[], Any], label: str = "") -> None:
        super().__init__()
        self.fn = fn
        self.label = label
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.result.emit(self.fn())
        except Exception as exc:
            # Without this the traceback dies inside the thread pool and the
            # user only ever sees the message box.
            logger.exception("背景工作失敗：%s", self.label)
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class StreamWorker(QRunnable):
    """Drain a text generator on the pool, handing each chunk to the UI thread."""

    def __init__(self, fn: Callable[[], Iterator[str]], label: str = "") -> None:
        super().__init__()
        self.fn = fn
        self.label = label
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            for chunk in self.fn():
                self.signals.delta.emit(chunk)
        except Exception as exc:
            logger.exception("串流工作失敗：%s", self.label)
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class LogBridge(QObject):
    message = pyqtSignal(str)


class UiLogHandler(logging.Handler):
    """Mirror every log record into the window's log panel, from any thread."""

    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.renderer = LogHtmlRenderer()

    def emit(self, record: logging.LogRecord) -> None:
        self.bridge.message.emit(self.renderer.render(record))
