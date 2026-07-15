"""Cursor + selection state for the list widgets.

Pure: no terminal, no Rich, no I/O. Keeping the logic here means wrap-around
navigation, pre-checking, and selection order are unit-testable directly, and
:mod:`calfcord.cli.tui.widgets` is left with only rendering and the key loop.
"""

from __future__ import annotations

from calfcord.cli._prompts import Choice


class _ListState:
    """Shared cursor over a non-empty choice list."""

    def __init__(self, choices: list[Choice]) -> None:
        # An empty list renders a prompt with nothing to answer, which would hang
        # the operator with no way forward but Ctrl-C. Callers building choices
        # dynamically (models fetched from a provider, servers read from
        # mcp.json) can produce one, so reject it here — loudly and at
        # construction — rather than at paint time.
        if not choices:
            raise ValueError("a prompt needs at least one choice")
        self.choices = choices
        self.cursor = 0

    def up(self) -> None:
        self.cursor = (self.cursor - 1) % len(self.choices)

    def down(self) -> None:
        self.cursor = (self.cursor + 1) % len(self.choices)


class SelectState(_ListState):
    """Single-choice cursor.

    ``default`` names the :attr:`Choice.value` to start on. An unrecognized
    default falls back to the first row rather than raising: the value often
    comes from stored config that may have gone stale, and a stale value must
    not make the command uncallable.
    """

    def __init__(self, choices: list[Choice], *, default: str | None = None) -> None:
        super().__init__(choices)
        if default is not None:
            self.cursor = next((i for i, c in enumerate(choices) if c.value == default), 0)

    @property
    def value(self) -> str:
        return self.choices[self.cursor].value


class CheckboxState(_ListState):
    """Multi-select cursor, pre-checked from :attr:`Choice.checked`."""

    def __init__(self, choices: list[Choice]) -> None:
        super().__init__(choices)
        self._checked = {c.value for c in choices if c.checked}

    def toggle(self) -> None:
        value = self.choices[self.cursor].value
        self._checked.symmetric_difference_update({value})

    def is_checked(self, value: str) -> bool:
        return value in self._checked

    @property
    def selected(self) -> list[str]:
        """Checked values in CHOICE order, never toggle order.

        Callers persist this straight into an agent's ``tools:`` list, so a
        stable order keeps the rendered ``.md`` diff clean across edits.
        """
        return [c.value for c in self.choices if c.value in self._checked]
