"""Normalized intermediate run-step event — the bridge's renderers and the A2A
dispatcher depend on THIS, never on calfkit's ``RunEvent`` union.

The single adapter :func:`normalize_run_event` is the only code that knows the
calfkit transport types, so the step source can change without touching the
renderers (spec §5.1, "swappable step-source seam").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from calfkit.client import RunCompleted, RunEvent, RunFailed

StepKind = Literal["agent_message", "tool_call", "tool_result", "handoff"]

Outcome = Literal["success", "failed", "denied"]
"""The result of a ``tool_result`` step, mirroring calfkit's ``ToolResultEvent.outcome``.
Owned here (not imported) so the renderers depend on THIS seam, never on calfkit's
types — the step source can change behind :func:`normalize_run_event` (spec §5.1).
``success`` renders as a return; ``failed`` (the call raised / retry-marked) and
``denied`` (the caller refused to dispatch) are distinct error kinds — a plain bool
would collapse them, so Discord could not render them 1:1."""


@dataclass(frozen=True)
class StepEvent:
    """One normalized intermediate step from a run's ``stream()``.

    Carries the union of fields the renderers need; which are populated depends
    on ``kind``. ``correlation_id`` + ``depth`` + ``emitter`` are always set
    (every calfkit step event carries them — steps for the whole run tree reach
    the root caller, attributed by ``emitter``/``depth``).
    """

    kind: StepKind
    correlation_id: str
    depth: int
    emitter: str
    text: str = ""
    """Rendered text — the concatenated ``TextPart`` content of an
    ``agent_message`` or a ``tool_result``."""
    tool_call_id: str | None = None
    name: str | None = None
    """``tool_call``: the tool name. ``tool_result``: the tool name echoed from the
    call's marker — so an A2A reply reads ``"message_agent"``, never the peer's
    name (the peer is only on the request's ``args``)."""
    args: dict[str, Any] | None = None
    """``tool_call`` arguments (normalized to a dict)."""
    outcome: Outcome = "success"
    """``tool_result``: whether the call succeeded, failed, or was denied."""
    target: str | None = None
    """``handoff``: the agent control transfers to."""
    reason: str | None = None
    """``handoff``: the model's stated reason."""


def _render_text(parts: list[Any]) -> str:
    """Concatenate the ``TextPart`` text of a step's parts — the human-readable
    content the renderers show; non-text parts (files, data) are skipped."""
    return "".join(p.text for p in parts if getattr(p, "kind", None) == "text")


def _args_to_dict(args: str | dict[str, Any] | None) -> dict[str, Any]:
    """Normalize ``ToolCallEvent.args`` (a JSON ``str``, a dict, or ``None``)."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_terminal(event: RunEvent) -> bool | None:
    """Whether ``event`` is the run's terminal and, if so, whether it FAILED.

    ``True`` = failed, ``False`` = completed, ``None`` = not a terminal. The
    tri-state matters: ``False`` ("terminal, succeeded") and ``None`` ("not a
    terminal") are different answers, so a bare bool would collapse them.

    This exists because the step trace must be sealed with the run's outcome,
    and the only place that knows it in time is the drain: calfkit's stream is
    ``zero or more step events then exactly one terminal``, so the terminal
    arrives as the stream's last item — BEFORE the handler's ``finally`` calls
    ``finish``. Reading it here needs no control-flow change (ADR-0025).

    ``result()`` stays the authority for the reply and the fault notice; the two
    cannot disagree, since it derives its ``NodeFaultError`` from this same
    terminal. Kept in this module so :func:`normalize_run_event` and this one are
    the only code aware of calfkit's event types (spec §5.1).

    Discriminated by ``isinstance`` rather than a ``kind`` tag: unlike the step
    events, the terminals carry no ``kind`` field.
    """
    if isinstance(event, RunFailed):
        return True
    if isinstance(event, RunCompleted):
        return False
    return None


def normalize_run_event(event: RunEvent) -> StepEvent | None:
    """Adapt a calfkit ``RunEvent`` into a :class:`StepEvent`, or ``None`` for the
    terminals (``RunCompleted`` / ``RunFailed``).

    A terminal is :func:`normalize_terminal`'s business — it seals the trace —
    and ``handle.result()`` remains the authority for the reply and the fault.

    The ONLY code that knows calfkit's step-event types; the renderers and the
    A2A dispatcher depend on :class:`StepEvent`, so the transport can change
    behind this one seam.
    """
    kind = getattr(event, "kind", None)
    if kind == "agent_message":
        return StepEvent(
            kind="agent_message",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            text=_render_text(event.parts),
        )
    if kind == "tool_call":
        return StepEvent(
            kind="tool_call",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            tool_call_id=event.tool_call_id,
            name=event.name,
            args=_args_to_dict(event.args),
        )
    if kind == "tool_result":
        return StepEvent(
            kind="tool_result",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            tool_call_id=event.tool_call_id,
            name=event.name,
            text=_render_text(event.parts),
            outcome=event.outcome,
        )
    if kind == "handoff":
        return StepEvent(
            kind="handoff",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            target=event.target,
            reason=event.reason,
        )
    return None
