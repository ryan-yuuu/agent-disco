"""Live-progress renderer: aggregated Components-V2 step messages (ADR-0017).

The bridge's :class:`~calfcord.bridge.mention_handler.MentionHandler` drains a
run's ``stream()`` and, for every non-A2A
:class:`~calfcord.bridge.step_events.StepEvent`, calls
:meth:`ProgressRenderer.on_step`; a ``finally`` always calls
:meth:`ProgressRenderer.finish`.

Steps are aggregated into ONE growing, persistent **Components-V2** message per
persona *segment*: the first renderable step posts the message, later steps are
appended and the message is edited in place. A new message starts only when

* the **persona changes** — a webhook edit cannot change ``username``/
  ``avatar_url``, so a handoff (or a peer emitter) opens a fresh message under
  the new identity; or
* the **4000-char v2 cap** would overflow — rollover to a fresh message, same
  persona, full trace preserved (no elision).

Nothing is ever deleted: the aggregate(s) persist as the turn's visible trace.
Because a v2 message carries no ``content`` (only components), the bridge's
history fetcher excludes these from an agent's ``message_history`` — the
model's tool memory rides the separate transcript replay, so the display never
double-counts (the ADR-0016 invariant, unchanged).

**Writer task (leading-edge throttle).** ``on_step`` never touches Discord: it
appends the rendered block(s) to the correlation's entry and sets a wake event.
A per-entry writer task flushes every dirty segment (post, then in-place edits),
then waits :data:`_MIN_EDIT_INTERVAL_SECONDS` before the next flush — so an
idle stream renders each step immediately, a burst coalesces into ≤1 edit per
interval, and a Discord stall back-pressures only the trace display, never the
stream drain. :meth:`finish` signals the writer, which does a final flush and
exits — pending content is never lost, and the interval wait is interrupted so
the terminal reply (behind ``finish`` in the handler) is not delayed.

**Persona** is resolved per step. For agent-authored steps (``agent_message``,
``handoff``) the emitter's persona is used so a peer after a handoff stamps its
own identity. For tool steps (``tool_call``, ``tool_result``) the *owning agent*
—the agent currently in control of the run—is used instead, because a tool is a
utility, not a conversational participant. The owning agent is tracked by the
handler (initialized to the mention target, updated on handoff) and passed in
via ``owning_agent``.

**Failure semantics.** Every Discord call is best-effort
(:func:`_best_effort_progress`): a failed send/edit must never crash the run or
affect the terminal reply. A failed *post* leaves the segment dirty so the next
wake (or the final flush) retries it — unposted content is never dropped. A
failed *edit* is dropped without a retry (avoiding a hot loop on e.g. a deleted
message); every edit re-renders the segment's full current body, so the next
append heals the gap. A non-Discord error inside a flush is caught and logged
by the writer loop itself, so it cannot unwind ``finish``. A mid-run bridge
restart strands only a persistent partial trace (cosmetic; no state to recover).

**Typing** is disabled for now — the fire call is commented out; the notifier is
still accepted (dormant) for a one-line re-enable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import discord

from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.steps_render import _V2_TEXT_LIMIT, render_step_message

if TYPE_CHECKING:
    from calfcord.bridge.mention_handler import MentionRequest
    from calfcord.bridge.step_events import StepEvent
    from calfcord.discord.persona import DiscordPersonaSender, Persona
    from calfcord.discord.typing import TypingNotifier

logger = logging.getLogger(__name__)

_V2_ACCENT = discord.Colour(0xE74C3C)
"""Accent stripe (Discord red) on every step-trace message's container."""

_MIN_EDIT_INTERVAL_SECONDS: Final[float] = 1.0
"""Leading-edge throttle: the minimum spacing between two flushes of one run's
trace. An idle writer flushes a new step immediately; steps landing inside the
interval coalesce into the next flush. 1s keeps a chatty run comfortably inside
Discord's per-webhook bucket (~5 req/2s) — the same webhook also posts the
terminal reply — and matches what streaming-LLM Discord frontends converge on."""

_BLOCK_JOINER: Final[str] = "\n"
"""Rendered blocks are newline-joined into the segment's single TextDisplay."""


