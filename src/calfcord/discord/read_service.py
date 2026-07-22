"""Read-only Discord access for bridge-hosted agent tools.

The bridge is the sole owner of Discord credentials and of the authenticated
:class:`discord.Client`.  This service projects that live client into a small,
JSON-safe read surface.  It deliberately has no mutation methods: no send,
edit, delete, reaction, webhook, thread, or moderation operation can be reached
through it.

Every read is pinned to one configured guild and checked against the bot's
effective Discord permissions.  Tool callers therefore receive exactly the
server view granted to the bot, never an arbitrary guild selected by the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

_MAX_MESSAGES = 100
_DEFAULT_MESSAGES = 50


class DiscordReadService:
    """Guild-scoped, read-only projection of an authenticated Discord client."""

    def __init__(self, client: discord.Client, guild_id: int) -> None:
        self._client = client
        self._guild_id = guild_id
        # Bound model-driven REST traffic on the bridge's shared Discord client.
        self._rest_slots = asyncio.Semaphore(2)

    async def list_channels(self) -> dict[str, Any]:
        """List channels and active threads visible to the configured bot."""
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            return _error("guild_unavailable", "The configured Discord server is not available to the bot.")

        member = guild.me
        if member is None:
            return _error("bot_member_unavailable", "The bot's server membership is not available yet.")

        try:
            async with self._rest_slots:
                channels = await guild.fetch_channels()
                threads = await guild.active_threads()
        except discord.Forbidden:
            return _error("forbidden", "The bot cannot list channels in the configured Discord server.")
        except discord.NotFound:
            return _error("guild_not_found", "The configured Discord server no longer exists or is inaccessible.")
        except discord.HTTPException as exc:
            return _discord_error(exc, "Discord failed while listing channels.")

        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for channel in [*channels, *threads]:
            if channel.id in seen or not _is_visible(channel, member):
                continue
            seen.add(channel.id)
            permissions = channel.permissions_for(member)
            rows.append(_channel_row(channel, permissions))

        rows.sort(key=lambda row: (row["position"], row["name"], row["id"]))
        return {"ok": True, "guild_id": self._guild_id, "channels": rows}

    async def read_messages(
        self,
        channel_id: int,
        *,
        limit: int = _DEFAULT_MESSAGES,
        before_message_id: int | None = None,
        after_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Read bounded message history from one visible guild channel or thread."""
        if before_message_id is not None and after_message_id is not None:
            return _error("invalid_pagination", "Use either before_message_id or after_message_id, not both.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return _error("invalid_limit", f"limit must be an integer between 1 and {_MAX_MESSAGES}.")
        limit = min(limit, _MAX_MESSAGES)

        channel, resolve_error = await self._resolve_channel(channel_id)
        if resolve_error is not None:
            return resolve_error
        assert channel is not None

        guild = getattr(channel, "guild", None)
        if guild is None or guild.id != self._guild_id:
            return _error("wrong_guild", "That channel does not belong to the configured Discord server.")
        member = guild.me
        if member is None:
            return _error("bot_member_unavailable", "The bot's server membership is not available yet.")
        if not hasattr(channel, "history"):
            return _error("not_messageable", "That Discord channel does not have readable message history.")

        permissions = channel.permissions_for(member)
        if not permissions.view_channel:
            return _error("forbidden", "The bot cannot view that Discord channel.")
        if not permissions.read_message_history:
            return _error("forbidden", "The bot lacks Read Message History in that Discord channel.")

        before = discord.Object(id=before_message_id) if before_message_id is not None else None
        after = discord.Object(id=after_message_id) if after_message_id is not None else None
        try:
            async with self._rest_slots:
                messages = [
                    message
                    async for message in channel.history(
                        limit=limit,
                        before=before,
                        after=after,
                        oldest_first=after_message_id is not None,
                    )
                ]
        except discord.Forbidden:
            return _error("forbidden", "The bot cannot read message history in that Discord channel.")
        except discord.NotFound:
            return _error("channel_not_found", "That Discord channel no longer exists or is inaccessible.")
        except discord.HTTPException as exc:
            return _discord_error(exc, "Discord failed while reading message history.")

        messages.sort(key=lambda message: (message.created_at, message.id))
        return {
            "ok": True,
            "guild_id": self._guild_id,
            "channel": _channel_row(channel, permissions),
            "messages": [_message_row(message) for message in messages],
            "limit": limit,
        }

    async def _resolve_channel(
        self, channel_id: int
    ) -> tuple[Any | None, dict[str, Any] | None]:
        if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id < 1:
            return None, _error("invalid_channel_id", "channel_id must be a positive Discord snowflake integer.")

        channel = self._client.get_channel(channel_id)
        if channel is not None:
            return channel, None
        try:
            async with self._rest_slots:
                return await self._client.fetch_channel(channel_id), None
        except discord.Forbidden:
            return None, _error("forbidden", "The bot cannot access that Discord channel.")
        except discord.NotFound:
            return None, _error("channel_not_found", "That Discord channel does not exist or is inaccessible.")
        except discord.HTTPException as exc:
            return None, _discord_error(exc, "Discord failed while resolving that channel.")


def _is_visible(channel: Any, member: discord.Member) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    return callable(permissions_for) and bool(permissions_for(member).view_channel)


def _channel_row(channel: Any, permissions: Any) -> dict[str, Any]:
    category = getattr(channel, "category", None)
    return {
        "id": channel.id,
        "name": getattr(channel, "name", str(channel.id)),
        "type": str(getattr(channel, "type", "unknown")),
        "position": int(getattr(channel, "position", 0)),
        "category_id": getattr(category, "id", None),
        "category_name": getattr(category, "name", None),
        "parent_id": getattr(channel, "parent_id", None),
        "is_thread": isinstance(channel, discord.Thread),
        "can_read_history": bool(getattr(permissions, "read_message_history", False)),
    }


def _message_row(message: discord.Message) -> dict[str, Any]:
    reference = message.reference
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at is not None else None,
        "content": message.content,
        "author": {
            "id": message.author.id,
            "name": message.author.name,
            "display_name": getattr(message.author, "display_name", message.author.name),
            "is_bot": message.author.bot,
        },
        "webhook_id": message.webhook_id,
        "reply_to_message_id": reference.message_id if reference is not None else None,
        "attachments": [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "url": attachment.url,
                "content_type": attachment.content_type,
                "size": attachment.size,
            }
            for attachment in message.attachments
        ],
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _discord_error(exc: discord.HTTPException, fallback: str) -> dict[str, Any]:
    return _error(
        "discord_http_error",
        f"{fallback} HTTP {exc.status}; retry the request later.",
    )
