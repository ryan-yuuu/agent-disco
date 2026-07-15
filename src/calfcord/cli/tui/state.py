"""Cursor + selection state for the list widgets.

Pure: no terminal, no Rich, no I/O. Keeping the logic here means wrap-around
navigation, pre-checking, and selection order are unit-testable directly, and
:mod:`calfcord.cli.tui.widgets` is left with only rendering and the key loop.
"""

from __future__ import annotations

from calfcord.cli._prompts import Choice

# Fallback for direct construction (and tests). Production never reaches it —
# the widgets measure the terminal via ``widgets.viewport_for``, whose docstring
# explains why any fixed count is wrong in both directions. Kept only so this
# module stays standalone-constructible without importing the render layer.
DEFAULT_VIEWPORT = 10


class ListState:
    """Shared scrolling cursor over a non-empty choice list."""

    def __init__(self, choices: list[Choice], *, viewport: int = DEFAULT_VIEWPORT) -> None:
        # An empty list renders a prompt with nothing to answer, which would hang
        # the operator with no way forward but Ctrl-C. Callers building choices
        # dynamically (models fetched from a provider, servers read from
        # mcp.json) can produce one, so reject it here — loudly and at
        # construction — rather than at paint time.
        if not choices:
            raise ValueError("a prompt needs at least one choice")
        # ``value`` is a primary key: CheckboxState's ``_checked`` is a set of
        # values and ``selected`` filters by membership, so two rows sharing a
        # value toggle as one and emit that value TWICE — into an agent's
        # persisted ``tools:`` list, with a checkmark on a row nobody touched.
        # Callers keep duplicates out today (unique registry keys, a set union
        # over MCP servers, agent_tools' ``current - offered``), which is exactly
        # why this belongs here instead: that care lives far from the type that
        # depends on it, and the next choice source would inherit no guardrail.
        if len({c.value for c in choices}) != len(choices):
            raise ValueError("choice values must be unique")
        self.choices = choices
        self.cursor = 0
        self.viewport = max(1, viewport)
        self._offset = 0

    def up(self) -> None:
        self.cursor = (self.cursor - 1) % len(self.choices)
        self._scroll_to_cursor()

    def down(self) -> None:
        self.cursor = (self.cursor + 1) % len(self.choices)
        self._scroll_to_cursor()

    def resize(self, viewport: int) -> None:
        """Change visible capacity while keeping the active row on screen."""
        self.viewport = max(1, viewport)
        self._scroll_to_cursor()

    def _scroll_to_cursor(self) -> None:
        """Shift the window the minimum needed to keep the cursor inside it.

        Minimum-shift rather than re-centring: it keeps the list visually still
        while the cursor moves through the middle, which is what makes scanning a
        long list feel steady. The two branches also cover the wrap-around jumps
        (last→first, first→last), which move the cursor by more than one row and
        would otherwise strand the window at the far end of the list.
        """
        if self.cursor < self._offset:
            self._offset = self.cursor
        elif self.cursor >= self._offset + self.viewport:
            self._offset = self.cursor - self.viewport + 1
        self._offset = max(0, min(self._offset, len(self.choices) - self.viewport))

    def window(self) -> tuple[int, int]:
        """The ``(start, stop)`` slice of :attr:`choices` currently on screen.

        A list shorter than the viewport returns its whole extent — the window
        never pads past the end, so a 2-row prompt draws a 2-row panel rather
        than a 10-row one with eight blank lines.
        """
        return self._offset, min(self._offset + self.viewport, len(self.choices))

    @property
    def scrolled(self) -> bool:
        """True when the list is taller than its viewport, so rows are hidden."""
        return len(self.choices) > self.viewport


class SelectState(ListState):
    """Single-choice cursor.

    ``default`` names the :attr:`Choice.value` to start on. An unrecognized
    default falls back to the first row rather than raising: the value often
    comes from stored config that may have gone stale, and a stale value must
    not make the command uncallable.
    """

    def __init__(self, choices: list[Choice], *, default: str | None = None, viewport: int = DEFAULT_VIEWPORT) -> None:
        super().__init__(choices, viewport=viewport)
        if default is not None:
            self.cursor = next((i for i, c in enumerate(choices) if c.value == default), 0)
            # Scroll the default into view at construction: a stored value far
            # down a long list (a model slug, an agent name) must be visible when
            # the prompt opens, not hidden below the fold with the cursor on it.
            self._scroll_to_cursor()

    @property
    def value(self) -> str:
        return self.choices[self.cursor].value


class CheckboxState(ListState):
    """Multi-select cursor, pre-checked from :attr:`Choice.checked`."""

    def __init__(self, choices: list[Choice], *, viewport: int = DEFAULT_VIEWPORT) -> None:
        super().__init__(choices, viewport=viewport)
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
