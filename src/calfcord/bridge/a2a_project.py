"""Render A2A activity (consults + handoffs) into the unified Discord audit channel.

The stateful :class:`~calfcord.bridge.a2a_dispatch.A2ADispatcher` pulls native
``message_agent`` consults and ``HandoffEvent``s off a run's stream and emits the
:class:`~calfcord.bridge.a2a_dispatch.A2AProjection` dataclasses; this projector
turns each into Discord posts in the unified A2A channel (re-homed from the old
``private_chat`` tool, spec §6.2). It is the ``A2AProjectorLike`` collaborator the
bridge's :class:`~calfcord.bridge.mention_handler.MentionHandler` drives.

Anchoring (round-3 M3): one thread per **``correlation_id``** (one human turn's A2A
activity), created lazily on the first projection for that turn — its message is the
thread's starter. Every later request/reply/reject/handoff/fault for the same
``correlation_id`` posts into that thread. Peer identity comes from the projection
dataclasses (already resolved by the dispatcher from the request's ``args["name"]``,
the one source stable across success and rejection); personas come from the pure
:func:`~calfcord.bridge.persona_resolve.persona_for` (no roster).

Best-effort audit: the bridge is no longer the A2A *transport* (the consult already
happened inside the agent runtime and its reply is in-hand on the stream), so a
failed Discord render is logged and swallowed — it never faults the human turn.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Literal, assert_never

import discord

from calfcord.bridge.a2a_dispatch import (
    A2ACall,
    A2AFailed,
    A2AProjection,
    A2AReject,
    A2AReply,
    A2ARequest,
    consult_outcome,
)
from calfcord.bridge.egress import A2AChannelResolver
from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.trace import Destination, StepTraceRenderer
from calfcord.discord.chunking import chunk_split
from calfcord.discord.persona import DiscordPersonaSender

logger = logging.getLogger(__name__)

_EMPTY_PLACEHOLDER = "(empty response)"
"""Discord rejects an empty webhook message; substitute this for empty content."""

_AUDIT_GAP_REMEDY = (
    "Agent-to-agent exchanges are NOT being recorded. A 403 (error code 50013) here usually means "
    "the bot lacks Manage Channels and so cannot create the audit channel: re-run the invite from "
    "`disco init` to re-authorize, or create the channel by hand and grant it View Channel + "
    "Manage Webhooks + Create Public Threads + Send Messages in Threads. See docs/a2a-threads.md."
)
"""Named in the first failure's log line. The projection is best-effort by design, so without an
actionable line a broken audit channel is indistinguishable from an idle one — the failure mode
this text exists to end."""

# Thread-name shaping (re-homed from the old private_chat tool).
_THREAD_NAME_MAX_TOTAL = 100
"""Discord's hard cap on thread names; exceeding it 400s the create."""
_THREAD_NAME_EMPTY_PLACEHOLDER = "consultation"
"""Turn-level subject when the triggering human message is empty."""


