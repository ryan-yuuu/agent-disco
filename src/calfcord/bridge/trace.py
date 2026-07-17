"""Live-step-trace renderer: aggregated Components-V2 step messages (ADR-0017).

The bridge's :class:`~calfcord.bridge.mention_handler.MentionHandler` drains a
run's ``stream()`` and, for every non-A2A
:class:`~calfcord.bridge.step_events.StepEvent`, calls
:meth:`StepTraceRenderer.on_step`; a ``finally`` always calls
:meth:`StepTraceRenderer.finish`. :meth:`StepTraceRenderer.on_consult` folds in
the bridge's OWN annotation of a consult — same trace, no ``StepEvent``, since
the A2A dispatcher intercepts both halves before ``on_step`` is reached.

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
appends (or resolves) rows on the correlation's entry and sets a wake event.
A per-entry writer task flushes every dirty segment (post, then in-place edits),
then waits :data:`_MIN_EDIT_INTERVAL_SECONDS` before the next flush — so an
idle stream renders each step immediately, a burst coalesces into ≤1 edit per
interval, and a Discord stall back-pressures only the trace display, never the
stream drain. :meth:`finish` signals the writer, which does a final flush and
exits — pending content is never lost, and the interval wait is interrupted so
the terminal reply (behind ``finish`` in the handler) is not delayed.

**Persona** is resolved per step. For agent-authored steps (``agent_message``,
``handoff``) the emitter's persona is used so a peer after a handoff stamps its
own identity. For tool steps (``tool_call``, ``tool_result``) the **acting
agent** — the agent currently in control of the run — is used instead, because a
tool is a utility, not a conversational participant. The acting agent is tracked
by the handler (initialised to the mention target, updated on handoff) and passed
in via ``acting_agent``.

**Rows are values** (ADR-0024): a ``tool_call`` opens a PENDING row and its
result RESOLVES that same row in place, keyed by ``tool_call_id`` — results
arrive in completion order, so a row is never found by position. **The seal**
(ADR-0025) closes the trace with the run's outcome, taken from the stream's
terminal.

**Failure semantics.** Every Discord call is best-effort
(:func:`_best_effort_trace`): a failed send/edit must never crash the run or
affect the terminal reply. A failed *post* leaves the segment dirty so the next
wake (or the final flush) retries it — unposted content is never dropped. A
failed *edit* is dropped without a retry (avoiding a hot loop on e.g. a deleted
message); every edit re-renders the segment's full current body, so the next
append heals the gap. A non-Discord error inside a flush is caught and logged
by the writer loop itself, so it cannot unwind ``finish``. A mid-run bridge
restart strands only a persistent partial trace (cosmetic; no state to recover)
— the one case the seal cannot rescue, since the run dies with the bridge.

**Typing** is disabled for now — the fire call is commented out; the notifier is
still accepted (dormant) for a one-line re-enable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import aiohttp
import discord

from calfcord.bridge.persona_resolve import accent_for, persona_for
from calfcord.bridge.trace_rows import (
    _ROW_GROWTH_RESERVE,
    _V2_CHUNK,
    ConsultRow,
    HandoffRow,
    ProseRow,
    RowState,
    SealOutcome,
    SealRow,
    ToolRow,
    TraceRow,
    _chunk_text,
    _plain,
    _summarise_args,
    render_row,
)

if TYPE_CHECKING:
    from calfcord.bridge.mention_handler import MentionRequest
    from calfcord.bridge.step_events import StepEvent
    from calfcord.discord.persona import DiscordPersonaSender, Persona
    from calfcord.discord.typing import TypingNotifier

logger = logging.getLogger(__name__)

_V2_TEXT_LIMIT: Final[int] = 4000
"""Discord's Components-V2 hard cap — per ``TextDisplay`` AND per whole message
(the sum of all text across every container/text display). Verified against the
live API (4000 accepted, 4001 rejected); discord.py does not enforce it
client-side (``LayoutView.content_length()`` is a helper, not a guard). This is a
*message* limit, so the segment owns it; :data:`~calfcord.bridge.trace_rows._ROW_GROWTH_RESERVE`
is the row's concern."""