@dataclass(slots=True)
class _Segment:
    """One Discord message of the aggregate: a contiguous run of blocks sharing
    a persona, capped at the v2 whole-message limit.

    ``message_id`` stays ``None`` until the first successful post. ``chars`` is
    the joined-body length, maintained incrementally so the cap check is O(1).
    ``dirty`` marks content Discord has not shown yet; the writer clears it
    before flushing (a failed POST re-marks it — unposted content must survive
    to the next wake; a failed EDIT does not — the next append re-marks anyway
    and every edit re-renders the full body).
    """

    persona: Persona
    blocks: list[str] = field(default_factory=list)
    chars: int = 0
    message_id: int | None = None
    dirty: bool = False

    def fits(self, block: str) -> bool:
        return self.chars + len(_BLOCK_JOINER) + len(block) <= _V2_TEXT_LIMIT

    def append(self, block: str) -> None:
        self.chars += len(block) if not self.blocks else len(_BLOCK_JOINER) + len(block)
        self.blocks.append(block)
        self.dirty = True

    def body(self) -> str:
        return _BLOCK_JOINER.join(self.blocks)


@dataclass(slots=True)
class _Entry:
    """Per-correlation state for one in-flight run's trace.

    ``thread_id`` routes the messages INTO the thread the wire originated in
    (the persona webhook still hosts on the parent ``channel_id``); ``None`` for
    a top-level channel. Only the LAST segment ever receives new blocks, so the
    trace stays chronological. ``wake`` is set on every append (and by
    ``finish``); ``finished`` tells the writer to do a final flush and exit.
    ``writer`` is the entry's single flusher task, awaited by ``finish``.
    """

    channel_id: int
    thread_id: int | None
    segments: list[_Segment] = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    writer: asyncio.Task[None] | None = None


class _SegmentView(discord.ui.LayoutView):
    """One trace message: an accent :class:`~discord.ui.Container` wrapping the
    segment's joined body as a single :class:`~discord.ui.TextDisplay` (one
    TextDisplay per message keeps clear of the per-message component-count cap).
    ``timeout=None`` — the view has no interactive components, so it never
    needs dispatching."""

    def __init__(self, body: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content=body),
                accent_colour=_V2_ACCENT,
            )
        )


def _build_segment_view(body: str) -> discord.ui.LayoutView:
    """Wrap one segment body in a Components-V2 accent container."""
    return _SegmentView(body)


async def _best_effort_progress[T](coro: Awaitable[T], *, channel_id: int) -> T | None:
    """Await a best-effort trace send/edit, swallowing the usual Discord
    failures so the trace can never crash the run. Returns the call's result,
    or ``None`` if it failed.

    ``NotFound`` (already gone) is DEBUG; ``Forbidden`` and the broader
    ``DiscordException`` (which also funnels the sibling ``RateLimited``, NOT a
    subclass of ``HTTPException``) are WARNING. ``CancelledError`` is a
    ``BaseException`` and is intentionally not caught, so shutdown stays clean.
    """
    try:
        return await coro
    except discord.NotFound:
        logger.debug("progress: trace call hit NotFound channel_id=%d (already gone)", channel_id)
    except discord.Forbidden:
        logger.warning("progress: trace call Forbidden channel_id=%d", channel_id)
    except discord.DiscordException as e:
        logger.warning(
            "progress: trace call failed channel_id=%d status=%s: %s",
            channel_id,
            getattr(e, "status", None),
            e,
        )
    return None