def _build_thread_name(root_agent: str, content: str) -> str:
    """Produce a turn-level thread name like ``'marketing · plan launch'``.

    Control characters are normalized to spaces and runs collapsed; the topic tail
    The leading routing token is removed when it names ``root_agent``; thread
    identity describes the human turn, never whichever parallel peer happened to
    be projected first.
    """
    cleaned = " ".join("".join(c if c.isprintable() else " " for c in content).split())
    cleaned = re.sub(rf"^!{re.escape(root_agent)}(?:\s+|$)", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = cleaned or _THREAD_NAME_EMPTY_PLACEHOLDER
    name = f"{root_agent} · {cleaned}"
    return name[:_THREAD_NAME_MAX_TOTAL]


BranchState = Literal["pending", "replied", "rejected", "failed", "interrupted"]


@dataclass(slots=True)
class _Branch:
    request: A2ARequest
    message_id: int
    opened_at: float
    state: BranchState = "pending"


@dataclass(slots=True)
class _Turn:
    root_agent: str
    subject: str
    thread_id: int | None = None
    branches: dict[str, _Branch] = field(default_factory=dict)


class _ConsultCard(discord.ui.LayoutView):
    def __init__(
        self,
        request: A2ARequest,
        *,
        state: BranchState,
        note: str = "",
        elapsed: float | None = None,
    ) -> None:
        super().__init__(timeout=None)
        glyph, status = {
            "pending": ("↗", "Consulting"),
            "replied": ("✓", "Consulted"),
            "rejected": ("⊘", "Not dispatched"),
            "failed": ("❌", "Failed"),
            "interrupted": ("⚠", "No response"),
        }[state]
        timing = f" · {elapsed:.1f}s" if elapsed is not None else ""
        status_line = f"-# **{status}**{timing}"
        if note:
            status_line += f" · {_card_plain(note, 240)}"
        prompt = _card_plain(request.message, 3000) or _EMPTY_PLACEHOLDER
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    content=f"### {glyph} {_card_plain(request.caller)} → {_card_plain(request.peer)}"
                ),
                discord.ui.TextDisplay(content=status_line),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(content=f"> {prompt}"),
            )
        )


def _card_plain(text: str, limit: int = 120) -> str:
    """Flatten, markdown-escape, and bound model-controlled card text."""
    flat = " ".join(text.split())
    escaped = re.sub(r"([\\`*_~|])", r"\\\1", flat)
    return escaped[:limit]


_REQUEST_PREVIEW_MAX = 60
"""How much of a NESTED consult's prompt to fold onto its trace row (ADR-0027).
A glimpse of the ask, not the whole thing — the row is a signal, not the message.
The row itself re-hygienises and hard-bounds this, so it is a display budget, not
a safety one."""


def _request_preview(message: str) -> str:
    """A short, single-line glimpse of a consult's prompt for its trace row.

    Truncation is on the raw text; the row's own ``_plain`` then flattens and
    markdown-escapes it, so this need only decide *how much* to show. May be empty
    (a blank prompt) — the row renders as inline regardless, so a blank ask shows
    a bare marker rather than a link or the audit-gap.
    """
    trimmed = message.strip()
    if len(trimmed) <= _REQUEST_PREVIEW_MAX:
        return trimmed
    return trimmed[:_REQUEST_PREVIEW_MAX].rstrip() + "…"


