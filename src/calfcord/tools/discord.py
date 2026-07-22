"""Bridge-hosted, read-only Discord tool nodes.

These nodes are built dynamically around the bridge's authenticated
:class:`~calfcord.discord.read_service.DiscordReadService`.  They are NOT part
of :mod:`calfcord.tools`' generic registry: only the Discord bridge receives the
bot token, and only its co-located Worker may advertise this surface.
"""

from __future__ import annotations

from typing import Any

from calfkit.nodes import ToolNodeDef, agent_tool

from calfcord.discord.read_service import DiscordReadService

DISCORD_TOOL_NAMES = frozenset({"discord_list_channels", "discord_read_messages"})


def build_discord_tool_nodes(service: DiscordReadService) -> list[ToolNodeDef]:
    """Build the two read-only tool nodes bound to ``service``."""

    async def discord_list_channels() -> dict[str, Any]:
        """List Discord channels and active threads visible to the bot.

        Returns channel IDs, names, types, categories, parent relationships, and
        whether message history is readable. The server is fixed by the bridge
        configuration; this tool cannot inspect another server.
        """
        return await service.list_channels()

    async def discord_read_messages(
        channel_id: int,
        limit: int = 50,
        before_message_id: int | None = None,
        after_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Read recent messages from one visible Discord channel or thread.

        Args:
            channel_id: Channel or thread ID returned by discord_list_channels.
            limit: Number of messages to return, from 1 to 100 (default 50).
            before_message_id: Return messages older than this message ID.
            after_message_id: Return messages newer than this message ID. Do not
                combine with before_message_id.
        """
        return await service.read_messages(
            channel_id,
            limit=limit,
            before_message_id=before_message_id,
            after_message_id=after_message_id,
        )

    return [
        agent_tool(discord_list_channels),
        agent_tool(discord_read_messages),
    ]
