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

import errno
import termios
from enum import Enum, auto

import readchar


class Key(Enum):
    """A control key a widget acts on. Printable input never becomes a ``Key``.

    Space is deliberately absent. It is *printable text* — the character at the
    centre of every prose answer — and only the checkbox reads it as a command.
    Binding it here would make it a control key everywhere, which is exactly the
    bug that silently turned "npx -y pkg" into "npx-ypkg": the text field asks
    for literal input, sees a Key, and drops it. A widget that wants space as a
    command matches the raw character itself, the way ``confirm`` matches y/n.
    """

    UP = auto()
    DOWN = auto()
    ENTER = auto()
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
    readchar.key.BACKSPACE: Key.BACKSPACE,
    "\x08": Key.BACKSPACE,
    readchar.key.CTRL_D: Key.EOF,
}


def resolve(raw: str) -> Key | None:
    """Map a raw readchar string onto a :class:`Key`, or ``None`` if unbound.

    ``None`` means **"not a bound key"** — which is a superset of text, not a
    synonym for it. Tab, Ctrl-A, Home and the left/right arrows all return
    ``None`` while being unprintable, so a caller must still ask whether ``raw``
    is printable before treating it as input. A caller that skips that check
    injects raw escape sequences into the value.

    (An earlier version of this docstring said ``None`` meant the caller could
    treat ``raw`` as literal input. That was wrong, and it is the same shape as
    the space bug: input filed under the wrong category. The check lives at
    ``widgets._typed_field``.)
    """
    return _BINDINGS.get(raw)


def read_key() -> str:
    """Block for one keypress and return its raw string.

    Delegates to :func:`readchar.readkey`, which handles raw mode and escape
    sequences and raises :exc:`KeyboardInterrupt` on Ctrl-C (which the CLI entry
    point maps to a clean "aborted." exit 130). This is the seam widgets inject
    in tests.

    The one translation: on a piped / CI stdin, ``termios.tcgetattr`` raises
    :exc:`termios.error` — which, despite being an OS-level failure, does **not**
    subclass :exc:`OSError`. Left raw it would sail past the entry point's
    non-TTY handler and dump a traceback at an operator whose only mistake was
    running an interactive command without a terminal. Re-raising it as an
    ``OSError`` carrying ``ENOTTY`` routes it into that existing handler, which
    prints "this command needs an interactive terminal" and exits 1.
    """
    try:
        return readchar.readkey()
    except termios.error as exc:
        raise OSError(errno.ENOTTY, "stdin is not an interactive terminal") from exc
