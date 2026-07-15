"""The Live path — the only tests here that actually paint.

Every other widget test injects a non-terminal console, which makes ``Live``
refresh a no-op: the key loops are driven with **rendering switched off**. That
is the right trade for testing behaviour, but it leaves the painting itself
unasserted, and two things live only there:

* **The first frame.** ``_loop`` reads a key *before* it ever calls
  ``live.update``, so the opening frame comes from ``Live.__enter__`` alone. If
  that regressed, the widget would be invisible until the operator pressed a key
  — and every other test would still pass.
* **The transient erase**, which the module docstring calls the point of the
  inline model: the widget is wiped and replaced by a one-line record, so
  scrollback reads as a transcript rather than a graveyard of dead frames.

``record=True`` cannot capture Live output, so these write to a real StringIO
with ``force_terminal=True`` and read the emitted ANSI.
"""

from __future__ import annotations

import io

from rich.console import Console

from calfcord.cli._prompts import Choice
from calfcord.cli.tui import widgets

from .test_widgets import ENTER, keys

CHOICES = [Choice("a", "Anthropic"), Choice("o", "OpenAI")]


def painting_console(buffer: io.StringIO) -> Console:
    """A console that really paints — force_terminal makes Live emit frames."""
    return Console(file=buffer, width=60, force_terminal=True, highlight=False)


def test_the_first_frame_paints_before_any_key_is_pressed() -> None:
    """The opening frame comes from Live.__enter__, not from a live.update.

    A widget that only painted after the first keypress would look like a hang.
    """
    buffer = io.StringIO()
    widgets.select("Model provider", CHOICES, read=keys(ENTER), console=painting_console(buffer))
    painted = buffer.getvalue()
    assert "Model provider" in painted
    assert "Anthropic" in painted


def test_the_frame_shows_the_hint_and_the_border() -> None:
    buffer = io.StringIO()
    widgets.select("Model provider", CHOICES, read=keys(ENTER), console=painting_console(buffer))
    painted = buffer.getvalue()
    assert "enter select" in painted
    assert "╭" in painted


def test_the_widget_is_erased_and_replaced_by_its_record() -> None:
    """Transient teardown: the answered widget must not stay in scrollback.

    Rich erases by emitting cursor-up + erase-line sequences, so their presence
    is the observable proof the frame was taken back down.
    """
    buffer = io.StringIO()
    widgets.select("Model provider", CHOICES, read=keys(ENTER), console=painting_console(buffer))
    painted = buffer.getvalue()
    assert "\x1b[2K" in painted  # erase-line: the frame was torn down
    assert painted.rstrip().endswith("Anthropic\x1b[0m")  # ...and the record is what remains


def test_a_checkbox_paints_its_markers() -> None:
    buffer = io.StringIO()
    rows = [Choice("a", "filesystem", checked=True), Choice("b", "terminal")]
    widgets.checkbox("Tools", rows, read=keys(ENTER), console=painting_console(buffer))
    painted = buffer.getvalue()
    assert "◉" in painted
    assert "○" in painted


def test_a_secret_never_paints_its_value_through_the_live_path() -> None:
    """The masking must hold where it actually matters — on a real terminal."""
    buffer = io.StringIO()
    widgets.secret("Token", read=keys(*"hunter2", ENTER), console=painting_console(buffer))
    painted = buffer.getvalue()
    assert "hunter2" not in painted
    assert "•" in painted
