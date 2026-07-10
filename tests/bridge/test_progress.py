"""Unit tests for :class:`calfcord.bridge.progress.ProgressRenderer`.

New model (Components-V2): the
:class:`~calfcord.bridge.mention_handler.MentionHandler` drains a run's
``stream()`` and calls ``on_step`` per non-A2A ``StepEvent``; a ``finally`` calls
``finish``. Each renderable step is rendered (``render_step_message``) to one or
more short bodies and POSTED as persistent, inline Components-V2 messages under
the emitting agent's persona — never edited, never deleted. ``finish`` is a
no-op (the messages persist as the turn's visible trace, and the history fetcher
excludes them). Every send is best-effort and never escapes the drain. Typing is
disabled for now (the fire call is commented out).

discord.py and the LLM stack are mocked out; the repo runs
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import calfcord.bridge.steps_render as steps_render
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.progress import _V2_ACCENT, ProgressRenderer, _build_step_view
from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage

_CORRELATION_ID = "evt-1"
_CHANNEL_ID = 6789
_MESSAGE_ID = 12345
_STEP_MESSAGE_ID = 99999


@pytest.fixture
def persona_sender() -> AsyncMock:
    """REST-only persona sender. ``send_components`` returns a SentMessage; the
    old ``edit_message`` / ``delete_message`` must never be called now."""
    sender = AsyncMock()
    sender.send_components = AsyncMock(return_value=SentMessage(id=_STEP_MESSAGE_ID, channel_id=_CHANNEL_ID))
    return sender


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


def _view_bodies(view: discord.ui.LayoutView) -> list[str]:
    """The text of every ``TextDisplay`` in a posted step view (in order)."""
    return [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]


class TestBuildStepView:
    """``_build_step_view`` wraps one rendered body in a red accent container."""

    def test_wraps_body_in_accent_container_text_display(self) -> None:
        view = _build_step_view("🔧 `read_file` called")
        assert isinstance(view, discord.ui.LayoutView)
        assert view.has_components_v2() is True  # a real v2 view (container present)
        assert _view_bodies(view) == ["🔧 `read_file` called"]
        containers = [i for i in view.walk_children() if isinstance(i, discord.ui.Container)]
        assert containers and containers[0].accent_colour == _V2_ACCENT


class TestOnStepPosts:
    """Each renderable step posts a persistent v2 message under the appropriate
    persona — no edit, no delete."""

    async def test_tool_call_posts_one_message_under_owning_agent_persona(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="read_file"), _req(), owning_agent="aksel")

        assert persona_sender.send_components.await_count == 1
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_not_called()

        call = persona_sender.send_components.call_args
        assert call.kwargs["persona"].name == "aksel"
        assert call.kwargs["channel_id"] == _CHANNEL_ID
        assert call.kwargs["thread_id"] is None
        assert _view_bodies(call.kwargs["view"]) == ["🔧 `read_file` called"]

    async def test_tool_result_uses_owning_agent_not_tool_emitter(self, persona_sender: AsyncMock) -> None:
        """The bug fix: a tool_result's emitter is the tool node (e.g. 'todo'),
        but the progress message must appear under the calling agent's persona."""
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(
            _step("tool_result", name="todo", emitter="todo"),
            _req(),
            owning_agent="aksel",
        )
        assert persona_sender.send_components.call_args.kwargs["persona"].name == "aksel"

    async def test_tool_result_failed_posts_failed_body(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(
            _step("tool_result", name="read_file", outcome="failed"), _req(), owning_agent="aksel"
        )
        assert _view_bodies(persona_sender.send_components.call_args.kwargs["view"]) == ["❌ `read_file` failed"]

    async def test_handoff_posts_bare_target_body(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("handoff", target="/billing"), _req(), owning_agent="aksel")
        assert persona_sender.send_components.await_count == 1
        assert _view_bodies(persona_sender.send_components.call_args.kwargs["view"]) == ["➡️ handed off to `billing`"]

    async def test_agent_message_uses_emitter_not_owning_agent(self, persona_sender: AsyncMock) -> None:
        """agent_message keeps the emitter persona (genuine peer-emitter case)
        even when owning_agent differs — e.g. after a handoff the peer's text
        appears under the peer's identity."""
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(
            _step("agent_message", text="hello", emitter="billing"),
            _req(),
            owning_agent="aksel",
        )
        assert persona_sender.send_components.call_args.kwargs["persona"].name == "billing"

    async def test_handoff_uses_emitter_not_owning_agent(self, persona_sender: AsyncMock) -> None:
        """The handoff announcement stays under the handing-off agent (emitter),
        not the receiving agent (target/owning_agent after update)."""
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(
            _step("handoff", target="billing", emitter="aksel"),
            _req(),
            owning_agent="aksel",
        )
        assert persona_sender.send_components.call_args.kwargs["persona"].name == "aksel"

    async def test_long_agent_message_posts_one_message_per_chunk(self, persona_sender: AsyncMock) -> None:
        text = "\n".join("y" * 80 for _ in range(200))  # well over the v2 cap → multiple chunks
        step = _step("agent_message", text=text)
        expected = steps_render.render_step_message(step)
        assert len(expected) >= 2  # sanity: this really does chunk

        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(step, _req(), owning_agent="aksel")

        assert persona_sender.send_components.await_count == len(expected)
        posted = [_view_bodies(c.kwargs["view"])[0] for c in persona_sender.send_components.call_args_list]
        assert posted == expected  # every chunk posted, in order


class TestNothingRenderable:
    """A step that renders nothing posts nothing."""

    async def test_whitespace_agent_message_posts_nothing(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="   \n  "), _req(), owning_agent="aksel")
        persona_sender.send_components.assert_not_called()


