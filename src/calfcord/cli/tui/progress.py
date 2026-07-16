"""Live progress for the cold workspace open (``disco start``).

``start`` waits on three things the operator cannot see — the supervisor's REST
server, the bridge's Discord connect (tens of seconds, the long pole), and the
tools host — and printed nothing until its final banner. Twenty-odd seconds of
dead terminal is indistinguishable from a wedge, so the slowness read as a bug
(ADR-0023).

This is the rendering half of :mod:`calfcord.supervisor._progress`'s seam: the
supervisor layer emits stable step ids, and every word the operator reads is
chosen here. Two rules keep it safe to bolt onto a lifecycle command:

* **Only resolved steps are ticked.** A step that was entered and never marked is
  the one that hung; leaving it un-ticked is what makes the display agree with
  ``start``'s own error line instead of contradicting it (§12.6 applied to the
  wait). The reporter deliberately does NOT print a failure summary — ``start``
  already names the cause and the log to read, and a second, vaguer line would
  only compete with it.
* **A render fault never reaches the caller.** This decorates a workspace open; a
  cosmetic side-channel that can abort one is worse than no side-channel.

Self-branches on ``console.is_terminal``: a terminal gets a transient live view
(matching :mod:`~calfcord.cli.tui.widgets`), anything else — a pipe, CI — gets
append-only milestone lines, because a Live view off-TTY is a storm of cursor
escapes in a file nobody watches.
"""

from __future__ import annotations

import contextlib
from types import TracebackType

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from calfcord.cli.tui import render, theme
from calfcord.supervisor._progress import BRIDGE_STEP, SUPERVISOR_STEP, TOOLS_STEP

_TITLE = "opening the workspace"

# The rows, in the order `start` resolves them. Ids come from the supervisor seam;
# the labels are the CLI's to choose. "bridge" carries its parenthetical because the
# Discord connect is where nearly all the wall-clock goes — naming it turns the long
# wait from a hang into an explanation.
_STEPS: tuple[tuple[str, str], ...] = (
    (SUPERVISOR_STEP, "supervisor"),
    (BRIDGE_STEP, "bridge (connecting to Discord)"),
    (TOOLS_STEP, "tools host"),
)


class ConsoleStartReporter:
    """Renders ``start``'s waits as they resolve; a no-op for unknown step ids."""

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = render.target(console)
        self._labels = dict(_STEPS)
        self._done: list[str] = []
        self._active: str | None = None
        self._live: Live | None = None

    def __enter__(self) -> ConsoleStartReporter:
        # Guarded like every other render here: a console that faults on entry must
        # still hand back a usable (silent) reporter rather than abort the start
        # before it begins.
        with contextlib.suppress(Exception):
            if self._console.is_terminal:
                live = Live(self._render(), console=self._console, transient=True)
                live.start()
                self._live = live
            else:
                self._console.print(Text(_TITLE), markup=False, highlight=False)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._live is not None:
            # A teardown glitch must not mask an in-flight exception from the start.
            with contextlib.suppress(Exception):
                self._live.stop()
            self._live = None
        return None

    def step(self, name: str) -> None:
        if name not in self._labels:
            return
        self._active = name
        self._refresh()

    def done(self, name: str) -> None:
        if name not in self._labels or name in self._done:
            return
        self._done.append(name)
        if self._active == name:
            self._active = None
        if self._console.is_terminal:
            self._refresh()
        else:
            # Off-TTY the milestone IS the output: one line, as it happens, so a CI
            # log shows how far a failed start got.
            with contextlib.suppress(Exception):
                self._console.print(
                    Text(f"{theme.TICK} {self._labels[name]} ({len(self._done)}/{len(_STEPS)})"),
                    markup=False,
                    highlight=False,
                )

    def _refresh(self) -> None:
        if self._live is None:
            return
        # Cosmetic only: a render glitch degrades to a stale frame, never a failed start.
        with contextlib.suppress(Exception):
            self._live.update(self._render())

    def _render(self) -> RenderableType:
        rows: list[RenderableType] = [
            Text(f"{_TITLE}  {len(self._done)}/{len(_STEPS)}", style=theme.TITLE)
        ]
        for name, label in _STEPS:
            if name in self._done:
                rows.append(Text(f"{theme.TICK} {label}"))
            elif name == self._active:
                rows.append(Spinner("dots", text=Text(f" {label}")))
            else:
                rows.append(Text(f"  {label}", style=theme.MUTED))
        return Group(*rows)
