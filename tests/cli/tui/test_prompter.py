"""``RichPrompter`` — the production :class:`Prompter` the CLI flows receive.

The Protocol is the contract every interactive command already depends on, so
these tests pin conformance and the delegation, not the widget internals (those
are covered in ``test_widgets.py``).
"""

from __future__ import annotations

import pytest
import readchar

from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli.tui.prompter import RichPrompter
from calfcord.cli.tui.render import make_console

from .test_widgets import ENTER, FakeEditor, keys

CHOICES = [Choice("a", "Anthropic"), Choice("o", "OpenAI")]


def prompter(*sequence: str, answer: str = "") -> RichPrompter:
    """A prompter wired to a scripted keyboard and a console that paints nowhere."""
    return RichPrompter(
        read=keys(*sequence),
        editor=FakeEditor(answer),
        console=make_console(width=60, record=True),
    )


def test_rich_prompter_satisfies_the_protocol() -> None:
    """The 7 interactive commands type-depend on this and nothing more."""
    assert isinstance(RichPrompter(), Prompter)


def test_select_returns_the_chosen_value() -> None:
    assert prompter(readchar.key.DOWN, ENTER).select("Provider", CHOICES) == "o"


def test_select_honours_the_default() -> None:
    assert prompter(ENTER).select("Provider", CHOICES, default="o") == "o"


def test_text_returns_the_typed_value() -> None:
    assert prompter(answer="hi").text("Name") == "hi"


def test_text_returns_the_default_on_a_bare_enter() -> None:
    assert prompter(answer="scribe").text("Name", default="scribe") == "scribe"


def test_secret_returns_empty_when_skipped() -> None:
    assert prompter(answer="").secret("Token") == ""


def test_confirm_returns_the_answer() -> None:
    assert prompter("y").confirm("Start now?") is True


def test_checkbox_returns_the_selected_values() -> None:
    assert prompter(" ", ENTER).checkbox("Tools", CHOICES) == ["a"]


class TestPause:
    """``pause`` stays a bare ``input()`` — deliberately not a widget.

    A press-Enter gate needs no rendering and no key loop, and keeping it on
    ``input()`` preserves the contract ``tests/cli/test_prompts.py`` pins.
    """

    def test_waits_on_input_and_discards_the_typed_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr("builtins.input", lambda prompt="": seen.append(prompt) or "typed")
        assert RichPrompter().pause("press Enter…") is None
        assert seen == ["press Enter…"]

    def test_swallows_eof_and_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The finish flow must never crash after the workspace is already live."""

        def _eof(_prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        assert RichPrompter().pause("press Enter…") is None
