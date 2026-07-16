"""The output surface — and the two rules that keep it safe to print through.

The interactive flows print operator-facing prose that already contains
bracketed and ``$``-sigil text, and ~235 existing tests substring-match that
prose. Both rules below exist to keep Rich from silently rewriting it.
"""

from __future__ import annotations

import contextlib
import io
import time

from rich.console import Console

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

    def test_the_shared_console_does_not_highlight(self) -> None:
        """Rich's highlighter would colour numbers and paths inside plain prose."""
        render._console = None
        try:
            assert render.console()._highlight is False
        finally:
            render._console = None

    def test_the_shared_console_does_not_force_soft_wrap(self) -> None:
        """soft_wrap belongs per-call, not on the console.

        As a constructor default it applies no_wrap to EVERY render, and the
        option propagates into a Panel's children — cutting long choice labels at
        the panel edge instead of wrapping them. The prose helpers opt in per
        call; the widgets must be free to wrap.
        """
        render._console = None
        try:
            assert render.console().soft_wrap is False
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


class TestStep:
    """``step`` — the completed-step record for a multi-step flow.

    The same two-column, glyph-led grammar ``answer`` uses and ``doctor``
    independently converged on (``✓ <name padded>  <detail>``), but with a caller-set
    label column so a *block* of consecutive records aligns. ``answer`` hard-codes a
    two-space gap, which is right for a record printed alone after a prompt and ragged
    for four printed together.
    """

    def test_renders_glyph_label_and_value(self) -> None:
        assert _render(render.step, "workspace", "broker + bridge") == "✓ workspace  broker + bridge\n"

    def test_pads_the_label_column_so_a_block_aligns(self) -> None:
        # Built here rather than via ``_render``: that helper's ``width`` is the
        # console's, and the label column is what's under test.
        console = render.make_console(width=40, record=True)
        render.step("tools", "up", width=9, console=console)
        assert console.export_text() == "✓ tools      up\n"

    def test_warn_and_fail_carry_their_own_glyph(self) -> None:
        assert _render(render.step, "agent", "not seen", status="warn").startswith("⚠ agent")
        assert _render(render.step, "tools", "not running", status="fail").startswith("✗ tools")

    def test_value_is_not_dimmed_by_the_label(self) -> None:
        """The value is the one thing the record exists to show.

        ``answer_text``'s docstring pins the same invariant: a base style on the
        ``Text`` is inherited by every appended span, so a dim base would render the
        value bold *and* dimmed — the outcome becoming the quietest thing on the line.
        """
        console = render.make_console(width=40, record=True)
        render.step("workspace", "broker + bridge", console=console)
        export = console.export_text(styles=True)
        assert "\x1b[1mbroker + bridge" in export  # bold, not "bold dim"

    def test_does_not_interpret_square_brackets_as_markup(self) -> None:
        assert "[bold]" in _render(render.step, "agent", "keep [bold] literal")


class TestPair:
    """``pair`` — a label/value row with no glyph.

    ``step``'s hierarchy minus the outcome mark, for rows that aren't outcomes: the
    "what next" block a flow signs off with. Sharing the padded two-column shape is
    what lets that block read as the same object as the record board above it.
    """

    def test_renders_label_and_value_without_a_glyph(self) -> None:
        assert _render(render.pair, "Learn more", "docs/using-disco.md") == "Learn more  docs/using-disco.md\n"

    def test_pads_the_label_column(self) -> None:
        console = render.make_console(width=60, record=True)
        render.pair("Try it", "!scribe hello", width=14, console=console)
        assert console.export_text() == "Try it          !scribe hello\n"

    def test_value_is_not_dimmed_by_the_label(self) -> None:
        console = render.make_console(width=60, record=True)
        render.pair("Try it", "!scribe hello", console=console)
        assert "\x1b[1m!scribe hello" in console.export_text(styles=True)

    def test_does_not_interpret_square_brackets_as_markup(self) -> None:
        assert "[bold]" in _render(render.pair, "x", "keep [bold] literal")


class TestWorking:
    """``working`` — a transient spinner for a step slow enough to look hung.

    The same arc the widgets already perform: a transient Live that is torn down and
    replaced by a durable one-line record. Progress is decoration; the record is the
    fact. So this renders nothing off-TTY by design, and the caller's ``step`` record
    prints either way.
    """

    def _paint(self, label: str = "waiting…") -> str:
        # A real StringIO with force_terminal: record=True cannot capture Live output
        # (see test_live_rendering.py, which pins the same constraint).
        buffer = io.StringIO()
        console = Console(file=buffer, width=60, force_terminal=True, highlight=False)
        with render.working(label, console=console):
            time.sleep(0.15)
        return buffer.getvalue()

    def test_paints_a_spinner_and_the_label_on_a_terminal(self) -> None:
        out = self._paint("waiting for scribe to come online…")
        assert "waiting for scribe to come online…" in out
        assert "⠋" in out  # the spinner actually animated

    def test_the_spinner_carries_no_hue(self) -> None:
        """Rich's default status spinner is GREEN — the theme is monochrome.

        `theme` permits exactly one colour, ERROR, and only for genuine failures; a
        waiting spinner is not one. Left at Rich's default this would have been the
        single hued glyph in the whole CLI, which is why the style is pinned rather
        than inherited.
        """
        out = self._paint()
        assert not any(code in out for code in ("\x1b[32m", "\x1b[33m", "\x1b[34m", "\x1b[35m", "\x1b[36m"))

    def test_renders_nothing_off_a_terminal(self) -> None:
        """A piped or CI run must not collect spinner frames.

        Live is inert off-TTY, which is what makes this safe — and is also why the
        caller must print its record OUTSIDE the spinner, or the step would vanish
        entirely from a captured log.
        """
        buffer = io.StringIO()
        console = Console(file=buffer, width=60, highlight=False)
        with render.working("waiting…", console=console):
            pass
        assert buffer.getvalue() == ""

    def test_the_body_still_runs_off_a_terminal(self) -> None:
        """Guards the vacuous pass: silence must mean "not painted", not "not run"."""
        ran = []
        buffer = io.StringIO()
        console = Console(file=buffer, width=60, highlight=False)
        with render.working("x", console=console):
            ran.append(True)
        assert ran == [True]

    def test_the_spinner_is_torn_down_on_a_raise(self) -> None:
        """A failure inside the step must not leave the terminal's cursor hidden."""
        buffer = io.StringIO()
        console = Console(file=buffer, width=60, force_terminal=True, highlight=False)
        with contextlib.suppress(RuntimeError), render.working("x", console=console):
            raise RuntimeError("boom")
        assert "\x1b[?25h" in buffer.getvalue()  # cursor restored
