"""The widgets: their key loops and what they paint.

Every widget takes ``read`` — the one input seam — so the loops are driven by a
scripted key list with no TTY anywhere. Rendering is a pure function of state, so
what the operator sees is asserted directly rather than scraped off a terminal.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import readchar

from calfcord.cli._prompts import Choice
from calfcord.cli.tui import widgets
from calfcord.cli.tui.render import make_console
from calfcord.cli.tui.state import CheckboxState, SelectState

CHOICES = [
    Choice("a", "Anthropic", ),
    Choice("o", "OpenAI"),
    Choice("c", "Codex"),
]

ENTER = "\r"
UP = readchar.key.UP
DOWN = readchar.key.DOWN
SPACE = " "


def keys(*sequence: str) -> Callable[[], str]:
    """A scripted stand-in for :func:`calfcord.cli.tui.keys.read_key`."""
    pending = iter(sequence)

    def _read() -> str:
        try:
            return next(pending)
        except StopIteration:  # pragma: no cover - a widget that over-reads is a bug
            raise AssertionError("widget read more keys than the test scripted") from None

    return _read


def silent():
    """A non-terminal console: Live paints nothing through it, so a widget under
    test emits no stray frames into pytest's captured output."""
    return make_console(width=60, record=True)


def paint(renderable) -> str:
    console = make_console(width=60, record=True)
    console.print(renderable)
    return console.export_text()


def style_of(renderable, needle: str):
    """The style Rich will actually paint ``needle`` with.

    ``export_text`` drops styling, so it cannot see a washed-out title. Reading
    the rendered segments is the only way to assert on emphasis — and in a
    monochrome design, emphasis carries all of the hierarchy.
    """
    for segment in make_console(width=60).render(renderable):
        if segment.text and needle in segment.text and segment.style is not None:
            return segment.style
    raise AssertionError(f"nothing rendered for {needle!r}")


class TestVisualHierarchy:
    """Monochrome means weight IS the hierarchy — so a washed-out title is a bug.

    Rich applies a Panel's ``border_style`` to its title, so a dim border silently
    dims the question with it. Nothing in an export_text assertion can see that.
    """

    def test_the_question_is_not_dimmed_by_the_border(self) -> None:
        assert style_of(widgets.select_panel("Model provider", SelectState(CHOICES)), "Model provider").dim is not True

    def test_the_question_is_emphasised(self) -> None:
        assert style_of(widgets.select_panel("Model provider", SelectState(CHOICES)), "Model provider").bold is True

    def test_the_hint_stays_subordinate(self) -> None:
        """The hint is reference text; it must never compete with the question."""
        assert style_of(widgets.select_panel("Model provider", SelectState(CHOICES)), "enter select").dim is True


class TestSelect:
    def test_enter_returns_the_row_under_the_cursor(self) -> None:
        assert widgets.select("Provider", CHOICES, read=keys(ENTER), console=silent()) == "a"

    def test_down_then_enter_returns_the_next_row(self) -> None:
        assert widgets.select("Provider", CHOICES, read=keys(DOWN, ENTER), console=silent()) == "o"

    def test_navigation_wraps(self) -> None:
        assert widgets.select("Provider", CHOICES, read=keys(UP, ENTER), console=silent()) == "c"

    def test_default_positions_the_cursor(self) -> None:
        got = widgets.select("Provider", CHOICES, default="o", read=keys(ENTER), console=silent())
        assert got == "o"

    def test_ctrl_d_raises_eof(self) -> None:
        """The CLI entry point maps EOFError onto a clean 'needs a terminal' exit."""
        with pytest.raises(EOFError):
            widgets.select("Provider", CHOICES, read=keys(readchar.key.CTRL_D), console=silent())

    def test_interrupt_from_the_reader_is_not_swallowed(self) -> None:
        """readchar raises KeyboardInterrupt on Ctrl-C; main() maps it to exit 130.

        A widget that caught it would break the resumable-Ctrl-C contract init
        teaches operators to rely on.
        """

        def _interrupt() -> str:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            widgets.select("Provider", CHOICES, read=_interrupt, console=silent())

    def test_panel_marks_the_cursor_row_and_only_that_row(self) -> None:
        out = paint(widgets.select_panel("Provider", SelectState(CHOICES)))
        assert "❯ Anthropic" in out  # noqa: RUF001
        assert "❯ OpenAI" not in out  # noqa: RUF001

    def test_panel_shows_the_message_and_every_label(self) -> None:
        out = paint(widgets.select_panel("Provider", SelectState(CHOICES)))
        assert "Provider" in out
        for choice in CHOICES:
            assert choice.label in out

    def test_panel_advertises_ctrl_c_and_never_esc(self) -> None:
        """Esc cannot be observed through readchar, so offering it would be a lie."""
        out = paint(widgets.select_panel("Provider", SelectState(CHOICES)))
        assert "ctrl-c" in out
        assert "esc" not in out.lower()