class A2AProjector:
    """Renders :class:`A2AProjection`s into the unified A2A audit channel.

    One instance per bridge; its ``correlation_id → thread_id`` map is the only
    state, mirroring one thread per human turn's A2A activity.
    """

    def __init__(
        self,
        resolver: A2AChannelResolver,
        personas: DiscordPersonaSender,
        steps: StepTraceRenderer,
    ) -> None:
        self._resolver = resolver
        self._personas = personas
        self._threads: dict[str, int] = {}
        self._turns: dict[str, _Turn] = {}
        # The audit channel's OWN step renderer — a DIFFERENT instance from the
        # one rendering the human's thread (ADR-0026). Two surfaces, two entry
        # maps: both are keyed by correlation_id and both see the same one for a
        # turn, so sharing an instance would collide them onto one trace.
        self._steps = steps
        self._channel_id: int | None = None
        self._degraded = False

    async def begin_turn(self, *, correlation_id: str, root_agent: str, subject: str) -> None:
        """Register metadata used when the first consult lazily anchors its thread."""
        self._turns[correlation_id] = _Turn(root_agent=root_agent, subject=subject)

    async def project(self, projection: A2AProjection) -> str | None:
        """Render one projection; return the audit thread's jump URL, or ``None``.

        The URL is this render's **receipt** — returned only when the post actually
        reached Discord — so the bridge can cross-link the exchange it just wrote.
        ``None`` means the render failed and was swallowed (best-effort: a Discord
        failure must never fault the human turn), leaving nothing to link to.

        Deriving the link from the thread map instead would answer a subtly
        different question — "does a thread exist for this turn?" — which diverges
        the moment a turn's SECOND consult fails: the first consult's thread is
        still mapped, so the caller would confidently link a thread that never
        received this exchange. A receipt cannot drift from what was written.
        """
        thread_id = await self._guarded(self._dispatch(projection), type(projection).__name__)
        if thread_id is None:
            return None
        return f"https://discord.com/channels/{self._resolver.guild_id}/{thread_id}"

    async def _guarded(self, render: Awaitable[int], kind: str) -> int | None:
        """Run one best-effort render: swallow a Discord failure (it must never
        fault the human turn — this runs inside the mention handler's stream-drain
        loop), note the audit gap, and report the thread it reached or ``None``.

        Both render entry points funnel through here so the gap latch arms *and
        re-arms* identically on either — an asymmetry would silently lose the loud
        line for the next real outage.
        """
        try:
            thread_id = await render
        except Exception:
            self._note_gap(kind)
            return None
        self._degraded = False  # this outage is over; a LATER one earns its own loud line
        return thread_id

    def _note_gap(self, kind: str) -> None:
        """Log an audit gap: loudly the first time, quietly while it persists.

        The projection is best-effort, so a broken audit channel produces no
        user-visible error and no fault — only this line. It is therefore an ERROR
        naming the remedy, not a bare WARN. Repeats drop to DEBUG because the
        failure is almost always systemic (a missing permission fails identically
        on every consult), and re-logging a traceback per projection buries the
        one line that matters under its own noise.
        """
        if self._degraded:
            # exc_info stays on: the latch is a bare bool, so a DIFFERENT failure
            # arriving during an outage lands here too — without the traceback it
            # would vanish with no diagnostic at any level.
            logger.debug("A2A projection still failing (audit gap); continuing kind=%s", kind, exc_info=True)
            return
        self._degraded = True
        logger.error(
            "A2A projection failed (audit gap); continuing kind=%s. %s",
            kind,
            _AUDIT_GAP_REMEDY,
            exc_info=True,
        )

    async def project_fault(self, call: A2ACall) -> None:
        """Resolve the original card when a consult never produced a result."""
        await self._guarded(self._resolve_branch(call.correlation_id, call.tool_call_id, "interrupted"), "A2AFault")

    async def project_step(self, step: StepEvent) -> None:
        """Fold one CONSULTED agent's step into its trace in that turn's thread
        (ADR-0026). The drain hands us every step whose emitter is not the turn's
        acting agent; the acting agent's own steps go to the human's thread.

        ``acting_agent=step.emitter`` renders every step under the agent that
        actually emitted it — in an audit view a tool call belongs to the agent
        that made it, and a nested consult's peer is its own participant. It goes
        through the SAME renderer the human's thread uses, so 23 tool calls are
        one edited-in-place message rather than 23 (ADR-0017), rendered in the
        same row grammar (ADR-0024).

        A step never CREATES the thread: the first consult's route card anchors
        the turn-level thread, and a step cannot precede its own consult. So no
        thread means the request's render failed (best-effort) —
        drop, matching the audit gap already logged there.
        """
        thread_id = self._threads.get(step.correlation_id)
        if thread_id is None:
            logger.debug(
                "A2A: no thread for correlation=%s; dropping consulted agent's step emitter=%s kind=%s",
                step.correlation_id,
                step.emitter,
                step.kind,
            )
            return
        channel_id = self._channel_id
        if channel_id is None:  # pragma: no cover - a thread implies a channel
            return
        await self._steps.on_step(
            step,
            Destination(channel_id=channel_id, thread_id=thread_id),
            acting_agent=step.emitter,
        )

    async def project_consult(self, request: A2ARequest) -> None:
        """Announce a NESTED consult as a resolving row in the CALLER's trace
        (ADR-0027): ``◐ consulting <peer>`` under the caller's persona, with a
        glimpse of the ask folded on. Its peer's work then renders below in the
        same thread, so the peer no longer appears unannounced.

        This REPLACES the standalone prompt message for a nested consult — the
        drain does not also ``project`` the request — so the row is the single,
        resolving signal rather than a bare ``[caller] <prompt>`` line. The row is
        ``inline`` (its exchange is in this thread, nothing to link), so it never
        renders the top-level audit-gap marker even when the ask is blank.

        Thread lookup mirrors :meth:`project_step`: a nested consult cannot precede
        the top-level one that named the thread, so a missing thread means that
        render failed (best-effort) — drop, rather than anchor an unnameable one.
        """
        thread_id = self._threads.get(request.correlation_id)
        if thread_id is None:
            logger.debug(
                "A2A: no thread for correlation=%s; dropping nested consult row caller=%s peer=%s",
                request.correlation_id,
                request.caller,
                request.peer,
            )
            return
        channel_id = self._channel_id
        if channel_id is None:  # pragma: no cover - a thread implies a channel
            return
        await self._steps.on_consult(
            request.tool_call_id,
            request.peer,
            None,  # the exchange is inline in THIS thread — nothing to link to
            Destination(channel_id=channel_id, thread_id=thread_id),
            correlation_id=request.correlation_id,
            persona_name=request.caller,
            inline=True,
            request_preview=_request_preview(request.message),
        )

    async def project_consult_result(self, projection: A2AReply | A2AReject | A2AFailed) -> None:
        """Resolve a nested consult's row from its outcome (ADR-0027).

        The state/note mapping is shared with the human-thread row via
        :func:`~calfcord.bridge.a2a_dispatch.consult_outcome`. A never-seen key or
        correlation is silently ignored by the renderer (its row may have been
        dropped for want of a thread), matching the best-effort contract.
        """
        state, note = consult_outcome(projection)
        await self._steps.on_consult_result(
            projection.tool_call_id,
            state=state,
            note=note,
            correlation_id=projection.correlation_id,
        )

    async def seal(self, correlation_id: str, *, faulted: bool) -> None:
        """Close the consulted agents' trace with the run's outcome (ADR-0025).

        Driven by the same stream terminal that seals the human's thread. Without
        it ``finish`` below would seal every consulted trace defensively as
        ``interrupted`` — wrong for a clean run, and it would bury the case this
        whole surface exists for: an agent that made 23 tool calls and then
        faulted must read "run failed after 23 tools", not simply stop.
        """
        await self._steps.seal(correlation_id, faulted=faulted)

    async def finish(self, correlation_id: str) -> None:
        """Retire the turn's projector state.

        MUST run after terminal delivery: a faulted run synthesises dangling
        consult outcomes there, and their cards need the turn's thread mapping.

        The eviction is unconditional (a ``finally``) because ``_threads`` was
        previously never evicted at all. That was tolerable at ~100 bytes per
        turn; it is not now the projector owns a renderer, because an unretired
        entry strands a live asyncio writer task per turn.
        """
        turn = self._turns.get(correlation_id)
        if turn is not None:
            for tool_call_id, branch in list(turn.branches.items()):
                if branch.state != "pending":
                    continue
                await self._guarded(
                    self._resolve_branch(correlation_id, tool_call_id, "interrupted"),
                    "A2AInterruptedCard",
                )
        try:
            await self._steps.finish(correlation_id)
        finally:
            self._threads.pop(correlation_id, None)
            self._turns.pop(correlation_id, None)

    async def _dispatch(self, projection: A2AProjection) -> int:
        """Render one projection and return the thread it landed in."""
        if isinstance(projection, A2ARequest):
            return await self._open_branch(projection)
        elif isinstance(projection, A2AReply):
            turn = self._turns.get(projection.correlation_id)
            has_card = turn is not None and projection.tool_call_id in turn.branches
            if has_card:
                # Card edits are cosmetic: a failed edit must not suppress the peer's
                # substantive response, so it has its own best-effort boundary.
                try:
                    thread_id = await self._resolve_branch(
                        projection.correlation_id, projection.tool_call_id, "replied"
                    )
                except Exception:
                    self._note_gap("A2AReplyCard")
                    thread_id = self._threads[projection.correlation_id]
            else:
                # Nested requests are represented by an inline trace row, not a
                # standalone card. Their reply still needs its explicit return route.
                thread_id = self._threads[projection.correlation_id]
            await self._post_response(projection, thread_id)
            return thread_id
        elif isinstance(projection, A2AReject):
            turn = self._turns.get(projection.correlation_id)
            if turn is None or projection.tool_call_id not in turn.branches:
                return self._threads[projection.correlation_id]
            return await self._resolve_branch(
                projection.correlation_id, projection.tool_call_id, "rejected", note=projection.text
            )
        elif isinstance(projection, A2AFailed):
            turn = self._turns.get(projection.correlation_id)
            if turn is None or projection.tool_call_id not in turn.branches:
                return self._threads[projection.correlation_id]
            return await self._resolve_branch(
                projection.correlation_id, projection.tool_call_id, "failed", note=projection.text
            )
        else:
            assert_never(projection)

    async def _channel(self) -> int:
        if self._channel_id is None:
            self._channel_id = await self._resolver.resolve_unified_channel()
        return self._channel_id

    async def _open_branch(self, request: A2ARequest) -> int:
        channel_id = await self._channel()
        turn = self._turns.setdefault(
            request.correlation_id,
            _Turn(root_agent=request.caller, subject=request.message),
        )
        sent = await self._personas.send_components(
            persona=persona_for(request.caller),
            channel_id=channel_id,
            view=_ConsultCard(request, state="pending"),
            thread_id=turn.thread_id,
        )
        if turn.thread_id is None:
            turn.thread_id = await self._resolver.create_anchored_thread(
                channel_id,
                sent.id,
                name=_build_thread_name(turn.root_agent, turn.subject),
            )
            self._threads[request.correlation_id] = turn.thread_id
        turn.branches[request.tool_call_id] = _Branch(
            request=request,
            message_id=sent.id,
            opened_at=time.monotonic(),
        )
        return turn.thread_id

    async def _resolve_branch(
        self,
        correlation_id: str,
        tool_call_id: str,
        state: BranchState,
        *,
        note: str = "",
    ) -> int:
        turn = self._turns.get(correlation_id)
        if turn is None or turn.thread_id is None:
            raise RuntimeError(f"A2A result has no turn thread: {correlation_id}")
        branch = turn.branches.get(tool_call_id)
        if branch is None:
            raise RuntimeError(f"A2A result has no request card: {tool_call_id}")
        # Runtime truth is independent of Discord projection success. Once the
        # outcome event exists, finish must never relabel it "No response" merely
        # because this best-effort card edit failed.
        branch.state = state
        channel_id = self._channel_id or await self._channel()
        await self._personas.edit_components(
            channel_id=channel_id,
            message_id=branch.message_id,
            view=_ConsultCard(
                branch.request,
                state=state,
                note=note,
                elapsed=max(0.0, time.monotonic() - branch.opened_at),
            ),
            thread_id=turn.thread_id,
        )
        return turn.thread_id

    async def _post_response(self, projection: A2AReply, thread_id: int) -> None:
        await self._steps.flush(projection.correlation_id)
        channel_id = self._channel_id or await self._channel()
        prefix = f"-# **↩ {projection.peer} → {projection.caller} · response**"
        content = projection.text or _EMPTY_PLACEHOLDER
        chunks = chunk_split(f"{prefix}\n\n{content}")
        for chunk in chunks:
            await self._personas.send(
                persona_for(projection.peer), channel_id=channel_id, content=chunk, thread_id=thread_id
            )