class TestThreadRouting:
    """A thread-originated request posts INTO the thread; the persona webhook
    still hosts on the parent channel."""

    async def test_thread_originated_step_posts_into_thread(self, persona_sender: AsyncMock) -> None:
        thread_id = 555_001
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(source_channel_id=thread_id), owning_agent="aksel")
        call = persona_sender.send_components.call_args
        assert call.kwargs["channel_id"] == _CHANNEL_ID  # webhook host = parent
        assert call.kwargs["thread_id"] == thread_id  # routed into the thread


class TestFinishIsNoop:
    """``finish`` has nothing to tear down — the messages are persistent."""

    async def test_finish_after_posts_does_nothing(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        persona_sender.send_components.reset_mock()
        await renderer.finish(_CORRELATION_ID)
        persona_sender.send_components.assert_not_called()
        persona_sender.delete_message.assert_not_called()

    async def test_finish_on_unseen_correlation_is_fine(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.finish("never-seen")  # must not raise
        persona_sender.send_components.assert_not_called()


class TestFailureSwallowing:
    """A failed send must never escape the drain."""

    async def test_forbidden_on_send_is_logged_and_swallowed(
        self, persona_sender: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        persona_sender.send_components = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
        renderer = ProgressRenderer(persona_sender)
        with caplog.at_level(logging.WARNING, logger="calfcord.bridge.progress"):
            await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")  # must not raise
        assert any("Forbidden" in r.getMessage() for r in caplog.records)

    async def test_notfound_on_send_is_swallowed(self, persona_sender: AsyncMock) -> None:
        # A gone message (NotFound) on the send is swallowed at DEBUG.
        persona_sender.send_components = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")  # must not raise

    async def test_rate_limited_on_send_is_swallowed(self, persona_sender: AsyncMock) -> None:
        # discord.RateLimited is a DiscordException but NOT an HTTPException — the
        # broader catch must funnel it through.
        persona_sender.send_components = AsyncMock(side_effect=discord.RateLimited(retry_after=1.0))
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")  # must not raise

    async def test_one_chunk_failing_does_not_stop_the_rest(self, persona_sender: AsyncMock) -> None:
        # A failed body is swallowed and the loop continues to the next chunk.
        persona_sender.send_components = AsyncMock(
            side_effect=[
                _http_exc(discord.Forbidden, 403),
                SentMessage(id=_STEP_MESSAGE_ID, channel_id=_CHANNEL_ID),
            ]
        )
        para = "y" * (steps_render._V2_CHUNK - 100)
        step = _step("agent_message", text=f"{para}\n{para}")  # two paragraphs → two chunks
        assert len(steps_render.render_step_message(step)) == 2  # exactly two chunks

        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(step, _req(), owning_agent="aksel")  # must not raise despite the first failing
        assert persona_sender.send_components.await_count == 2


class TestTypingDisabled:
    """Typing is disabled for now — the notifier is accepted but never fired."""

    async def test_typing_notifier_is_not_fired(self, persona_sender: AsyncMock) -> None:
        notifier = MagicMock()
        renderer = ProgressRenderer(persona_sender, notifier)
        await renderer.on_step(_step("tool_call", name="t"), _req(), owning_agent="aksel")
        notifier.fire.assert_not_called()
