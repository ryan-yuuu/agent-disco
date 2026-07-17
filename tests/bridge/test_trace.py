"""Unit tests for :class:`calfcord.bridge.trace.StepTraceRenderer`.

Aggregated model (ADR-0017): the
:class:`~calfcord.bridge.mention_handler.MentionHandler` drains a run's
``stream()`` and calls ``on_step`` per non-A2A ``StepEvent``; a ``finally`` calls
``finish``. Steps are aggregated into ONE growing Components-V2 message per
persona segment — posted on the first renderable step, then edited in place as
later steps append. A new message starts only when the persona changes (webhook
edits cannot change identity) or when the 4000-char v2 cap would overflow
(rollover). Edits are paced by a leading-edge throttle (min interval between
edits, immediate when the writer is idle); ``finish`` flushes pending content
and the messages persist (nothing is deleted).

``on_step`` itself never touches Discord — a per-run writer task does — so the
tests drive the writer by yielding to the event loop (``_until``) and assert on
a recording fake sender. discord.py and the LLM stack are mocked out; the repo
runs ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import aiohttp
import discord
import pytest

import calfcord.bridge.trace as trace_mod
import calfcord.bridge.trace_rows as trace_rows
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.persona_resolve import accent_for, persona_for
from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.trace import StepTraceRenderer, _build_segment_view, _Segment
from calfcord.bridge.trace_rows import ConsultRow, ProseRow, ToolRow
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage

_CORRELATION_ID = "evt-1"
_CHANNEL_ID = 6789
_MESSAGE_ID = 12345


def _reject_what_discord_would(body: str) -> None:
    """Fail loudly on a body the real API would reject.

    The renderer swallows a 400 to a WARNING and leaves the segment dirty
    forever, so in production this failure is silent and permanent. In tests it
    should be the opposite: loud, and attributed to whichever test caused it.
    """
    assert body, "Discord rejects an empty TextDisplay (min length 1)"
    assert len(body) <= trace_mod._V2_TEXT_LIMIT, (
        f"body is {len(body)} chars — Discord rejects over {trace_mod._V2_TEXT_LIMIT}; "
        "the growth reservation did not hold"
    )


class _FakeSender:
    """Recording stand-in for :class:`DiscordPersonaSender`.

    Records every ``send_components`` / ``edit_components`` ATTEMPT (including
    failing ones) with the view's body extracted at call time, so tests can
    assert exactly what would have rendered on Discord. Queued exceptions are
    raised FIFO, letting tests script per-call failures.

    It also ADJUDICATES, rather than accepting anything: Discord rejects a body
    over 4000 chars or under 1, so this does too. That makes every test in this
    file a cap regression test for free — which matters, because the growth
    reservation's whole job is keeping the segment's silent hard cap
    unreachable, and a fake that accepts any length can never notice it failing.
    """

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.send_failures: list[Exception] = []
        self.edit_failures: list[Exception] = []
        self._next_id = 1000

    async def send_components(
        self,
        *,
        persona: Any,
        channel_id: int,
        view: discord.ui.LayoutView,
        thread_id: int | None = None,
    ) -> SentMessage:
        call = {
            "persona": persona,
            "channel_id": channel_id,
            "thread_id": thread_id,
            "body": _view_body(view),
            "accent": _view_accent_of(view),
            "ok": True,
        }
        _reject_what_discord_would(call["body"])
        self.sends.append(call)
        if self.send_failures:
            call["ok"] = False
            raise self.send_failures.pop(0)
        self._next_id += 1
        call["message_id"] = self._next_id
        return SentMessage(id=self._next_id, channel_id=thread_id if thread_id is not None else channel_id)

    async def edit_components(
        self,
        *,
        channel_id: int,
        message_id: int,
        view: discord.ui.LayoutView,
        thread_id: int | None = None,
    ) -> None:
        call = {
            "channel_id": channel_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "body": _view_body(view),
            "accent": _view_accent_of(view),
            "ok": True,
        }
        _reject_what_discord_would(call["body"])
        self.edits.append(call)
        if self.edit_failures:
            call["ok"] = False
            raise self.edit_failures.pop(0)

    def ok_sends(self) -> list[dict[str, Any]]:
        return [c for c in self.sends if c["ok"]]

    def ok_edits(self) -> list[dict[str, Any]]:
        return [c for c in self.edits if c["ok"]]


class _BlockingSender(_FakeSender):
    """A sender whose calls actually SUSPEND, like real HTTP does.

    :class:`_FakeSender` never awaits, so the writer task can never be observed
    mid-flush and a whole class of races is structurally invisible to the suite.
    This one parks inside the call until released, making "content appended
    while a flush is in flight" a deterministic scenario rather than a timing
    accident.

    BOTH calls block. The edit matters more than the send: in a real turn the
    message is posted long before the terminal arrives, so the seal almost always
    races an in-flight EDIT, not a send.
    """

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()

    async def _park(self) -> None:
        self.in_flight.set()
        await self.release.wait()

    async def send_components(self, **kwargs: Any) -> SentMessage:
        await self._park()
        return await super().send_components(**kwargs)

    async def edit_components(self, **kwargs: Any) -> None:
        await self._park()
        await super().edit_components(**kwargs)


def _view_body(view: discord.ui.LayoutView) -> str:
    """The text of the view's single ``TextDisplay`` (the segment body)."""
    bodies = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
    assert len(bodies) == 1, f"expected exactly one TextDisplay per segment view, got {len(bodies)}"
    return bodies[0]


def _view_accent_of(view: discord.ui.LayoutView) -> discord.Colour | None:
    """The accent stripe of the view's single Container."""
    containers = [item for item in view.walk_children() if isinstance(item, discord.ui.Container)]
    assert len(containers) == 1, f"expected exactly one Container per segment view, got {len(containers)}"
    return containers[0].accent_colour


def _view_accent(call: dict[str, Any]) -> discord.Colour | None:
    return call["accent"]  # type: ignore[no-any-return]


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Yield to the event loop until ``predicate`` holds (the writer task runs
    between iterations). Fails the test on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out waiting for the writer task"
        await asyncio.sleep(0.001)


