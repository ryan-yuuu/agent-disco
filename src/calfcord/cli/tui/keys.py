"""Semantic key vocabulary over :mod:`readchar`.

readchar owns the parts that are genuinely hard — putting the terminal into raw
mode, restoring it on the way out, and parsing multi-byte escape sequences into
one string. This module owns only the mapping from those raw strings onto the
:class:`Key` vocabulary the widgets navigate by, plus the two aliases readchar
does not cover for our use (see ``docs/design/cli-tui-migration.md`` §4.1).

:func:`read_key` is the single input seam: widgets take it as an injectable, so
every widget is testable by feeding a scripted key list with no TTY.
"""

from __future__ import annotations

from enum import Enum, auto

import readchar


class Key(Enum):
    """A control key a widget acts on. Printable input never becomes a ``Key``."""

    UP = auto()
    DOWN = auto()
    ENTER = auto()
    SPACE = auto()
    BACKSPACE = auto()
    EOF = auto()


# Raw sequence -> Key. Two entries here are deliberate corrections to readchar's
# constants rather than restatements of them:
#
# * CR ("\r") maps to ENTER alongside LF ("\n"). A POSIX terminal in raw mode
#   sends CR when Enter is pressed, but ``readchar.key.ENTER`` is LF — binding
#   only to the constant would leave the Enter key dead in a real terminal.
# * "\x1bOA"/"\x1bOB" are the application-cursor-mode (DECCKM) arrow forms.
#   readchar defines only the normal-mode "\x1b[A"/"\x1b[B" forms, so a terminal
#   that has switched modes would navigate nowhere without these.
#
# Esc is deliberately absent: ``readchar.readkey`` blocks after "\x1b" waiting to
# disambiguate an escape sequence, so a lone Esc press cannot be observed. Ctrl-C
# is the cancel key instead — readchar raises KeyboardInterrupt for it, which the
# CLI entry point already maps to "aborted." and exit 130.
_BINDINGS: dict[str, Key] = {
    readchar.key.UP: Key.UP,
    readchar.key.DOWN: Key.DOWN,
    "\x1bOA": Key.UP,
    "\x1bOB": Key.DOWN,
    "\r": Key.ENTER,
    "\n": Key.ENTER,
    readchar.key.SPACE: Key.SPACE,
    readchar.key.BACKSPACE: Key.BACKSPACE,
    "\x08": Key.BACKSPACE,
    readchar.key.CTRL_D: Key.EOF,
}


def resolve(raw: str) -> Key | None:
    """Map a raw readchar string onto a :class:`Key`, or ``None`` if it is text.

    ``None`` means "not a control key" — the caller treats ``raw`` as literal
    input (a character typed into a text field, say).
    """
    return _BINDINGS.get(raw)


def read_key() -> str:
    """Block for one keypress and return its raw string.

    Delegates to :func:`readchar.readkey`, which handles raw mode and escape
    sequences and raises :exc:`KeyboardInterrupt` on Ctrl-C. This is the seam
    widgets inject in tests.
    """
    return readchar.readkey()
