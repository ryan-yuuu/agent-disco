"""Gateway-based Discord receiver.

Holds a long-lived WebSocket connection to Discord, normalizes incoming
``discord.Message`` events into ``IncomingMessage``, and fans them out
to registered handlers.

Handlers are dispatched sequentially per message; one slow handler will
delay later handlers for the same message but not for subsequent
messages. Exceptions raised inside a handler are logged and isolated so
they don't poison sibling handlers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from calfcord.discord.messages import IncomingMessage
from calfcord.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

type MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordReceiver:
    """Long-lived gateway consumer for Discord messages.

    Example::

        receiver = DiscordReceiver(settings)

        @receiver.on_message
        async def handle(msg: IncomingMessage) -> None:
            print(msg.author_name, msg.content)

        await receiver.start()  # blocks until cancelled
    """

    def __init__(self, settings: DiscordSettings) -> None:
        self._settings = settings
        self._handlers: list[MessageHandler] = []
        self._client = _DispatchingClient(receiver=self, intents=self._build_intents())

    @staticmethod
    def _build_intents() -> discord.Intents:
        # message_content is privileged and must be enabled in the Developer
        # Portal under Bot → Privileged Gateway Intents. ``members`` is
        # deliberately NOT requested: nothing here consumes member events/cache,
        # and requesting it would hard-fail boot with PrivilegedIntentsRequired
        # if the portal toggle is off. The docs still ask users to enable the
        # portal toggle as future-proofing (an unrequested intent is inert).
        intents = discord.Intents.default()
        intents.message_content = True
        return intents

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        """Register an async handler for every received message.

        Returns the handler unchanged so the call doubles as a decorator.
        """
        self._handlers.append(handler)
        return handler

    async def _dispatch(self, message: discord.Message) -> None:
        bot_user = self._client.user
        is_from_self = bot_user is not None and message.author.id == bot_user.id

        incoming = IncomingMessage(
            id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild is not None else None,
            author_id=message.author.id,
            author_name=message.author.name,
            content=message.content,
            created_at=message.created_at,
            is_from_self=is_from_self,
            is_bot=message.author.bot,
        )

        for handler in self._handlers:
            try:
                await handler(incoming)
            except Exception:
                logger.exception(
                    "handler %s raised on message id=%s",
                    getattr(handler, "__name__", repr(handler)),
                    incoming.id,
                )

    async def start(self) -> None:
        """Connect to the gateway and run until cancelled. Blocking."""
        token = self._settings.bot_token.get_secret_value()
        logger.info("DiscordReceiver connecting to gateway")
        await self._client.start(token)

    async def close(self) -> None:
        """Disconnect from the gateway gracefully. Idempotent."""
        if self._client.is_closed():
            return
        await self._client.close()


class _DispatchingClient(discord.Client):
    """Discord client that forwards message events to a ``DiscordReceiver``."""

    def __init__(self, receiver: DiscordReceiver, *, intents: discord.Intents) -> None:
        super().__init__(intents=intents)
        self._receiver = receiver

    async def on_ready(self) -> None:
        user_id = self.user.id if self.user is not None else None
        logger.info("DiscordReceiver ready as %s (id=%s)", self.user, user_id)

    async def on_message(self, message: discord.Message) -> None:
        await self._receiver._dispatch(message)