class TestMarkupIsNeverInterpreted:
    """Operator-supplied text must never be parsed as Rich markup.

    Agent names, tool descriptions, and MCP server names are arbitrary strings.
    Rich eats any ``[...]`` in them as a style tag, so an agent named
    ``[bot] ops`` would paint as `` ops`` — text silently deleted from a prompt
    the operator is trying to answer. Every label and message therefore goes
    through ``Text``, which does not parse markup.
    """

    def test_a_bracketed_choice_label_survives(self) -> None:
        rows = [Choice("b", "[bot] ops"), Choice("p", "plain")]
        assert "[bot] ops" in paint(widgets.select_panel("Agent", SelectState(rows)))

    def test_a_bracketed_message_survives(self) -> None:
        assert "Pick [one]" in paint(widgets.select_panel("Pick [one]", SelectState(CHOICES)))

    def test_a_bracketed_checkbox_label_survives(self) -> None:
        rows = [Choice("b", "[bot] ops", checked=True)]
        assert "[bot] ops" in paint(widgets.checkbox_panel("Tools", CheckboxState(rows)))

    def test_a_bracketed_text_default_survives(self) -> None:
        assert "[bot] ops" in paint(widgets.text_panel("Name", "", default="[bot] ops"))

    def test_a_bracketed_typed_value_survives(self) -> None:
        assert "[x]" in paint(widgets.text_panel("Name", "[x]"))


class TestCheckbox:
    def test_space_toggles_and_enter_confirms(self) -> None:
        got = widgets.checkbox("Tools", CHOICES, read=keys(SPACE, ENTER), console=silent())
        assert got == ["a"]

    def test_enter_with_nothing_checked_returns_empty(self) -> None:
        assert widgets.checkbox("Tools", CHOICES, read=keys(ENTER), console=silent()) == []

    def test_prechecked_rows_survive_an_immediate_enter(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B")]
        assert widgets.checkbox("Tools", rows, read=keys(ENTER), console=silent()) == ["a"]

    def test_space_unchecks_a_prechecked_row(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B")]
        assert widgets.checkbox("Tools", rows, read=keys(SPACE, ENTER), console=silent()) == []

    def test_panel_distinguishes_checked_from_unchecked(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B")]
        out = paint(widgets.checkbox_panel("Tools", CheckboxState(rows)))
        assert "◉ A" in out
        assert "○ B" in out


class TestText:
    def test_typed_characters_are_returned_on_enter(self) -> None:
        assert widgets.text("Name", read=keys("a", "b", ENTER), console=silent()) == "ab"

    def test_enter_on_an_empty_field_returns_the_default(self) -> None:
        """The wizard's press-Enter-to-accept contract depends on this."""
        assert widgets.text("Name", default="scribe", read=keys(ENTER), console=silent()) == "scribe"

    def test_typing_replaces_the_default(self) -> None:
        got = widgets.text("Name", default="scribe", read=keys("x", ENTER), console=silent())
        assert got == "x"

    def test_backspace_deletes_the_last_character(self) -> None:
        got = widgets.text("Name", read=keys("a", "b", readchar.key.BACKSPACE, ENTER), console=silent())
        assert got == "a"

    def test_backspace_on_an_empty_field_is_harmless(self) -> None:
        got = widgets.text("Name", read=keys(readchar.key.BACKSPACE, "a", ENTER), console=silent())
        assert got == "a"


class TestSecret:
    def test_returns_what_was_typed(self) -> None:
        assert widgets.secret("Token", read=keys("s", "3", ENTER), console=silent()) == "s3"

    def test_skipping_returns_empty_so_callers_keep_the_stored_value(self) -> None:
        """Every .env secret prompt treats '' as keep-what-is-there."""
        assert widgets.secret("Token", read=keys(ENTER), console=silent()) == ""

    def test_panel_never_paints_the_secret(self) -> None:
        out = paint(widgets.secret_panel("Token", "hunter2"))
        assert "hunter2" not in out
        assert "•" in out


class TestConfirm:
    @pytest.mark.parametrize(("pressed", "expected"), [("y", True), ("Y", True), ("n", False), ("N", False)])
    def test_y_and_n_answer_directly(self, pressed: str, expected: bool) -> None:
        assert widgets.confirm("Start now?", read=keys(pressed), console=silent()) is expected

    @pytest.mark.parametrize("default", [True, False])
    def test_enter_takes_the_default(self, default: bool) -> None:
        assert widgets.confirm("Start now?", default=default, read=keys(ENTER), console=silent()) is default

    def test_unrelated_keys_are_ignored_until_a_real_answer(self) -> None:
        """A stray keypress must not be read as consent."""
        assert widgets.confirm("Start now?", read=keys("q", "z", "y"), console=silent()) is True
