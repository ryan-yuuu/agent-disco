"""Tests for the guild-scoped Discord read service and bridge-only tool nodes.

The service is the security boundary for model-driven Discord reads, so the
error paths matter as much as the happy ones: every failure must come back as a
structured ``ok: False`` envelope with a stable machine-readable ``code``, never
as an exception escaping into the tool runner and never as an empty result that
a model would read as "the channel is empty".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from calfcord.discord.read_service import DiscordReadService
from calfcord.tools import TOOL_REGISTRY
from calfcord.tools.discord import DISCORD_TOOL_NAMES, build_discord_tool_nodes

_GUILD_ID = 10


def _http_error(kind: type[discord.HTTPException], status: int = 503) -> discord.HTTPException:
    """Build a real discord.py exception; the service branches on these types."""
    return kind(SimpleNamespace(status=status, reason="Service Unavailable"), "boom")


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
        position: int = 1,
        category: object | None = None,
        parent_id: int | None = None,
        history_error: Exception | None = None,
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.type = "text"
        self.position = position
        self.category = category
        self.parent_id = parent_id
        self._permissions = permissions or _Permissions()
        self._messages = messages or []
        self._history_error = history_error
        self.history_kwargs: dict[str, object] | None = None

    def permissions_for(self, _member: object) -> _Permissions:
        return self._permissions

    def history(self, **kwargs):
        self.history_kwargs = kwargs

        async def _iterate():
            # discord.py surfaces HTTP failures while the paginator is being
            # consumed, not at call time — mirror that so the service's
            # ``async for`` is what actually raises.
            if self._history_error is not None:
                raise self._history_error
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


# ------------------------------------------------------- list_channels: failures ---


async def test_list_channels_reports_guild_unavailable_when_bot_is_not_a_member() -> None:
    """An unconfigured/kicked bot must say so, not return an empty channel list."""
    result = await DiscordReadService(_client(guild=None), _GUILD_ID).list_channels()

    assert result["ok"] is False
    assert result["error"]["code"] == "guild_unavailable"


async def test_list_channels_reports_bot_member_unavailable_before_cache_fills() -> None:
    """``guild.me`` is None until the member cache populates; permissions cannot
    be evaluated then, so the read must fail closed rather than list everything."""
    guild = SimpleNamespace(id=_GUILD_ID, me=None)
    guild.fetch_channels = AsyncMock(return_value=[])
    guild.active_threads = AsyncMock(return_value=[])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert result["ok"] is False
    assert result["error"]["code"] == "bot_member_unavailable"
    guild.fetch_channels.assert_not_awaited()


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (discord.Forbidden, "forbidden"),
        (discord.NotFound, "guild_not_found"),
        (discord.HTTPException, "discord_http_error"),
    ],
)
async def test_list_channels_maps_discord_failures_to_stable_codes(
    raised: type[discord.HTTPException], code: str
) -> None:
    guild = _guild()
    guild.fetch_channels = AsyncMock(side_effect=_http_error(raised))
    guild.active_threads = AsyncMock(return_value=[])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert result["ok"] is False
    assert result["error"]["code"] == code


async def test_list_channels_http_error_names_the_status_without_leaking_internals() -> None:
    """The model-visible message carries the HTTP status and a retry hint only —
    no Discord response body, headers, or token-bearing repr."""
    guild = _guild()
    guild.fetch_channels = AsyncMock(side_effect=_http_error(discord.HTTPException, status=502))
    guild.active_threads = AsyncMock(return_value=[])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert result["error"]["message"] == (
        "Discord failed while listing channels. HTTP 502; retry the request later."
    )


# ------------------------------------------------------ list_channels: projection ---


async def test_list_channels_dedupes_threads_already_returned_as_channels() -> None:
    """``fetch_channels`` and ``active_threads`` can overlap; a channel must not
    appear twice just because it was reported by both calls."""
    guild = _guild()
    channel = _Channel(1, guild, name="general")
    guild.fetch_channels = AsyncMock(return_value=[channel])
    guild.active_threads = AsyncMock(return_value=[channel])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert [row["id"] for row in result["channels"]] == [1]


async def test_list_channels_sorts_by_position_then_name_then_id() -> None:
    """A stable order keeps the model's channel picks reproducible across calls."""
    guild = _guild()
    guild.fetch_channels = AsyncMock(
        return_value=[
            _Channel(30, guild, name="beta", position=2),
            _Channel(20, guild, name="alpha", position=2),
            _Channel(12, guild, name="same", position=1),
            _Channel(11, guild, name="same", position=1),
        ]
    )
    guild.active_threads = AsyncMock(return_value=[])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert [row["id"] for row in result["channels"]] == [11, 12, 20, 30]


