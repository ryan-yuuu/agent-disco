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
