"""Echo agent — a starter calfkit agent for testing the bridge end-to-end.

The agent subscribes to one or more configured Discord channels via Kafka,
filters incoming events with two gates (not-from-self, slash-addressed-to-me),
and replies to each accepted event with ``echo: <content>`` posted via
:class:`DiscordPersonaSender` as an inline reply.

This demonstrates the per-agent runtime pattern that every other agent in
the org will follow: a separate process, its own consumer group, its own
gates, posting back into Discord through webhooks.

Configuration (environment variables):

    DISCORD_BOT_TOKEN              required — REST access for persona sends
    DISCORD_APPLICATION_ID         required by DiscordSettings
    ECHO_CHANNEL_IDS               comma-separated channel IDs this agent
                                   listens on (e.g. "12345,67890");
                                   falls back to DISCORD_DEFAULT_CHANNEL_ID
    CALF_HOST_URL                  Kafka bootstrap; defaults to "localhost"

The agent's ``display_name`` ("Echo") must match the corresponding entry in
``config/agents.toml``; the bridge's normalizer uses display-name matching to
resolve persona-webhook messages back to ``agent_id="echo"`` for the
not-from-self gate.

Run::

    uv run python agents/echo.py
"""

from __future__ import annotations

import asyncio
import logging
import os

from calfkit.client import Client
from calfkit.models import NodeResult, SessionRunContext, Silent, State
from calfkit.nodes import BaseNodeDef
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.discord.persona import DiscordPersonaSender, Persona
from calfkit_organization.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

load_dotenv()

AGENT_ID = "echo"
DISPLAY_NAME = "Echo"


def _addressable(ctx: SessionRunContext) -> bool:
    """Reject messages from self or unrecognized bots; allow humans and other agents.

    Decision logic:
        - author.agent_id == AGENT_ID → reject (this agent's own persona;
          prevents echo→echo loops).
        - author.is_bot == True AND author.agent_id is None → reject. Covers
          the bridge bot's direct messages and any third-party bots in the
          guild that aren't registered agents.
        - everything else → accept. Includes human users AND other agents'
          recognized personas (so the echo agent can respond to peers if
          they ever address it).
    """
    discord = ctx.deps.provided_deps.get("discord")
    if discord is None:
        return False
    author = discord.get("author", {})
    if author.get("agent_id") == AGENT_ID:
        return False
    if author.get("is_bot", False) and not author.get("agent_id"):
        return False
    return True


def _addressed_to_me(ctx: SessionRunContext) -> bool:
    """Accept plain channel messages; for slash invocations, only those targeted at us.

    Decision table:
        kind="message" → accept (no slash present means the agent is free to respond)
        kind="slash", slash_target == AGENT_ID → accept
        kind="slash", slash_target != AGENT_ID → reject (slash was for some other agent)
    """
    discord = ctx.deps.provided_deps.get("discord")
    if discord is None:
        return False
    if discord.get("kind") == "slash":
        return discord.get("slash_target") == AGENT_ID
    return True


class EchoNode(BaseNodeDef):
    """Replies ``echo: <content>`` as an inline reply to the bridge's slash echo."""

    def __init__(
        self,
        *,
        node_id: str,
        subscribe_topics: list[str],
        persona_sender: DiscordPersonaSender,
    ) -> None:
        super().__init__(node_id=node_id, subscribe_topics=subscribe_topics)
        self._persona_sender = persona_sender
        self._persona = Persona(name=DISPLAY_NAME)

    async def run(self, ctx: SessionRunContext) -> NodeResult[State]:
        wire = WireMessage.model_validate(ctx.deps.provided_deps["discord"])

        sent = await self._persona_sender.send(
            persona=self._persona,
            channel_id=wire.channel_id,
            content=f"echo: {wire.content}",
            reply_to_message_id=wire.message_id,
        )
        logger.info(
            "echoed event_id=%s reply_to=%s reply_id=%s channel=%s",
            wire.event_id,
            wire.message_id,
            sent.id,
            wire.channel_id,
        )
        return Silent()


def _resolve_channel_ids() -> list[int]:
    raw = os.getenv("ECHO_CHANNEL_IDS") or os.getenv("DISCORD_DEFAULT_CHANNEL_ID")
    if not raw:
        raise SystemExit(
            "ECHO_CHANNEL_IDS (or DISCORD_DEFAULT_CHANNEL_ID as fallback) is required: "
            "comma-separated channel IDs the echo agent should listen on."
        )
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = DiscordSettings()  # type: ignore[call-arg]
    channel_ids = _resolve_channel_ids()
    subscribe_topics = [f"discord.channel.{cid}" for cid in channel_ids]
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with DiscordPersonaSender(settings) as persona_sender:
        async with Client.connect(server_urls) as client:
            node = EchoNode(
                node_id=AGENT_ID,
                subscribe_topics=subscribe_topics,
                persona_sender=persona_sender,
            )
            # AND-semantics: both gates must accept. Authorship check first so
            # we short-circuit on self/unknown-bot before doing content-based
            # addressed-to-me checks.
            node.gate(_addressable)
            node.gate(_addressed_to_me)

            worker = Worker(client, [node])
            logger.info(
                "echo agent starting on channels=%s broker=%s",
                channel_ids,
                server_urls,
            )
            await worker.run()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("echo agent shutting down")


if __name__ == "__main__":
    main()
