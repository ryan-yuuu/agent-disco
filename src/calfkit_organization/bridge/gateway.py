"""Discord ingress gateway daemon and CLI entry point.

Holds the long-lived gateway WebSocket, wires the slash command manager,
publisher, and normalizers together, and exposes ``main()`` as the script
entry point. Run via::

    uv run calfkit-bridge

The daemon depends on a running Kafka broker reachable at ``CALF_HOST_URL``
(defaults to ``localhost``) and a Discord bot configured via the
``DISCORD_*`` environment variables (see ``.env.example``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import discord
from calfkit.client import Client

from calfkit_organization.bridge.normalizer import (
    MessageNormalizer,
    SlashNormalizer,
    UnknownAgentMentionError,
)
from calfkit_organization.bridge.publisher import KafkaPublisher
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.slash import SlashCommandManager
from calfkit_organization.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)


class DiscordIngressGateway:
    """Long-lived gateway daemon. Translates Discord events into Kafka publishes."""

    def __init__(
        self,
        settings: DiscordSettings,
        calfkit_client: Client,
        registry: AgentRegistry,
    ) -> None:
        self._settings = settings
        self._calfkit_client = calfkit_client
        self._registry = registry
        self._publisher = KafkaPublisher(calfkit_client)
        self._client = _GatewayClient(self)

        # MessageNormalizer needs bot_user_id, which we don't know until on_ready.
        self._message_normalizer: MessageNormalizer | None = None
        self._bot_user_id: int | None = None
        self._slash_normalizer = SlashNormalizer(
            registry=registry,
            human_owner_id=settings.owner_user_id,
        )
        self._slash = SlashCommandManager(
            client=self._client,
            registry=registry,
            publisher=self._publisher,
            slash_normalizer=self._slash_normalizer,
        )
        # Native Discord slash commands are disabled. The bridge now uses
        # @<agent_id> text-prefix invocation parsed by MessageNormalizer.
        # We still call slash.sync() in _on_ready with an empty tree to
        # remove any stale slash commands that earlier deploys registered.
        # To re-enable native slashes, uncomment the next line.
        # self._slash.register_all()

    async def start(self) -> None:
        """Connect to the Discord gateway. Blocks until cancelled or disconnect."""
        logger.info(
            "DiscordIngressGateway starting (guild_id=%s)",
            self._settings.guild_id,
        )
        await self._client.start(self._settings.bot_token.get_secret_value())

    async def close(self) -> None:
        """Disconnect cleanly. Idempotent."""
        if not self._client.is_closed():
            await self._client.close()
        await self._publisher.close()

    async def _on_ready(self) -> None:
        bot_user = self._client.user
        assert bot_user is not None, "on_ready fires after authentication completes"
        self._bot_user_id = bot_user.id
        self._message_normalizer = MessageNormalizer(
            registry=self._registry,
            bot_user_id=bot_user.id,
            human_owner_id=self._settings.owner_user_id,
        )
        logger.info("gateway ready as %s (id=%s)", bot_user, bot_user.id)
        await self._slash.sync(self._settings.guild_id)

    async def _on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if self._settings.guild_id is not None and message.guild.id != self._settings.guild_id:
            return
        if self._message_normalizer is None:
            # Pre-ready; shouldn't fire in practice but defensive.
            return
        # Skip the bot's own non-webhook messages (e.g. error replies from
        # _reply_unknown_mention). These are bridge-internal infrastructure
        # noise; agents never need to react to them. Webhook messages (the
        # bot acting as an agent persona) are NOT filtered here — those flow
        # through so the originating agent can self-recognize and other
        # agents can see peer activity.
        if (
            self._bot_user_id is not None
            and message.author.id == self._bot_user_id
            and message.webhook_id is None
        ):
            return
        try:
            wire = self._message_normalizer.normalize(message)
        except UnknownAgentMentionError as err:
            await self._reply_unknown_mention(message, err.unknown_names)
            return
        except Exception:
            logger.exception("failed to normalize message id=%s", message.id)
            return
        try:
            await self._publisher.publish(wire)
        except Exception:
            logger.exception("failed to publish message id=%s", message.id)

    async def _reply_unknown_mention(
        self,
        message: discord.Message,
        unknown_names: list[str],
    ) -> None:
        """Inline-reply to the user that one or more @<name> mentions are unknown.

        The original message is NOT published to Kafka — the user must fix the
        mention(s) and resend for any agent to receive it.
        """
        bad = ", ".join(f"`@{n}`" for n in unknown_names)
        known_specs = list(self._registry.all())
        known_part = (
            f"Known agents: {', '.join(f'`@{s.agent_id}`' for s in known_specs)}."
            if known_specs
            else "No agents are currently registered."
        )
        text = (
            f"No agent matches {bad}. {known_part} "
            f"Please fix the mention and resend the message."
        )
        logger.info(
            "rejected unknown mention(s)=%s message_id=%s",
            unknown_names,
            message.id,
        )
        try:
            await message.reply(text)
        except discord.HTTPException:
            logger.exception("failed to send unknown-mention reply")


class _GatewayClient(discord.Client):
    """``discord.Client`` subclass that delegates events to a ``DiscordIngressGateway``."""

    def __init__(self, gateway: DiscordIngressGateway) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self._gateway = gateway

    async def on_ready(self) -> None:
        await self._gateway._on_ready()

    async def on_message(self, message: discord.Message) -> None:
        await self._gateway._on_message(message)


def main() -> None:
    """CLI entry point. Loads config, constructs the gateway, runs until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit("DISCORD_GUILD_ID is required (global slash sync is too slow for dev)")

    agents_dir = Path(os.getenv("CALFKIT_AGENTS_DIR", "agents"))
    registry = AgentRegistry.from_agents_dir(agents_dir)

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async def _run() -> None:
        async with Client.connect(server_urls) as calfkit_client:
            # Start the broker eagerly so the reply dispatcher's subscriber is active.
            # (Client._invoke would also lazy-start on first call, but we want the
            # dispatcher's consumer group reachable from boot for symmetry.)
            if not calfkit_client.broker._connection:
                await calfkit_client.broker.start()

            gateway = DiscordIngressGateway(settings, calfkit_client, registry)

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)

            gateway_task = asyncio.create_task(gateway.start())
            stop_task = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait(
                    {gateway_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (gateway_task, stop_task):
                    if not t.done():
                        t.cancel()
                await gateway.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