class ProgressRenderer:
    """Aggregates run steps into throttled, edited-in-place v2 trace messages.

    Satisfies the ``ProgressRenderer`` protocol the
    :class:`~calfcord.bridge.mention_handler.MentionHandler` injects. Construct
    once per bridge process from the REST-only persona sender and (optionally) a
    typing notifier (currently dormant). ``min_edit_interval`` exists for tests;
    production uses the default.
    """

    def __init__(
        self,
        persona_sender: DiscordPersonaSender,
        typing_notifier: TypingNotifier | None = None,
        *,
        min_edit_interval: float = _MIN_EDIT_INTERVAL_SECONDS,
    ) -> None:
        self._persona_sender = persona_sender
        # Dormant: typing is disabled for now (see on_step). Kept so re-enabling
        # is a one-line change and the gateway wiring stays intact.
        self._typing = typing_notifier
        self._min_edit_interval = min_edit_interval
        # Plain dict — finish() removes each correlation deterministically (the
        # handler calls it in a ``finally``), so there is no eviction pressure.
        self._entries: dict[str, _Entry] = {}

    async def on_step(self, step: StepEvent, req: MentionRequest, *, owning_agent: str) -> None:
        """Fold one step into the correlation's aggregate — no Discord I/O here.

        Renders the step (:func:`render_step_message`) — a tool call/result
        short line, a handoff note, or the full agent text split into ≤-cap
        chunks — and appends each block to the entry's latest segment, starting
        a new segment on a persona change or when the v2 cap would overflow.
        A step that renders nothing touches nothing (no entry, no writer task).
        The writer task (created lazily with the entry) is woken to flush.

        ``owning_agent`` is the agent currently in control of the run (the
        mention target initially, updated on handoff). It is the persona for
        ``tool_call``/``tool_result`` steps so tool progress lines don't appear
        under the tool's own identity; ``agent_message``/``handoff`` steps keep
        ``step.emitter`` — the genuine peer-emitter cases.
        """
        blocks = render_step_message(step)
        if not blocks:
            return
        # Tools are utilities, not conversational participants — their progress
        # lines appear under the calling agent's persona. Agent-authored steps
        # (messages, handoffs) keep the emitter's persona so a peer after a
        # handoff stamps its own identity.
        persona_name = owning_agent if step.kind in ("tool_call", "tool_result") else step.emitter
        persona = persona_for(persona_name)
        entry = self._entries.get(step.correlation_id)
        if entry is None:
            entry = _Entry(
                channel_id=req.channel_id,
                thread_id=(req.source_channel_id if req.source_channel_id != req.channel_id else None),
            )
            entry.writer = asyncio.create_task(self._write_loop(entry))
            self._entries[step.correlation_id] = entry
        # Typing disabled for now — re-enable by uncommenting (the notifier is
        # still wired through the gateway, just dormant):
        # if self._typing is not None:
        #     self._typing.fire(entry.thread_id or entry.channel_id)
        for block in blocks:
            self._append(entry, persona, block)
        entry.wake.set()

    async def finish(self, correlation_id: str) -> None:
        """Flush pending content and retire the correlation's writer.

        Runs on success AND fault (the handler calls it in a ``finally``). Pops
        the entry, signals the writer — interrupting its interval wait so the
        terminal reply behind this call is not delayed — and awaits its final
        flush. The trace messages persist; nothing is deleted. A no-op for a
        correlation that never produced a renderable step.
        """
        entry = self._entries.pop(correlation_id, None)
        if entry is None:
            return
        entry.finished.set()
        entry.wake.set()
        if entry.writer is not None:
            await entry.writer

    def _append(self, entry: _Entry, persona: Persona, block: str) -> None:
        """Append one rendered block, opening a new segment when needed.

        Only the last segment is appendable (chronological order); a persona
        change or a cap overflow closes it. ``persona_for`` is deterministic, so
        dataclass equality is a correct same-identity check.
        """
        segment = entry.segments[-1] if entry.segments else None
        if segment is None or segment.persona != persona or not segment.fits(block):
            segment = _Segment(persona=persona)
            entry.segments.append(segment)
        segment.append(block)

    async def _write_loop(self, entry: _Entry) -> None:
        """The entry's single flusher: wake → flush → spacing → repeat.

        The spacing wait is interruptible ONLY by ``finished`` (never by new
        content), so two flushes are always ≥ the interval apart; an idle writer
        parked on ``wake`` flushes a fresh step immediately (leading edge). A
        flush bug (non-Discord) is contained here — logged, loop continues — so
        it can never unwind ``finish`` or cost the terminal reply.
        """
        while True:
            await entry.wake.wait()
            entry.wake.clear()
            try:
                await self._flush(entry)
            except Exception:
                logger.exception("progress: trace flush failed; continuing")
            if entry.finished.is_set():
                return
            if self._min_edit_interval > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(entry.finished.wait(), self._min_edit_interval)
            else:
                await asyncio.sleep(0)  # tests: still yield so the loop can't starve

    async def _flush(self, entry: _Entry) -> None:
        """Render every dirty segment to Discord: first post, then in-place edits.

        The body snapshot is taken before the await; a block appended mid-call
        re-marks the segment dirty and is carried by the next flush. Iterates a
        copy — ``on_step`` may append a NEW segment while an older one is being
        sent (it is picked up on the next wake, which that append also set).
        """
        for segment in list(entry.segments):
            if not segment.dirty:
                continue
            segment.dirty = False
            view = _build_segment_view(segment.body())
            if segment.message_id is None:
                sent = await _best_effort_progress(
                    self._persona_sender.send_components(
                        persona=segment.persona,
                        channel_id=entry.channel_id,
                        view=view,
                        thread_id=entry.thread_id,
                    ),
                    channel_id=entry.channel_id,
                )
                if sent is not None:
                    segment.message_id = sent.id
                else:
                    # Unposted content must not be lost: retry on the next wake
                    # (or the final flush). Bounded — one attempt per wake.
                    segment.dirty = True
            else:
                await _best_effort_progress(
                    self._persona_sender.edit_components(
                        channel_id=entry.channel_id,
                        message_id=segment.message_id,
                        view=view,
                        thread_id=entry.thread_id,
                    ),
                    channel_id=entry.channel_id,
                )
