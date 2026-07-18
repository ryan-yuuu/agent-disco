"""Stateful classifier that splits A2A activity from the step trace on a run's
stream (D-1/D-2).

A native ``message_agent`` consult is a ``tool_call`` step (name
``"message_agent"``); its reply is a ``tool_result`` whose ``tool_call_id``
matches. The dispatcher records each consult's ``tool_call_id`` and routes the
matching result to A2A rather than reading the result's own fields, because the
peer is not on them: calfkit mints a tool result on the CALLER's fold hop
(``nodes/_steps.py`` ``folded``, echoing ``name`` from the call's marker; the
flush stamps ``emitter=self.node_id``), so an A2A reply reads
``emitter=<caller>``, ``name="message_agent"`` on success AND on rejection.
Pairing is reliable because the whole run shares one ``correlation_id``
(single partition → request-before-reply order) and the handle stream is
lossless and ordered.

Peer identity for the projected thread therefore always comes from the
*request*'s ``args["name"]`` — the only place it appears — recorded in
:class:`A2ACall`.
"""

from __future__ import annotations

from dataclasses import dataclass

from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.trace_rows import RowState

_MESSAGE_AGENT = "message_agent"


@dataclass(frozen=True)
class A2ACall:
    """A recorded ``message_agent`` consult still awaiting its reply."""

    tool_call_id: str
    correlation_id: str
    caller: str
    peer: str
    message: str


@dataclass(frozen=True)
class A2ARequest:
    """Render the consult request into the per-``correlation_id`` thread under
    the caller's persona."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    message: str


@dataclass(frozen=True)
class A2AReply:
    """Render the peer's reply under the peer's persona (happy path)."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    text: str


@dataclass(frozen=True)
class A2AReject:
    """Render a rejected consult (peer offline / cycle / self) as a system note,
    not a peer post (``denied`` result — the caller refused to dispatch)."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    text: str


@dataclass(frozen=True)
class A2AFailed:
    """Render a consult that reached the peer but faulted (``failed`` result) as a
    system note — distinct from :class:`A2AReject` (a refused dispatch) and from a
    dangling fault (the peer never emitted a result at all)."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    text: str


A2AProjection = A2ARequest | A2AReply | A2AReject | A2AFailed


def consult_outcome(projection: A2AReply | A2AReject | A2AFailed) -> tuple[RowState, str]:
    """A resolved consult projection → the row state it resolves to and the note
    the row may carry.

    Shared by the two surfaces that resolve a consult row — the human's thread
    (``mention_handler._render_consult``) and the audit thread
    (``A2AProjector.project_consult_result``) — so the mapping lives once. Only a
    REJECTION's reason reaches the row: the dispatcher's own note about why the
    call never left, not part of any exchange. A reply's or a fault's prose is the
    peer's own words and stays out of the trace (ADR-0020).
    """
    state: RowState = {A2AReply: "ok", A2AFailed: "failed", A2AReject: "denied"}[type(projection)]
    return state, projection.text if isinstance(projection, A2AReject) else ""


class A2ADispatcher:
    """Classify each :class:`StepEvent` as an A2A render instruction or
    ``None`` (the step trace). One dispatcher per run — its open-consult state is
    that run's."""

    def __init__(self) -> None:
        self._open: dict[str, A2ACall] = {}

    def classify(self, step: StepEvent) -> A2AProjection | None:
        # NB: a ``handoff`` step is deliberately NOT classified here — a handoff
        # transfers conversation control (the peer replies in the caller's
        # place), unlike a ``message_agent`` consult where the caller keeps
        # control (both are ADR-0011). It is rendered inline in the main step
        # stream by the step-trace renderer, so it falls through to ``return None``.
        if step.kind == "tool_call" and step.name == _MESSAGE_AGENT:
            args = step.args or {}
            call = A2ACall(
                tool_call_id=step.tool_call_id or "",
                correlation_id=step.correlation_id,
                caller=step.emitter,
                peer=str(args.get("name", "")),
                message=str(args.get("message", "")),
            )
            self._open[call.tool_call_id] = call
            return A2ARequest(
                correlation_id=call.correlation_id,
                tool_call_id=call.tool_call_id,
                caller=call.caller,
                peer=call.peer,
                message=call.message,
            )
        if step.kind == "tool_result" and step.tool_call_id is not None and step.tool_call_id in self._open:
            call = self._open.pop(step.tool_call_id)
            cls = {"success": A2AReply, "failed": A2AFailed, "denied": A2AReject}[step.outcome]
            return cls(
                correlation_id=call.correlation_id,
                tool_call_id=call.tool_call_id,
                caller=call.caller,
                peer=call.peer,
                text=step.text,
            )
        return None

    def dangling(self) -> list[A2ACall]:
        """Consults with no reply yet — a faulted peer faults the whole run
        (RunFailed, no ``tool_result``), so on stream end the bridge synthesizes
        a failure note for each of these (D-2)."""
        return list(self._open.values())
