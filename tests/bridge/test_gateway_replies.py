"""Unit tests for the gateway's ``_on_message`` intake and handler-task lifecycle.

Post-0.12 the gateway is a pure caller surface: for each ``!mention`` it builds a
:class:`MentionRequest` and runs :meth:`MentionHandler.handle` as a tracked
asyncio task. There is no ingress/outbox/Worker anymore. These tests pin the
intake seam and the task machinery, all offline (no Discord, no broker):

* **Filtering** — DMs, wrong-guild, pre-ready, the bot's own non-webhook posts
  (e.g. ``/clear`` markers, notices), and ambient (non-``!mention``) messages are
  dropped before a handler task is ever spawned (C2). A webhook post carrying the
  bot's user id (an agent persona) is NOT self-filtered.
* **Dedupe** — a redelivered ``MESSAGE_CREATE`` (same ``message.id``) spawns the
  handler only once.
* **Spawn** — a real ``!mention`` reaches ``handler.handle`` with a correctly
  populated :class:`MentionRequest` (mention ids, author label, channel flattening,
  reply target, the serialized wire).
* **Crash isolation** — an *unexpected* handler exception posts a generic notice
  via the reply poster; ``CancelledError`` (shutdown) propagates untouched.
* **Drain** — ``drain_inflight`` cancels in-flight handler tasks at shutdown.

The gateway is built with mocked collaborators; ``_handler`` is swapped for a
recording/failing fake so a mention only has to REACH ``handler.handle``. The
``_GatewayClient`` constructor is sync + offline, so no network is touched.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from aiokafka.errors import KafkaConnectionError
from pydantic import SecretStr

from calfcord.bridge.gateway import DiscordIngressGateway
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.normalizer import MessageNormalizer
from calfcord.bridge.settings import (
    BridgeSettings,
    MessageHistorySettings,
    StickyRepliesSettings,
)
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.settings import DiscordSettings

_GUILD_ID = 5678
_BOT_USER_ID = 555
_OWNER_USER_ID = 9999


def _settings() -> DiscordSettings:
    return DiscordSettings(
        bot_token=SecretStr("test-bot-token"),
        application_id=1234,
        guild_id=_GUILD_ID,
        owner_user_id=_OWNER_USER_ID,
    )


class _StickyStore:
    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner
        self.gets: list[str] = []
        self.clears: list[str] = []

    async def get_sticky_owner(self, conversation_key: str) -> str | None:
        self.gets.append(conversation_key)
        return self.owner

    async def set_sticky_owner(self, conversation_key: str, owner_agent_id: str) -> None:
        self.owner = owner_agent_id

    async def clear_sticky_owner(self, conversation_key: str) -> None:
        self.clears.append(conversation_key)
        self.owner = None


def _gateway(
    *,
    bridge_settings: BridgeSettings | None = None,
    sticky_store: _StickyStore | None = None,
) -> DiscordIngressGateway:
    """A real gateway with mocked collaborators and a stubbed ``add_view``."""
    gateway = DiscordIngressGateway(
        _settings(),
        calfkit_client=MagicMock(),
        persona_sender=MagicMock(),
        transcript_store=MagicMock(),
        roster=MagicMock(),
        overrides=MagicMock(),
        a2a=MagicMock(),
        trace=MagicMock(),
        reply=MagicMock(),
        memory_deps=MagicMock(),
        bridge_settings=bridge_settings,
        sticky_store=sticky_store,
    )
    gateway._client.add_view = MagicMock()  # type: ignore[method-assign]
    return gateway


def _ready(gateway: DiscordIngressGateway) -> None:
    """Put the gateway into its post-``on_ready`` state without a live handshake.

    ``_on_message`` no-ops until the normalizer + bot user id are set on ready;
    setting them directly is how we exercise intake in isolation (no client.user).
    """
    gateway._message_normalizer = MessageNormalizer(_OWNER_USER_ID)
    gateway._bot_user_id = _BOT_USER_ID


class _RecordingHandler:
    """A ``MentionHandler`` stand-in that records the requests handed to it."""

    def __init__(self) -> None:
        self.calls: list[MentionRequest] = []

    async def handle(self, req: MentionRequest) -> None:
        self.calls.append(req)


async def _settle(gateway: DiscordIngressGateway) -> None:
    """Await any spawned handler tasks so their effects are observable."""
    tasks = list(gateway._inflight)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _deliver(gateway: DiscordIngressGateway, msg: Any) -> _RecordingHandler:
    """Drive one message through intake and settle; return the recording handler.

    Whether a message routes is then just ``handler.calls`` — so the same helper
    serves both the "must dispatch" and "must drop" assertions.
    """
    handler = _RecordingHandler()
    gateway._handler = handler  # type: ignore[assignment]
    await gateway._on_message(msg)
    await _settle(gateway)
    return handler


def _req() -> MentionRequest:
    """A minimal request for driving ``_run_handler`` / ``_spawn_handle`` directly."""
    return MentionRequest(
        content="!scribe hi",
        mention_ids=("scribe",),
        author_label="alice",
        message_id=1,
        source_channel_id=10,
        channel_id=10,
        wire=WireMessage(
            event_id="e1",
            kind="message",
            message_id=1,
            channel_id=10,
            source_channel_id=10,
            guild_id=1,
            content="!scribe hi",
            author=WireAuthor(discord_user_id=1, display_name="alice", is_bot=False, is_webhook=False),
            created_at=datetime.now(UTC),
        ),
        reply_target=object(),
    )


class _FakeBotUser:
    """A ``discord.Client.user`` stand-in: ``str()`` → name, ``.id`` → id."""

    def __init__(self, *, name: str = "Calfbot#1234", user_id: int = 42) -> None:
        self.id = user_id
        self._name = name

    def __str__(self) -> str:
        return self._name


class TestOnMessageSpawnsHandler:
    """A real ``!mention`` reaches ``handler.handle`` with a correct request."""

    async def test_mention_spawns_handler_with_populated_request(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(
            message_id=42,
            channel_id=200,
            guild_id=_GUILD_ID,
            author_display_name="Alice",
            content="!scribe help me",
        )
        await gateway._on_message(msg)
        await _settle(gateway)

        assert len(handler.calls) == 1
        req = handler.calls[0]
        assert req.mention_ids == ("scribe",)
        assert req.content == "!scribe help me"
        assert req.author_label == "Alice"
        assert req.message_id == 42
        assert req.channel_id == 200
        assert req.source_channel_id == 200
        assert req.reply_target is msg
        # The typed WireMessage rides along (the handler serializes it into
        # ``deps["discord"]``; the reply poster reads its typed fields).
        assert req.wire.content == "!scribe help me"
        assert req.wire.slash_target == "scribe"

    async def test_thread_message_flattens_parent_but_keeps_thread_source(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(channel_id=500, thread_parent_id=200, guild_id=_GUILD_ID, content="!scribe hi")
        await gateway._on_message(msg)
        await _settle(gateway)

        req = handler.calls[0]
        assert req.channel_id == 200, "parent channel hosts the persona webhook"
        assert req.source_channel_id == 500, "thread id drives history fetching"

    async def test_first_of_multiple_mentions_carried_in_order(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(guild_id=_GUILD_ID, content="!scribe loop in !echo")
        await gateway._on_message(msg)
        await _settle(gateway)

        assert handler.calls[0].mention_ids == ("scribe", "echo")


class TestStickyRouting:
    async def test_ambient_message_routes_to_sticky_owner_for_thread_source(self, fake_message) -> None:
        store = _StickyStore(owner="scribe")
        gateway = _gateway(sticky_store=store)
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(channel_id=500, thread_parent_id=200, guild_id=_GUILD_ID, content="continue here")
        await gateway._on_message(msg)
        await _settle(gateway)

        assert store.gets == ["500"]
        assert len(handler.calls) == 1
        req = handler.calls[0]
        assert req.mention_ids == ("scribe",)
        assert req.route_kind == "sticky"
        assert req.content == "continue here"
        assert req.channel_id == 200
        assert req.source_channel_id == 500

    async def test_ambient_message_is_ignored_when_sticky_replies_disabled(self, fake_message) -> None:
        store = _StickyStore(owner="scribe")
        gateway = _gateway(
            bridge_settings=BridgeSettings(sticky_replies=StickyRepliesSettings(enabled=False)),
            sticky_store=store,
        )
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        await gateway._on_message(fake_message(guild_id=_GUILD_ID, content="continue here"))
        await _settle(gateway)

        assert store.gets == []
        assert handler.calls == []

    async def test_explicit_mention_bypasses_sticky_owner(self, fake_message) -> None:
        store = _StickyStore(owner="planner")
        gateway = _gateway(sticky_store=store)
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        await gateway._on_message(fake_message(guild_id=_GUILD_ID, content="!scribe explicit"))
        await _settle(gateway)

        assert store.gets == []
        assert handler.calls[0].mention_ids == ("scribe",)
        assert handler.calls[0].route_kind == "explicit"

    async def test_unstick_clears_owner_posts_notice_and_does_not_route_trailing_text(self, fake_message) -> None:
        store = _StickyStore(owner="scribe")
        gateway = _gateway(sticky_store=store)
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(channel_id=200, guild_id=_GUILD_ID, content=" !Unstick keep chatting")
        await gateway._on_message(msg)
        await _settle(gateway)

        assert store.clears == ["200"]
        assert handler.calls == []
        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req.content == " !Unstick keep chatting"
        assert text == "Sticky replies cleared for this thread."

    async def test_ambient_webhook_post_does_not_route_to_sticky_owner(self, fake_message) -> None:
        store = _StickyStore(owner="scribe")
        gateway = _gateway(sticky_store=store)
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(author_id=_BOT_USER_ID, webhook_id=777, guild_id=_GUILD_ID, content="agent reply")
        await gateway._on_message(msg)
        await _settle(gateway)

        assert store.gets == []
        assert handler.calls == []


class TestMessageHistoryBudgetWiring:
    """``message_history.max_json_bytes`` must reach the history provider.

    The knob is inert unless the gateway threads it into the provider it builds,
    and a silently-ignored budget looks identical to a working one until an
    envelope is rejected — so pin the wiring itself.
    """

    def test_budget_is_wired_from_settings(self) -> None:
        gateway = _gateway(
            bridge_settings=BridgeSettings(
                message_history=MessageHistorySettings(max_json_bytes=12_345)
            )
        )

        assert gateway._handler._history._max_json_bytes == 12_345


class TestNewThreadCommand:
    async def test_new_with_explicit_mention_creates_thread_and_dispatches_from_thread(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]
        _ready(gateway)

        with patch(
            "calfcord.bridge.gateway.create_thread_from_message",
            new=AsyncMock(return_value=9000),
        ) as create_thread:
            msg = fake_message(message_id=42, channel_id=200, guild_id=_GUILD_ID, content="!new !scribe help me")
            await gateway._on_message(msg)
            await _settle(gateway)

        create_thread.assert_awaited_once_with(msg, name="!new !scribe help me")
        assert len(handler.calls) == 1
        req = handler.calls[0]
        assert req.mention_ids == ("scribe",)
        assert req.content == "!new !scribe help me"
        assert req.channel_id == 200
        assert req.source_channel_id == 9000
        assert req.wire.source_channel_id == 9000
        assert req.wire.content == "!new !scribe help me"
        gateway._reply.post_notice.assert_not_awaited()

    async def test_new_without_mention_dispatches_to_parent_sticky_owner(self, fake_message) -> None:
        store = _StickyStore(owner="planner")
        gateway = _gateway(sticky_store=store)
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        with patch("calfcord.bridge.gateway.create_thread_from_message", new=AsyncMock(return_value=9000)):
            msg = fake_message(message_id=42, channel_id=200, guild_id=_GUILD_ID, content="!new plan this")
            await gateway._on_message(msg)
            await _settle(gateway)

        assert store.gets == ["200"]
        assert len(handler.calls) == 1
        req = handler.calls[0]
        assert req.mention_ids == ("planner",)
        assert req.route_kind == "sticky"
        assert req.source_channel_id == 9000
        assert req.channel_id == 200

    async def test_new_without_mention_or_sticky_posts_note_in_new_thread(self, fake_message) -> None:
        store = _StickyStore(owner=None)
        gateway = _gateway(sticky_store=store)
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]
        _ready(gateway)

        with patch("calfcord.bridge.gateway.create_thread_from_message", new=AsyncMock(return_value=9000)):
            msg = fake_message(message_id=42, channel_id=200, guild_id=_GUILD_ID, content="!new plan this")
            await gateway._on_message(msg)
            await _settle(gateway)

        assert handler.calls == []
        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req.channel_id == 200
        assert posted_req.source_channel_id == 9000
        assert "No agent" in text

    async def test_new_only_triggers_as_first_word(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        with patch(
            "calfcord.bridge.gateway.create_thread_from_message",
            new=AsyncMock(return_value=9000),
        ) as create_thread:
            msg = fake_message(guild_id=_GUILD_ID, content="hello !new !scribe")
            await gateway._on_message(msg)
            await _settle(gateway)

        create_thread.assert_not_awaited()
        assert len(handler.calls) == 1
        assert handler.calls[0].mention_ids == ("scribe",)
        assert handler.calls[0].source_channel_id == 2000

    async def test_new_inside_thread_posts_notice_only(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]
        _ready(gateway)

        with patch(
            "calfcord.bridge.gateway.create_thread_from_message",
            new=AsyncMock(return_value=9000),
        ) as create_thread:
            msg = fake_message(channel_id=500, thread_parent_id=200, guild_id=_GUILD_ID, content="!new !scribe hi")
            await gateway._on_message(msg)
            await _settle(gateway)

        create_thread.assert_not_awaited()
        assert handler.calls == []
        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req.source_channel_id == 500
        assert "parent channel" in text

    async def test_new_thread_creation_failure_posts_notice_and_does_not_dispatch(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]
        _ready(gateway)

        with patch(
            "calfcord.bridge.gateway.create_thread_from_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            msg = fake_message(guild_id=_GUILD_ID, content="!new !scribe hi")
            await gateway._on_message(msg)
            await _settle(gateway)

        assert handler.calls == []
        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req.source_channel_id == 2000
        assert "couldn't create" in text


class TestOnMessageFilters:
    """Messages that must never spawn a handler task."""

    async def test_ambient_message_without_mention_is_ignored(self, fake_message) -> None:
        gateway = _gateway()
        _ready(gateway)
        handler = await _deliver(gateway, fake_message(guild_id=_GUILD_ID, content="just chatting"))
        assert handler.calls == []

    async def test_own_non_webhook_message_is_ignored(self, fake_message) -> None:
        # The /clear marker and operator notices are the bot's own non-webhook
        # posts; re-ingesting them would fan the bot's own text back out to agents.
        gateway = _gateway()
        _ready(gateway)
        msg = fake_message(author_id=_BOT_USER_ID, webhook_id=None, guild_id=_GUILD_ID, content="!scribe hi")
        handler = await _deliver(gateway, msg)
        assert handler.calls == []

    async def test_webhook_post_with_bot_id_passes_through(self, fake_message) -> None:
        # A webhook post (an agent persona) is NOT self-filtered even though it
        # carries the bot's user id — which is exactly why /clear posts its marker
        # as a plain, non-webhook message so the seam above can drop it.
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)
        msg = fake_message(author_id=_BOT_USER_ID, webhook_id=777, guild_id=_GUILD_ID, content="!scribe hi")
        await gateway._on_message(msg)
        await _settle(gateway)
        assert len(handler.calls) == 1

    async def test_dm_is_ignored(self, fake_message) -> None:
        gateway = _gateway()
        _ready(gateway)
        handler = await _deliver(gateway, fake_message(guild_id=None, content="!scribe hi"))
        assert handler.calls == []

    async def test_wrong_guild_is_ignored(self, fake_message) -> None:
        gateway = _gateway()
        _ready(gateway)
        handler = await _deliver(gateway, fake_message(guild_id=_GUILD_ID + 1, content="!scribe hi"))
        assert handler.calls == []

    async def test_pre_ready_message_is_ignored(self, fake_message) -> None:
        # Before on_ready there is no normalizer; intake must no-op defensively.
        gateway = _gateway()
        gateway._message_normalizer = None
        gateway._bot_user_id = None
        handler = await _deliver(gateway, fake_message(guild_id=_GUILD_ID, content="!scribe hi"))
        assert handler.calls == []


class TestSystemMessageFilter:
    """Discord-constructed messages must never trigger a routing decision.

    Discord authors these as the *human* who caused them, so they are
    indistinguishable from real input downstream. The load-bearing case is
    ``thread_created``: opening a thread from the channel's "new thread" button
    posts one into the PARENT channel whose ``content`` is the thread's TITLE
    (discord.py renders it as "X started a thread: **<content>**"). Left
    unfiltered, that title routes in the parent — so the parent's sticky agent
    answers a thread the user opened for someone else.
    """

    async def test_thread_created_title_does_not_route_to_parent_sticky_owner(self, fake_message) -> None:
        # The reported bug: !sol was mentioned in a new thread's seed message, but
        # the parent channel's sticky owner (terra) also answered — because the
        # thread's title arrived in the parent as a thread_created system message.
        store = _StickyStore(owner="terra")
        gateway = _gateway(sticky_store=store)
        _ready(gateway)

        msg = fake_message(
            channel_id=200,
            guild_id=_GUILD_ID,
            content="fix the disco TUI",  # the thread TITLE, echoed into the parent
            message_type=discord.MessageType.thread_created,
        )
        handler = await _deliver(gateway, msg)

        assert handler.calls == []
        assert store.gets == [], "a system message must not even reach a sticky lookup"

    async def test_thread_created_title_with_mention_does_not_route(self, fake_message) -> None:
        # A title like "!scribe fix the TUI" would otherwise dispatch a SECOND
        # time in the parent, so the guard cannot live in the sticky branch alone.
        gateway = _gateway()
        _ready(gateway)

        msg = fake_message(
            guild_id=_GUILD_ID,
            content="!scribe fix the TUI",
            message_type=discord.MessageType.thread_created,
        )
        handler = await _deliver(gateway, msg)

        assert handler.calls == []

    @pytest.mark.parametrize(
        "message_type",
        [
            discord.MessageType.pins_add,
            discord.MessageType.new_member,
            discord.MessageType.channel_name_change,
            # The four below are the ones ``is_system()`` reports as NON-system,
            # so an ``is_system()`` guard would let them through; pin them so the
            # allowlist rationale stays honest.
            discord.MessageType.thread_starter_message,
            discord.MessageType.poll_result,
            discord.MessageType.chat_input_command,
            discord.MessageType.context_menu_command,
        ],
    )
    async def test_system_message_types_never_route(self, fake_message, message_type) -> None:
        store = _StickyStore(owner="terra")
        gateway = _gateway(sticky_store=store)
        _ready(gateway)

        msg = fake_message(guild_id=_GUILD_ID, content="!scribe hi", message_type=message_type)
        handler = await _deliver(gateway, msg)

        assert handler.calls == []
        assert store.gets == [], "the drop must happen before the sticky lookup"

    async def test_user_reply_still_routes(self, fake_message) -> None:
        # A reply (type 19) carries real user content and MUST keep working —
        # the allowlist is not "default only".
        gateway = _gateway()
        _ready(gateway)

        msg = fake_message(guild_id=_GUILD_ID, content="!scribe hi", message_type=discord.MessageType.reply)
        handler = await _deliver(gateway, msg)

        assert len(handler.calls) == 1
        assert handler.calls[0].mention_ids == ("scribe",)


class TestOnMessageDedup:
    async def test_redelivered_message_spawns_handler_once(self, fake_message) -> None:
        # discord.py can replay MESSAGE_CREATE on gateway reconnect; the bounded
        # LRU of message ids must collapse the duplicate.
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(message_id=42, guild_id=_GUILD_ID, content="!scribe hi")
        await gateway._on_message(msg)
        await gateway._on_message(msg)  # redelivery of the SAME id
        await _settle(gateway)

        assert len(handler.calls) == 1


class TestRunHandlerErrorHandling:
    """``_run_handler`` isolates handler crashes from the Discord event loop."""

    async def test_unexpected_crash_posts_generic_notice(self) -> None:
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]

        class _Crash:
            async def handle(self, req: MentionRequest) -> None:
                raise RuntimeError("boom")

        gateway._handler = _Crash()  # type: ignore[assignment]
        req = _req()
        await gateway._run_handler(req)

        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req is req
        assert "Something went wrong" in text
        # The notice must not leak internal detail.
        assert "RuntimeError" not in text

    async def test_broker_bounce_posts_a_transient_notice_not_a_crash_notice(self) -> None:
        """A mention landing during a broker restart is transient, not a bug.

        The substrate runs the broker under ``restart: always`` with a 15s backoff, so a
        mention CAN land while it is down — and calfkit's ``_ensure_started`` leaves the
        client startable on failure, so the very next mention self-heals. The honest
        notice is "try again in a moment", not "something went wrong … an operator should
        check the bridge logs", which sends someone hunting a bug that isn't there.
        """
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]

        class _BrokerDown:
            async def handle(self, req: MentionRequest) -> None:
                raise KafkaConnectionError("Unable to bootstrap from [('localhost', 9092)]")

        gateway._handler = _BrokerDown()  # type: ignore[assignment]
        req = _req()
        await gateway._run_handler(req)

        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req is req
        assert "Something went wrong" not in text
        assert "moment" in text
        assert "bootstrap" not in text  # no internal detail leaks to the channel

    async def test_broker_bounce_is_logged_without_a_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A transient blip must not be logged as a crash.

        ``logger.exception`` at ERROR with a full traceback is how a *bug* is recorded;
        emitting that for a broker restart is noise that trains operators to ignore the
        channel and can trip error alerting on a self-healing condition.
        """
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]

        class _BrokerDown:
            async def handle(self, req: MentionRequest) -> None:
                raise KafkaConnectionError("Unable to bootstrap from [('localhost', 9092)]")

        gateway._handler = _BrokerDown()  # type: ignore[assignment]
        with caplog.at_level(logging.DEBUG):
            await gateway._run_handler(_req())

        records = [r for r in caplog.records if "broker" in r.getMessage().lower()]
        assert records, "the blip must still be recorded for operators"
        assert all(r.levelno < logging.ERROR for r in records)
        assert all(r.exc_info is None for r in records)  # no traceback: not a crash

    async def test_cancelled_error_propagates_without_notice(self) -> None:
        # Shutdown cancellation must propagate so drain sees the task as cancelled;
        # it is NOT an "unexpected crash", so no user-facing notice is posted.
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]

        class _Cancels:
            async def handle(self, req: MentionRequest) -> None:
                raise asyncio.CancelledError

        gateway._handler = _Cancels()  # type: ignore[assignment]
        with pytest.raises(asyncio.CancelledError):
            await gateway._run_handler(_req())
        gateway._reply.post_notice.assert_not_awaited()

    async def test_notice_failure_is_swallowed(self) -> None:
        # Even the best-effort notice can fail (Discord down); that must not escape.
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock(side_effect=RuntimeError("discord down"))  # type: ignore[method-assign]

        class _Crash:
            async def handle(self, req: MentionRequest) -> None:
                raise RuntimeError("boom")

        gateway._handler = _Crash()  # type: ignore[assignment]
        await gateway._run_handler(_req())  # must not raise


class TestDrainInflight:
    async def test_drain_cancels_running_handler_tasks(self) -> None:
        gateway = _gateway()
        started = asyncio.Event()

        class _Blocks:
            async def handle(self, req: MentionRequest) -> None:
                started.set()
                await asyncio.Event().wait()  # block until cancelled

        gateway._handler = _Blocks()  # type: ignore[assignment]
        gateway._spawn_handle(_req())
        await started.wait()
        assert len(gateway._inflight) == 1
        task = next(iter(gateway._inflight))

        await gateway.drain_inflight()
        assert task.cancelled()

    async def test_drain_is_a_noop_when_idle(self) -> None:
        gateway = _gateway()
        await gateway.drain_inflight()  # must not raise


class TestOnReadyRegistersNoPersistentView:
    async def test_on_ready_registers_no_persistent_view(self, tmp_path, monkeypatch) -> None:
        # The N-steps toggle was removed; step traces are now plain persistent v2
        # messages, so _on_ready must no longer register any persistent view.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()  # add_view is stubbed to a MagicMock
        with (
            patch.object(type(gateway._client), "user", new=_FakeBotUser(), create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
        ):
            await gateway._on_ready()

        gateway._client.add_view.assert_not_called()
