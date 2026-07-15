"""The semantic key layer over readchar.

readchar owns raw terminal handling and escape-sequence parsing; this layer only
maps its raw strings onto the :class:`Key` vocabulary the widgets navigate by.
The two aliasing rules pinned here are the ones readchar gets wrong for our use
(see ``docs/design/cli-tui-migration.md`` §4.1).
"""

from __future__ import annotations

import pytest
import readchar

from calfcord.cli.tui.keys import Key, read_key, resolve


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (readchar.key.UP, Key.UP),
        (readchar.key.DOWN, Key.DOWN),
        (" ", Key.SPACE),
        ("\x7f", Key.BACKSPACE),
        ("\x04", Key.EOF),
    ],
)
def test_resolve_maps_readchar_sequences_to_keys(raw: str, expected: Key) -> None:
    assert resolve(raw) == expected


@pytest.mark.parametrize("raw", ["\r", "\n"])
def test_resolve_accepts_both_cr_and_lf_as_enter(raw: str) -> None:
    """POSIX raw mode delivers CR on Enter, but ``readchar.key.ENTER`` is LF.

    Binding only to ``readchar.key.ENTER`` would leave the Enter key dead in a
    real terminal, so BOTH must resolve. Regression guard for §4.1 trap 1.
    """
    assert resolve(raw) == Key.ENTER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("\x1bOA", Key.UP), ("\x1bOB", Key.DOWN)],
)
def test_resolve_accepts_application_cursor_mode_arrows(raw: str, expected: Key) -> None:
    """A terminal in DECCKM mode sends ``\\x1bOA``/``\\x1bOB`` for the arrows.

    readchar defines only the ``\\x1b[A``/``\\x1b[B`` normal-mode forms, so these
    aliases are ours to add.
    """
    assert resolve(raw) == expected


def test_resolve_returns_none_for_a_printable_character() -> None:
    """Printable text is not a control key — the caller keeps the raw character."""
    assert resolve("a") is None


def test_enter_is_not_confused_with_ctrl_j() -> None:
    """Ctrl-J and LF share a byte; treating Enter as text would break every prompt."""
    assert resolve(readchar.key.CTRL_J) == Key.ENTER


class TestReadKeyOnANonTerminal:
    """A piped / CI stdin must produce the CLI's clean error, never a traceback.

    ``termios.tcgetattr`` raises ``termios.error`` there, and ``termios.error``
    does NOT subclass ``OSError`` — so raw, it would sail straight past ``main``'s
    non-TTY handler and dump a traceback. Translating it to ``OSError`` is what
    lets that existing handler print "needs an interactive terminal" and exit 1.
    """

    def test_translates_termios_error_to_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import termios

        def _not_a_terminal() -> str:
            raise termios.error(25, "Inappropriate ioctl for device")

        monkeypatch.setattr(readchar, "readkey", _not_a_terminal)
        with pytest.raises(OSError):
            read_key()

    def test_keeps_the_interrupt_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ctrl-C must still surface as KeyboardInterrupt → the CLI's exit 130."""

        def _interrupt() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(readchar, "readkey", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            read_key()