def _first_row(sender: _FakeSender) -> str:
    """The first row of the trace's latest body.

    A finished trace always ends with a seal row, so tests about ROW rendering
    look at the row rather than the whole body.
    """
    return _last_body(sender).split("\n")[0]


async def _end(renderer: StepTraceRenderer, *, faulted: bool = False) -> None:
    """End a run the way the handler does: the stream's terminal seals, then the
    ``finally`` finishes. Tests that call ``finish`` alone are modelling a drain
    that died, and will (correctly) get a defensive ``interrupted`` seal."""
    await renderer.seal(_CORRELATION_ID, faulted=faulted)
    await renderer.finish(_CORRELATION_ID)


def _last_body(sender: _FakeSender) -> str:
    """The most recent body Discord would have seen for a segment.

    Whether that lands as a send or an edit depends on whether the writer got a
    turn between appends — which is a throttle detail, not the thing these tests
    are about.
    """
    calls = sender.ok_edits() or sender.ok_sends()
    assert calls, "nothing was posted"
    return str(calls[-1]["body"])


async def _settle(cycles: int = 20) -> None:
    """Give the writer task ample loop cycles WITHOUT asserting anything —
    used before asserting that something did NOT happen."""
    for _ in range(cycles):
        await asyncio.sleep(0.001)


def _req(*, channel_id: int = _CHANNEL_ID, source_channel_id: int = _CHANNEL_ID) -> MentionRequest:
    """A mention request. ``source_channel_id != channel_id`` represents a wire
    that originated inside a Discord thread (the renderer reads only these two)."""
    return MentionRequest(
        content="hello",
        mention_ids=("aksel",),
        author_label="alice",
        message_id=_MESSAGE_ID,
        source_channel_id=source_channel_id,
        channel_id=channel_id,
        wire=WireMessage(
            event_id="e1",
            kind="message",
            message_id=_MESSAGE_ID,
            channel_id=channel_id,
            source_channel_id=source_channel_id,
            guild_id=1,
            content="hello",
            author=WireAuthor(discord_user_id=1, display_name="alice", is_bot=False, is_webhook=False),
            created_at=datetime.now(UTC),
        ),
        reply_target=None,
    )


def _step(
    kind: str,
    *,
    emitter: str = "aksel",
    correlation_id: str = _CORRELATION_ID,
    text: str = "",
    name: str | None = None,
    args: dict[str, object] | None = None,
    outcome: str = "success",
    target: str | None = None,
    reason: str | None = None,
    tool_call_id: str | None = None,
) -> StepEvent:
    return StepEvent(
        kind=kind,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        depth=0,
        emitter=emitter,
        text=text,
        name=name,
        args=args,
        outcome=outcome,  # type: ignore[arg-type]
        target=target,
        reason=reason,
        tool_call_id=tool_call_id,
    )


def _call(tool_call_id: str, name: str, args: dict[str, object] | None = None, **kw: Any) -> StepEvent:
    return _step("tool_call", tool_call_id=tool_call_id, name=name, args=args or {}, **kw)


def _result(tool_call_id: str, name: str, **kw: Any) -> StepEvent:
    return _step("tool_result", tool_call_id=tool_call_id, name=name, **kw)


