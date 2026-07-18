"""The bridge's per-``!mention`` orchestration (spec §5.2).

Replaces the old publish-to-Kafka → outbox-consumer round trip with the calfkit
caller surface. For each ``!mention`` the handler:

1. resolves the target against the live mesh roster (R-A2 fail-fast);
2. starts the agent by name on the caller surface (``client.agent(name).start``);
3. drains the run's ``stream()`` — splitting native A2A activity (consults +
   handoffs) from the step trace via the stateful :class:`A2ADispatcher`;
4. awaits the terminal ``result()`` and posts it under the **responding** agent's
   persona (emitter-driven, so a handoff posts the peer's persona for free).

The collaborators (history, overrides, the A2A projector, the step-trace renderer,
the reply poster) are injected so this orchestration is unit-testable against a
``FakeHandle`` with no Kafka or Discord.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from calfkit._vendor.pydantic_ai.messages import ModelMessage
from calfkit.exceptions import NodeFaultError
from calfkit.models.error_report import ErrorReport, FaultTypes

from calfcord.agents.identifier import MENTION_PREFIX
from calfcord.agents.thinking import build_model_settings_union
from calfcord.bridge.a2a_dispatch import (
    A2ACall,
    A2ADispatcher,
    A2AFailed,
    A2AProjection,
    A2AReject,
    A2AReply,
    A2ARequest,
)
from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.step_events import StepEvent, normalize_run_event, normalize_terminal
from calfcord.bridge.trace import Destination
from calfcord.bridge.trace_rows import RowState
from calfcord.bridge.wire import WireMessage
from calfcord.discord.persona import Persona

logger = logging.getLogger(__name__)

_ROSTER_UNAVAILABLE = "I can't reach the agent roster right now — please try again in a moment."
_REPLY_DROPPED = (
    "I finished, but couldn't post my reply — a Discord error is blocking it. "
    "If this keeps happening, an operator should check the bot's channel permissions."
)
_STICKY_OWNER_OFFLINE = (
    "This conversation is sticky to `{mention}`, but that agent is offline. "
    "Use `!unstick` or address another agent with `!name`."
)

# Notice budget: Discord caps a message at 2000 chars. The cap below is a SOFT
# target — the "and N more" elision line (when appended) lives in the 100-char
# headroom, so a worst-case notice lands near ~1950, never past 2000. The full
# detail is in the log regardless.
_DISCORD_NOTICE_LIMIT = 2000
_MAX_NOTICE_CHARS = _DISCORD_NOTICE_LIMIT - 100
# Per-cause truncation: a single WebFetchError message can run long (full URL +
# the echoed URL); cap each line so one verbose cause can't crowd out the others.
_MAX_CAUSE_MSG_CHARS = 240


def _none_online_text(mention_ids: tuple[str, ...]) -> str:
    names = ", ".join(f"`{MENTION_PREFIX}{m}`" for m in mention_ids)
    return f"No agent matching {names} is online right now."


def _root_cause_failures(report: ErrorReport | None) -> list[ErrorReport]:
    """The outermost report per independent failure path in a fault_group.

    This is a DELIBERATE narrowing of ``ErrorReport.walk()``. The framework's
    ``walk()`` yields every nested report — including ``__cause__`` chain links
    (a ``WebFetchError`` wrapping an ``httpx.ConnectError`` yields both), which
    would double-count failures in the notice. Calfkit provides no
    "sibling-failures-only" traversal (no ``walk_leaves()`` helper exists; the
    ``causes`` field serves double duty as both group children and ``__cause__``
    links), so we branch on ``error_type == FaultTypes.FAULT_GROUP`` — the stable
    code ``_build_fault_group`` (calfkit ``base.py:1441``) is the sole producer
    of, and whose ``causes`` are by construction independent failure paths.

    Within a group, EVERY direct child is surfaced (whether it carries a
    harvested ``exception`` slot or is itself a minted typed fault like
    ``billing.quota_exceeded``) — a mixed group must not silently drop its
    typed-fault children. Nested groups are recursed. A top-level non-group fault
    surfaces itself only if it carries an ``exception`` slot; minted/framework
    faults at the top level yield nothing and the caller falls back to
    ``report.message``.
    """
    if report is None:
        return []
    if report.error_type == FaultTypes.FAULT_GROUP:
        failures: list[ErrorReport] = []
        for child in report.causes:
            if child.error_type == FaultTypes.FAULT_GROUP:
                failures.extend(_root_cause_failures(child))
            else:
                failures.append(child)
        return failures
    if report.exception is not None:
        return [report]
    return []


def _format_cause_line(failure: ErrorReport) -> str:
    """One Discord notice line for a root-cause failure: origin, type, and message.

    The origin + type prefix the message so a multi-cause notice stays scannable
    even when (as is common) several failures share a type (``web_fetch:
    WebFetchError — ...``). The type falls back to ``error_type`` for minted
    typed faults (``billing.quota_exceeded``) that carry no harvested exception
    slot. The message is truncated per-leaf so one verbose cause (a long URL)
    can't crowd out the rest.
    """
    origin = failure.origin_node_id or "unknown"
    exc_type = failure.exception.type if failure.exception is not None else failure.error_type
    message = (failure.message or "")[:_MAX_CAUSE_MSG_CHARS]
    return f"  • {origin}: {exc_type} — {message}"


def _agent_error_text(target: str | None, report: ErrorReport | None = None) -> str:
    """Build the user-facing fault notice, surfacing root-cause exceptions.

    Surfaces the outermost exception per failure path so the user can tell "4
    fetches 403'd" from "agent crashed" — without this, a fault_group's children
    are invisible (they live in ``report.causes``, which the old origin-only
    notice never read). When there are no failures to surface (a minted/framework
    fault like ``billing.quota_exceeded``), the notice includes ``report.message``
    if it adds actionable context, else falls back to the honest generic form.

    Leak posture: ``failure.message`` is ``safe_exc_message(exc)`` — the raw
    exception message the failing tool raised. It is posted to Discord on the
    TRUST assumption that the agent's own tool exceptions (the only producers in
    this system) keep their messages free of secrets. ``exception.attrs`` (which
    may carry sanitized ``vars(exc)`` like status codes) is deliberately kept
    log-only, never posted.
    """
    who = f"`{target}`" if target else "The agent"
    header = f"{who} hit an error handling that message:"
    failures = _root_cause_failures(report)

    if not failures:
        # No harvested exception to surface. Include the report's message if it's
        # informative (a minted fault's message often is — e.g. "quota exhausted");
        # otherwise stay generic rather than expose a raw framework code.
        message = report.message if report is not None else None
        if message:
            return f"{header} {message}. Please try again."
        return f"{header} Please try again."

    footer = "Please try again, or ask an operator to check the logs."
    cause_lines = [_format_cause_line(f) for f in failures]
    # Soft budget: show as many lines as fit under the cap. The "and N more"
    # line (when it appends) lands in the 100-char headroom above the cap.
    budget = _MAX_NOTICE_CHARS - len(header) - len(footer) - 2
    shown = 0
    for line in cause_lines:
        if len(line) + 1 > budget:  # +1 for the newline joining it
            break
        budget -= len(line) + 1
        shown += 1
    lines = [header, *cause_lines[:shown]]
    hidden = len(failures) - shown
    if hidden:
        lines.append(f"… and {hidden} more failure(s) — see the bridge logs.")
    lines.append(footer)
    return "\n".join(lines)


def _log_agent_fault(exc: NodeFaultError, target: str) -> None:
    """Log the full ``ErrorReport`` calfkit shipped, plus each root cause.

    Agents run on other hosts, so the report the bridge received on the fault
    (spec §11.1) is the operator's only in-hand diagnostic — log ``error_type``,
    ``message``, ``retryable`` and the harvested upstream exception at ERROR, not
    just ``origin`` (which alone forces a cross-host log dig for every fault).
    Then surface each root-cause FAILURE (one ERROR line per outermost exception)
    with its origin/type/message/attrs: a fault_group summarizes ("N unhandled
    fault(s)") but the actual failures (the 403s, the exception types) live in
    the children — without this the operator sees the count but not the causes.
    The defensive ``getattr`` chain on the summary line tolerates a malformed
    fault with no report; ``_root_cause_failures`` is null-safe on its own.
    """
    report = getattr(exc, "report", None)
    origin = getattr(report, "origin_node_id", None)
    exception = getattr(report, "exception", None)
    upstream = f"{exception.type}: {exception.attrs}" if exception is not None else None
    logger.error(
        "agent run faulted target=%s origin=%s error_type=%s retryable=%s message=%s upstream=%s",
        target,
        origin,
        getattr(report, "error_type", None),
        getattr(report, "retryable", None),
        getattr(report, "message", None),
        upstream,
    )
    # One ERROR line per root-cause failure so each is named individually
    # (origin/type/message/attrs) — the group summary alone hides them. A failure
    # may lack an ``exception`` slot (a minted typed fault like
    # ``billing.quota_exceeded`` in a mixed group); fall back to ``error_type``.
    failures = _root_cause_failures(report)
    for i, failure in enumerate(failures, 1):
        failure_exc = failure.exception
        logger.error(
            "agent run root cause [%d/%d] origin=%s type=%s message=%s attrs=%s",
            i,
            len(failures),
            failure.origin_node_id,
            failure_exc.type if failure_exc is not None else failure.error_type,
            failure.message,
            failure_exc.attrs if failure_exc is not None else None,
        )


@dataclass(frozen=True)
class MentionRequest:
    """A normalized inbound ``!mention`` — what the Discord gateway hands the
    handler.

    ``mention_ids`` are the parsed ``!<id>`` tokens in order; ``wire`` is the typed
    :class:`WireMessage` the normalizer already produced (validated once at the
    gateway boundary) — the handler serializes it into ``deps["discord"]`` for the
    agent, and the reply poster reads its typed ``channel_id``/``thread_id`` without
    re-validating. ``reply_target`` is the opaque discord.py object the reply /
    notice posts against.

    ``message_id`` is the triggering Discord message id — the history-fetch anchor
    (``before=``) and the transcript-replay join key. ``source_channel_id`` is the
    un-flattened channel the message landed in (the thread itself, for history
    fetching); ``channel_id`` is the flattened parent (the webhook host).
    """

    content: str
    mention_ids: tuple[str, ...]
    author_label: str
    message_id: int
    source_channel_id: int
    channel_id: int
    wire: WireMessage
    reply_target: Any
    route_kind: Literal["explicit", "sticky"] = "explicit"


class HistoryProvider(Protocol):
    async def message_history(self, req: MentionRequest) -> list[ModelMessage]: ...


class OverrideProvider(Protocol):
    def effort_for(self, agent_id: str) -> str | None: ...


class A2AProjectorLike(Protocol):
    """``project`` returns the audit thread's jump URL as a receipt for the render
    it just performed, or ``None`` when that render failed (best-effort) and there
    is therefore nothing to link to.

    ``project_step`` takes a CONSULTED agent's own step (ADR-0026). ``seal``
    closes that agent's trace with the run's outcome, exactly as the human's is
    sealed. ``finish`` retires the turn's projector state and MUST run after the
    terminal reply, so ``project_fault`` still finds the turn's thread.

    ``project_consult``/``project_consult_result`` announce a NESTED consult as a
    resolving row in the caller's trace inside the audit thread (ADR-0026) — the
    audit-thread counterpart of ``_render_consult``'s human-thread row, for the
    consults ``is_acting`` routes here instead.
    """

    async def project(self, projection: A2AProjection) -> str | None: ...
    async def project_fault(self, call: A2ACall) -> None: ...
    async def project_step(self, step: StepEvent) -> None: ...
    async def project_consult(self, request: A2ARequest) -> None: ...
    async def project_consult_result(self, projection: A2AReply | A2AReject | A2AFailed) -> None: ...
    async def seal(self, correlation_id: str, *, faulted: bool) -> None: ...
    async def finish(self, correlation_id: str) -> None: ...


class StepTraceRendererLike(Protocol):
    """The step trace's write surface.

    ``…Like`` matches :class:`A2AProjectorLike`, and keeps this name clear of the
    concrete :class:`~calfcord.bridge.trace.StepTraceRenderer` this module now
    imports from (for :class:`~calfcord.bridge.trace.Destination`) — same name,
    two meanings, one of which would silently shadow the other.

    ``on_step`` takes the run's own steps. ``on_consult``/``on_consult_result``
    take the bridge's OWN annotation of a consult — not run steps, since the A2A
    dispatcher intercepts both halves of a ``message_agent`` call before
    ``on_step`` is reached, so the correlation and persona come explicitly.
    ``seal`` closes the trace with the run's outcome, driven by the stream's
    terminal; ``finish`` flushes and retires the writer, and seals defensively if
    ``seal`` never ran.
    """

    async def on_step(self, step: StepEvent, dest: Destination, *, acting_agent: str) -> None: ...
    async def on_consult(
        self,
        key: str,
        peer: str,
        thread_url: str | None,
        dest: Destination,
        *,
        correlation_id: str,
        persona_name: str,
        request_preview: str = "",
    ) -> None: ...
    async def on_consult_result(self, key: str, *, state: RowState, note: str, correlation_id: str) -> None: ...
    async def seal(self, correlation_id: str, *, faulted: bool) -> None: ...
    async def finish(self, correlation_id: str) -> None: ...


class StickyStore(Protocol):
    async def set_sticky_owner(self, conversation_key: str, owner_agent_id: str) -> None: ...


class ReplyPoster(Protocol):
    """``post_reply`` chunk-splits the reply and posts every chunk, reporting
    ``"posted"`` (≥1 chunk delivered — set the sticky owner), ``"empty"``
    (nothing to post — a no-op), or ``"lost"`` (every chunk failed — surface
    an operator notice)."""

    async def post_reply(
        self, req: MentionRequest, persona: Persona, result: Any, *, initial_len: int, correlation_id: str
    ) -> Literal["posted", "empty", "lost"]: ...
    async def post_notice(self, req: MentionRequest, text: str) -> None: ...


class MentionHandler:
    """Orchestrates one ``!mention`` end to end on the caller surface."""

    def __init__(
        self,
        *,
        client: Any,
        roster: Any,
        history: HistoryProvider,
        overrides: OverrideProvider,
        a2a: A2AProjectorLike,
        trace: StepTraceRendererLike,
        reply: ReplyPoster,
        memory_deps: Any = dict,
        sticky: StickyStore | None = None,
    ) -> None:
        self._client = client
        self._roster = roster
        self._history = history
        self._overrides = overrides
        self._a2a = a2a
        self._trace = trace
        self._reply = reply
        self._memory_deps = memory_deps
        self._sticky = sticky

    async def handle(self, req: MentionRequest) -> None:
        # Refresh the mesh snapshot once per turn so the (synchronous) online()
        # read below reflects the current roster — there is no background refresh
        # loop; a mesh read is an in-memory ktable snapshot.
        await self._roster.refresh()
        online = self._roster.online()
        if online is None:
            # Mesh unavailable — we cannot tell who is online, so fail fast
            # rather than route blindly (R-A2). reader_dead stays here until the
            # bridge restarts; the roster already alerted.
            await self._reply.post_notice(req, _ROSTER_UNAVAILABLE)
            return
        target = next((m for m in req.mention_ids if m in online), None)
        if target is None:
            if req.mention_ids:
                if req.route_kind == "sticky":
                    await self._reply.post_notice(
                        req,
                        _STICKY_OWNER_OFFLINE.format(mention=f"{MENTION_PREFIX}{req.mention_ids[0]}"),
                    )
                    return
                # Mentioned an agent that is not online right now.
                await self._reply.post_notice(req, _none_online_text(req.mention_ids))
            # else: no !mention at all → ambient → unanswered (C2): do nothing.
            return

        history = await self._history.message_history(req)
        # Serialize the typed wire into deps once per turn (the agent reads
        # ``deps["discord"]`` as JSON); the reply poster uses ``req.wire`` typed.
        deps = {"discord": req.wire.model_dump(mode="json"), **self._memory_deps()}
        # The C11 effort override for this turn (provider-blind union).
        model_settings = build_model_settings_union(self._overrides.effort_for(target))
        handle = await self._client.agent(target).start(
            req.content,
            message_history=history,
            deps=deps,
            author=req.author_label,
            model_settings=model_settings,
        )

        dispatcher = A2ADispatcher()
        # The acting agent is who is currently in control of the run. It starts
        # as the mention target and transfers to the peer on each handoff, so
        # tool trace rows (tool_call/tool_result) after a handoff are stamped
        # with the new agent's persona, not the original target's.
        acting_agent = target
        # The human's trace renders where the mention landed: the webhook hosts on
        # the flattened parent, and posts into the thread when the wire came from
        # one. Fixed for the turn — a handoff changes who speaks, never where.
        human_dest = Destination(
            channel_id=req.channel_id,
            thread_id=(req.source_channel_id if req.source_channel_id != req.channel_id else None),
        )
        # The A2A projector is retired in the OUTER finally, after _deliver: a
        # faulted run synthesizes its dangling-consult notes during delivery, and
        # those need the turn's audit thread still mapped (ADR-0026). Retiring it
        # alongside the human trace below would evict the mapping first and orphan
        # each note in a freshly created second thread.
        try:
            await self._drain_and_deliver(req, handle, dispatcher, target, history, acting_agent, human_dest)
        finally:
            await self._a2a.finish(handle.correlation_id)

    async def _drain_and_deliver(
        self,
        req: MentionRequest,
        handle: Any,
        dispatcher: A2ADispatcher,
        target: str,
        history: list[ModelMessage],
        acting_agent: str,
        human_dest: Destination,
    ) -> None:
        """Drain the run's stream into both trace surfaces, then post the reply.

        Split out so ``handle`` can wrap it in the outer ``finally`` that retires
        the A2A projector — which must happen after ``_deliver``, not alongside
        the human trace's ``finish``.
        """
        try:
            async for event in handle.stream():
                try:
                    step = normalize_run_event(event)
                    if step is None:
                        # The stream's terminal. It seals the trace with the run's
                        # outcome — the ONLY moment the bridge knows it in time,
                        # since `finish` runs in the `finally` below while the
                        # fault only surfaces afterwards in `_await_terminal`
                        # (ADR-0025). `result()` still owns the reply and the
                        # notice; this is display only, and best-effort.
                        faulted = normalize_terminal(event)
                        if faulted is not None:
                            # BOTH surfaces seal: a consulted agent's trace ends
                            # with the same run, and #124's motivating case is
                            # exactly this — 23 tool calls then a fault, which
                            # must read "run failed after 23 tools", not trail off.
                            await self._trace.seal(handle.correlation_id, faulted=faulted)
                            await self._a2a.seal(handle.correlation_id, faulted=faulted)
                            continue
                        # Not a terminal either: an event kind the normalizer does
                        # not know (a future calfkit step type). Dropping it is
                        # right, dropping it SILENTLY is not — this is the only
                        # place the two "None"s are distinguishable.
                        logger.warning("bridge: unrenderable run event %s; dropping", type(event).__name__)
                        continue
                    # THE invariant, applied uniformly below: only the agent in
                    # control of the human's turn may touch the human's thread or
                    # transfer that control. Any other emitter is inside a consulted
                    # peer's private sub-tree, reaching us only because calfkit
                    # flushes EVERY hop to the ROOT caller (base.py ``_flush_steps``
                    # → ``stack.root.callback_topic``). ``A2ARequest.caller`` IS the
                    # step's emitter, so this one predicate covers both branches.
                    is_acting = step.emitter == acting_agent
                    projection = dispatcher.classify(step)
                    if projection is not None and is_acting:
                        # The ACTING agent's consult: project the exchange to the
                        # audit thread and cross-link it with ONE resolving row in
                        # the human's thread — opened by the request, resolved by
                        # its outcome, carrying only the peer, state, and link
                        # (ADR-0020). The url is the render's receipt: None means
                        # it failed (swallowed), so there is no thread to link.
                        url = await self._a2a.project(projection)
                        await self._render_consult(projection, url, human_dest)
                    elif isinstance(projection, A2ARequest):
                        # A NESTED consult's request (the peer is not the acting
                        # agent). Announce it as a resolving row in the caller's
                        # OWN trace inside the audit thread (ADR-0026), and SUPPRESS
                        # the standalone prompt message — the row is the single
                        # signal, not a bare `[caller] <prompt>` line that reads as
                        # the peer's own words and leaves its work unannounced.
                        await self._a2a.project_consult(projection)
                    elif projection is not None:
                        # A nested consult's reply/reject/fault: keep the peer's
                        # answer (or the system note) for the audit's record, AND
                        # resolve the caller's row from the same outcome.
                        await self._a2a.project(projection)
                        await self._a2a.project_consult_result(projection)
                    elif is_acting:
                        await self._trace.on_step(step, human_dest, acting_agent=acting_agent)
                    else:
                        # A consulted agent's own step (its preamble, its tool
                        # calls). That is private A2A work — the audit channel's
                        # business — so it renders in the turn's A2A thread under
                        # that agent's own persona (ADR-0026). Rendering it HERE
                        # would spill the exchange into the human's conversation,
                        # which is what the is_acting guard exists to prevent.
                        await self._a2a.project_step(step)
                    # A handoff TRANSFERS control, so the new owner's steps keep
                    # rendering. Update AFTER rendering, so the announcement itself
                    # stays under the handing-off agent's persona — and only for the
                    # OWNER's own handoff: a consulted peer handing off inside its
                    # private sub-tree never had the human's turn to give away, and
                    # letting it seize ownership blackholes every later owner step.
                    if step.kind == "handoff" and step.target and is_acting:
                        acting_agent = step.target.removeprefix("/")
                except Exception:
                    # A render/normalize/classify bug (or a future calfkit event
                    # shape) must NOT unwind the drain and cost the user the
                    # already-computed terminal reply — the trace and A2A
                    # contracts both promise the render path can't fault the turn.
                    # Drop just this step (logged) and keep draining; _deliver posts
                    # the terminal reply below regardless. CancelledError is a
                    # BaseException, so shutdown still propagates.
                    logger.exception("bridge: dropping unrenderable run step; terminal reply unaffected")
        finally:
            await self._trace.finish(handle.correlation_id)

        await self._deliver(req, handle, dispatcher, target, history)

    async def _deliver(
        self,
        req: MentionRequest,
        handle: Any,
        dispatcher: A2ADispatcher,
        target: str,
        history: list[ModelMessage],
    ) -> None:
        """Post the agent's reply as chunked persona messages (single-pass).

        The poster chunk-splits the reply and posts every chunk — there is no
        retry that re-invokes the agent. ``"posted"`` (≥1 chunk delivered)
        sets the sticky owner. ``"lost"`` (every chunk failed — e.g. missing
        Manage Webhooks, bad token, persistent 5xx) would otherwise ghost the
        user — the step-trace message was already deleted in ``handle()`` — so
        surface an operator notice via the native-reply path, which needs only
        Send Messages and is independent of the failing webhook path.
        """
        result = await self._await_terminal(req, handle, dispatcher, target)
        if result is None:
            return  # faulted — notice already posted

        persona = persona_for(result.emitter_node_id or target)
        outcome = await self._reply.post_reply(
            req, persona, result, initial_len=len(history), correlation_id=handle.correlation_id
        )
        if outcome == "posted":
            await self._set_sticky_owner(req, persona.name)
        elif outcome == "lost":
            await self._reply.post_notice(req, _REPLY_DROPPED)

    async def _set_sticky_owner(self, req: MentionRequest, owner_agent_id: str) -> None:
        """Persist the visible terminal responder as this conversation's owner."""
        if self._sticky is None:
            return
        await self._sticky.set_sticky_owner(str(req.source_channel_id or req.channel_id), owner_agent_id)

    async def _render_consult(self, projection: A2AProjection, url: str | None, dest: Destination) -> None:
        """Open or resolve the acting agent's consult row in the step trace.

        Every projection carries the ``tool_call_id``, so the request opens a
        keyed row and each outcome resolves that same row. Previously only the
        REQUEST rendered — leaving an optimistic "consulted" line even when the
        consult was rejected or faulted, because the ⚠️ went solely to the audit
        thread.
        """
        if isinstance(projection, A2ARequest):
            await self._trace.on_consult(
                projection.tool_call_id,
                projection.peer,
                url,
                dest,
                correlation_id=projection.correlation_id,
                persona_name=projection.caller,
            )
            return
        state: RowState = {A2AReply: "ok", A2AFailed: "failed", A2AReject: "denied"}[type(projection)]
        await self._trace.on_consult_result(
            projection.tool_call_id,
            state=state,
            # The peer's own words never reach the human's thread — only a
            # REJECTION's reason does, which is the dispatcher's own note about
            # why the call never left, not part of the exchange.
            note=projection.text if isinstance(projection, A2AReject) else "",
            correlation_id=projection.correlation_id,
        )

    async def _post_fault_notice(self, req: MentionRequest, exc: NodeFaultError, target: str) -> None:
        """Log the fault's full report (I-1) and post the user-facing error notice.

        Surfaces root-cause exceptions (the 403s behind a fault_group) in BOTH the
        log and the notice — pass the whole report so ``_agent_error_text`` can
        walk ``report.causes`` rather than read only the top-level origin.
        """
        _log_agent_fault(exc, target)
        await self._reply.post_notice(req, _agent_error_text(target, getattr(exc, "report", None)))

    async def _await_terminal(
        self, req: MentionRequest, handle: Any, dispatcher: A2ADispatcher, target: str
    ) -> Any | None:
        """Await the run's terminal, or ``None`` after handling a fault.

        No timeout: per spec §5.2 the bridge awaits the terminal unbounded (C5
        drops app-side timeout policing; a durable run may legitimately pause). A
        genuine peer/agent fault faults the whole run (D-2) — calfkit maps
        ``RunFailed`` → :class:`NodeFaultError`; any consult still open never got a
        reply, so synthesize an A2A failure note for each, then post a user-facing
        error (best-effort persona from the faulting node when the report names it).
        """
        try:
            return await handle.result()
        except NodeFaultError as exc:
            for call in dispatcher.dangling():
                await self._a2a.project_fault(call)
            await self._post_fault_notice(req, exc, target)
            return None