_MIN_EDIT_INTERVAL_SECONDS: Final[float] = 1.0
"""Leading-edge throttle: the minimum spacing between two flushes of one run's
trace. An idle writer flushes a new step immediately; steps landing inside the
interval coalesce into the next flush. 1s keeps a chatty run comfortably inside
Discord's per-webhook bucket (~5 req/2s) — the same webhook also posts the
terminal reply — and matches what streaming-LLM Discord frontends converge on."""

_ROW_JOINER: Final[str] = "\n"
"""Rows are newline-joined into the segment's single TextDisplay."""

_RESULT_STATE: Final[dict[str, RowState]] = {"success": "ok", "failed": "failed", "denied": "denied"}
"""``StepEvent.outcome`` → the row state it resolves to. ``denied`` stays
distinct from ``failed`` all the way to the glyph: a denial is routine (a winning
handoff stubs its siblings) and must not spend the red a real failure needs."""


def _is_pending(row: TraceRow) -> bool:
    """Whether ``row`` still has a growth reservation booked against it.

    Only the keyed rows resolve; prose, handoffs, and the seal are born final.
    """
    return isinstance(row, ToolRow | ConsultRow) and row.state == "pending"


@dataclass(slots=True)
class _Segment:
    """One Discord message of a step trace: a contiguous run of rows sharing a
    persona, capped at the v2 whole-message limit.

    Rows are VALUES (ADR-0024) — resolving one replaces it and re-renders. That
    is affordable because every flush re-sends the whole body anyway, so
    revising an earlier row costs exactly what appending one costs.

    ``message_id`` stays ``None`` until the first successful post. ``dirty``
    marks content Discord has not shown yet; the writer clears it before
    flushing (a failed POST re-marks it — unposted content must survive to the
    next wake; a failed EDIT does not — the next append re-marks anyway and
    every edit re-renders the full body).
    """

    persona: Persona
    rows: list[TraceRow] = field(default_factory=list)
    message_id: int | None = None
    dirty: bool = False

    @property
    def pending(self) -> int:
        return sum(1 for row in self.rows if _is_pending(row))

    def _join(self, rows: list[TraceRow]) -> str:
        return _ROW_JOINER.join(render_row(row) for row in rows)

    def fits(self, row: TraceRow) -> bool:
        """Whether ``row`` fits — INCLUDING the worst-case growth of every row
        that has yet to resolve.

        A segment that has been posted cannot be re-split, so a resolve must
        never be the thing that overflows it. Each pending row books
        ``_ROW_GROWTH_RESERVE``; the booking is released as it resolves, so the
        cost is transient and proportional to the fan-out actually in flight.
        """
        booked = _ROW_GROWTH_RESERVE * (self.pending + (1 if _is_pending(row) else 0))
        return len(self._join([*self.rows, row])) + booked <= _V2_TEXT_LIMIT

    def append(self, row: TraceRow) -> int:
        """Append ``row``; returns its index, which is stable — rows are never
        removed or reordered, only replaced."""
        self.rows.append(row)
        self.dirty = True
        return len(self.rows) - 1

    def replace(self, index: int, row: TraceRow) -> None:
        self.rows[index] = row
        self.dirty = True

    def body(self) -> str:
        """The segment's rendered body, hard-capped so it is ALWAYS postable.

        The cap is a backstop that :meth:`fits` should keep unreachable, and it
        exists because the alternative failure is silent and permanent: an
        over-cap body is a 400, which the best-effort send swallows to a
        WARNING, leaving the segment dirty and retried on every wake — forever,
        and never rendered.

        Reaching it means the growth-reservation invariant broke, so it warns:
        this is the one place that can silently drop real trace.
        """
        rendered = self._join(self.rows)
        if len(rendered) > _V2_TEXT_LIMIT:
            logger.warning(
                "trace: segment over the v2 cap (%d chars across %d rows); truncating — "
                "the fits() growth reservation did not hold",
                len(rendered),
                len(self.rows),
            )
        return rendered[:_V2_TEXT_LIMIT]


