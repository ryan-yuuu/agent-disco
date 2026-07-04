"""Tests for the production :class:`InquirerPrompter` behaviours that don't need a TTY.

Most ``InquirerPrompter`` methods are thin lazy-import wrappers over InquirerPy and
are exercised through the injected fake seam (see the per-command ``FakePrompter``s),
not here. ``pause`` is the exception: it is a plain ``input()`` gate with no InquirerPy
dependency, so its non-interactive behaviour is unit-testable directly.
"""

from __future__ import annotations

import pytest

from calfcord.cli._prompts import InquirerPrompter


def test_pause_swallows_eof_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed / non-interactive stdin (Ctrl-D) at the press-Enter gate must not
    crash. ``input()`` raises ``EOFError`` there, but ``pause`` degrades to
    "acknowledged" so the finish flow never aborts *after* the workspace is already
    up (matching ``_await_presence``'s never-crash-once-live contract)."""

    def _eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert InquirerPrompter().pause("press Enter…") is None  # must not raise


def test_pause_waits_on_input_and_discards_the_typed_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pause`` blocks on ``input`` with the given message and discards the answer —
    it is an acknowledgment gate, not a value prompt."""
    seen: list[str] = []

    def _fake_input(prompt: str = "") -> str:
        seen.append(prompt)
        return "whatever the operator typed"

    monkeypatch.setattr("builtins.input", _fake_input)

    assert InquirerPrompter().pause("hello?") is None
    assert seen == ["hello?"]
