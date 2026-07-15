"""The widgets: their key loops and what they paint.

Every widget takes ``read`` — the one input seam — so the loops are driven by a
scripted key list with no TTY anywhere. Rendering is a pure function of state, so
what the operator sees is asserted directly rather than scraped off a terminal.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import readchar
from rich.console import Console

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


class TestViewportFollowsTheTerminal:
    """The viewport is measured, not assumed.

    A fixed 10 rows crops anyway in a short tmux pane — the exact failure the
    viewport exists to prevent — and wastes two thirds of a tall terminal.
    """

    def _rows(self, count: int) -> list[Choice]:
        return [Choice(f"v{i}", f"row_{i}") for i in range(count)]

    def test_a_short_terminal_gets_a_small_viewport(self) -> None:
        assert widgets.viewport_for(make_console(width=60)) > 0

    def test_the_panel_fits_inside_a_short_terminal(self) -> None:
        """The whole point: the frame must not be taller than the screen."""
        console = Console(width=60, height=12)
        state = SelectState(self._rows(40), viewport=widgets.viewport_for(console))
        painted = paint(widgets.select_panel("Pick", state))
        assert len(painted.rstrip("\n").splitlines()) <= 12

    def test_a_tall_terminal_shows_more_rows_than_a_short_one(self) -> None:
        tall = widgets.viewport_for(Console(width=60, height=50))
        short = widgets.viewport_for(Console(width=60, height=12))
        assert tall > short

    def test_a_tiny_terminal_still_shows_at_least_one_row(self) -> None:
        """Never zero or negative — a list with nothing visible is unanswerable."""
        assert widgets.viewport_for(Console(width=60, height=3)) >= 1


class TestScrolling:
    """A long list paints only its window, and says what it is hiding."""

    def _rows(self, count: int) -> list[Choice]:
        return [Choice(f"v{i}", f"row_{i}") for i in range(count)]

    def test_only_the_window_is_painted(self) -> None:
        """The whole point: 40 rows must not paint 40 lines into a 24-line terminal."""
        out = paint(widgets.select_panel("Pick", SelectState(self._rows(40), viewport=10)))
        assert "row_0" in out
        assert "row_39" not in out

    def test_a_short_list_shows_no_scroll_markers(self) -> None:
        out = paint(widgets.select_panel("Pick", SelectState(self._rows(3), viewport=10)))
        assert "more" not in out

    def test_a_long_list_reports_what_is_hidden_below(self) -> None:
        out = paint(widgets.select_panel("Pick", SelectState(self._rows(40), viewport=10)))
        assert "30 more" in out

    def test_scrolling_down_reports_what_is_hidden_above(self) -> None:
        state = SelectState(self._rows(40), viewport=10)
        for _ in range(15):
            state.down()
        assert "6 more" in paint(widgets.select_panel("Pick", state))

    def test_the_cursor_row_is_painted_after_scrolling(self) -> None:
        state = SelectState(self._rows(40), viewport=10)
        for _ in range(15):
            state.down()
        assert "row_15" in paint(widgets.select_panel("Pick", state))

    def test_the_panel_height_is_steady_across_the_ends(self) -> None:
        """A frame that changes height as the cursor moves reads as a jump."""
        state = SelectState(self._rows(40), viewport=10)
        top = len(paint(widgets.select_panel("Pick", state)).splitlines())
        for _ in range(39):
            state.down()
        bottom = len(paint(widgets.select_panel("Pick", state)).splitlines())
        assert top == bottom


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

    def test_down_moves_before_toggling(self) -> None:
        """Navigation in a CHECKBOX, not just a select — its own key loop, its own bug.

        Without this, a step() that called down() for both arrows (or dropped
        navigation entirely) would ship: every other checkbox test toggles row 0,
        where a broken cursor is invisible.
        """
        assert widgets.checkbox("Tools", CHOICES, read=keys(DOWN, SPACE, ENTER), console=silent()) == ["o"]

    def test_up_moves_before_toggling(self) -> None:
        assert widgets.checkbox("Tools", CHOICES, read=keys(UP, SPACE, ENTER), console=silent()) == ["c"]

    def test_several_rows_can_be_toggled_in_one_pass(self) -> None:
        got = widgets.checkbox(
            "Tools", CHOICES, read=keys(SPACE, DOWN, DOWN, SPACE, ENTER), console=silent()
        )
        assert got == ["a", "c"]

    def test_enter_with_nothing_checked_returns_empty(self) -> None:
        assert widgets.checkbox("Tools", CHOICES, read=keys(ENTER), console=silent()) == []

    def test_prechecked_rows_survive_an_immediate_enter(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B")]
        assert widgets.checkbox("Tools", rows, read=keys(ENTER), console=silent()) == ["a"]

    def test_space_unchecks_a_prechecked_row(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B")]
        assert widgets.checkbox("Tools", rows, read=keys(SPACE, ENTER), console=silent()) == []

    def test_the_instruction_is_shown_when_given(self) -> None:
        """The Protocol declares ``instruction``, so it must not vanish silently.

        A parameter a widget accepts and drops is a lie to the next caller: they
        pass guidance, see nothing, and have no way to tell it was ignored.
        """
        out = paint(widgets.checkbox_panel("Tools", CheckboxState(CHOICES), instruction="pick carefully"))
        assert "pick carefully" in out

    def test_no_instruction_line_when_none_is_given(self) -> None:
        """The hint already states the mechanics; an empty line would be noise."""
        out = paint(widgets.checkbox_panel("Tools", CheckboxState(CHOICES)))
        assert out.count("\n") == paint(widgets.select_panel("Tools", SelectState(CHOICES))).count("\n")

    def test_panel_distinguishes_checked_from_unchecked(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B")]
        out = paint(widgets.checkbox_panel("Tools", CheckboxState(rows)))
        assert "◉ A" in out
        assert "○ B" in out


class TestTypingRealProse:
    """Space is TEXT in a text field, not a command.

    The whole suite typed single non-space characters ("a", "b", "x"), so a space
    binding that swallowed the key passed every test while making prose prompts
    unusable: "Agent description:" and mcp add's "Command (e.g. npx -y ...)" both
    take input where spaces are the point. shlex.split("npx-ypkg") yields one
    garbage token, so a swallowed space silently produces a broken MCP server.
    """

    def test_a_space_is_typed_into_a_text_field(self) -> None:
        assert widgets.text("Desc", read=keys(*"hello world", ENTER), console=silent()) == "hello world"

    def test_a_command_line_with_flags_survives(self) -> None:
        got = widgets.text("Command", read=keys(*"npx -y pkg", ENTER), console=silent())
        assert got == "npx -y pkg"

    def test_a_space_is_typed_into_a_secret(self) -> None:
        """A mangled token fails auth with no hint as to why."""
        assert widgets.secret("Token", read=keys(*"a b", ENTER), console=silent()) == "a b"

    def test_leading_and_trailing_spaces_are_preserved_for_the_caller(self) -> None:
        """Callers .strip() themselves; the widget must not decide for them."""
        assert widgets.text("Desc", read=keys(*" hi ", ENTER), console=silent()) == " hi "


class TestLongLabels:
    """A label longer than the panel must wrap, never be cut.

    Real builtin tool descriptions run to 88 characters — 5 of the 11 overflow an
    80-column panel — so a cut label loses the end of the sentence that says what
    the tool does, in the very prompt that asks you to choose it.
    """

    LONG = "execute_code — Run a Python script that can call Hermes tools programmatically"

    def _paint80(self, renderable) -> str:
        console = make_console(width=80, record=True)
        console.print(renderable)
        return console.export_text()

    def test_a_long_choice_label_is_not_truncated(self) -> None:
        out = self._paint80(widgets.select_panel("Tools", SelectState([Choice("x", self.LONG)])))
        assert "programmatically" in out

    def test_a_long_checkbox_label_is_not_truncated(self) -> None:
        out = self._paint80(widgets.checkbox_panel("Tools", CheckboxState([Choice("x", self.LONG)])))
        assert "programmatically" in out


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

    def test_arrow_keys_do_not_land_in_the_value(self) -> None:
        """Operators press arrows in text fields constantly.

        readchar delivers UP as the three-byte "\\x1b[A". Appending the raw string
        would put an escape sequence inside the agent name and write it to disk.
        """
        got = widgets.text("Name", read=keys("a", UP, DOWN, "b", ENTER), console=silent())
        assert got == "ab"

    def test_unbound_escape_sequences_do_not_land_in_the_value(self) -> None:
        """F1 and friends resolve to no Key at all, and are not printable text."""
        got = widgets.text("Name", read=keys("a", "\x1bOP", ENTER), console=silent())
        assert got == "a"

    def test_arrow_keys_do_not_land_in_a_secret(self) -> None:
        got = widgets.secret("Token", read=keys("s", UP, "3", ENTER), console=silent())
        assert got == "s3"

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
