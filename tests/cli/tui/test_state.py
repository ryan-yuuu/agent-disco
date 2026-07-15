"""Pure navigation/selection reducers — the widget logic, with no terminal."""

from __future__ import annotations

import pytest

from calfcord.cli._prompts import Choice
from calfcord.cli.tui.state import CheckboxState, SelectState

CHOICES = [
    Choice("a", "Anthropic"),
    Choice("o", "OpenAI"),
    Choice("c", "Codex"),
]


class TestSelectState:
    def test_starts_on_the_first_row_without_a_default(self) -> None:
        assert SelectState(CHOICES).value == "a"

    def test_starts_on_the_default_row(self) -> None:
        assert SelectState(CHOICES, default="c").value == "c"

    def test_an_unknown_default_falls_back_to_the_first_row(self) -> None:
        """Callers pass a stored value that may no longer exist in the choices.

        Falling back beats raising: a stale ``.env`` value must not make the
        command uncallable.
        """
        assert SelectState(CHOICES, default="gone").value == "a"

    def test_down_advances(self) -> None:
        s = SelectState(CHOICES)
        s.down()
        assert s.value == "o"

    def test_down_wraps_from_the_last_row_to_the_first(self) -> None:
        s = SelectState(CHOICES, default="c")
        s.down()
        assert s.value == "a"

    def test_up_wraps_from_the_first_row_to_the_last(self) -> None:
        s = SelectState(CHOICES)
        s.up()
        assert s.value == "c"

    def test_empty_choices_is_rejected(self) -> None:
        """An empty list renders an unanswerable prompt, so fail loudly here.

        InquirerPy crashed obscurely on this and ``_providers.pick_model``
        carries a defensive guard because of it.
        """
        with pytest.raises(ValueError, match="at least one choice"):
            SelectState([])


class TestChoiceValuesAreUnique:
    """``value`` is a primary key here, so a duplicate corrupts silently.

    ``CheckboxState`` keys everything off ``Choice.value``: ``_checked`` is a set
    of values, and ``selected`` filters by membership. Two rows sharing a value
    therefore toggle as one and emit that value TWICE — straight into the agent's
    persisted ``tools:`` list, with the checkmark appearing on a row the operator
    never touched.

    No caller produces duplicates today: ``_build_choices`` keeps them out with
    unique registry keys, a set union over MCP servers, and the ``current -
    offered`` subtraction at agent_tools.py. That is the problem — the invariant
    lives 100 lines from the type that depends on it, and a fifth choice source
    added later has no guardrail. Enforce it where it is relied upon.
    """

    def test_duplicate_values_are_rejected(self) -> None:
        rows = [Choice("mcp/x", "from the server list"), Choice("mcp/x", "kept row")]
        with pytest.raises(ValueError, match="unique"):
            CheckboxState(rows)

    def test_duplicate_values_are_rejected_for_select_too(self) -> None:
        """``select`` returns a value; two rows sharing one make the answer ambiguous."""
        with pytest.raises(ValueError, match="unique"):
            SelectState([Choice("a", "One"), Choice("a", "Two")])

    def test_distinct_values_with_identical_labels_are_fine(self) -> None:
        """Labels are cosmetic — only the value is the key."""
        assert CheckboxState([Choice("a", "same"), Choice("b", "same")]).selected == []


class TestViewport:
    """A list taller than the terminal must scroll, not overflow it.

    Rich's Live renders content taller than the screen anyway (vertical_overflow
    defaults to "visible"), so the terminal scrolls and Live's transient teardown
    cannot erase the lines that scrolled off — leaving wreckage behind and
    repainting the whole list on every keypress. InquirerPy paged its lists, so
    shipping without a viewport would be a REGRESSION, not just a rough edge.
    ``disco agent tools`` reaches it: a row per builtin, per MCP server, and per
    live MCP tool.
    """

    def _rows(self, count: int) -> list[Choice]:
        return [Choice(f"v{i}", f"row {i}") for i in range(count)]

    def test_a_short_list_is_shown_whole(self) -> None:
        assert SelectState(self._rows(3), viewport=10).window() == (0, 3)

    def test_a_long_list_is_capped_to_the_viewport(self) -> None:
        assert SelectState(self._rows(30), viewport=10).window() == (0, 10)

    def test_the_window_follows_the_cursor_down(self) -> None:
        state = SelectState(self._rows(30), viewport=10)
        for _ in range(12):
            state.down()
        start, stop = state.window()
        assert start <= state.cursor < stop

    def test_the_window_follows_the_cursor_back_up(self) -> None:
        state = SelectState(self._rows(30), viewport=10)
        for _ in range(20):
            state.down()
        for _ in range(15):
            state.up()
        start, stop = state.window()
        assert start <= state.cursor < stop

    def test_the_cursor_stays_visible_after_wrapping_to_the_end(self) -> None:
        """Wrapping up from row 0 jumps to the last row — the window must follow."""
        state = SelectState(self._rows(30), viewport=10)
        state.up()
        start, stop = state.window()
        assert state.cursor == 29
        assert start <= state.cursor < stop

    def test_the_cursor_stays_visible_after_wrapping_to_the_start(self) -> None:
        state = SelectState(self._rows(30), viewport=10)
        for _ in range(30):
            state.down()
        start, stop = state.window()
        assert state.cursor == 0
        assert start <= state.cursor < stop

    def test_the_window_never_runs_past_the_end(self) -> None:
        state = SelectState(self._rows(12), viewport=10)
        for _ in range(11):
            state.down()
        start, stop = state.window()
        assert stop <= 12
        assert stop - start == 10

    def test_a_viewport_larger_than_the_list_does_not_pad(self) -> None:
        assert SelectState(self._rows(2), viewport=10).window() == (0, 2)

    def test_a_default_deep_in_a_long_list_opens_on_screen(self) -> None:
        """The saved value must be visible on open, not somewhere below the fold.

        ``agent edit`` and ``_providers.pick_model`` pass a stored default into a
        list that can be far longer than the viewport; opening scrolled to the top
        would show the operator a cursor they cannot see.
        """
        state = SelectState(self._rows(30), default="v25", viewport=10)
        start, stop = state.window()
        assert state.cursor == 25
        assert start <= state.cursor < stop

    def test_the_checkbox_scrolls_too(self) -> None:
        state = CheckboxState(self._rows(30), viewport=10)
        for _ in range(15):
            state.down()
        start, stop = state.window()
        assert start <= state.cursor < stop


class TestCheckboxState:
    def test_preselects_rows_marked_checked(self) -> None:
        rows = [Choice("a", "A", checked=True), Choice("b", "B"), Choice("c", "C", checked=True)]
        assert CheckboxState(rows).selected == ["a", "c"]

    def test_nothing_is_selected_when_no_row_is_checked(self) -> None:
        assert CheckboxState(CHOICES).selected == []

    def test_toggle_checks_the_row_under_the_cursor(self) -> None:
        s = CheckboxState(CHOICES)
        s.toggle()
        assert s.selected == ["a"]

    def test_toggle_is_reversible(self) -> None:
        rows = [Choice("a", "A", checked=True)]
        s = CheckboxState(rows)
        s.toggle()
        assert s.selected == []

    def test_selected_follows_choice_order_not_toggle_order(self) -> None:
        """Callers persist this list; a stable order keeps diffs of the .md clean."""
        s = CheckboxState(CHOICES)
        s.down()
        s.down()
        s.toggle()  # c
        s.up()
        s.up()
        s.toggle()  # a
        assert s.selected == ["a", "c"]

    def test_empty_choices_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one choice"):
            CheckboxState([])
