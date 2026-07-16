"""Unit tests for :class:`calfcord.bridge.progress.ProgressRenderer`.

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

import discord
import pytest

import calfcord.bridge.steps_render as steps_render
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.progress import _V2_ACCENT, ProgressRenderer, _build_segment_view
from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage

_CORRELATION_ID = "evt-1"
_CHANNEL_ID = 6789
_MESSAGE_ID = 12345


class _FakeSender:
    """Recording stand-in for :class:`DiscordPersonaSender`.

    Records every ``send_components`` / ``edit_components`` ATTEMPT (including
    failing ones) with the view's body extracted at call time, so tests can
    assert exactly what would have rendered on Discord. Queued exceptions are
    raised FIFO, letting tests script per-call failures.
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
            "ok": True,
        }
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
            "ok": True,
        }
        self.edits.append(call)
        if self.edit_failures:
            call["ok"] = False
            raise self.edit_failures.pop(0)

    def ok_sends(self) -> list[dict[str, Any]]:
        return [c for c in self.sends if c["ok"]]

    def ok_edits(self) -> list[dict[str, Any]]:
        return [c for c in self.edits if c["ok"]]


def _view_body(view: discord.ui.LayoutView) -> str:
    """The text of the view's single ``TextDisplay`` (the segment body)."""
    bodies = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
    assert len(bodies) == 1, f"expected exactly one TextDisplay per segment view, got {len(bodies)}"
    return bodies[0]


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Yield to the event loop until ``predicate`` holds (the writer task runs
    between iterations). Fails the test on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out waiting for the writer task"
        await asyncio.sleep(0.001)


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
    )


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


@pytest.fixture
def sender() -> _FakeSender:
    return _FakeSender()


def _renderer(sender: _FakeSender, *, interval: float = 0.0) -> ProgressRenderer:
    """A renderer with a deterministic edit cadence: ``interval=0`` flushes as
    fast as the loop turns; a large interval parks the writer so coalescing can
    be asserted."""
    return ProgressRenderer(sender, min_edit_interval=interval)  # type: ignore[arg-type]


class TestBuildSegmentView:
    """``_build_segment_view`` wraps one segment body in a red accent container."""

    def test_wraps_body_in_accent_container_text_display(self) -> None:
        view = _build_segment_view("🔧 `read_file` called")
        assert isinstance(view, discord.ui.LayoutView)
        assert view.has_components_v2() is True  # a real v2 view (container present)
        assert _view_body(view) == "🔧 `read_file` called"
        containers = [i for i in view.walk_children() if isinstance(i, discord.ui.Container)]
        assert containers and containers[0].accent_colour == _V2_ACCENT


