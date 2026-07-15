"""The production :class:`~calfcord.cli._prompts.Prompter`, backed by the TUI.

Each method is a one-line delegation to :mod:`calfcord.cli.tui.widgets`. The
value is in what it does *not* do: the Protocol keeps its exact shape, so the 7
interactive commands and the 8 test fakes that mirror it need no change at all.

``read`` and ``console`` are constructor-injected rather than per-call arguments
because the Protocol's signatures are fixed. They default to the real terminal;
tests pass a scripted keyboard and a console that paints nowhere.
"""

from __future__ import annotations

from rich.console import Console

from calfcord.cli._prompts import Choice
from calfcord.cli.tui import widgets
from calfcord.cli.tui.keys import read_key

Reader = widgets.Reader


class RichPrompter:
    def __init__(self, *, read: Reader = read_key, console: Console | None = None) -> None:
        self._read = read
        self._console = console

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        return widgets.select(message, choices, default=default, read=self._read, console=self._console)

    def text(self, message: str, *, default: str = "") -> str:
        return widgets.text(message, default=default, read=self._read, console=self._console)

    def secret(self, message: str) -> str:
        return widgets.secret(message, read=self._read, console=self._console)

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return widgets.confirm(message, default=default, read=self._read, console=self._console)

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        return widgets.checkbox(
            message, choices, instruction=instruction, read=self._read, console=self._console
        )

    def pause(self, message: str) -> None:
        """A press-Enter gate — deliberately a bare ``input()``, not a widget.

        There is nothing to render and nothing to navigate, so a Live frame and a
        key loop would be pure overhead.

        All three failures are swallowed because this gate runs *after* the
        workspace is up: the answer is discarded, so there is nothing a failure
        could cost, and a traceback here would turn a working org into an apparent
        crash. That is the never-crash-once-live contract, and honouring it for
        only one of the three ways stdin can be missing honoured it for none:

        * piped / EOF        -> ``EOFError``
        * fd 0 closed        -> ``RuntimeError: input(): lost sys.stdin``
        * closed file object -> ``ValueError: I/O operation on closed file``
        """
        try:
            input(message)
        except (EOFError, RuntimeError, ValueError):
            print()
