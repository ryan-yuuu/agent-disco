"""Unit tests for :class:`DiscordPersonaSender` identity helpers.

:meth:`DiscordPersonaSender.owns_webhook` is the predicate the history fetcher
uses to recognize an agent turn (R-A3): a fetched message whose ``webhook_id``
is one of this sender's persona webhooks came from a bridge persona post (its
username *is* the agent name under C8), so it is stamped as a ModelResponse.
Matching is by id — not by display name, not against a live roster — so it is
liveness-independent. These tests inject fake webhooks into the sender's cache
and check membership without touching Discord.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import SecretStr

from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import DiscordPersonaSender, Persona
from calfcord.discord.settings import DiscordSettings


def _settings() -> DiscordSettings:
    """Minimal valid settings (only the two required fields)."""
    return DiscordSettings(bot_token=SecretStr("test-bot-token"), application_id=1234)


class _FakeWebhook:
    """Minimal stand-in for a ``discord.Webhook``: records each ``send`` /
    ``edit_message`` call's args and returns an object carrying a message id
    (as a real ``wait=True`` send does)."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.send_calls: list[dict[str, Any]] = []
        self.edit_calls: list[tuple[int, dict[str, Any]]] = []

    async def send(self, **kwargs: Any) -> SimpleNamespace:
        self.send_calls.append(kwargs)
        return SimpleNamespace(id=self.message_id)

    async def edit_message(self, message_id: int, **kwargs: Any) -> SimpleNamespace:
        self.edit_calls.append((message_id, kwargs))
        return SimpleNamespace(id=message_id)


class TestOwnsWebhook:
    def test_recognizes_own_cached_webhooks(self) -> None:
        """True for any id in the sender's cached webhook set. ``_webhooks`` is
        keyed by channel id; each value is a ``discord.Webhook`` look-alike
        exposing only the ``.id`` the predicate reads."""
        sender = DiscordPersonaSender(_settings())
        sender._webhooks = {
            111: SimpleNamespace(id=999),
            222: SimpleNamespace(id=888),
        }
        assert sender.owns_webhook(999) is True
        assert sender.owns_webhook(888) is True

    def test_rejects_foreign_webhook(self) -> None:
        """False for an id that is not one of the sender's webhooks — a
        third-party webhook is never mis-read as an agent."""
        sender = DiscordPersonaSender(_settings())
        sender._webhooks = {
            111: SimpleNamespace(id=999),
            222: SimpleNamespace(id=888),
        }
        assert sender.owns_webhook(123) is False

    def test_empty_sender_owns_nothing(self) -> None:
        """A sender that has not discovered/created any webhook this process
        lifetime owns nothing (agent history in a channel degrades to
        human-attributed until the first persona send there)."""
        sender = DiscordPersonaSender(_settings())
        assert sender.owns_webhook(999) is False


class TestSendComponents:
    """:meth:`DiscordPersonaSender.send_components` posts a Components-V2
    ``LayoutView`` under a persona identity — no content/embeds (v2 forbids
    them), silent, and returning where the message actually lives."""

    async def test_posts_layout_view_under_persona_silently(self) -> None:
        sender = DiscordPersonaSender(_settings())
        sender._client = object()  # non-None: satisfy the "started" guard
        hook = _FakeWebhook(message_id=777)
        sender._webhooks = {123: hook}  # pre-cache so no Discord round-trip
        view = object()  # opaque LayoutView stand-in — forwarded verbatim
        persona = Persona(name="Astra", avatar_url="https://cdn/a.png")

        sent = await sender.send_components(persona, channel_id=123, view=view)

        assert sent == SentMessage(id=777, channel_id=123)
        assert len(hook.send_calls) == 1
        call = hook.send_calls[0]
        assert call["view"] is view
        assert call["username"] == "Astra"
        assert call["avatar_url"] == "https://cdn/a.png"
        assert call["wait"] is True
        assert call["silent"] is True
        assert call["thread"] is discord.utils.MISSING
        # v2 messages carry no content/embeds — they must not be sent.
        assert "content" not in call
        assert "embeds" not in call

    async def test_routes_into_thread_and_omits_avatar_when_unset(self) -> None:
        sender = DiscordPersonaSender(_settings())
        sender._client = object()
        hook = _FakeWebhook(message_id=42)
        sender._webhooks = {100: hook}
        persona = Persona(name="Bo")  # no avatar override

        sent = await sender.send_components(persona, channel_id=100, view=object(), thread_id=200)

        # The message lives in the thread, not the parent channel.
        assert sent == SentMessage(id=42, channel_id=200)
        call = hook.send_calls[0]
        # avatar unset → MISSING (use the webhook default), never None (which clears).
        assert call["avatar_url"] is discord.utils.MISSING
        # thread targeted via a snowflake Object carrying the thread id.
        assert isinstance(call["thread"], discord.Object)
        assert call["thread"].id == 200

    async def test_raises_when_not_started(self) -> None:
        sender = DiscordPersonaSender(_settings())  # _client is None
        with pytest.raises(RuntimeError):
            await sender.send_components(Persona(name="X"), channel_id=1, view=object())


