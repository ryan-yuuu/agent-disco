"""The prompt seam itself.

:class:`Choice` and the :class:`Prompter` Protocol live here and are what all 7
interactive commands depend on. The production implementation moved to
:mod:`calfcord.cli.tui.prompter` (and is tested under ``tests/cli/tui/``, where
``pause``'s ``input()``-gate behaviour is now pinned), so these tests cover only
the seam: that the factory hands back a conforming prompter.
"""

from __future__ import annotations

from calfcord.cli._prompts import Choice, Prompter, make_prompter
from calfcord.cli.tui.prompter import RichPrompter


def test_make_prompter_returns_a_conforming_prompter() -> None:
    assert isinstance(make_prompter(), Prompter)


def test_make_prompter_returns_the_rich_tui_prompter() -> None:
    """The production backend is the Rich TUI — InquirerPy is gone."""
    assert isinstance(make_prompter(), RichPrompter)


def test_choice_defaults_to_unchecked() -> None:
    """``checked`` pre-checks a checkbox row; ``select`` ignores it entirely."""
    assert Choice("v", "Label").checked is False