class _FakeClock:
    """A monotonic clock the tests advance by hand, so an elapsed assertion is
    deterministic rather than a race against real time."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


@pytest.fixture
def sender() -> _FakeSender:
    return _FakeSender()


def _renderer(sender: _FakeSender, *, interval: float = 0.0) -> StepTraceRenderer:
    """A renderer with a deterministic edit cadence: ``interval=0`` flushes as
    fast as the loop turns; a large interval parks the writer so coalescing can
    be asserted.

    The clock is FROZEN, so every resolved row renders ``· 0ms``. Without this
    the rendered duration races real time — ``_until`` sleeps between polls, so
    a row would read ``0ms`` or ``1ms`` depending on scheduling. Tests that care
    about elapsed drive their own :class:`_FakeClock`.
    """
    return StepTraceRenderer(sender, min_edit_interval=interval, now=lambda: 0.0)  # type: ignore[arg-type]


class TestSegmentHoldsRows:
    """A segment holds row VALUES and folds ``render_row`` at flush (ADR-0024).

    Resolving replaces a row rather than mutating a pre-rendered string, so the
    state machine lives in the type and these tests assert on values, not on
    cosmetics.
    """

    def _segment(self) -> _Segment:
        return _Segment(persona=persona_for("aksel"))

    def test_body_folds_render_row_over_its_rows(self) -> None:
        seg = self._segment()
        seg.append(ProseRow(text="Let me look into that."))
        seg.append(ToolRow(key="t1", name="read_file", subject="a.py", state="ok", elapsed_ms=40))
        assert seg.body() == "Let me look into that.\n-# ● read_file a.py · 40ms"

    def test_replacing_a_row_re_renders_it_in_place(self) -> None:
        seg = self._segment()
        index = seg.append(ToolRow(key="t1", name="read_file", subject="a.py"))
        assert seg.body() == "◐ read_file a.py"
        seg.replace(index, ToolRow(key="t1", name="read_file", subject="a.py", state="ok", elapsed_ms=40))
        assert seg.body() == "-# ● read_file a.py · 40ms"

    def test_append_and_replace_both_mark_the_segment_dirty(self) -> None:
        seg = self._segment()
        index = seg.append(ProseRow(text="hi"))
        seg.dirty = False
        seg.replace(index, ProseRow(text="bye"))
        assert seg.dirty is True

    def test_a_pending_row_reserves_room_for_its_growth(self) -> None:
        # The reservation rule: a POSTED segment cannot be re-split, so a row
        # that will grow when it resolves must have that growth booked now.
        # Sized so the reserve is the ONLY thing separating the two: the longer
        # (resolved) row fits, the shorter (pending) one does not.
        seg = self._segment()
        seg.append(ProseRow(text="x" * (trace_mod._V2_TEXT_LIMIT - 60)))
        assert seg.fits(ToolRow(key="t2", name="read_file", state="ok", elapsed_ms=40)) is True
        assert seg.fits(ToolRow(key="t1", name="read_file")) is False

    def test_the_reservation_is_released_when_a_row_resolves(self) -> None:
        seg = self._segment()
        seg.append(ProseRow(text="x" * 3900))
        index = seg.append(ToolRow(key="t1", name="read_file"))
        crowded = ToolRow(key="t2", name="b", state="ok", elapsed_ms=1)
        assert seg.fits(crowded) is False  # t1's reserve is still booked
        seg.replace(index, ToolRow(key="t1", name="read_file", state="ok", elapsed_ms=40))
        assert seg.fits(crowded) is True  # released

    def test_only_pending_rows_reserve(self) -> None:
        seg = self._segment()
        for _ in range(5):
            seg.append(ToolRow(key="k", name="t", state="ok", elapsed_ms=1))
        assert seg.pending == 0

    def test_pending_counts_consult_rows_too(self) -> None:
        # A consult row is keyed and resolves exactly like a tool row.
        seg = self._segment()
        seg.append(ConsultRow(key="c1", peer="conan"))
        seg.append(ToolRow(key="t1", name="read_file"))
        seg.append(ProseRow(text="prose never reserves"))
        assert seg.pending == 2

    def test_body_is_hard_capped_at_the_v2_limit(self) -> None:
        # The backstop that kills the dirty-forever bug: _append used to append
        # unconditionally, so an over-cap row produced a segment Discord 400s,
        # swallowed to a WARNING, retried on every wake, never rendered. Whatever
        # lands in a segment, its body is postable.
        seg = self._segment()
        seg.append(ProseRow(text="x" * 99_999))
        assert len(seg.body()) <= trace_mod._V2_TEXT_LIMIT


class TestRowLifecycle:
    """``on_step`` builds a pending row on ``tool_call`` and RESOLVES it in place
    on ``tool_result`` — one row per tool, never a called/returned pair."""

    async def test_call_then_result_is_one_row_not_two(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {"path": "a.py"}), _req(), acting_agent="aksel")
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        await r.finish(_CORRELATION_ID)
        body = _last_body(sender)
        assert body.count("read") == 1
        assert body.startswith(r"-# ● read\_file a.py · ")

    async def test_the_pending_row_renders_before_its_result_arrives(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {"path": "a.py"}), _req(), acting_agent="aksel")
        await _until(lambda: bool(sender.ok_sends()))
        assert sender.ok_sends()[0]["body"] == r"◐ read\_file a.py"
        await r.finish(_CORRELATION_ID)

    async def test_results_resolve_out_of_order_and_rows_keep_call_order(self, sender: _FakeSender) -> None:
        # calfkit fans out parallel calls and each sibling folds on its OWN hop,
        # so results arrive in COMPLETION order with unbounded steps between.
        r = _renderer(sender)
        await r.on_step(_call("t1", "slow", {"path": "a"}), _req(), acting_agent="aksel")
        await r.on_step(_call("t2", "fast", {"path": "b"}), _req(), acting_agent="aksel")
        await r.on_step(_result("t2", "fast"), _req(), acting_agent="aksel")
        await r.on_step(_result("t1", "slow"), _req(), acting_agent="aksel")
        await r.finish(_CORRELATION_ID)
        rows = _last_body(sender).split("\n")
        assert "slow" in rows[0] and "fast" in rows[1]  # call order preserved
        assert rows[0].startswith("-# ● ") and rows[1].startswith("-# ● ")  # both resolved

    async def test_a_failed_result_escapes_the_dim_register_with_its_error(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_call("t1", "search_docs", {"query": "x"}), _req(), acting_agent="aksel")
        await r.on_step(
            _result("t1", "search_docs", outcome="failed", text="connection timed out"),
            _req(),
            acting_agent="aksel",
        )
        await r.finish(_CORRELATION_ID)
        assert _first_row(sender) == r"❌ search\_docs x — connection timed out"

    async def test_a_denied_result_is_dim_and_struck(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_call("t1", "search_docs", {}), _req(), acting_agent="aksel")
        await r.on_step(
            _result("t1", "search_docs", outcome="denied", text="superseded by handoff"),
            _req(),
            acting_agent="aksel",
        )
        await r.finish(_CORRELATION_ID)
        assert _first_row(sender) == r"-# ~~⊘ search\_docs~~ — superseded by handoff"

    async def test_a_second_result_for_one_call_cannot_re_resolve_the_row(self, sender: _FakeSender) -> None:
        # A resolved row books ZERO growth reserve, so re-resolving one lets it
        # grow into budget nobody reserved — straight through fits() and into the
        # segment's silent hard cap. Resolving retires the key, so a second
        # result for the same id is an orphan (appended) rather than a re-resolve.
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        entry = r._entries[_CORRELATION_ID]
        assert "t1" not in entry.locate, "a resolved row is still re-resolvable"
        await _end(r)

    async def test_an_orphan_result_appends_instead_of_raising(self, sender: _FakeSender) -> None:
        # The drain's contract is that the render path can NEVER fault the turn.
        # A result whose call row is absent must still render, not blow up.
        r = _renderer(sender)
        await r.on_step(_result("nope", "read_file"), _req(), acting_agent="aksel")
        await r.finish(_CORRELATION_ID)
        assert sender.ok_sends()[0]["body"].startswith(r"-# ● read\_file")

    async def test_a_result_resolves_a_row_in_an_earlier_posted_segment(self, sender: _FakeSender) -> None:
        # The call row lives in an ALREADY-POSTED message while a later segment
        # is being appended to. _flush iterates every segment, each with its own
        # message_id, so the older one simply goes dirty and is edited. Each
        # _until forces a real flush, which is what makes this the cross-message
        # case rather than one coalesced post.
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {"path": "a.py"}), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.ok_sends()) == 1)
        await r.on_step(_step("handoff", target="billing"), _req(), acting_agent="aksel")
        await r.on_step(_step("agent_message", emitter="billing", text="on it"), _req(), acting_agent="billing")
        await _until(lambda: len(sender.ok_sends()) == 2)  # the persona change opened message 2
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        await r.finish(_CORRELATION_ID)
        edits = [e["body"] for e in sender.ok_edits() if "read" in e["body"]]
        assert edits, "the earlier, already-posted segment was never re-edited"
        assert edits[-1].startswith(r"-# ● read\_file a.py · ")

    async def test_elapsed_is_measured_between_call_and_result(self, sender: _FakeSender) -> None:
        clock = _FakeClock()
        r = StepTraceRenderer(sender, min_edit_interval=0.0, now=clock)  # type: ignore[arg-type]
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        clock.advance(0.25)
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        await r.finish(_CORRELATION_ID)
        assert _first_row(sender) == r"-# ● read\_file · 250ms"

    async def test_a_handoff_renders_its_reason(self, sender: _FakeSender) -> None:
        # `reason` is always populated by calfkit and is currently rendered in
        # NO surface at all.
        r = _renderer(sender)
        await r.on_step(
            _step("handoff", target="billing", reason="card expired"), _req(), acting_agent="aksel"
        )
        await r.finish(_CORRELATION_ID)
        assert _first_row(sender) == "➜ handed off to billing — card expired"


class TestNothingIsLostMidFlush:
    """``finish`` promises "pending content is never lost". It must hold even
    when the content lands WHILE a flush is in flight.

    The writer clears ``wake`` before flushing, so an append during the flush
    re-sets it. Exiting on ``finished`` alone would drop that content forever:
    the entry is already popped and the writer is gone, so the Discord message
    stays as-is. The seal is the likeliest victim — it is appended at exactly
    the moment the writer is busy — which would silently defeat ADR-0025 and
    leave the frozen ``◐`` the seal exists to prevent.
    """

    async def test_the_seal_survives_landing_during_an_in_flight_flush(self) -> None:
        sender = _BlockingSender()
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await sender.in_flight.wait()  # the writer is now INSIDE the send

        # Everything below lands while that send is still in flight.
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        await r.seal(_CORRELATION_ID, faulted=False)
        finishing = asyncio.create_task(r.finish(_CORRELATION_ID))
        await asyncio.sleep(0)  # let finish() set `finished` before the send returns
        sender.release.set()
        await finishing

        body = _last_body(sender)
        assert "-# 1 tool · 0ms" in body, "the seal was dropped by the writer's exit"
        assert "◐" not in body, "a resolved row was left frozen as pending"

    async def test_the_seal_survives_landing_during_an_in_flight_edit(self) -> None:
        # The likelier shape of the same race: by the time the terminal arrives
        # the message is long since posted, so the seal collides with an EDIT.
        sender = _BlockingSender()
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await sender.in_flight.wait()
        sender.release.set()
        await _until(lambda: len(sender.ok_sends()) == 1)  # posted

        sender.release.clear()
        sender.in_flight.clear()
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        await sender.in_flight.wait()  # the writer is now INSIDE the edit

        await r.seal(_CORRELATION_ID, faulted=False)
        finishing = asyncio.create_task(r.finish(_CORRELATION_ID))
        await asyncio.sleep(0)
        sender.release.set()
        await finishing

        assert "-# 1 tool · 0ms" in _last_body(sender), "the seal was dropped mid-edit"


class TestRemoteControlledFieldsAreHygienised:
    """`_plain` must reach EVERY field a model or peer controls.

    A newline breaks out of the per-line ``-# `` prefix and renders the
    remainder bright; an unbounded value blows the growth reserve and forces the
    segment's silent hard cap. Subject/detail/note/reason were covered from the
    start — these three were not, and ``peer`` is the sharpest: it is the
    model's own ``message_agent`` argument, unvalidated and unbounded, and its
    row is rendered at REQUEST time even if the call is then rejected.
    """

    async def test_a_tool_name_cannot_break_out_of_the_row(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_call("t1", "evil\nrm -rf /", {}), _req(), acting_agent="aksel")
        await _end(r)
        assert "\nrm -rf /" not in _last_body(sender)

    async def test_a_consult_peer_cannot_break_out_of_the_row(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "bob\n# HUGE", None, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await _end(r)
        assert "\n# HUGE" not in _last_body(sender)

    async def test_a_consult_peer_cannot_blow_the_growth_reserve(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "b" * 10_000, None, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await _end(r)
        assert len(_first_row(sender)) < trace_rows._DETAIL_MAX * 2

    async def test_a_handoff_target_cannot_break_out_of_the_row(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_step("handoff", target="peer\n-# fake", reason="because"), _req(), acting_agent="aksel")
        await _end(r)
        assert "\n-# fake" not in _last_body(sender)


class TestConsultLifecycle:
    """A consult is a row that RESOLVES, not a one-shot marker.

    Fixes a real bug: today's marker is emitted at REQUEST time in the PAST
    tense (``💬 consulted \\`conan\\``) and never updates, so a rejected or faulted
    consult leaves an optimistic line in the human's thread while the ⚠️ goes
    only to the audit thread. ADR-0020 still applies — the row shows THAT the
    consult happened and where to read it, never what was said.
    """

    _URL = "https://discord.com/channels/42/9001"

    async def test_a_consult_opens_in_the_present_tense_with_its_link(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await _until(lambda: bool(sender.ok_sends()))
        assert sender.ok_sends()[0]["body"] == f"◐ consulting conan · [view exchange]({self._URL})"
        await _end(r)

    async def test_the_consult_row_posts_under_the_caller_s_persona(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await _until(lambda: bool(sender.ok_sends()))
        assert sender.ok_sends()[0]["persona"].name == "aksel"
        await _end(r)

    async def test_the_consult_row_flows_inline_with_the_caller_s_own_rows(self, sender: _FakeSender) -> None:
        # A consult must NOT open a new message — the caller is speaking
        # continuously either side of it, so it shares their segment.
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["body"] == f"◐ read\\_file\n◐ consulting conan · [view exchange]({self._URL})"
        assert len(sender.sends) == 1  # same message, edited in place
        await _end(r)

    async def test_a_reply_resolves_the_consult_row(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await r.on_consult_result("c1", state="ok", note="", correlation_id=_CORRELATION_ID)
        await _end(r)
        assert _first_row(sender) == f"-# ● consulted conan · [view exchange]({self._URL})"

    async def test_a_faulted_consult_escapes_the_dim_register(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await r.on_consult_result("c1", state="failed", note="", correlation_id=_CORRELATION_ID)
        await _end(r)
        assert _first_row(sender) == f"❌ conan didn't answer · [view exchange]({self._URL})"

    async def test_a_rejected_consult_is_dim_and_struck_with_its_reason(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await r.on_consult_result("c1", state="denied", note="conan is offline", correlation_id=_CORRELATION_ID)
        await _end(r)
        assert _first_row(sender) == f"-# ~~⊘ conan~~ — conan is offline · [view exchange]({self._URL})"

    async def test_a_consult_still_open_at_the_seal_says_the_peer_never_replied(self, sender: _FakeSender) -> None:
        # dispatcher.dangling() territory: the run faulted with the consult open.
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await _end(r, faulted=True)
        assert _first_row(sender) == f"-# ~~⊘ conan~~ — never replied · [view exchange]({self._URL})"

    async def test_a_consult_without_an_audit_thread_states_the_gap(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_consult("c1", "conan", None, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await r.on_consult_result("c1", state="ok", note="", correlation_id=_CORRELATION_ID)
        await _end(r)
        assert _first_row(sender) == "-# ● consulted conan · ⚠️ couldn't write the audit log"

    async def test_resolving_an_unknown_consult_never_raises(self, sender: _FakeSender) -> None:
        # Same contract as an orphan tool result: the render path can never
        # fault the turn.
        r = _renderer(sender)
        await r.on_consult_result("nope", state="ok", note="", correlation_id=_CORRELATION_ID)
        await r.on_consult_result("nope", state="ok", note="", correlation_id="never-seen")
        await _settle()

    async def test_the_consult_row_never_carries_the_exchange(self, sender: _FakeSender) -> None:
        # ADR-0020: the exchange is private and lives in the audit thread. The
        # row resolves its STATE, which is not exchange content.
        r = _renderer(sender)
        await r.on_consult("c1", "conan", self._URL, _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await r.on_consult_result("c1", state="ok", note="the secret is 42", correlation_id=_CORRELATION_ID)
        await _end(r)
        assert "secret" not in _last_body(sender)


class TestSeal:
    """The trace ends with the run's OUTCOME (ADR-0025).

    ``finish()`` cannot know it — it runs in a ``finally`` around the drain while
    the fault only surfaces later, in ``_await_terminal``. The terminal already
    arrives on the stream the drain is reading, so the seal is taken from there.
    """

    async def test_a_completed_run_seals_with_its_tool_count_and_duration(self, sender: _FakeSender) -> None:
        clock = _FakeClock()
        r = StepTraceRenderer(sender, min_edit_interval=0.0, now=clock)  # type: ignore[arg-type]
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        clock.advance(12.3)
        await r.seal(_CORRELATION_ID, faulted=False)
        await r.finish(_CORRELATION_ID)
        assert _last_body(sender).endswith("\n-# 1 tool · 12.3s")

    async def test_a_faulted_run_seals_bright_and_points_at_the_notice(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_step("agent_message", text="working"), _req(), acting_agent="aksel")
        await r.seal(_CORRELATION_ID, faulted=True)
        await r.finish(_CORRELATION_ID)
        assert _last_body(sender).endswith("\n⚠️ run failed after 0ms — details below")

    async def test_the_seal_resolves_rows_still_pending(self, sender: _FakeSender) -> None:
        # A fault strands a row mid-flight. The bridge is alive and knows, so a
        # frozen ◐ is never the failure mode.
        r = _renderer(sender)
        # The subject arrives escaped (`_plain` neutralises Discord markdown, so
        # the underscore is `\_` on the wire and a literal `_` on screen).
        await r.on_step(_call("t1", "lookup_account", {"path": "acct_88213"}), _req(), acting_agent="aksel")
        await r.seal(_CORRELATION_ID, faulted=True)
        await r.finish(_CORRELATION_ID)
        assert r"-# ~~⊘ lookup\_account acct\_88213~~ — interrupted" in _last_body(sender)
        assert "◐" not in _last_body(sender)

    async def test_the_seal_counts_only_tools_not_prose(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_step("agent_message", text="hi"), _req(), acting_agent="aksel")
        await r.on_step(_call("t1", "a", {}), _req(), acting_agent="aksel")
        await r.on_step(_result("t1", "a"), _req(), acting_agent="aksel")
        await r.on_step(_call("t2", "b", {}), _req(), acting_agent="aksel")
        await r.on_step(_result("t2", "b"), _req(), acting_agent="aksel")
        await r.seal(_CORRELATION_ID, faulted=False)
        await r.finish(_CORRELATION_ID)
        assert _last_body(sender).endswith("-# 2 tools · 0ms")

    async def test_the_seal_lands_under_the_last_acting_persona(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_step("agent_message", emitter="aksel", text="hi"), _req(), acting_agent="aksel")
        await r.on_step(_step("handoff", target="billing"), _req(), acting_agent="aksel")
        await r.on_step(_step("agent_message", emitter="billing", text="on it"), _req(), acting_agent="billing")
        await r.seal(_CORRELATION_ID, faulted=False)
        await r.finish(_CORRELATION_ID)
        assert sender.ok_sends()[-1]["persona"].name == "billing"
        assert "-# 0ms" in _last_body(sender)

    async def test_finish_seals_defensively_when_the_stream_never_terminated(
        self, sender: _FakeSender, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Covers a drain that raised, a broken stream, and a calfkit contract
        # violation. An unsealed trace must never be left asserting "running".
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        with caplog.at_level(logging.WARNING, logger="calfcord.bridge.trace"):
            await r.finish(_CORRELATION_ID)
        assert r"-# ~~⊘ read\_file~~ — interrupted" in _last_body(sender)
        assert any("unsealed" in rec.message for rec in caplog.records)

    async def test_a_sealed_trace_is_not_resealed_by_finish(self, sender: _FakeSender) -> None:
        r = _renderer(sender)
        await r.on_step(_step("agent_message", text="hi"), _req(), acting_agent="aksel")
        await r.seal(_CORRELATION_ID, faulted=False)
        await r.finish(_CORRELATION_ID)
        assert _last_body(sender).count("0ms") == 1

    async def test_sealing_an_unseen_correlation_is_fine(self, sender: _FakeSender) -> None:
        # A turn that produced no renderable step has no entry — sealing it must
        # not conjure a trace message out of nothing.
        r = _renderer(sender)
        await r.seal("never-seen", faulted=False)
        await r.finish("never-seen")
        await _settle()
        assert sender.sends == []


class TestAccentIsIdentity:
    """The stripe says WHO, not what happened (see ``accent_for``)."""

    async def test_each_persona_s_segment_carries_its_own_accent(self, sender: _FakeSender) -> None:
        # Each segment is striped by ITS agent. Note this does not guarantee two
        # agents differ — a curated palette collides by design (aksel and billing
        # both land on green today), and that is fine: the persona change already
        # swaps the webhook's name and avatar, so identity never rests on colour.
        r = _renderer(sender)
        await r.on_step(_step("agent_message", emitter="aksel", text="hi"), _req(), acting_agent="aksel")
        await r.on_step(_step("handoff", target="billing"), _req(), acting_agent="aksel")
        await r.on_step(_step("agent_message", emitter="billing", text="on it"), _req(), acting_agent="billing")
        await r.finish(_CORRELATION_ID)
        accents = [_view_accent(c) for c in sender.ok_sends()]
        assert accents == [accent_for("aksel"), accent_for("billing")]

    async def test_a_failure_does_not_turn_the_stripe_red(self, sender: _FakeSender) -> None:
        # Colour is identity. The failure is carried by the row escaping `-# `.
        r = _renderer(sender)
        await r.on_step(_call("t1", "search_docs", {}), _req(), acting_agent="aksel")
        await r.on_step(_result("t1", "search_docs", outcome="failed", text="boom"), _req(), acting_agent="aksel")
        await r.finish(_CORRELATION_ID)
        assert _view_accent(sender.ok_sends()[0]) == accent_for("aksel")
        assert _last_body(sender).startswith("❌ ")


class TestBuildSegmentView:
    """``_build_segment_view`` wraps one segment body in a red accent container."""

    def test_wraps_body_in_accent_container_text_display(self) -> None:
        view = _build_segment_view("◐ read_file", accent_for("aksel"))
        assert isinstance(view, discord.ui.LayoutView)
        assert view.has_components_v2() is True  # a real v2 view (container present)
        assert _view_body(view) == "◐ read_file"
        assert _view_accent_of(view) == accent_for("aksel")

    def test_the_view_carries_its_text_only_in_components(self) -> None:
        # ADR-0016's history-exclusion invariant: a v2 message carries text ONLY
        # inside its components, and the flag is what ChannelHistoryFetcher drops
        # on. If a trace message ever gained `content` it would re-enter model
        # history and double-count the agent's own tool activity.
        view = _build_segment_view("◐ read_file", accent_for("aksel"))
        assert view.has_components_v2() is True
        assert not hasattr(view, "content"), "a LayoutView must carry no content"
        assert _view_body(view) == "◐ read_file"  # the text lives in the TextDisplay


class TestAggregation:
    """Steps accumulate into ONE message: first step posts, later steps edit."""

    async def test_second_step_edits_instead_of_posting(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="read_file"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["body"] == r"◐ read\_file"

        await renderer.on_step(_step("tool_result", name="read_file"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)

        # The edit re-renders the WHOLE segment: both lines, joined.
        assert sender.edits[0]["body"] == r"-# ● read\_file · 0ms"
        assert sender.edits[0]["message_id"] == sender.sends[0]["message_id"]
        assert len(sender.sends) == 1  # still one message

    async def test_first_step_posts_immediately_even_with_long_interval(self, sender: _FakeSender) -> None:
        """Leading edge: an idle writer flushes a new step with no interval wait."""
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)  # far sooner than 60s

    async def test_burst_coalesces_into_one_final_edit(self, sender: _FakeSender) -> None:
        """Steps landing inside the interval produce NO interim edits; finish
        flushes them all as ONE edit carrying the full trace."""
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        await renderer.on_step(_step("tool_result", name="a"), _req(), acting_agent="aksel")
        await renderer.on_step(_step("tool_call", name="b"), _req(), acting_agent="aksel")
        await _settle()
        assert sender.edits == []  # writer is parked in its interval

        await _end(renderer)
        assert len(sender.edits) == 1
        assert sender.edits[0]["body"] == (
            "-# ● a · 0ms\n-# ~~⊘ b~~ — interrupted\n-# 2 tools · 0ms"
        )

    async def test_finish_interrupts_interval_promptly(self, sender: _FakeSender) -> None:
        """finish() must not wait out the edit interval (the terminal reply is
        behind it in the handler)."""
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.on_step(_step("tool_result", name="a"), _req(), acting_agent="aksel")

        loop = asyncio.get_running_loop()
        start = loop.time()
        await renderer.finish(_CORRELATION_ID)
        assert loop.time() - start < 5.0  # ≪ the 60s interval
        assert len(sender.edits) == 1

    async def test_clean_finish_makes_no_extra_calls(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.seal(_CORRELATION_ID, faulted=False)
        await _until(lambda: len(sender.edits) == 1)  # the seal is real content
        await renderer.finish(_CORRELATION_ID)
        assert len(sender.sends) == 1 and len(sender.edits) == 1


class TestPersonaSegmentation:
    """One message per contiguous persona run; a persona change starts a new one."""

    async def test_tool_steps_post_under_acting_agent_persona(self, sender: _FakeSender) -> None:
        """A tool step's trace must appear under the calling agent's persona, not
        the tool's (#96).

        The emitter is pinned to a tool name here to hold that guarantee at the
        renderer no matter what the wire carries. Under calfkit 0.12.9 it cannot
        actually happen: EVERY ``ToolResultStep`` is minted by the hop ledger
        (``nodes/_steps.py`` ``folded``/``fold_failed``), whose only call sites are
        the fold path (``nodes/base.py:1389-1442``) — which runs on the node
        *receiving* the reply, i.e. the CALLER — and the flush stamps
        ``emitter=self.node_id`` there. A tool node folds no replies and declares no
        facts, so its ledger is empty and ``flush`` returns before publishing. #96
        predates calfkit's caller-side step-emission redesign, when the tool node
        published its own result step. Keep the pin: it costs nothing and the
        renderer's contract shouldn't depend on that history.
        """
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_result", name="todo", emitter="todo"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["persona"].name == "aksel"

    async def test_agent_message_uses_emitter_not_acting_agent(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("agent_message", text="hi", emitter="billing"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["persona"].name == "billing"

    async def test_handoff_stays_in_old_segment_new_acting_agent_opens_new_message(self, sender: _FakeSender) -> None:
        """The handoff announcement aggregates under the handing-off agent; the
        receiving agent's first step opens a NEW message under its persona."""
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        await renderer.on_step(_step("handoff", target="/billing", emitter="aksel"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["body"] == "◐ t\n➜ handed off to billing"

        await renderer.on_step(_step("tool_call", name="u"), _req(), acting_agent="billing")
        await _until(lambda: len(sender.sends) == 2)
        assert sender.sends[1]["persona"].name == "billing"
        assert sender.sends[1]["body"] == "◐ u"
        assert sender.sends[1]["message_id"] != sender.sends[0]["message_id"]


class TestRollover:
    """The 4000-char whole-message cap forces a fresh message, same persona."""

    async def test_overflowing_block_rolls_to_new_message(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        big = "y" * 3000  # one chunk, near the cap
        await renderer.on_step(_step("agent_message", text=big), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        await renderer.on_step(_step("agent_message", text=big), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 2)

        assert sender.sends[0]["body"] == big
        assert sender.sends[1]["body"] == big  # rolled over, not appended
        assert sender.sends[0]["persona"] == sender.sends[1]["persona"]
        assert sender.edits == []  # nothing was appended to message 1

    async def test_small_step_still_fits_before_rollover(self, sender: _FakeSender) -> None:
        """Sanity: the cap check is about the JOINED body, not step count."""
        renderer = _renderer(sender)
        await renderer.on_step(_step("agent_message", text="y" * 3000), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["body"] == "y" * 3000 + "\n◐ t"
        assert len(sender.sends) == 1

    async def test_multi_chunk_agent_message_posts_one_message_per_chunk(self, sender: _FakeSender) -> None:
        # Prose is the ONLY row whose length the model controls, so it is the
        # only one that chunks rather than truncating — an answer must never be
        # silently cut.
        text = "\n".join("y" * 80 for _ in range(200))  # well over the v2 cap → multiple chunks
        step = _step("agent_message", text=text)
        expected = trace_rows._chunk_text(text, trace_rows._V2_CHUNK)
        assert len(expected) >= 2  # sanity: this really does chunk

        renderer = _renderer(sender)
        await renderer.on_step(step, _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == len(expected))

        assert [c["body"] for c in sender.sends] == expected  # every chunk, in order


class TestThreadRouting:
    """A thread-originated request posts INTO the thread; the persona webhook
    still hosts on the parent channel. Edits carry the same routing."""

    async def test_posts_and_edits_route_into_thread(self, sender: _FakeSender) -> None:
        thread_id = 555_001
        renderer = _renderer(sender)
        req = _req(source_channel_id=thread_id)
        await renderer.on_step(_step("tool_call", name="t"), req, acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["channel_id"] == _CHANNEL_ID  # webhook host = parent
        assert sender.sends[0]["thread_id"] == thread_id  # routed into the thread

        await renderer.on_step(_step("tool_result", name="t"), req, acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["channel_id"] == _CHANNEL_ID
        assert sender.edits[0]["thread_id"] == thread_id


class TestNothingRenderable:
    """A step that renders nothing creates no entry, no writer, no posts."""

    async def test_whitespace_agent_message_posts_nothing(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("agent_message", text="   \n  "), _req(), acting_agent="aksel")
        await _settle()
        assert sender.sends == [] and sender.edits == []
        await renderer.finish(_CORRELATION_ID)  # no entry — must be a no-op
        assert sender.sends == [] and sender.edits == []

    async def test_finish_on_unseen_correlation_is_fine(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.finish("never-seen")  # must not raise
        assert sender.sends == [] and sender.edits == []


class TestFailureSemantics:
    """Best-effort: no Discord failure ever escapes on_step/finish; a failed
    post is retried (content must not be lost); a failed edit is dropped
    (the next flush re-renders the full body anyway)."""

    async def test_a_retried_segment_never_lands_below_a_later_one(self, sender: _FakeSender) -> None:
        # Nothing is ever deleted, so an out-of-order post is PERMANENT. If
        # segment 1's post keeps failing while segment 2 opens (a handoff) and
        # posts, segment 1's eventual retry lands underneath it and the turn
        # reads backwards forever. Hold a later segment back rather than invert
        # the trace; the earlier one retries on the next wake.
        # Two failures, both landing DURING the run — one is not enough, since a
        # single flush already retries the earlier segment before the later one.
        sender.send_failures.append(_http_exc(discord.HTTPException, 503))
        sender.send_failures.append(_http_exc(discord.HTTPException, 503))
        r = _renderer(sender)
        await r.on_step(_step("agent_message", emitter="aksel", text="FIRST: aksel"), _req(), acting_agent="aksel")
        await _settle()  # failure 1
        await r.on_step(_step("handoff", target="billing"), _req(), acting_agent="aksel")
        await r.on_step(
            _step("agent_message", emitter="billing", text="SECOND: billing"), _req(), acting_agent="billing"
        )
        await _settle()  # failure 2 — and billing's segment must NOT post ahead of aksel's
        assert sender.ok_sends() == [], "a later segment posted while an earlier one was still unposted"
        await _end(r)
        bodies = [c["body"] for c in sender.ok_sends()]
        assert len(bodies) == 2, f"expected both segments to land, got {bodies}"
        assert "FIRST" in bodies[0] and "SECOND" in bodies[1], f"the trace reads backwards: {bodies}"

    async def test_the_seal_s_own_edit_is_retried_when_it_fails(self, sender: _FakeSender) -> None:
        # "A failed edit heals on the next append" holds for every edit EXCEPT
        # the last — and the seal is always the last, so there is no next append.
        # Without a retry a single transient 500 freezes the trace mid-`◐`
        # forever: exactly the outcome ADR-0025 exists to prevent, on Discord's
        # most common failure.
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.ok_sends()) == 1)
        sender.edit_failures.append(_http_exc(discord.HTTPException, 500))
        await r.on_step(_result("t1", "read_file"), _req(), acting_agent="aksel")
        await _end(r)
        assert sender.ok_edits(), "the seal never landed — the trace is frozen mid-flight"
        assert "-# 1 tool · 0ms" in sender.ok_edits()[-1]["body"]
        assert "◐" not in sender.ok_edits()[-1]["body"]

    async def test_a_transport_error_leaves_the_segment_dirty_for_retry(self, sender: _FakeSender) -> None:
        # Observed against LIVE Discord: aiohttp raises ServerDisconnectedError
        # on a dropped keep-alive, and it is NOT a discord.DiscordException — so
        # it escaped _best_effort_trace, and `_flush` had already cleared `dirty`,
        # leaving the segment clean and its content gone. A transient blip must
        # degrade to "post failed, retry", like every other transient failure.
        #
        # Asserts on `dirty` rather than on a later post, because any subsequent
        # append (the seal, in a real turn) re-marks it and would mask the loss.
        sender.send_failures.append(aiohttp.ServerDisconnectedError())
        r = _renderer(sender)
        await r.on_step(_call("t1", "read_file", {}), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)  # the failing attempt
        assert sender.ok_sends() == []
        segment = r._entries[_CORRELATION_ID].segments[0]
        assert segment.dirty is True, "a transport error dropped the segment's content"
        await _end(r)

    async def test_failed_post_retries_on_next_wake_with_full_body(self, sender: _FakeSender) -> None:
        sender.send_failures.append(_http_exc(discord.Forbidden, 403))
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)  # the failing attempt
        assert sender.ok_sends() == []

        await renderer.on_step(_step("tool_result", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.ok_sends()) == 1)
        # The retry is a POST (no message exists yet) carrying BOTH lines.
        assert sender.ok_sends()[0]["body"] == "-# ● a · 0ms"
        assert sender.edits == []

    async def test_failed_post_retried_by_final_flush(self, sender: _FakeSender) -> None:
        sender.send_failures.append(_http_exc(discord.Forbidden, 403))
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await _end(renderer)
        assert len(sender.ok_sends()) == 1  # finish retried the post
        assert sender.ok_sends()[0]["body"].startswith("-# ~~⊘ a~~ — interrupted")

    async def test_a_mid_run_failed_edit_is_not_hot_retried(self, sender: _FakeSender) -> None:
        # NOT a dirty-flag test: review proved this passes even with failed edits
        # re-marking dirty, because the retry merges into the seal's flush. What
        # actually prevents a hot loop is the `wake` gate — only an append sets
        # it — so that is what this asserts: no edit happens between the failure
        # and the next append.
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        sender.edit_failures.append(_http_exc(discord.NotFound, 404))
        await renderer.on_step(_step("tool_result", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)  # the failing attempt
        await _settle()
        assert len(sender.edits) == 1, "the dropped edit was hot-retried with no new content"
        await renderer.finish(_CORRELATION_ID)

    async def test_failed_edit_recovers_on_next_step(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        sender.edit_failures.append(_http_exc(discord.NotFound, 404))
        await renderer.on_step(_step("tool_result", name="a"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)

        await renderer.on_step(_step("tool_call", name="b"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.ok_edits()) == 1)
        # The recovery edit re-renders the FULL segment — the dropped window heals.
        assert sender.ok_edits()[0]["body"] == "-# ● a · 0ms\n◐ b"

    async def test_forbidden_is_logged_and_swallowed(
        self, sender: _FakeSender, caplog: pytest.LogCaptureFixture
    ) -> None:
        sender.send_failures.append(_http_exc(discord.Forbidden, 403))
        renderer = _renderer(sender)
        with caplog.at_level(logging.WARNING, logger="calfcord.bridge.trace"):
            await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
            await _until(lambda: len(sender.sends) == 1)
            await renderer.finish(_CORRELATION_ID)  # must not raise
        assert any("Forbidden" in r.getMessage() for r in caplog.records)

    async def test_rate_limited_is_swallowed(self, sender: _FakeSender) -> None:
        # discord.RateLimited is a DiscordException but NOT an HTTPException — the
        # broader catch must funnel it through.
        sender.send_failures.append(discord.RateLimited(retry_after=1.0))
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await renderer.finish(_CORRELATION_ID)  # must not raise

    async def test_non_discord_error_never_escapes_finish(self, sender: _FakeSender) -> None:
        """A systematic bug (e.g. sender never started → RuntimeError) must not
        unwind the drain or block the terminal reply."""
        sender.send_failures.append(RuntimeError("sender not started"))
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await renderer.finish(_CORRELATION_ID)  # must not raise


class TestConcurrentRuns:
    """Two in-flight correlations keep independent aggregates."""

    async def test_two_correlations_get_separate_messages(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a", correlation_id="run-1"), _req(), acting_agent="aksel")
        await renderer.on_step(_step("tool_call", name="b", correlation_id="run-2"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 2)

        await renderer.on_step(_step("tool_result", name="a", correlation_id="run-1"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        by_body = {c["body"]: c for c in sender.sends}
        assert sender.edits[0]["message_id"] == by_body["◐ a"]["message_id"]

        await renderer.finish("run-1")
        await renderer.finish("run-2")


class TestTypingDisabled:
    """Typing is disabled for now — the notifier is accepted but never fired."""

    async def test_typing_notifier_is_not_fired(self, sender: _FakeSender) -> None:
        notifier = SimpleNamespace(fire=lambda _cid: pytest.fail("typing must stay dormant"))
        renderer = StepTraceRenderer(sender, notifier, min_edit_interval=0.0)  # type: ignore[arg-type]
        await renderer.on_step(_step("tool_call", name="t"), _req(), acting_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.finish(_CORRELATION_ID)
