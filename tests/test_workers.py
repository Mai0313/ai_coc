"""The thread-pool plumbing under the window, driven on the calling thread.

A `QRunnable` is only a `run` method, and its signals connect to plain Python
callables without an event loop, so what the pool would do is exactly what
calling `run` does here.
"""

from __future__ import annotations

import logging
import unittest

from ai_coc.ui.workers import Worker, LogBridge, UiLogHandler


class WorkerTests(unittest.TestCase):
    def test_a_result_arrives_and_finished_follows_it(self) -> None:
        got: list[object] = []
        order: list[str] = []
        worker = Worker(lambda: 42, "answer")
        worker.signals.result.connect(lambda value: (got.append(value), order.append("result")))
        worker.signals.finished.connect(lambda: order.append("finished"))
        worker.run()
        assert got == [42]
        assert order == ["result", "finished"]

    def test_an_exception_becomes_an_error_and_still_finishes(self) -> None:
        """Without the error the traceback dies in the pool and the box never opens."""
        errors: list[str] = []
        results: list[object] = []
        finished: list[bool] = []
        worker = Worker(lambda: 1 / 0, "boom")
        worker.signals.error.connect(errors.append)
        worker.signals.result.connect(results.append)
        worker.signals.finished.connect(lambda: finished.append(True))
        worker.run()
        assert errors == ["division by zero"]
        assert results == []
        assert finished == [True]


class LogHandlerTests(unittest.TestCase):
    def test_a_record_reaches_the_bridge_as_one_html_line(self) -> None:
        bridge = LogBridge()
        lines: list[str] = []
        bridge.message.connect(lines.append)
        record = logging.LogRecord(
            "ai_coc.test", logging.WARNING, __file__, 1, "找不到", None, None
        )
        UiLogHandler(bridge).emit(record)
        [html] = lines
        assert html.startswith("<pre")
        assert "找不到" in html
        assert "WARNING" in html


if __name__ == "__main__":
    unittest.main()
