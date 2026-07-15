"""Prompt seam for the interactive CLI commands.

The flows in :mod:`calfcord.cli.init` and the ``agent tools`` editor
(:mod:`calfcord.cli.agent_tools`) must be unit-testable without a TTY, so they
never touch a prompting library directly. Instead they take a
:class:`Prompter` — a small Protocol covering the six prompt shapes we use
(single-select, free text, masked secret, yes/no, multi-select checkbox, and a
press-Enter-to-continue pause). Tests inject a scripted fake; production injects
:class:`~calfcord.cli.tui.prompter.RichPrompter`.

This module holds the *contract* only — the Protocol, :class:`Choice`, and the
factory. The implementation lives in :mod:`calfcord.cli.tui`, imported lazily
inside :func:`make_prompter` so that merely importing this module (which the
argparse entry point does at startup, and which tests do) pulls in neither Rich
nor readchar, and needs no TTY. That lazy import also breaks the cycle: the TUI
imports :class:`Choice` from here.

Both ``select`` and ``checkbox`` take a ``list[Choice]`` — a named
``(value, label, checked)`` triple — rather than two unnamed tuple shapes, so a
label/value transposition is a type error rather than a silent UI bug.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable


class Choice(NamedTuple):
    """A selectable row for :meth:`Prompter.select` / :meth:`Prompter.checkbox`.

    ``value`` is what the prompter returns when the row is chosen; ``label`` is
    the human-readable text shown to the operator. ``checked`` pre-checks the
    row in a ``checkbox`` and is ignored by ``select`` (single-select has no
    pre-check concept). Naming the fields removes the value/label ordering
    ambiguity the two anonymous tuple shapes used to invite.
    """

    value: str
    label: str
    checked: bool = False


@runtime_checkable
class Prompter(Protocol):
    """The interactive operations the CLI flows depend on.

    A Protocol (not a base class) so a test fake satisfies it structurally and
    both interactive flows share the exact same seam. ``runtime_checkable`` lets
    callers/tests ``isinstance(obj, Prompter)`` as a cheap guard — it asserts the
    required methods are present, not that their signatures conform.
    """

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        """Single-choice select; returns the chosen :attr:`Choice.value` (``checked`` ignored)."""
        ...

    def text(self, message: str, *, default: str = "") -> str:
        """Free-text input. Returns the entered string (``default`` when the operator just hits enter)."""
        ...

    def secret(self, message: str) -> str:
        """Masked input for a secret. Returns ``""`` when the operator skips it."""
        ...

    def confirm(self, message: str, *, default: bool = False) -> bool:
        """Yes/no prompt. Returns the boolean answer."""
        ...

    def pause(self, message: str) -> None:
        """Blocking press-Enter-to-continue gate — an acknowledgment, not a choice.

        Use this (not :meth:`confirm`) when the flow just needs the operator to do
        something out-of-band and signal ready; there is no yes/no to record, so a
        ``(Y/n)`` would imply a decision that does not exist. Returns nothing.
        """
        ...

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        """Multi-select; returns the selected :attr:`Choice.value`s (``checked`` pre-checks a row)."""
        ...


def make_prompter() -> Prompter:
    """Return the production prompter — the Rich TUI.

    A factory (rather than instantiating at import time) keeps the heavy imports
    off the argparse startup path, gives every flow one place to swap the backend,
    and breaks the import cycle with :mod:`calfcord.cli.tui`, which depends on
    :class:`Choice` from this module.
    """
    from calfcord.cli.tui.prompter import RichPrompter

    return RichPrompter()
