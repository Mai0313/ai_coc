"""Turn log records into the HTML the window's 執行紀錄 panel displays."""

from __future__ import annotations

from io import StringIO
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, PrivateAttr
from rich.text import Text
from rich.console import Console
from rich.traceback import Traceback
from rich.terminal_theme import MONOKAI

from ai_coc.logging_setup import TIME_FORMAT

if TYPE_CHECKING:
    import logging

LEVEL_STYLES = {
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


class LogHtmlRenderer(BaseModel):
    """One rich console per log panel, exporting each record as a coloured HTML line.

    A model with its console in a `PrivateAttr`, which is the shape every adapter
    in this project already has: what a caller may set is a field, and the
    third-party object built from it is not.
    """

    width: int = 160

    _buffer: StringIO = PrivateAttr(default_factory=StringIO)
    # Built in `model_post_init` rather than by a factory, because it needs the
    # width the caller asked for.
    _console: Console = PrivateAttr()

    def model_post_init(self, context: object, /) -> None:
        self._console = Console(
            file=self._buffer,
            record=True,
            width=self.width,
            color_system="truecolor",
            soft_wrap=True,
        )

    def render(self, record: logging.LogRecord) -> str:
        self._console.print(
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
            self._console.print(
                Traceback.from_exception(kind, value, trace, width=self._console.width)
            )
        # Without a theme rich exports the light-terminal palette, whose red is
        # #800000 — unreadable on the panel's dark background.
        html = self._console.export_html(
            theme=MONOKAI, inline_styles=True, code_format="{code}", clear=True
        )
        # export_html only clears the recorded segments; the sink grows without this.
        self._buffer.seek(0)
        self._buffer.truncate(0)
        return f'<pre style="margin:0; white-space:pre-wrap">{html.rstrip()}</pre>'
