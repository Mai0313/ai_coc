"""The thread-pool plumbing under the window, driven on the calling thread.

A `QRunnable` is only a `run` method, and its signals connect to plain Python
callables without an event loop, so what the pool would do is exactly what
calling `run` does here.
"""

from __future__ import annotations

import logging
import unittest

from ai_coc.ui.workers import Worker, LogBridge, StreamWorker, UiLogHandler


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


class StreamWorkerTests(unittest.TestCase):
    def test_every_chunk_is_handed_over_in_order(self) -> None:
        chunks: list[str] = []
        worker = StreamWorker(lambda: iter(["好", "的"]), "reply")
        worker.signals.delta.connect(chunks.append)
        worker.run()
        assert chunks == ["好", "的"]

    def test_a_stream_that_breaks_midway_keeps_what_arrived_and_reports_why(self) -> None:
        def broken() -> object:
            yield "半"
            raise RuntimeError("quota")

        chunks: list[str] = []
        errors: list[str] = []
        finished: list[bool] = []
        worker = StreamWorker(broken, "reply")
        worker.signals.delta.connect(chunks.append)
        worker.signals.error.connect(errors.append)
        worker.signals.finished.connect(lambda: finished.append(True))
        worker.run()
        assert chunks == ["半"]
        assert errors == ["quota"]
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