class TestEditComponents:
    """:meth:`DiscordPersonaSender.edit_components` edits a previously posted
    Components-V2 message in place — view only (identity is frozen at send:
    webhook edits cannot carry ``username``/``avatar_url``, and v2 forbids
    ``content``/``embeds``)."""

    async def test_edits_view_in_place(self) -> None:
        sender = DiscordPersonaSender(_settings())
        sender._client = object()  # non-None: satisfy the "started" guard
        hook = _FakeWebhook(message_id=777)
        sender._webhooks = {123: hook}  # pre-cache so no Discord round-trip
        view = object()  # opaque LayoutView stand-in — forwarded verbatim

        await sender.edit_components(channel_id=123, message_id=777, view=view)

        assert len(hook.edit_calls) == 1
        message_id, call = hook.edit_calls[0]
        assert message_id == 777
        assert call["view"] is view
        assert call["thread"] is discord.utils.MISSING
        # A webhook edit can neither change identity nor carry v2-forbidden
        # fields — none of these may be passed.
        assert "username" not in call
        assert "avatar_url" not in call
        assert "content" not in call
        assert "embeds" not in call

    async def test_routes_edit_into_thread(self) -> None:
        sender = DiscordPersonaSender(_settings())
        sender._client = object()
        hook = _FakeWebhook(message_id=42)
        sender._webhooks = {100: hook}

        await sender.edit_components(channel_id=100, message_id=42, view=object(), thread_id=200)

        _, call = hook.edit_calls[0]
        # thread targeted via a snowflake Object carrying the thread id.
        assert isinstance(call["thread"], discord.Object)
        assert call["thread"].id == 200

    async def test_raises_when_not_started(self) -> None:
        sender = DiscordPersonaSender(_settings())  # _client is None
        with pytest.raises(RuntimeError):
            await sender.edit_components(channel_id=1, message_id=2, view=object())


class TestTraceMentionSuppression:
    """Trace rows carry arbitrary tool output, which can contain `<@id>` or
    `@everyone`. `_plain` escapes markdown but deliberately NOT mentions —
    suppression belongs at the send layer, where Discord actually decides.

    `silent=True` only suppresses the push. The Components-V2 docs additionally
    warn that an edit WITHOUT an explicit `allowed_mentions` re-parses with
    DEFAULT allowances — and the trace edits constantly — so both paths must
    pass it.
    """

    async def test_send_components_suppresses_every_mention(self) -> None:
        settings = _settings()
        sender = DiscordPersonaSender(settings)
        webhook = MagicMock()
        webhook.send = AsyncMock(return_value=SimpleNamespace(id=7))
        sender._client = MagicMock()
        sender._get_or_create_webhook = AsyncMock(return_value=webhook)

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(content="@everyone <@1> hi")))
        await sender.send_components(Persona(name="aksel"), 123, view)

        allowed = webhook.send.await_args.kwargs["allowed_mentions"]
        assert allowed.everyone is False
        assert allowed.users is False
        assert allowed.roles is False

    async def test_edit_components_suppresses_every_mention(self) -> None:
        settings = _settings()
        sender = DiscordPersonaSender(settings)
        webhook = MagicMock()
        webhook.edit_message = AsyncMock()
        sender._client = MagicMock()
        sender._get_or_create_webhook = AsyncMock(return_value=webhook)

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(content="@everyone <@1> hi")))
        await sender.edit_components(channel_id=123, message_id=7, view=view)

        allowed = webhook.edit_message.await_args.kwargs["allowed_mentions"]
        assert allowed.everyone is False
