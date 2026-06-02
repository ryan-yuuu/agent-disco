"""REST-only Discord sender.

Authenticates with the bot token via ``Client.login`` but never opens a
gateway WebSocket. Suitable for short-lived processes (CLI scripts, web
request handlers, background workers) that need to post messages without
also subscribing to events.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self

import discord

from calfcord.discord.messages import SentMessage
from calfcord.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)


class DiscordSender:
    """REST-only client for posting messages to Discord channels.

    Use as an async context manager for automatic cleanup, or call
    ``start()`` and ``close()`` explicitly for long-lived instances.

    Example::

        async with DiscordSender(settings) as sender:
            await sender.send(channel_id=123, content="hello")
    """

    def __init__(self, settings: DiscordSettings) -> None:
        self._settings = settings
        self._client: discord.Client | None = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Authenticate against Discord's REST API. Idempotent."""
        if self._client is not None:
            return
        # Intents.none() because we never connect to the gateway; this
        # client only authorizes HTTP calls.
        client = discord.Client(intents=discord.Intents.none())
        await client.login(self._settings.bot_token.get_secret_value())
        self._client = client
        logger.info("DiscordSender authenticated")

    async def close(self) -> None:
        """Close the underlying HTTP session. Idempotent."""
        if self._client is None:
            return
        await self._client.close()
        self._client = None
        logger.info("DiscordSender closed")

    @property
    def client(self) -> discord.Client:
        """The authenticated underlying ``discord.Client``.

        Available after :meth:`start` and before :meth:`close`. Bridge components
        (e.g. ``A2AChannelResolver``) use this to perform REST calls beyond
        message sending — guild fetches, channel creation, etc.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
        """
        if self._client is None:
            raise RuntimeError(
                "DiscordSender not started; call start() or use as an async context manager."
            )
        return self._client

    async def send(
        self,
        channel_id: int,
        content: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> SentMessage:
        """Send a text message to a channel or thread.

        Args:
            channel_id: Numeric ID of the destination channel or thread.
                The bot must already be a member with Send Messages permission.
            content: Plain message text. Discord's 2000-character limit applies.
            reply_to_message_id: When set, posts as an inline reply to that
                message. ``fail_if_not_exists=False`` so a deleted target
                degrades to a normal post rather than raising.

        Returns:
            SentMessage carrying the new message's ID.

        Raises:
            RuntimeError: If ``start()`` has not been called.
            discord.HTTPException: For Discord-side failures (permissions,
                invalid channel, rate-limit exhaustion, etc.).
        """
        if self._client is None:
            raise RuntimeError(
                "DiscordSender not started; call start() or use as an async context manager."
            )

        # PartialMessageable lets us .send() without first fetching the
        # full channel object — saves a REST round trip per send.
        messageable = self._client.get_partial_messageable(channel_id)

        reference: discord.MessageReference | None = None
        if reply_to_message_id is not None:
            reference = discord.MessageReference(
                message_id=reply_to_message_id,
                channel_id=channel_id,
                fail_if_not_exists=False,
            )

        sent = await messageable.send(content=content, reference=reference)
        logger.debug("sent message id=%s channel=%s", sent.id, channel_id)
        return SentMessage(id=sent.id, channel_id=channel_id)
