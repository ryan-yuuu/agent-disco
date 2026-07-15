"""Semantic key vocabulary over :mod:`readchar`.

readchar owns the parts that are genuinely hard — reconfiguring the terminal,
restoring it on the way out, and parsing multi-byte escape sequences into one
string. (Not *raw* mode: it clears ICANON/ECHO from c_lflag and leaves c_iflag
alone, so input translation such as CR->LF stays on. That distinction matters —
see the CR note below.) This module owns only the mapping from those raw strings
onto the :class:`Key` vocabulary the widgets navigate by, plus the aliases
readchar does not cover for our use (``docs/design/cli-tui-migration.md`` §4.1).

:func:`read_key` is the single input seam: widgets take it as an injectable, so
every widget is testable by feeding a scripted key list with no TTY.
"""

from __future__ import annotations

import errno
import sys
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


# Raw sequence -> Key. Some entries are hand-written literals rather than
# readchar constants, for three different reasons:
#
# * "\x1bOA"/"\x1bOB" are the application-cursor-mode (DECCKM) arrow forms.
#   readchar defines only the normal-mode "\x1b[A"/"\x1b[B" forms, so a terminal
#   that has switched modes would navigate nowhere without these. THIS is the
#   load-bearing alias.
# * CR ("\r") maps to ENTER alongside LF. On POSIX this is belt-and-braces, NOT
#   a correction: readchar is not in raw mode — it clears only c_lflag bits
#   (ICANON/ECHO) and never touches ICRNL in c_iflag — so the tty driver still
#   translates CR to LF and ``readchar.key.ENTER`` ("\n") matches on its own.
#   The "\r" binding covers the cases where that is not true (Windows, or a
#   terminal already in true raw mode). An earlier version of this comment
#   claimed Enter would be "dead" without it; that was inferred from the
#   termios call without checking which flag word it touched, and a pty probe
#   disproved it.
# * "\x08" is Ctrl-H / the Windows backspace. readchar's BACKSPACE is "\x7f" on
#   POSIX, so this is a genuinely separate binding, not a restatement.
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

    Everything below funnels a broken stdin into one ``OSError(ENOTTY)``, because
    the entry point's non-TTY handler catches ``OSError`` and nothing else — and
    a broken stdin breaks in three different ways, on three different lines:

    * **fd 0 closed at exec** (a supervisor or daemon spawning us with no stdin):
      CPython sets ``sys.stdin`` to ``None``, and readchar reaches
      ``sys.stdin.fileno()`` *before* ``termios``, so this raises
      :exc:`AttributeError` — checked first, since the later guards never see it.
    * **piped / CI stdin**: ``termios.tcgetattr`` raises :exc:`termios.error`,
      which despite being an OS-level failure does **not** subclass ``OSError``.
    * **a closed file object**: ``ValueError: I/O operation on closed file``.

    Only the second of those was handled at first, and the end-to-end test used
    ``DEVNULL`` — a real fd — so it exercised that one path and missed the rest.
    Each raw exception dumps a traceback at an operator whose only mistake was
    running an interactive command without a terminal.

    ``ValueError`` is caught but :exc:`io.UnsupportedOperation` is deliberately
    NOT special-cased: it already subclasses ``OSError``, so it reaches the
    handler on its own. That is an accident of CPython's hierarchy rather than a
    guarantee, which is why the explicit guards exist around it.
    """
    # Not a broad `except AttributeError` around readkey(): that would mask
    # genuine readchar bugs. The condition is knowable up front, so ask.
    if sys.stdin is None:
        raise OSError(errno.ENOTTY, "stdin is not available (fd 0 is closed)")
    try:
        return readchar.readkey()
    except termios.error as exc:
        raise OSError(errno.ENOTTY, "stdin is not an interactive terminal") from exc
    except ValueError as exc:
        raise OSError(errno.ENOTTY, "stdin is closed") from exc
