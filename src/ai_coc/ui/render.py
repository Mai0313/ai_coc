"""Turn Markdown replies and log records into the HTML the Qt panels display."""

from __future__ import annotations

from io import StringIO
from html import escape
import time
from typing import TYPE_CHECKING

from rich.text import Text
from markdown_it import MarkdownIt
from rich.console import Console
from rich.traceback import Traceback
from rich.terminal_theme import MONOKAI

from ai_coc.logging_setup import TIME_FORMAT

if TYPE_CHECKING:
    import logging

    from ai_coc.models import ChatTranscript

# Qt's rich text is a subset of CSS 2.1; keep to selectors and properties it honours.
CHAT_STYLESHEET = """
h1, h2, h3, h4 { color: #8fb8ff; }
a { color: #70a4ff; }
code { background-color: #0b1220; color: #ffd479; font-family: Consolas, monospace; }
pre { background-color: #0b1220; color: #d7e3f7; font-family: Consolas, monospace; }
blockquote { color: #9fb3d0; }
th, td { border: 1px solid #33415a; padding: 4px; }
.speaker { font-weight: bold; }
.user { color: #7fd1a8; }
.assistant { color: #70a4ff; }
.system { color: #91a3c0; }
"""

LEVEL_STYLES = {
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}

# Raw HTML stays off: a model reply is text to display, never markup to trust.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def markdown_to_html(text: str) -> str:
    return _MARKDOWN.render(text)


def transcript_to_html(transcript: ChatTranscript) -> str:
    """The whole conversation; a streaming reply is re-rendered on every repaint."""
    blocks = []
    for message in transcript.root:
        blocks.append(f'<p class="speaker {message.role}">{escape(message.heading)}</p>')
        if message.body:
            blocks.append(markdown_to_html(message.body))
    return "".join(blocks)


class LogHtmlRenderer:
    """One rich console per log panel, exporting each record as a coloured HTML line."""

    def __init__(self, width: int = 160) -> None:
        self.buffer = StringIO()
        self.console = Console(
            file=self.buffer, record=True, width=width, color_system="truecolor", soft_wrap=True
        )

    def render(self, record: logging.LogRecord) -> str:
        self.console.print(
            Text.assemble(
                (time.strftime(TIME_FORMAT, time.localtime(record.created)), "dim"),
                " ",
                (f"{record.levelname:<8}", LEVEL_STYLES.get(record.levelname, "white")),
                (record.name, "cyan"),
                ": ",
                record.getMessage(),
            )
        )
        kind, value, trace = record.exc_info or (None, None, None)
        if kind and value:
            self.console.print(
                Traceback.from_exception(kind, value, trace, width=self.console.width)
            )
        # Without a theme rich exports the light-terminal palette, whose red is
        # #800000 — unreadable on the panel's dark background.
        html = self.console.export_html(
            theme=MONOKAI, inline_styles=True, code_format="{code}", clear=True
        )
        # export_html only clears the recorded segments; the sink grows without this.
        self.buffer.seek(0)
        self.buffer.truncate(0)
        return f'<pre style="margin:0; white-space:pre-wrap">{html.rstrip()}</pre>'