async def test_list_channels_skips_objects_without_a_permission_check() -> None:
    """Categories/forums surface without ``permissions_for``; they are not
    readable channels, so they must be dropped rather than assumed visible."""
    guild = _guild()
    category_like = SimpleNamespace(id=99, name="Category", position=0)
    guild.fetch_channels = AsyncMock(return_value=[category_like, _Channel(1, guild)])
    guild.active_threads = AsyncMock(return_value=[])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert [row["id"] for row in result["channels"]] == [1]


async def test_list_channels_marks_threads_and_carries_category_metadata() -> None:
    guild = _guild()
    thread = MagicMock(spec=discord.Thread)
    thread.id = 5
    thread.name = "standup"
    thread.type = "public_thread"
    thread.position = 0
    thread.parent_id = 1
    thread.category = SimpleNamespace(id=77, name="Team")
    thread.permissions_for.return_value = _Permissions()
    guild.fetch_channels = AsyncMock(return_value=[])
    guild.active_threads = AsyncMock(return_value=[thread])

    result = await DiscordReadService(_client(guild=guild), _GUILD_ID).list_channels()

    assert result["channels"] == [
        {
            "id": 5,
            "name": "standup",
            "type": "public_thread",
            "position": 0,
            "category_id": 77,
            "category_name": "Team",
            "parent_id": 1,
            "is_thread": True,
            "can_read_history": True,
        }
    ]


# --------------------------------------------------- read_messages: input guards ---


@pytest.mark.parametrize("limit", [0, -1, True, "50", 1.5, None])
async def test_read_messages_rejects_limits_outside_the_documented_range(limit: object) -> None:
    """``True`` is an ``int`` in Python; the bool guard stops it becoming ``limit=1``."""
    guild = _guild()
    channel = _Channel(3, guild)

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(
        3, limit=limit
    )

    assert result["error"]["code"] == "invalid_limit"
    assert channel.history_kwargs is None


@pytest.mark.parametrize("channel_id", [0, -1, True, "3", 4.0, None])
async def test_read_messages_rejects_non_snowflake_channel_ids(channel_id: object) -> None:
    client = _client(channel=None)

    result = await DiscordReadService(client, _GUILD_ID).read_messages(channel_id)

    assert result["error"]["code"] == "invalid_channel_id"
    # A malformed id must never reach Discord.
    client.get_channel.assert_not_called()
    client.fetch_channel.assert_not_awaited()


# ----------------------------------------------- read_messages: channel resolution ---


async def test_read_messages_falls_back_to_fetch_when_channel_is_not_cached() -> None:
    guild = _guild()
    channel = _Channel(3, guild)
    client = _client(channel=None)
    client.fetch_channel = AsyncMock(return_value=channel)

    result = await DiscordReadService(client, _GUILD_ID).read_messages(3)

    assert result["ok"] is True
    client.fetch_channel.assert_awaited_once_with(3)


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (discord.Forbidden, "forbidden"),
        (discord.NotFound, "channel_not_found"),
        (discord.HTTPException, "discord_http_error"),
    ],
)
async def test_read_messages_maps_channel_resolution_failures(
    raised: type[discord.HTTPException], code: str
) -> None:
    client = _client(channel=None)
    client.fetch_channel = AsyncMock(side_effect=_http_error(raised))

    result = await DiscordReadService(client, _GUILD_ID).read_messages(3)

    assert result["ok"] is False
    assert result["error"]["code"] == code


# ------------------------------------------------- read_messages: permission gates ---


async def test_read_messages_reports_bot_member_unavailable_before_cache_fills() -> None:
    guild = SimpleNamespace(id=_GUILD_ID, me=None)
    channel = _Channel(3, guild)

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(3)

    assert result["error"]["code"] == "bot_member_unavailable"


