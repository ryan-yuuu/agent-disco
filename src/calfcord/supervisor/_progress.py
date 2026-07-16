"""The progress seam a foreground lifecycle wait narrates itself through.

``disco start`` blocks on two things it does not control — the supervisor's REST
server answering, and the bridge reaching Ready (the Discord connect, the long
pole) — and used to print NOTHING until its final banner. Twenty-odd seconds of
apparent hang is the same experience as a wedge, so the operator cannot tell a
slow start from a broken one (ADR-0023).

The wait owns the work; the reporter only renders. Two calls carry everything a
renderer needs: :meth:`~StartReporter.step` on entering a wait,
:meth:`~StartReporter.done` when it resolves. A wait that fails is simply never
marked, which is what lets a reporter name the exact target that hung rather than
report a generic timeout.

The protocol lives HERE, not in the CLI, so :mod:`calfcord.supervisor.lifecycle`
can narrate without importing a console — the supervisor layer stays headless and
the CLI supplies both the wording and the rendering. :data:`NULL_REPORTER` is the
silent default, so every existing caller (and every test) is unaffected.
"""

from __future__ import annotations

from typing import Protocol


class StartReporter(Protocol):
    """The sink a lifecycle wait pushes step transitions to.

    Implementations must treat both calls as **advisory and non-throwing**: a
    reporter is a cosmetic side-channel, and a render glitch must never fail the
    operation it decorates.
    """

    def step(self, name: str) -> None:
        """Announce that the wait named ``name`` has been entered."""
        ...

    def done(self, name: str) -> None:
        """Mark the wait named ``name`` resolved."""
        ...


class _NullReporter:
    """The silent default: both calls are no-ops."""

    def step(self, name: str) -> None:
        return None

    def done(self, name: str) -> None:
        return None


NULL_REPORTER: StartReporter = _NullReporter()

# The stable ids `start` emits. Ids, not prose: the CLI maps them to whatever it
# shows the operator, so wording changes never reach the supervisor layer.
SUPERVISOR_STEP = "supervisor"
BRIDGE_STEP = "bridge"
TOOLS_STEP = "tools"