@dataclass(slots=True)
class _Entry:
    """Per-correlation state for one in-flight run's trace.

    ``thread_id`` routes the messages INTO the thread the wire originated in
    (the persona webhook still hosts on the parent ``channel_id``); ``None`` for
    a top-level channel. Only the LAST segment ever receives new rows, so the
    trace stays chronological — but ANY segment may be re-rendered when one of
    its rows resolves. ``wake`` is set on every append (and by ``finish``);
    ``finished`` tells the writer to do a final flush and exit. ``writer`` is
    the entry's single flusher task, awaited by ``finish``.

    ``locate`` is the whole design in one field: ``tool_call_id`` → the row's
    segment and index. Results arrive in COMPLETION order (calfkit fans out
    parallel calls, each folding on its own hop), so a row can only be found by
    id — never by position, and never by assuming adjacency. Indices are stable
    because rows are replaced, never removed or reordered.
    """

    channel_id: int
    thread_id: int | None
    opened_at: float
    segments: list[_Segment] = field(default_factory=list)
    locate: dict[str, tuple[_Segment, int]] = field(default_factory=dict)
    started: dict[str, float] = field(default_factory=dict)
    tool_count: int = 0
    sealed: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    writer: asyncio.Task[None] | None = None


class _SegmentView(discord.ui.LayoutView):
    """One trace message: an accent :class:`~discord.ui.Container` wrapping the
    segment's joined body as a single :class:`~discord.ui.TextDisplay` (one
    TextDisplay per message keeps clear of the per-message component-count cap).
    ``timeout=None`` — the view has no interactive components, so it never
    needs dispatching."""

    def __init__(self, body: str, accent: discord.Colour) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content=body),
                accent_colour=accent,
            )
        )


def _build_segment_view(body: str, accent: discord.Colour) -> discord.ui.LayoutView:
    """Wrap one segment body in a Components-V2 accent container.

    The accent is the acting agent's identity, never the run's state — a
    handoff therefore reads as a colour change, and a failure is carried by its
    row rather than by repainting the whole message.
    """
    return _SegmentView(body, accent)


async def _best_effort_trace[T](coro: Awaitable[T], *, channel_id: int) -> T | None:
    """Await a best-effort trace send/edit, swallowing the usual Discord
    failures so the trace can never crash the run. Returns the call's result,
    or ``None`` if it failed.

    ``NotFound`` (already gone) is DEBUG; ``Forbidden`` and the broader
    ``DiscordException`` (which also funnels the sibling ``RateLimited``, NOT a
    subclass of ``HTTPException``) are WARNING. ``CancelledError`` is a
    ``BaseException`` and is intentionally not caught, so shutdown stays clean.

    ``aiohttp.ClientError`` is caught too, and it is not a theoretical case:
    a dropped keep-alive surfaces as ``ServerDisconnectedError``, which
    discord.py does NOT wrap in a ``DiscordException`` — it comes straight from
    the transport. Left uncaught it escapes to the writer loop, which logs it and
    moves on, but by then ``_flush`` has cleared ``dirty``: the segment is clean,
    never retried, and its content is silently gone. Returning ``None`` instead
    routes a transport blip into the same "post failed, retry next wake" path as
    every other transient failure.
    """
    try:
        return await coro
    except discord.NotFound:
        logger.debug("trace: call hit NotFound channel_id=%d (already gone)", channel_id)
    except discord.Forbidden:
        logger.warning("trace: call Forbidden channel_id=%d", channel_id)
    except discord.DiscordException as e:
        logger.warning(
            "trace: call failed channel_id=%d status=%s: %s",
            channel_id,
            getattr(e, "status", None),
            e,
        )
    except aiohttp.ClientError as e:
        logger.warning("trace: call hit a transport error channel_id=%d: %r", channel_id, e)
    return None