async def test_read_messages_rejects_channels_with_no_message_history() -> None:
    """Voice/category/forum objects have no ``history``; say so explicitly rather
    than returning an empty message list that reads as "nothing was said"."""
    guild = _guild()
    voice_like = SimpleNamespace(id=4, guild=guild, name="Voice", position=0, category=None)
    voice_like.permissions_for = lambda _member: _Permissions()

    result = await DiscordReadService(_client(channel=voice_like), _GUILD_ID).read_messages(4)

    assert result["error"]["code"] == "not_messageable"


async def test_read_messages_requires_view_channel_before_history() -> None:
    """View is checked first so a hidden channel reports as unviewable, not as a
    missing-history problem that would invite the model to retry."""
    guild = _guild()
    channel = _Channel(3, guild, permissions=_Permissions(view=False, history=False))

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(3)

    assert result["error"]["message"] == "The bot cannot view that Discord channel."
    assert channel.history_kwargs is None


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (discord.Forbidden, "forbidden"),
        (discord.NotFound, "channel_not_found"),
        (discord.HTTPException, "discord_http_error"),
    ],
)
async def test_read_messages_maps_history_failures(
    raised: type[discord.HTTPException], code: str
) -> None:
    """Permissions can be revoked between the check and the paginator; the
    mid-iteration failure must still land as a structured error."""
    guild = _guild()
    channel = _Channel(3, guild, history_error=_http_error(raised))

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(3)

    assert result["ok"] is False
    assert result["error"]["code"] == code


# ------------------------------------------------ read_messages: paging + payload ---


async def test_read_messages_defaults_to_fifty_and_newest_first() -> None:
    guild = _guild()
    channel = _Channel(3, guild)

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(3)

    assert result["limit"] == 50
    assert channel.history_kwargs == {
        "limit": 50,
        "before": None,
        "after": None,
        "oldest_first": False,
    }


async def test_read_messages_after_id_pages_forward_oldest_first() -> None:
    """``after`` walks forward in time, so Discord must be asked for the OLDEST
    page after that marker — not the newest, which would skip the gap."""
    guild = _guild()
    channel = _Channel(3, guild)

    await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(
        3, after_message_id=99
    )

    assert channel.history_kwargs is not None
    assert channel.history_kwargs["oldest_first"] is True
    assert channel.history_kwargs["after"].id == 99
    assert channel.history_kwargs["before"] is None


async def test_read_messages_projects_edits_replies_attachments_and_bot_authors() -> None:
    guild = _guild()
    channel = _Channel(3, guild)
    attachment = SimpleNamespace(
        id=900, filename="log.txt", url="https://cdn.example/log.txt", content_type="text/plain", size=12
    )
    message = SimpleNamespace(
        id=42,
        channel=channel,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        edited_at=datetime(2026, 1, 2, tzinfo=UTC),
        content="hello",
        author=SimpleNamespace(id=8, name="calfbot", display_name="Calfbot", bot=True),
        webhook_id=555,
        reference=SimpleNamespace(message_id=41),
        attachments=[attachment],
    )
    channel._messages = [message]

    result = await DiscordReadService(_client(channel=channel), _GUILD_ID).read_messages(3)

    assert result["messages"] == [
        {
            "id": 42,
            "channel_id": 3,
            "created_at": "2026-01-01T00:00:00+00:00",
            "edited_at": "2026-01-02T00:00:00+00:00",
            "content": "hello",
            "author": {"id": 8, "name": "calfbot", "display_name": "Calfbot", "is_bot": True},
            "webhook_id": 555,
            "reply_to_message_id": 41,
            "attachments": [
                {
                    "id": 900,
                    "filename": "log.txt",
                    "url": "https://cdn.example/log.txt",
                    "content_type": "text/plain",
                    "size": 12,
                }
            ],
        }
    ]


# ------------------------------------------------------------ service invariants ---


