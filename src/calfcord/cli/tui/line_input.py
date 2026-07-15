"""Mature single-line editing for the Rich prompt surface.

Rich renders the CLI's menus and transcript, but deliberately does not implement
text editing. ``prompt_toolkit`` owns text fields: cursor movement, editable
defaults, Unicode widths, paste, masking, and terminal resize are all terminal
primitives that should not be recreated in a key loop.
"""

from __future__ import annotations

import errno
import sys
from collections.abc import Callable
from typing import Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from calfcord.cli.tui import theme


class LineInput(Protocol):
    """The injectable text-editing seam used by the prompt widgets."""

    def prompt(self, message: str, *, default: str = "", secret: bool = False) -> str: ...


_STYLE = Style.from_dict(
    {
        "border": "dim",
        "title": "bold",
        "input": "bold",
        "bottom-toolbar": "noreverse",
        "hint": "dim noreverse",
    }
)


def _width() -> int:
    """Current terminal width while prompt_toolkit owns the application."""
    return max(20, get_app().output.get_size().columns)


def _truncate(text: str, width: int) -> str:
    """Fit text to terminal columns, preserving complete Unicode code points."""
    if get_cwidth(text) <= width:
        return text
    if width <= 0:
        return ""
    kept: list[str] = []
    remaining = max(0, width - 1)
    for char in text:
        columns = get_cwidth(char)
        if columns > remaining:
            break
        kept.append(char)
        remaining -= columns
    return "".join(kept).rstrip() + "…"


def _framed_line(left: str, content: str, right: str, *, reserved: int = 1) -> str:
    """Fill one terminal row without entering its last autowrap column."""
    available = max(1, _width() - reserved)
    fixed = get_cwidth(left) + get_cwidth(right)
    content = _truncate(content, max(0, available - fixed - 1))
    fill = max(1, available - fixed - get_cwidth(content))
    return f"{left}{content}{'─' * fill}{right}"


def _message(message: str) -> Callable[[], StyleAndTextTuples]:
    def render() -> StyleAndTextTuples:
        # The input row's right prompt reserves two columns; use the same inner
        # width above it so both vertical edges align.
        top = _framed_line("╭─ ", f"{message} ", "╮", reserved=3)
        return [("class:border", top + "\n│ ")]

    return render


def _toolbar() -> StyleAndTextTuples:
    hint = theme.HINT_TEXT
    line = _framed_line("╰─ ", f"{hint} ", "╯")
    return [("class:hint", line)]


class PromptToolkitLineInput:
    """A synchronous, isolated prompt_toolkit line editor.

    A fresh session per field prevents history and mutable editing state from
    leaking between unrelated wizard questions. ``in_thread=True`` keeps this
    synchronous Protocol safe if a future caller reaches it from an active
    asyncio loop; prompt_toolkit owns its event loop in the worker thread.
    """

    def __init__(self, *, input: Input | None = None, output: Output | None = None) -> None:
        self._input = input
        self._output = output

    def prompt(self, message: str, *, default: str = "", secret: bool = False) -> str:
        # Match the readchar backend's public failure contract. prompt_toolkit's
        # raw failures vary by stdin state (AttributeError, EOFError, or OSError),
        # while the CLI deliberately handles one stable ENOTTY boundary.
        if self._input is None:
            try:
                interactive = sys.stdin is not None and sys.stdin.isatty()
            except ValueError:
                interactive = False
            if not interactive:
                raise OSError(errno.ENOTTY, "stdin is not an interactive terminal")

        processors = [PasswordProcessor(char="•")] if secret else None
        session: PromptSession[str] = PromptSession(
            message=_message(message),
            rprompt=[("class:border", " │")],
            bottom_toolbar=_toolbar,
            style=_STYLE,
            input_processors=processors,
            input=self._input,
            output=self._output,
            erase_when_done=True,
            complete_while_typing=False,
            enable_history_search=False,
            include_default_pygments_style=False,
            reserve_space_for_menu=0,
            wrap_lines=False,
        )
        return session.prompt(default=default, in_thread=True)


def prompt(message: str, *, default: str = "", secret: bool = False) -> str:
    """Edit one line using the production terminal streams."""
    return PromptToolkitLineInput().prompt(message, default=default, secret=secret)
