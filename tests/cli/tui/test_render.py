"""The output surface — and the two rules that keep it safe to print through.

The interactive flows print operator-facing prose that already contains
bracketed and ``$``-sigil text, and ~235 existing tests substring-match that
prose. Both rules below exist to keep Rich from silently rewriting it.
"""

from __future__ import annotations

from calfcord.cli.tui import render


def _render(fn, *args, width: int = 40, **kwargs) -> str:
    """Drive a render helper through a recording console and return its text."""
    console = render.make_console(width=width, record=True)
    fn(*args, console=console, **kwargs)
    return console.export_text()


def test_line_does_not_interpret_square_brackets_as_markup() -> None:
    """``[bold]`` in operator prose is literal text, not a Rich style tag.

    Real messages carry bracketed content; letting Rich parse it would delete
    the brackets and silently restyle the line.
    """
    assert "[bold]" in _render(render.line, "keep [bold] literal")


def test_line_does_not_wrap_long_text() -> None:
    """Wrapping would bisect the phrases the existing CLI tests match on.

    The console here is deliberately narrower than the message.
    """
    message = "the bot can post in 2 channel(s): #general, #dev — everything is fine"
    assert message in _render(render.line, message, width=20)


def test_line_leaves_channel_and_variable_sigils_alone() -> None:
    out = _render(render.line, "set $CALF_HOST_URL for #general")
    assert "$CALF_HOST_URL" in out
    assert "#general" in out


def test_note_emits_its_text() -> None:
    assert "just so you know" in _render(render.note, "just so you know")


def test_success_marks_the_line_with_a_check() -> None:
    out = _render(render.success, "scribe is online")
    assert "scribe is online" in out
    assert "✓" in out


def test_error_emits_its_text() -> None:
    assert "could not create agent" in _render(render.error, "could not create agent")


def test_header_shows_the_title_and_subtitle() -> None:
    out = _render(render.header, "disco init", subtitle="Create your agent")
    assert "disco init" in out
    assert "Create your agent" in out


def test_header_shows_step_progress_when_given() -> None:
    out = _render(render.header, "disco init", step=(1, 4), label="agent", width=60)
    assert "1/4" in out
    assert "agent" in out


def test_header_omits_step_progress_when_not_given() -> None:
    """Single-shot commands (agent tools, mcp add) have no phases to count."""
    assert "/" not in _render(render.header, "disco agent tools", width=60)


def test_header_shows_step_progress_without_a_label() -> None:
    """A phase can be counted without being named."""
    assert "2/4" in _render(render.header, "disco init", step=(2, 4), width=60)


class TestSharedConsole:
    def test_console_is_memoised(self) -> None:
        """One console for the process — a second would re-detect the terminal
        and could disagree with the first about width or colour."""
        render._console = None
        try:
            assert render.console() is render.console()
        finally:
            render._console = None

    def test_the_shared_console_does_not_wrap_or_highlight(self) -> None:
        """The production console must carry the same guarantees the helpers rely on."""
        render._console = None
        try:
            console = render.console()
            assert console.soft_wrap is True
            assert console._highlight is False
        finally:
            render._console = None


def test_answer_records_the_label_and_value() -> None:
    """The one-line record a widget collapses to once it is answered."""
    out = _render(render.answer, "Model provider", "Anthropic")
    assert "Model provider" in out
    assert "Anthropic" in out


class TestAnswerHierarchy:
    """The chosen value is the point of the record — it must not come out muted.

    A ``Text`` built with a base style passes that style down to every appended
    span, so a dim base silently dims the value too, however it is styled on
    append. In a monochrome design that erases the only signal the record has.
    """

    def _style_of(self, needle: str):
        console = render.make_console(width=60)
        for segment in console.render(render.answer_text("Model provider", "Anthropic")):
            if segment.text and needle in segment.text and segment.style is not None:
                return segment.style
        raise AssertionError(f"nothing rendered for {needle!r}")

    def test_the_value_is_not_dimmed(self) -> None:
        assert self._style_of("Anthropic").dim is not True

    def test_the_value_is_emphasised(self) -> None:
        assert self._style_of("Anthropic").bold is True

    def test_the_label_stays_subordinate_to_the_value(self) -> None:
        assert self._style_of("Model provider").dim is True
