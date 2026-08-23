import logging
import unittest

from ai_coc.models import ChatTranscript
from ai_coc.ui.render import LogHtmlRenderer, markdown_to_html, transcript_to_html


def _record(level: int, message: str, name: str = "ai_coc.test") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


class MarkdownTests(unittest.TestCase):
    def test_markdown_becomes_html(self) -> None:
        html = markdown_to_html("# 標題\n\n- 一\n\n`code`")
        assert "<h1>標題</h1>" in html
        assert "<li>一</li>" in html
        assert "<code>code</code>" in html

    def test_raw_html_in_a_reply_is_escaped(self) -> None:
        assert "<b>" not in markdown_to_html("<b>bold</b>")

    def test_transcript_renders_every_role(self) -> None:
        transcript = ChatTranscript()
        transcript.add("user", "你", "收資源")
        transcript.add("assistant", "AI 回覆", "**好的**")
        html = transcript_to_html(transcript)
        assert 'class="speaker user"' in html
        assert 'class="speaker assistant"' in html
        assert "<strong>好的</strong>" in html

    def test_streaming_body_grows_in_place(self) -> None:
        transcript = ChatTranscript()
        reply = transcript.add("assistant", "AI 回覆")
        for chunk in ("好", "的"):
            reply.body += chunk
        assert "好的" in transcript_to_html(transcript)
        assert transcript.tail(5) == "回覆\n好的"


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
        assert renderer.buffer.getvalue() == ""


if __name__ == "__main__":
    unittest.main()