class TestAggregation:
    """Steps accumulate into ONE message: first step posts, later steps edit."""

    async def test_second_step_edits_instead_of_posting(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="read_file"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["body"] == "🔧 `read_file` called"

        await renderer.on_step(_step("tool_result", name="read_file"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)

        # The edit re-renders the WHOLE segment: both lines, joined.
        assert sender.edits[0]["body"] == "🔧 `read_file` called\n✅ `read_file` returned"
        assert sender.edits[0]["message_id"] == sender.sends[0]["message_id"]
        assert len(sender.sends) == 1  # still one message

    async def test_first_step_posts_immediately_even_with_long_interval(self, sender: _FakeSender) -> None:
        """Leading edge: an idle writer flushes a new step with no interval wait."""
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)  # far sooner than 60s

    async def test_burst_coalesces_into_one_final_edit(self, sender: _FakeSender) -> None:
        """Steps landing inside the interval produce NO interim edits; finish
        flushes them all as ONE edit carrying the full trace."""
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        await renderer.on_step(_step("tool_result", name="a"), _req(), owning_agent="aksel")
        await renderer.on_step(_step("tool_call", name="b"), _req(), owning_agent="aksel")
        await _settle()
        assert sender.edits == []  # writer is parked in its interval

        await renderer.finish(_CORRELATION_ID)
        assert len(sender.edits) == 1
        assert sender.edits[0]["body"] == "🔧 `a` called\n✅ `a` returned\n🔧 `b` called"

    async def test_finish_interrupts_interval_promptly(self, sender: _FakeSender) -> None:
        """finish() must not wait out the edit interval (the terminal reply is
        behind it in the handler)."""
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.on_step(_step("tool_result", name="a"), _req(), owning_agent="aksel")

        loop = asyncio.get_running_loop()
        start = loop.time()
        await renderer.finish(_CORRELATION_ID)
        assert loop.time() - start < 5.0  # ≪ the 60s interval
        assert len(sender.edits) == 1

    async def test_clean_finish_makes_no_extra_calls(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.finish(_CORRELATION_ID)
        assert len(sender.sends) == 1 and sender.edits == []


class TestNotes:
    """``on_note`` appends a BRIDGE-authored annotation (a consult cross-link, an
    audit-gap warning) to the same aggregate as the run's steps. It is not a run
    step — it carries no ``StepEvent`` — so the correlation and persona are passed
    explicitly."""

    async def test_note_posts_into_the_trace(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_note(
            "💬 consulted `conan`", _req(), correlation_id=_CORRELATION_ID, persona_name="aksel"
        )
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["body"] == "💬 consulted `conan`"
        assert sender.sends[0]["persona"].name == "aksel"

    async def test_note_shares_the_segment_with_same_persona_steps(self, sender: _FakeSender) -> None:
        # A consult marker must flow inline with the caller's own trace, not open a
        # new message — the caller is speaking continuously either side of it.
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="read_file"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.on_note(
            "💬 consulted `conan`", _req(), correlation_id=_CORRELATION_ID, persona_name="aksel"
        )
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["body"] == "🔧 `read_file` called\n💬 consulted `conan`"
        assert len(sender.sends) == 1  # same message, edited in place

    async def test_empty_note_renders_nothing(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_note("", _req(), correlation_id=_CORRELATION_ID, persona_name="aksel")
        await renderer.finish(_CORRELATION_ID)
        assert sender.sends == []


class TestPersonaSegmentation:
    """One message per contiguous persona run; a persona change starts a new one."""

    async def test_tool_steps_post_under_owning_agent_persona(self, sender: _FakeSender) -> None:
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
        await renderer.on_step(_step("tool_result", name="todo", emitter="todo"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["persona"].name == "aksel"

    async def test_agent_message_uses_emitter_not_owning_agent(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("agent_message", text="hi", emitter="billing"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["persona"].name == "billing"

    async def test_handoff_stays_in_old_segment_new_owner_opens_new_message(self, sender: _FakeSender) -> None:
        """The handoff announcement aggregates under the handing-off agent; the
        receiving agent's first step opens a NEW message under its persona."""
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        await renderer.on_step(_step("handoff", target="/billing", emitter="aksel"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["body"] == "🔧 `t` called\n➡️ handed off to `billing`"

        await renderer.on_step(_step("tool_call", name="u"), _req(), owning_agent="billing")
        await _until(lambda: len(sender.sends) == 2)
        assert sender.sends[1]["persona"].name == "billing"
        assert sender.sends[1]["body"] == "🔧 `u` called"
        assert sender.sends[1]["message_id"] != sender.sends[0]["message_id"]


class TestRollover:
    """The 4000-char whole-message cap forces a fresh message, same persona."""

    async def test_overflowing_block_rolls_to_new_message(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        big = "y" * 3000  # one chunk, near the cap
        await renderer.on_step(_step("agent_message", text=big), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        await renderer.on_step(_step("agent_message", text=big), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 2)

        assert sender.sends[0]["body"] == big
        assert sender.sends[1]["body"] == big  # rolled over, not appended
        assert sender.sends[0]["persona"] == sender.sends[1]["persona"]
        assert sender.edits == []  # nothing was appended to message 1

    async def test_small_step_still_fits_before_rollover(self, sender: _FakeSender) -> None:
        """Sanity: the cap check is about the JOINED body, not step count."""
        renderer = _renderer(sender)
        await renderer.on_step(_step("agent_message", text="y" * 3000), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["body"] == "y" * 3000 + "\n🔧 `t` called"
        assert len(sender.sends) == 1

    async def test_multi_chunk_agent_message_posts_one_message_per_chunk(self, sender: _FakeSender) -> None:
        text = "\n".join("y" * 80 for _ in range(200))  # well over the v2 cap → multiple chunks
        step = _step("agent_message", text=text)
        expected = steps_render.render_step_message(step)
        assert len(expected) >= 2  # sanity: this really does chunk

        renderer = _renderer(sender)
        await renderer.on_step(step, _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == len(expected))

        assert [c["body"] for c in sender.sends] == expected  # every chunk, in order


class TestThreadRouting:
    """A thread-originated request posts INTO the thread; the persona webhook
    still hosts on the parent channel. Edits carry the same routing."""

    async def test_posts_and_edits_route_into_thread(self, sender: _FakeSender) -> None:
        thread_id = 555_001
        renderer = _renderer(sender)
        req = _req(source_channel_id=thread_id)
        await renderer.on_step(_step("tool_call", name="t"), req, owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        assert sender.sends[0]["channel_id"] == _CHANNEL_ID  # webhook host = parent
        assert sender.sends[0]["thread_id"] == thread_id  # routed into the thread

        await renderer.on_step(_step("tool_result", name="t"), req, owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        assert sender.edits[0]["channel_id"] == _CHANNEL_ID
        assert sender.edits[0]["thread_id"] == thread_id


class TestNothingRenderable:
    """A step that renders nothing creates no entry, no writer, no posts."""

    async def test_whitespace_agent_message_posts_nothing(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("agent_message", text="   \n  "), _req(), owning_agent="aksel")
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

    async def test_failed_post_retries_on_next_wake_with_full_body(self, sender: _FakeSender) -> None:
        sender.send_failures.append(_http_exc(discord.Forbidden, 403))
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)  # the failing attempt
        assert sender.ok_sends() == []

        await renderer.on_step(_step("tool_result", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.ok_sends()) == 1)
        # The retry is a POST (no message exists yet) carrying BOTH lines.
        assert sender.ok_sends()[0]["body"] == "🔧 `a` called\n✅ `a` returned"
        assert sender.edits == []

    async def test_failed_post_retried_by_final_flush(self, sender: _FakeSender) -> None:
        sender.send_failures.append(_http_exc(discord.Forbidden, 403))
        renderer = _renderer(sender, interval=60.0)
        await renderer.on_step(_step("tool_call", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.finish(_CORRELATION_ID)
        assert len(sender.ok_sends()) == 1  # finish retried the post
        assert sender.ok_sends()[0]["body"] == "🔧 `a` called"

    async def test_failed_edit_not_retried_until_new_content(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        sender.edit_failures.append(_http_exc(discord.NotFound, 404))
        await renderer.on_step(_step("tool_result", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)  # the failing attempt
        await renderer.finish(_CORRELATION_ID)
        assert len(sender.edits) == 1  # finish did NOT hot-retry the dropped edit

    async def test_failed_edit_recovers_on_next_step(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)

        sender.edit_failures.append(_http_exc(discord.NotFound, 404))
        await renderer.on_step(_step("tool_result", name="a"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)

        await renderer.on_step(_step("tool_call", name="b"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.ok_edits()) == 1)
        # The recovery edit re-renders the FULL segment — the dropped window heals.
        assert sender.ok_edits()[0]["body"] == "🔧 `a` called\n✅ `a` returned\n🔧 `b` called"

    async def test_forbidden_is_logged_and_swallowed(
        self, sender: _FakeSender, caplog: pytest.LogCaptureFixture
    ) -> None:
        sender.send_failures.append(_http_exc(discord.Forbidden, 403))
        renderer = _renderer(sender)
        with caplog.at_level(logging.WARNING, logger="calfcord.bridge.progress"):
            await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
            await _until(lambda: len(sender.sends) == 1)
            await renderer.finish(_CORRELATION_ID)  # must not raise
        assert any("Forbidden" in r.getMessage() for r in caplog.records)

    async def test_rate_limited_is_swallowed(self, sender: _FakeSender) -> None:
        # discord.RateLimited is a DiscordException but NOT an HTTPException — the
        # broader catch must funnel it through.
        sender.send_failures.append(discord.RateLimited(retry_after=1.0))
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await renderer.finish(_CORRELATION_ID)  # must not raise

    async def test_non_discord_error_never_escapes_finish(self, sender: _FakeSender) -> None:
        """A systematic bug (e.g. sender never started → RuntimeError) must not
        unwind the drain or block the terminal reply."""
        sender.send_failures.append(RuntimeError("sender not started"))
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await renderer.finish(_CORRELATION_ID)  # must not raise


class TestConcurrentRuns:
    """Two in-flight correlations keep independent aggregates."""

    async def test_two_correlations_get_separate_messages(self, sender: _FakeSender) -> None:
        renderer = _renderer(sender)
        await renderer.on_step(_step("tool_call", name="a", correlation_id="run-1"), _req(), owning_agent="aksel")
        await renderer.on_step(_step("tool_call", name="b", correlation_id="run-2"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 2)

        await renderer.on_step(_step("tool_result", name="a", correlation_id="run-1"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.edits) == 1)
        by_body = {c["body"]: c for c in sender.sends}
        assert sender.edits[0]["message_id"] == by_body["🔧 `a` called"]["message_id"]

        await renderer.finish("run-1")
        await renderer.finish("run-2")


class TestTypingDisabled:
    """Typing is disabled for now — the notifier is accepted but never fired."""

    async def test_typing_notifier_is_not_fired(self, sender: _FakeSender) -> None:
        notifier = SimpleNamespace(fire=lambda _cid: pytest.fail("typing must stay dormant"))
        renderer = ProgressRenderer(sender, notifier, min_edit_interval=0.0)  # type: ignore[arg-type]
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        await _until(lambda: len(sender.sends) == 1)
        await renderer.finish(_CORRELATION_ID)
