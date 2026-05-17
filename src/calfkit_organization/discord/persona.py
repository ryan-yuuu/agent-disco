"""Persona-based Discord sender backed by per-channel webhooks.

A :class:`Persona` is a display identity (name + avatar). The
:class:`DiscordPersonaSender` projects a chosen persona onto each
message by routing through a per-channel webhook and overriding
``username`` and ``avatar_url`` on every send. This is the same
mechanism that powers PluralKit and Tupperbox: many identities, one
underlying bot.

The webhook itself has no intrinsic connection to personas — it is
just our project's outbound write channel into a given Discord text
channel. We name webhooks after the bot/project, not the use case, so
the bot can recognize and reuse its own webhooks across restarts.

The bot user must have the ``Manage Webhooks`` permission in any
channel where this sender is used.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import aiohttp
import discord

from calfkit_organization.discord.messages import SentMessage
from calfkit_organization.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

# Marker name on every webhook this sender creates so the bot can
# recognize its own webhooks across process restarts. Owner-focused
# (matches the bot/project), not use-case-focused.
_WEBHOOK_NAME = "calfkit"

# Reason recorded in Discord's audit log when we create a webhook.
_AUDIT_REASON = "calfkit-organization persona sender"


@dataclass(frozen=True, slots=True)
class Persona:
    """A display identity to project through a webhook on send.

    Attributes:
        name: Display name shown in Discord. 1-80 characters. Discord
            rejects the literal name "Clyde".
        avatar_url: Public URL to an image for the persona's avatar.
            When ``None``, the underlying webhook's default avatar is used.
    """

    name: str
    avatar_url: str | None = None


class DiscordPersonaSender:
    """Send messages under arbitrary :class:`Persona` identities.

    Discovers or creates one webhook per channel on first send and
    caches it for the sender's lifetime. Subsequent sends to the same
    channel reuse the cached webhook. Discovery is serialized with an
    asyncio lock so concurrent first-sends to the same channel cannot
    create duplicate webhooks.

    Use as an async context manager for automatic cleanup, or call
    :meth:`start` and :meth:`close` explicitly for long-lived instances.

    Example::

        aksel = Persona(name="Aksel", avatar_url="https://example.com/aksel.png")
        async with DiscordPersonaSender(settings) as personas:
            await personas.send(aksel, channel_id=123, content="Hello.")
    """

    def __init__(self, settings: DiscordSettings) -> None:
        self._settings = settings
        self._client: discord.Client | None = None
        self._webhooks: dict[int, discord.Webhook] = {}
        self._discovery_lock = asyncio.Lock()

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
        # Intents.none() — we never connect to the gateway, only REST.
        client = discord.Client(intents=discord.Intents.none())
        await client.login(self._settings.bot_token.get_secret_value())
        self._client = client
        logger.info("DiscordPersonaSender authenticated")

    async def close(self) -> None:
        """Close the HTTP session and clear the webhook cache. Idempotent."""
        if self._client is None:
            return
        await self._client.close()
        self._client = None
        self._webhooks.clear()
        logger.info("DiscordPersonaSender closed")

    async def send(
        self,
        persona: Persona,
        channel_id: int,
        content: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> SentMessage:
        """Send a message rendered under ``persona``'s identity.

        Args:
            persona: Display name and avatar to render the message under.
            channel_id: ID of the *parent* text channel that hosts the
                webhook. The bot must have View Channel and Manage
                Webhooks permissions here.
            content: Plain message text. Discord's 2000-character limit applies.
            thread_id: When set, posts into this thread inside ``channel_id``.
                The webhook still lives on the parent channel.
            reply_to_message_id: When set, renders the message as an inline
                reply to that message ID. Routes through a raw HTTP call to
                the webhook execute endpoint because ``discord.Webhook.send``
                in discord.py 2.7.1 does not expose ``message_reference``.

        Returns:
            :class:`SentMessage`. Its ``channel_id`` field is ``thread_id``
            when set, otherwise ``channel_id`` — i.e. where the message
            actually lives.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
            TypeError: If ``channel_id`` does not refer to a text channel.
            discord.Forbidden: If the bot lacks ``Manage Webhooks`` and a
                webhook does not yet exist in the channel.
            discord.HTTPException: For other Discord-side failures.
        """
        if self._client is None:
            raise RuntimeError(
                "DiscordPersonaSender not started; call start() or use as an async context manager."
            )

        webhook = await self._get_or_create_webhook(channel_id)

        if reply_to_message_id is not None:
            return await self._send_via_raw_http(
                webhook=webhook,
                persona=persona,
                content=content,
                channel_id=channel_id,
                reply_to_message_id=reply_to_message_id,
                thread_id=thread_id,
            )

        # discord.utils.MISSING is the library's "argument omitted" sentinel.
        # Passing None would explicitly clear the field; MISSING means
        # "use the webhook's default" (which is what we want when the
        # persona has no avatar override).
        thread = discord.Object(id=thread_id) if thread_id is not None else discord.utils.MISSING
        avatar = persona.avatar_url if persona.avatar_url is not None else discord.utils.MISSING

        sent = await webhook.send(
            content=content,
            username=persona.name,
            avatar_url=avatar,
            thread=thread,
            wait=True,  # required so the response carries the message ID
        )

        message_channel = thread_id if thread_id is not None else channel_id
        logger.debug(
            "sent persona message id=%s persona=%s channel=%s",
            sent.id,
            persona.name,
            message_channel,
        )
        return SentMessage(id=sent.id, channel_id=message_channel)

    @staticmethod
    async def _send_via_raw_http(
        webhook: discord.Webhook,
        persona: Persona,
        content: str,
        channel_id: int,
        reply_to_message_id: int,
        thread_id: int | None,
    ) -> SentMessage:
        """Execute the webhook via raw HTTP to attach ``message_reference``.

        discord.py 2.7.1 does not expose ``message_reference`` on
        :meth:`discord.Webhook.send`, even though Discord's webhook execute
        endpoint supports it. We POST directly to the webhook URL (which
        embeds its token, so no bot auth header is needed) with the
        reference in the JSON body.
        """
        url = f"{webhook.url}?wait=true"
        if thread_id is not None:
            url += f"&thread_id={thread_id}"

        # Discord snowflakes can exceed JavaScript's safe-integer range (2^53)
        # so they MUST be serialized as JSON strings — otherwise Discord's
        # parser may lose precision and silently drop the reference.
        payload: dict[str, Any] = {
            "content": content,
            "username": persona.name,
            "message_reference": {
                "message_id": str(reply_to_message_id),
                "channel_id": str(channel_id),
                "fail_if_not_exists": False,
            },
        }
        if persona.avatar_url is not None:
            payload["avatar_url"] = persona.avatar_url

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise discord.HTTPException(resp, body)
                data = await resp.json()

        message_channel = thread_id if thread_id is not None else channel_id
        sent_id = int(data["id"])
        logger.debug(
            "sent persona reply id=%s persona=%s channel=%s reply_to=%s",
            sent_id,
            persona.name,
            message_channel,
            reply_to_message_id,
        )
        return SentMessage(id=sent_id, channel_id=message_channel)

    async def _get_or_create_webhook(self, channel_id: int) -> discord.Webhook:
        """Return our webhook for ``channel_id``, discovering or creating as needed."""
        cached = self._webhooks.get(channel_id)
        if cached is not None:
            return cached

        async with self._discovery_lock:
            # Re-check inside the lock: a peer task may have populated
            # the cache while we were waiting on it.
            cached = self._webhooks.get(channel_id)
            if cached is not None:
                return cached

            client = self._client
            assert client is not None, "internal: send() guarded that client is set"
            bot_user = client.user
            assert bot_user is not None, "internal: client.user is set after login()"

            channel = await self._fetch_text_channel(client, channel_id)

            for hook in await channel.webhooks():
                if (
                    hook.name == _WEBHOOK_NAME
                    and hook.user is not None
                    and hook.user.id == bot_user.id
                ):
                    logger.info(
                        "reusing existing webhook id=%s in channel=%s",
                        hook.id,
                        channel_id,
                    )
                    self._webhooks[channel_id] = hook
                    return hook

            new_hook = await channel.create_webhook(name=_WEBHOOK_NAME, reason=_AUDIT_REASON)
            logger.info("created webhook id=%s in channel=%s", new_hook.id, channel_id)
            self._webhooks[channel_id] = new_hook
            return new_hook

    @staticmethod
    async def _fetch_text_channel(client: discord.Client, channel_id: int) -> discord.TextChannel:
        """Fetch a TextChannel by ID, raising ``TypeError`` if it isn't one."""
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError(
                f"Channel {channel_id} is a {type(channel).__name__}, not a TextChannel; "
                "webhooks require a parent text channel. To post in a thread, pass the "
                "thread's ID via thread_id and the parent channel's ID via channel_id."
            )
        return channel
