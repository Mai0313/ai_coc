import logging
import unittest

from ai_coc.ui.render import LogHtmlRenderer


def _record(level: int, message: str, name: str = "ai_coc.test") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


class LogRenderTests(unittest.TestCase):
    def test_record_carries_level_name_and_message(self) -> None:
        html = LogHtmlRenderer().render(_record(logging.WARNING, "找不到 instance"))
        assert "WARNING" in html
        assert "ai_coc.test" in html
        assert "找不到 instance" in html

    def test_levels_are_exported_for_a_dark_panel(self) -> None:
        html = LogHtmlRenderer().render(_record(logging.ERROR, "boom"))
        # rich's default export palette is built for a white terminal, where red
        # is #800000; on the panel's #0e141f background that is unreadable.
        assert "#800000" not in html

    def test_markup_in_a_message_is_escaped(self) -> None:
        html = LogHtmlRenderer().render(_record(logging.INFO, "<b>x</b>"))
        assert "&lt;b&gt;x&lt;/b&gt;" in html

    def test_console_sink_does_not_grow(self) -> None:
        renderer = LogHtmlRenderer()
        for index in range(20):
            renderer.render(_record(logging.INFO, f"line {index}"))
        assert renderer._buffer.getvalue() == ""


if __name__ == "__main__":
    unittest.main()
