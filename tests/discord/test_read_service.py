"""Tests for the guild-scoped Discord read service and bridge-only tool nodes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfcord.discord.read_service import DiscordReadService
from calfcord.tools import TOOL_REGISTRY
from calfcord.tools.discord import DISCORD_TOOL_NAMES, build_discord_tool_nodes

_GUILD_ID = 10


class _Permissions:
    def __init__(self, *, view: bool = True, history: bool = True) -> None:
        self.view_channel = view
        self.read_message_history = history


class _Channel:
    def __init__(
        self,
        channel_id: int,
        guild: object,
        *,
        name: str = "general",
        permissions: _Permissions | None = None,
        messages: list[object] | None = None,
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.type = "text"
        self.position = 1
        self.category = None
        self.parent_id = None
        self._permissions = permissions or _Permissions()
        self._messages = messages or []
        self.history_kwargs: dict[str, object] | None = None

    def permissions_for(self, _member: object) -> _Permissions:
        return self._permissions

    def history(self, **kwargs):
        self.history_kwargs = kwargs

        async def _iterate():
            for message in self._messages:
                yield message

        return _iterate()


def _guild(guild_id: int = _GUILD_ID) -> SimpleNamespace:
    member = object()
    return SimpleNamespace(id=guild_id, me=member)


def _client(*, guild: object | None = None, channel: object | None = None) -> MagicMock:
    client = MagicMock()
    client.get_guild.return_value = guild
    client.get_channel.return_value = channel
    client.fetch_channel = AsyncMock(return_value=channel)
    return client


def _message(message_id: int, channel: object, created_at: datetime) -> SimpleNamespace:
    author = SimpleNamespace(id=7, name="ryan", display_name="Ryan", bot=False)
    return SimpleNamespace(
        id=message_id,
        channel=channel,
        created_at=created_at,
        edited_at=None,
        content=f"message {message_id}",
        author=author,
        webhook_id=None,
        reference=None,
        attachments=[],
    )


async def test_list_channels_filters_invisible_and_returns_permissions() -> None:
    guild = _guild()
    visible = _Channel(1, guild, name="general")
    hidden = _Channel(2, guild, name="private", permissions=_Permissions(view=False))
    guild.fetch_channels = AsyncMock(return_value=[hidden, visible])
    guild.active_threads = AsyncMock(return_value=[])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert result["ok"] is True
    assert [row["id"] for row in result["channels"]] == [1]
    assert result["channels"][0]["can_read_history"] is True


async def test_read_messages_is_guild_scoped_bounded_and_oldest_first() -> None:
    guild = _guild()
    channel = _Channel(3, guild)
    newer = _message(2, channel, datetime(2026, 1, 2, tzinfo=UTC))
    older = _message(1, channel, datetime(2026, 1, 1, tzinfo=UTC))
    channel._messages = [newer, older]

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(
        3, limit=999, before_message_id=50
    )

    assert result["ok"] is True
    assert result["limit"] == 100
    assert [row["id"] for row in result["messages"]] == [1, 2]
    assert channel.history_kwargs is not None
    assert channel.history_kwargs["limit"] == 100
    assert channel.history_kwargs["before"].id == 50


async def test_read_messages_rejects_wrong_guild_and_ambiguous_pagination() -> None:
    other_guild = _guild(999)
    channel = _Channel(3, other_guild)
    service = DiscordReadService(_client(channel=channel), _GUILD_ID)

    wrong = await service.read_messages(3)
    ambiguous = await service.read_messages(3, before_message_id=1, after_message_id=2)

    assert wrong["error"]["code"] == "wrong_guild"
    assert ambiguous["error"]["code"] == "invalid_pagination"


async def test_read_messages_distinguishes_forbidden_from_empty() -> None:
    guild = _guild()
    channel = _Channel(3, guild, permissions=_Permissions(history=False))

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(3)

    assert result == {
        "ok": False,
        "error": {
            "code": "forbidden",
            "message": "The bot lacks Read Message History in that Discord channel.",
        },
    }


def test_bridge_tool_nodes_have_exact_read_only_surface() -> None:
    service = MagicMock(spec=DiscordReadService)
    nodes = build_discord_tool_nodes(service)

    assert {node.tool_schema.name for node in nodes} == DISCORD_TOOL_NAMES
    for node in nodes:
        assert node.subscribe_topics == [f"tool.{node.tool_schema.name}.input"]
        assert node.publish_topic == f"tool.{node.tool_schema.name}.output"


def test_discord_tools_are_excluded_from_generic_registry() -> None:
    assert DISCORD_TOOL_NAMES.isdisjoint(TOOL_REGISTRY)
