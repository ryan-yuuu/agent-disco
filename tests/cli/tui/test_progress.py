"""Tests for the cold-open progress reporter (:mod:`calfcord.cli.tui.progress`).

``disco start`` blocks for tens of seconds on the Discord connect and used to
print nothing until its banner, which reads exactly like a wedge. The reporter is
the fix, so what matters here is that it stays HONEST (only resolved steps are
ticked) and stays HARMLESS (it is a cosmetic side-channel that must never fail the
start it decorates) — ADR-0023.
"""

from __future__ import annotations

import io

from rich.console import Console

from calfcord.cli.tui import progress


def _console(buffer: io.StringIO, *, tty: bool) -> Console:
    return Console(file=buffer, width=70, force_terminal=tty, highlight=False)


def test_non_tty_emits_a_milestone_line_per_resolved_step() -> None:
    """A pipe / CI log gets append-only milestones: a Live view would emit a storm of
    cursor escapes into a file nobody can watch."""
    buf = io.StringIO()
    with progress.ConsoleStartReporter(console=_console(buf, tty=False)) as reporter:
        reporter.step("supervisor")
        reporter.done("supervisor")
        reporter.step("bridge")
        reporter.done("bridge")

    out = buf.getvalue()
    assert "supervisor" in out
    assert "bridge" in out
    assert "\x1b[" not in out  # no ANSI control sequences off-TTY


def test_non_tty_does_not_report_an_unresolved_step_as_done() -> None:
    """The honesty rule: a step that was entered but never resolved is the one that
    hung, and must never be ticked."""
    buf = io.StringIO()
    with progress.ConsoleStartReporter(console=_console(buf, tty=False)) as reporter:
        reporter.step("supervisor")
        reporter.done("supervisor")
        reporter.step("bridge")  # never resolves — this is the timeout case

    out = buf.getvalue()
    assert "supervisor" in out
    # the bridge line must not carry a completion tick
    assert "✓ bridge" not in out


def test_tty_renders_the_step_labels_live() -> None:
    """On a terminal the operator gets a live view naming what is being waited on."""
    buf = io.StringIO()
    with progress.ConsoleStartReporter(console=_console(buf, tty=True)) as reporter:
        reporter.step("bridge")
        reporter.done("bridge")

    assert "bridge" in buf.getvalue()


def test_an_unknown_step_id_is_ignored() -> None:
    """A future lifecycle step the reporter has no row for must not crash a start."""
    buf = io.StringIO()
    with progress.ConsoleStartReporter(console=_console(buf, tty=False)) as reporter:
        reporter.step("no-such-step")
        reporter.done("no-such-step")  # must not raise


def test_a_render_failure_never_reaches_the_caller() -> None:
    """The advisory contract: the reporter decorates the start, it must not fail it.

    A console that raises stands in for any Rich/terminal fault mid-render. The
    start it wraps must still complete — a cosmetic side-channel that can abort a
    workspace open is worse than no side-channel.
    """

    class _BrokenConsole(Console):
        @property
        def is_terminal(self) -> bool:
            return False

        def print(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("terminal exploded")

    with progress.ConsoleStartReporter(console=_BrokenConsole(file=io.StringIO())) as reporter:
        reporter.step("bridge")
        reporter.done("bridge")  # must not raise