async def test_concurrent_reads_are_bounded_to_two_rest_slots() -> None:
    """Model-driven reads share the bridge's one Discord client. Without a bound,
    a fan-out of tool calls would spend the bridge's whole rate-limit budget and
    stall the message ingress the bridge exists to serve."""
    guild = _guild()
    release = asyncio.Event()
    inflight = 0
    peak = 0

    async def _fetch_channels() -> list[object]:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await release.wait()
        inflight -= 1
        return []

    guild.fetch_channels = _fetch_channels
    guild.active_threads = AsyncMock(return_value=[])
    service = DiscordReadService(_client(guild=guild), _GUILD_ID)

    tasks = [asyncio.create_task(service.list_channels()) for _ in range(5)]
    for _ in range(20):  # let every task reach the semaphore or block on it
        await asyncio.sleep(0)

    assert peak == 2

    release.set()
    await asyncio.gather(*tasks)


def test_read_service_exposes_no_mutation_surface() -> None:
    """The bridge owns the only authenticated Discord client. This service is the
    read projection of it; a send/edit/delete/react method appearing here would
    hand the model write access to the server."""
    public = {name for name in vars(DiscordReadService) if not name.startswith("_")}

    assert public == {"list_channels", "read_messages"}


# ------------------------------------------------------------------- tool nodes ---


def _body(nodes: list, name: str):
    """The bound closure behind a tool node.

    The delegation from tool signature to service call is exactly what these
    tests pin, so they invoke the closure directly instead of routing a
    ``ToolCallRef`` through the node's broker handler.
    """
    return next(node for node in nodes if node.tool_schema.name == name)._tool.function


async def test_list_channels_tool_delegates_to_the_service() -> None:
    service = MagicMock(spec=DiscordReadService)
    service.list_channels = AsyncMock(return_value={"ok": True, "channels": []})

    result = await _body(build_discord_tool_nodes(service), "discord_list_channels")()

    assert result == {"ok": True, "channels": []}
    service.list_channels.assert_awaited_once_with()


async def test_read_messages_tool_forwards_every_argument_by_keyword() -> None:
    service = MagicMock(spec=DiscordReadService)
    service.read_messages = AsyncMock(return_value={"ok": True, "messages": []})

    result = await _body(build_discord_tool_nodes(service), "discord_read_messages")(
        3, limit=25, before_message_id=100, after_message_id=None
    )

    assert result == {"ok": True, "messages": []}
    service.read_messages.assert_awaited_once_with(
        3, limit=25, before_message_id=100, after_message_id=None
    )


async def test_read_messages_tool_default_limit_matches_the_service_default() -> None:
    """The advertised default and the service default must not drift apart —
    the model only ever sees the schema's."""
    service = MagicMock(spec=DiscordReadService)
    service.read_messages = AsyncMock(return_value={"ok": True})

    await _body(build_discord_tool_nodes(service), "discord_read_messages")(3)

    service.read_messages.assert_awaited_once_with(
        3, limit=50, before_message_id=None, after_message_id=None
    )


def test_bridge_tool_nodes_have_exact_read_only_surface() -> None:
    service = MagicMock(spec=DiscordReadService)
    nodes = build_discord_tool_nodes(service)

    assert {node.tool_schema.name for node in nodes} == DISCORD_TOOL_NAMES
    for node in nodes:
        assert node.subscribe_topics == [f"tool.{node.tool_schema.name}.input"]
        assert node.publish_topic == f"tool.{node.tool_schema.name}.output"


def test_read_messages_tool_advertises_channel_id_as_its_only_requirement() -> None:
    """The model must be able to call this with just an id from list_channels."""
    service = MagicMock(spec=DiscordReadService)
    nodes = build_discord_tool_nodes(service)
    schema = _body_schema(nodes, "discord_read_messages")

    assert schema["required"] == ["channel_id"]
    assert set(schema["properties"]) == {
        "channel_id",
        "limit",
        "before_message_id",
        "after_message_id",
    }
    assert schema["properties"]["limit"]["default"] == 50


def _body_schema(nodes: list, name: str) -> dict:
    node = next(node for node in nodes if node.tool_schema.name == name)
    return node.tool_schema.parameters_json_schema


def test_discord_tools_are_excluded_from_generic_registry() -> None:
    assert DISCORD_TOOL_NAMES.isdisjoint(TOOL_REGISTRY)