class StepTraceRenderer:
    """Aggregates run steps into throttled, edited-in-place v2 trace messages.

    Satisfies the ``StepTraceRenderer`` protocol the
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
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._persona_sender = persona_sender
        # Dormant: typing is disabled for now (see on_step). Kept so re-enabling
        # is a one-line change and the gateway wiring stays intact.
        self._typing = typing_notifier
        self._min_edit_interval = min_edit_interval
        # Injected so an elapsed assertion is deterministic instead of a race
        # against real time. Monotonic: the trace measures a duration, never a
        # wall-clock instant, so a clock step must not skew it.
        self._now = now
        # Plain dict — finish() removes each correlation deterministically (the
        # handler calls it in a ``finally``), so there is no eviction pressure.
        self._entries: dict[str, _Entry] = {}

    async def on_step(self, step: StepEvent, req: MentionRequest, *, acting_agent: str) -> None:
        """Fold one step into the correlation's trace — no Discord I/O here.

        A ``tool_call`` opens a PENDING row; its ``tool_result`` resolves that
        same row in place, so one tool is one row rather than a called/returned
        pair. A ``handoff`` and an ``agent_message`` append final rows. A step
        that renders nothing touches nothing (no entry, no writer task). The
        writer task (created lazily with the entry) is woken to flush.

        ``acting_agent`` is the agent currently in control of the run (the
        mention target initially, updated on handoff). It is the persona for
        ``tool_call``/``tool_result`` steps so tool trace rows don't appear
        under the tool's own identity; ``agent_message``/``handoff`` steps keep
        ``step.emitter`` — the genuine peer-emitter cases.
        """
        # Tools are utilities, not conversational participants — their trace rows
        # appear under the calling agent's persona. Agent-authored steps
        # (messages, handoffs) keep the emitter's persona so a peer after a
        # handoff stamps its own identity.
        persona_name = acting_agent if step.kind in ("tool_call", "tool_result") else step.emitter
        persona = persona_for(persona_name)

        match step.kind:
            case "tool_call":
                entry = self._entry_for(step.correlation_id, req)
                subject, detail = _summarise_args(step.args or {})
                key = step.tool_call_id or ""
                self._append_keyed(
                    entry,
                    persona,
                    ToolRow(key=key, name=_plain(step.name or "?"), subject=subject, detail=detail),
                )
                entry.started[key] = self._now()
                entry.tool_count += 1
            case "tool_result":
                entry = self._entry_for(step.correlation_id, req)
                self._resolve_tool(entry, persona, step)
            case "handoff":
                entry = self._entry_for(step.correlation_id, req)
                self._append(
                    entry,
                    persona,
                    HandoffRow(
                        target=_plain((step.target or "").removeprefix("/")),
                        reason=_plain(step.reason or ""),
                    ),
                )
            case "agent_message":
                text = step.text.strip()
                if not text:
                    return  # renders nothing → touches nothing
                entry = self._entry_for(step.correlation_id, req)
                for chunk in _chunk_text(text, _V2_CHUNK):
                    self._append(entry, persona, ProseRow(text=chunk))
            case _:
                # Defensive: a future calfkit kind (e.g. ``agent_thinking``) must
                # render nothing rather than fault the drain — logged so the gap
                # is visible.
                logger.warning("trace: no row for step kind %r; rendering nothing", step.kind)
                return
        # Typing disabled for now — re-enable by uncommenting (the notifier is
        # still wired through the gateway, just dormant):
        # if self._typing is not None:
        #     self._typing.fire(entry.thread_id or entry.channel_id)
        entry.wake.set()

    def _resolve_tool(self, entry: _Entry, persona: Persona, step: StepEvent) -> None:
        """Resolve a ``tool_call``'s row in place from its result.

        The row may sit in an OLDER segment than the one currently being
        appended to (cap rollover mid-flight) — that is fine: every segment
        carries its own ``message_id``, so it simply goes dirty and is edited.
        """
        key = step.tool_call_id or ""
        state = _RESULT_STATE.get(step.outcome)
        if state is None:
            # A future calfkit outcome. Degrade to `failed` (visible, honest
            # about not having succeeded) rather than KeyError the step, which
            # would leave the row pending until the seal called it interrupted.
            logger.warning("trace: unknown tool outcome %r; rendering as failed", step.outcome)
            state = "failed"
        note = _plain(step.text)
        located = entry.locate.get(key)
        if located is None:
            # Orphan: no call row was ever seen for this id. Append a resolved
            # row rather than raise — the drain's contract is that the render
            # path can never fault the turn, and a dropped result would also
            # silently under-count the turn's tools. No duration: there was
            # nothing to measure from.
            subject, detail = _summarise_args(step.args or {})
            self._append(
                entry,
                persona,
                ToolRow(
                    key=key,
                    name=_plain(step.name or "?"),
                    subject=subject,
                    detail=detail,
                    state=state,
                    note=note,
                ),
            )
            return
        segment, index = located
        row = segment.rows[index]
        assert isinstance(row, ToolRow)  # locate only ever holds keyed rows
        started = entry.started.pop(key, None)
        elapsed_ms = int((self._now() - started) * 1000) if started is not None else None
        segment.replace(index, replace(row, state=state, note=note, elapsed_ms=elapsed_ms))

    async def on_consult(
        self,
        key: str,
        peer: str,
        thread_url: str | None,
        req: MentionRequest,
        *,
        correlation_id: str,
        persona_name: str,
    ) -> None:
        """Open a PENDING consult row — the bridge's own annotation on the turn.

        Not a run step: the A2A dispatcher intercepts both halves of a
        ``message_agent`` call before ``on_step`` is ever reached, so the
        correlation and persona are passed explicitly. It shares the trace
        deliberately — a consult is part of the same narrative, so under an
        unchanged persona it flows inline rather than opening a second message.

        ``key`` is the consult's ``tool_call_id``, which every A2A projection
        already carries — so the row resolves through exactly the same index as a
        tool row.

        Per ADR-0020 the exchange itself is private: this shows only THAT the
        consult happened and where to read it.
        """
        entry = self._entry_for(correlation_id, req)
        self._append_keyed(
            entry,
            persona_for(persona_name),
            # `peer` is the MODEL's own message_agent argument — unvalidated and
            # unbounded — and this row renders even if the call is then rejected.
            ConsultRow(key=key, peer=_plain(peer), thread_url=thread_url),
        )
        entry.wake.set()

    async def on_consult_result(self, key: str, *, state: RowState, note: str, correlation_id: str) -> None:
        """Resolve a consult row from its reply, rejection, or fault.

        Without this the row keeps its optimistic opening state forever: today's
        marker is written at REQUEST time in the PAST tense and never updated, so
        a consult that was rejected or faulted still reads as though the peer
        answered, while the ⚠️ goes only to the audit thread.

        Silently ignores an unknown key or correlation — same contract as an
        orphan tool result: the render path can never fault the turn.
        """
        entry = self._entries.get(correlation_id)
        if entry is None:
            return
        located = entry.locate.get(key)
        if located is None:
            return
        segment, index = located
        row = segment.rows[index]
        if not isinstance(row, ConsultRow):
            return
        segment.replace(index, replace(row, state=state, note=_plain(note)))
        entry.wake.set()

    def _entry_for(self, correlation_id: str, req: MentionRequest) -> _Entry:
        """The correlation's trace entry, created (with its writer task) on first
        use. Routing is fixed at creation: the webhook hosts on the parent
        ``channel_id`` and posts into ``thread_id`` when the wire came from a
        thread."""
        entry = self._entries.get(correlation_id)
        if entry is None:
            entry = _Entry(
                channel_id=req.channel_id,
                thread_id=(req.source_channel_id if req.source_channel_id != req.channel_id else None),
                opened_at=self._now(),
            )
            entry.writer = asyncio.create_task(self._write_loop(entry))
            self._entries[correlation_id] = entry
        return entry

    async def seal(self, correlation_id: str, *, faulted: bool) -> None:
        """Close the correlation's trace with the run's outcome (ADR-0025).

        Driven by the terminal (``RunCompleted``/``RunFailed``) the drain
        already receives — ``finish`` cannot do this, because it runs in a
        ``finally`` around the drain while the fault only surfaces afterwards in
        ``_await_terminal``. A seal written there would render ``4 tools ·
        12.3s`` on a crashed turn.

        Three jobs, all lookups on the one row index:

        * resolve every still-``pending`` row to ``interrupted`` — a fault
          strands rows mid-flight, and the bridge is alive and knows;
        * append the :class:`SealRow`;
        * mark the entry sealed so ``finish`` does not seal it again.

        A no-op for a correlation that never produced a renderable step: sealing
        must not conjure a trace out of nothing.
        """
        entry = self._entries.get(correlation_id)
        if entry is None or entry.sealed:
            return
        self._seal_entry(entry, outcome="faulted" if faulted else "ok")
        entry.wake.set()

    def _seal_entry(self, entry: _Entry, *, outcome: SealOutcome) -> None:
        """Resolve stranded rows and append the seal. Not async — the writer
        does the I/O."""
        for segment in entry.segments:
            for index, row in enumerate(segment.rows):
                if _is_pending(row):
                    segment.replace(index, replace(row, state="interrupted"))
        entry.locate.clear()
        entry.started.clear()
        persona = entry.segments[-1].persona if entry.segments else persona_for("")
        self._append(
            entry,
            persona,
            SealRow(
                outcome=outcome,
                tool_count=entry.tool_count,
                elapsed_ms=int((self._now() - entry.opened_at) * 1000),
            ),
        )
        entry.sealed = True

    async def finish(self, correlation_id: str) -> None:
        """Flush pending content and retire the correlation's writer.

        Seals defensively first: an entry that reached here unsealed means the
        drain raised, the stream broke, or calfkit violated its
        exactly-one-terminal contract. Sealing it ``interrupted`` keeps a frozen
        ``◐`` — a trace permanently asserting "still running" — off the channel.

        Runs on success AND fault (the handler calls it in a ``finally``). Pops
        the entry, signals the writer — interrupting its interval wait so the
        terminal reply behind this call is not delayed — and awaits its final
        flush. The trace messages persist; nothing is deleted. A no-op for a
        correlation that never produced a renderable step.
        """
        entry = self._entries.pop(correlation_id, None)
        if entry is None:
            return
        if not entry.sealed:
            logger.warning(
                "trace: correlation_id=%s finished unsealed (no stream terminal); sealing as interrupted",
                correlation_id,
            )
            # NOT "faulted": no terminal was seen, so the outcome is genuinely
            # unknown — result() may still have returned a reply.
            self._seal_entry(entry, outcome="interrupted")
        entry.finished.set()
        entry.wake.set()
        if entry.writer is not None:
            await entry.writer

    def _append(self, entry: _Entry, persona: Persona, row: TraceRow) -> tuple[_Segment, int]:
        """Append one row, opening a new segment when needed.

        Only the last segment is appendable (chronological order); a persona
        change or a cap overflow closes it. ``persona_for`` is deterministic, so
        dataclass equality is a correct same-identity check.

        A row that fits nowhere — not even a fresh segment — is still appended:
        there is nowhere else for it to go, and ``_Segment.body`` caps the result
        so the segment stays postable. Dropping it instead would lose trace, and
        posting an over-cap body would strand the segment dirty forever.
        """
        segment = entry.segments[-1] if entry.segments else None
        if segment is None or segment.persona != persona or not segment.fits(row):
            segment = _Segment(persona=persona)
            entry.segments.append(segment)
        return segment, segment.append(row)

    def _append_keyed(self, entry: _Entry, persona: Persona, row: ToolRow | ConsultRow) -> None:
        """Append a row that will later resolve, and record where it landed."""
        entry.locate[row.key] = self._append(entry, persona, row)

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
                logger.exception("trace: flush failed; continuing")
            # `and not wake` is load-bearing: a flush awaits a full Discord
            # round-trip, and anything appended DURING it re-sets `wake`.
            # Exiting on `finished` alone would drop that content permanently —
            # the entry is already popped and the writer is gone, so the message
            # would stay as-is forever. The seal is the likeliest casualty: it
            # lands exactly when the writer is busy, so the trace would freeze
            # mid-`◐`, which is what ADR-0025 exists to prevent.
            #
            # It cannot spin: only an append sets `wake`, and after `finish` the
            # drain is done, so at most one more pass runs. A failed POST
            # re-marks `dirty` WITHOUT setting `wake`, so a persistently failing
            # send still exits after one retry rather than hot-looping.
            if entry.finished.is_set() and not entry.wake.is_set():
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
            # Render BEFORE clearing dirty: if the render raises, the writer
            # loop logs it and continues — and a segment already marked clean
            # would never be retried, so its content would be gone silently.
            view = _build_segment_view(segment.body(), accent_for(segment.persona.name))
            segment.dirty = False
            if segment.message_id is None:
                sent = await _best_effort_trace(
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
                await _best_effort_trace(
                    self._persona_sender.edit_components(
                        channel_id=entry.channel_id,
                        message_id=segment.message_id,
                        view=view,
                        thread_id=entry.thread_id,
                    ),
                    channel_id=entry.channel_id,
                )
