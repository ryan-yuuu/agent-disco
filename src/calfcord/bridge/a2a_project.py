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
from collections.abc import Awaitable
from typing import assert_never

from calfcord.bridge.a2a_dispatch import (
    A2ACall,
    A2AFailed,
    A2AProjection,
    A2AReject,
    A2AReply,
    A2ARequest,
)
from calfcord.bridge.egress import A2AChannelResolver
from calfcord.bridge.persona_resolve import persona_for
from calfcord.discord.chunking import chunk_split
from calfcord.discord.persona import DiscordPersonaSender, Persona

logger = logging.getLogger(__name__)

_EMPTY_PLACEHOLDER = "(empty response)"
"""Discord rejects an empty webhook message; substitute this for empty content."""

_SYSTEM_PERSONA = Persona(name="a2a")
"""Persona for *meta* notes (rejections, handoffs, faults) that are not a peer's
own words — rendered as a system annotation, not attributed to an agent (D-2)."""

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
_THREAD_NAME_CONTENT_MAX = 40
"""Soft cap on the topic-tail portion (after ``caller→peer: ``)."""
_THREAD_NAME_EMPTY_PLACEHOLDER = "<empty>"
"""Substituted when the seed content is empty, avoiding a bare-trailing-space name."""


def _build_thread_name(caller: str, peer: str, content: str) -> str:
    """Produce a thread name like ``'conan→scribe: please summarize the doc'``.

    Control characters are normalized to spaces and runs collapsed; the topic tail
    is truncated to :data:`_THREAD_NAME_CONTENT_MAX` and the whole name hard-capped
    at :data:`_THREAD_NAME_MAX_TOTAL` (Discord's limit). The ``→`` (U+2192)
    separator is char-counted (not byte-counted) by Discord, so it is cheap.
    """
    cleaned = " ".join("".join(c if c.isprintable() else " " for c in content).split())
    if not cleaned:
        cleaned = _THREAD_NAME_EMPTY_PLACEHOLDER
    name = f"{caller}→{peer}: {cleaned[:_THREAD_NAME_CONTENT_MAX]}"
    return name[:_THREAD_NAME_MAX_TOTAL]


class A2AProjector:
    """Renders :class:`A2AProjection`s into the unified A2A audit channel.

    One instance per bridge; its ``correlation_id → thread_id`` map is the only
    state, mirroring one thread per human turn's A2A activity.
    """

    def __init__(self, resolver: A2AChannelResolver, personas: DiscordPersonaSender) -> None:
        self._resolver = resolver
        self._personas = personas
        self._threads: dict[str, int] = {}
        self._channel_id: int | None = None
        self._degraded = False

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
        """Note a consult that never got a reply because the peer faulted (D-2)."""
        await self._guarded(
            self._emit(
                call.correlation_id,
                _SYSTEM_PERSONA,
                f"⚠️ {call.peer} did not reply — the consult faulted before a response.",
                thread_name=_build_thread_name(call.caller, call.peer, call.message),
            ),
            "A2AFault",
        )

    async def _dispatch(self, projection: A2AProjection) -> int:
        """Render one projection and return the thread it landed in."""
        if isinstance(projection, A2ARequest):
            return await self._emit(
                projection.correlation_id,
                persona_for(projection.caller),
                projection.message,
                thread_name=_build_thread_name(projection.caller, projection.peer, projection.message),
            )
        elif isinstance(projection, A2AReply):
            return await self._emit(
                projection.correlation_id,
                persona_for(projection.peer),
                projection.text,
                thread_name=_build_thread_name(projection.caller, projection.peer, projection.text),
            )
        elif isinstance(projection, A2AReject):
            # A rejected consult (peer offline / cycle / self) is a system note,
            # not a peer post — the peer never spoke (D-2).
            return await self._emit(
                projection.correlation_id,
                _SYSTEM_PERSONA,
                f"⚠️ consult to {projection.peer} was rejected: {projection.text}",
                thread_name=_build_thread_name(projection.caller, projection.peer, projection.text),
            )
        elif isinstance(projection, A2AFailed):
            # A consult that reached the peer but faulted — a system note distinct
            # from a refused dispatch (A2AReject); the peer engaged but errored.
            return await self._emit(
                projection.correlation_id,
                _SYSTEM_PERSONA,
                f"💥 consult to {projection.peer} failed: {projection.text}",
                thread_name=_build_thread_name(projection.caller, projection.peer, projection.text),
            )
        else:
            # Exhaustiveness guard: a 4th A2AProjection variant added without a
            # branch here is a mypy error, not a silent no-op render. (Handoffs
            # are NOT projected here — they render inline in the main step stream
            # via the progress renderer; the dispatcher no longer emits them.)
            assert_never(projection)

    async def _channel(self) -> int:
        if self._channel_id is None:
            self._channel_id = await self._resolver.resolve_unified_channel()
        return self._channel_id

    async def _emit(self, correlation_id: str, persona: Persona, content: str, *, thread_name: str) -> int:
        """Post ``content`` under ``persona`` into ``correlation_id``'s thread, and
        return that thread's id.

        Creates the thread lazily on the first projection for the turn — the first
        content chunk becomes the thread's anchor/starter message — then posts any
        remaining chunks into the thread. Content over Discord's 2000-char limit is
        split (:func:`chunk_split`); empty content uses :data:`_EMPTY_PLACEHOLDER`.
        """
        channel_id = await self._channel()
        chunks = chunk_split(content) or [_EMPTY_PLACEHOLDER]
        thread_id = self._threads.get(correlation_id)
        if thread_id is None:
            sent = await self._personas.send(persona, channel_id=channel_id, content=chunks[0])
            thread_id = await self._resolver.create_anchored_thread(channel_id, sent.id, name=thread_name)
            self._threads[correlation_id] = thread_id
            rest = chunks[1:]
        else:
            rest = chunks
        for chunk in rest:
            await self._personas.send(persona, channel_id=channel_id, content=chunk, thread_id=thread_id)
        return thread_id
