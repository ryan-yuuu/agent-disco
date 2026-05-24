"""Unit tests for :class:`A2AChannelResolver`.

The resolver was rewritten from a per-pair design (``a2a-{x}-{y}``
channels indexed by canonical pair) to a unified-channel design (one
``a2a-audit`` channel hosting a thread per A2A conversation). These
tests pin the new contract: the resolver caches a single channel id,
lazily discovers / creates it, and exposes a ``create_anchored_thread``
helper for the ``private_chat`` tool to anchor each new conversation
on its first request message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from calfkit_organization.bridge.egress import A2AChannelResolver
from calfkit_organization.discord.sender import DiscordSender


def _category(*, name: str, channel_id: int) -> MagicMock:
    """Build a ``discord.CategoryChannel``-shaped mock."""
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.name = name
    cat.id = channel_id
    return cat


def _text_channel(*, name: str, channel_id: int) -> MagicMock:
    text = MagicMock(spec=discord.TextChannel)
    text.name = name
    text.id = channel_id
    return text


def _real_body_resolver(
    *,
    channel_name: str = "a2a-audit",
    category_name: str | None = None,
    existing_channels: list | None = None,
    created_text_id: int = 999,
    created_category_id: int = 12345,
) -> tuple[A2AChannelResolver, MagicMock, MagicMock]:
    """Build a resolver wired to a mocked discord client chain.

    The fixture exercises the real bodies of ``_discover``, ``_create``,
    and ``_resolve_category`` so tests can assert on the actual
    ``create_text_channel`` / ``create_category`` calls without
    monkey-patching the resolver's own methods.
    """
    created_text = _text_channel(name="placeholder", channel_id=created_text_id)
    created_category = _category(
        name=category_name or "", channel_id=created_category_id
    )
    guild = MagicMock()
    guild.fetch_channels = AsyncMock(return_value=existing_channels or [])
    guild.create_text_channel = AsyncMock(return_value=created_text)
    guild.create_category = AsyncMock(return_value=created_category)
    sender = MagicMock(spec=DiscordSender)
    sender.client = MagicMock()
    sender.client.fetch_guild = AsyncMock(return_value=guild)
    sender.client.fetch_channel = AsyncMock()
    resolver = A2AChannelResolver(
        sender,
        guild_id=42,
        channel_name=channel_name,
        category_name=category_name,
    )
    return resolver, guild, created_category


class TestConstructor:
    def test_channel_name_is_keyword_only(self) -> None:
        """Positional ``channel_name`` would silently shift any future
        third positional arg; pin keyword-only at the signature level."""
        sender = MagicMock(spec=DiscordSender)
        with pytest.raises(TypeError):
            A2AChannelResolver(sender, 42, "a2a-audit")  # type: ignore[misc]

    def test_channel_name_is_required(self) -> None:
        """The resolver no longer derives the channel name from a pair;
        it must be supplied. The tools runner is responsible for
        defaulting to ``a2a-audit`` from the env var."""
        sender = MagicMock(spec=DiscordSender)
        with pytest.raises(TypeError):
            A2AChannelResolver(sender, 42)  # type: ignore[call-arg]

    def test_category_name_defaults_to_none(self) -> None:
        """Opt-in: omitting ``category_name`` keeps the
        uncategorized-at-root behavior."""
        sender = MagicMock(spec=DiscordSender)
        resolver = A2AChannelResolver(sender, 42, channel_name="a2a-audit")
        assert resolver._category_name is None
        assert resolver._category is None
        assert resolver._unified_channel_id is None


class TestResolveUnifiedChannel:
    async def test_cache_hit_on_second_call(self) -> None:
        """After the first resolution, subsequent calls return the
        cached id without re-fetching the guild."""
        existing = _text_channel(name="a2a-audit", channel_id=555)
        resolver, guild, _ = _real_body_resolver(
            channel_name="a2a-audit",
            existing_channels=[existing],
        )

        first = await resolver.resolve_unified_channel()
        second = await resolver.resolve_unified_channel()

        assert first == second == 555
        # Discovery (which hits the guild) only fires once.
        assert guild.fetch_channels.await_count == 1

    async def test_creates_when_missing(self) -> None:
        """Full miss: ``create_text_channel`` is invoked with the
        configured ``channel_name`` and the resolved category."""
        resolver, guild, _ = _real_body_resolver(
            channel_name="a2a-audit",
            existing_channels=[],
            created_text_id=999,
        )

        channel_id = await resolver.resolve_unified_channel()

        assert channel_id == 999
        guild.create_text_channel.assert_awaited_once()
        kwargs = guild.create_text_channel.await_args.kwargs
        assert kwargs["name"] == "a2a-audit"
        # No category configured → explicit ``None`` lands at guild root.
        assert kwargs["category"] is None
        # The creation reason is operator-facing audit-log context.
        assert "a2a" in kwargs["reason"].lower()

    async def test_uses_configured_name(self) -> None:
        """The constructor's ``channel_name`` is what gets looked up
        and (on miss) created — not a hard-coded default."""
        existing = _text_channel(name="custom-audit", channel_id=777)
        decoy = _text_channel(name="a2a-audit", channel_id=111)
        resolver, guild, _ = _real_body_resolver(
            channel_name="custom-audit",
            existing_channels=[decoy, existing],
        )

        channel_id = await resolver.resolve_unified_channel()

        assert channel_id == 777
        # Did NOT create — the configured name matched an existing channel.
        guild.create_text_channel.assert_not_awaited()

    async def test_under_category(self) -> None:
        """When ``category_name`` is set and the unified channel does
        not yet exist, the channel is created under the resolved
        category (re-using the lazy category creation pattern)."""
        existing_category = _category(name="private-a2a", channel_id=12345)
        resolver, guild, _ = _real_body_resolver(
            channel_name="a2a-audit",
            category_name="private-a2a",
            existing_channels=[existing_category],
        )

        await resolver.resolve_unified_channel()

        guild.create_text_channel.assert_awaited_once()
        kwargs = guild.create_text_channel.await_args.kwargs
        assert kwargs["name"] == "a2a-audit"
        assert kwargs["category"] is existing_category

    async def test_under_lazily_created_category(self) -> None:
        """Cold start: neither category nor unified channel exist.
        Category is created first, then the channel is placed under it."""
        resolver, guild, created_category = _real_body_resolver(
            channel_name="a2a-audit",
            category_name="private-a2a",
            existing_channels=[],
        )

        await resolver.resolve_unified_channel()

        guild.create_category.assert_awaited_once()
        guild.create_text_channel.assert_awaited_once()
        kwargs = guild.create_text_channel.await_args.kwargs
        assert kwargs["category"] is created_category

    async def test_forbidden_propagates(self) -> None:
        """If the bot lacks Manage Channels and discovery misses, the
        ``Forbidden`` from ``create_text_channel`` must bubble out so
        the A2A turn fails loudly rather than silently routing
        projections to nowhere."""
        resolver, guild, _ = _real_body_resolver(
            channel_name="a2a-audit",
            existing_channels=[],
        )
        guild.create_text_channel = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "manage channels")
        )

        with pytest.raises(discord.Forbidden):
            await resolver.resolve_unified_channel()

    async def test_ignores_same_named_non_text_channel(self) -> None:
        """A category (or voice channel) with the same name as the
        configured unified channel must NOT be mistaken for it — only
        ``discord.TextChannel`` instances match."""
        decoy = _category(name="a2a-audit", channel_id=99999)
        resolver, guild, _ = _real_body_resolver(
            channel_name="a2a-audit",
            existing_channels=[decoy],
            created_text_id=999,
        )

        channel_id = await resolver.resolve_unified_channel()

        # Falls through to creation because the decoy doesn't qualify.
        assert channel_id == 999
        guild.create_text_channel.assert_awaited_once()


class TestCreateAnchoredThread:
    """``create_anchored_thread`` fetches the channel, wraps the anchor
    message id in a :class:`discord.Object`, and calls
    :meth:`TextChannel.create_thread` — no ``fetch_message`` round-trip."""

    async def test_uses_channel_create_thread(self) -> None:
        """Assert the call shape: fetches the channel, builds a
        :class:`discord.Object` for the anchor, calls ``create_thread``,
        returns the new thread's id from the returned Thread object."""
        resolver, _, _ = _real_body_resolver()

        # Mock the TextChannel that fetch_channel returns plus the
        # Thread that create_thread returns.
        thread = MagicMock(spec=discord.Thread)
        thread.id = 8888
        channel = _text_channel(name="a2a-audit", channel_id=555)
        channel.create_thread = AsyncMock(return_value=thread)
        resolver._sender.client.fetch_channel = AsyncMock(return_value=channel)

        thread_id = await resolver.create_anchored_thread(
            555, 12345, name="conan→scribe: hi"
        )

        assert thread_id == 8888
        resolver._sender.client.fetch_channel.assert_awaited_once_with(555)
        channel.create_thread.assert_awaited_once()
        kwargs = channel.create_thread.await_args.kwargs
        assert kwargs["name"] == "conan→scribe: hi"
        # The anchor is wrapped in a synthetic Snowflake — no
        # ``fetch_message`` is needed since ``create_thread`` only
        # reads the .id off the Snowflake.
        assert isinstance(kwargs["message"], discord.Object)
        assert kwargs["message"].id == 12345

    async def test_forbidden_propagates(self) -> None:
        """Operator forgot to grant ``Create Public Threads`` — the
        ``Forbidden`` must bubble so the tool can surface an actionable
        error rather than silently swallowing."""
        resolver, _, _ = _real_body_resolver()

        channel = _text_channel(name="a2a-audit", channel_id=555)
        channel.create_thread = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "no threads")
        )
        resolver._sender.client.fetch_channel = AsyncMock(return_value=channel)

        with pytest.raises(discord.Forbidden):
            await resolver.create_anchored_thread(555, 12345, name="t")

    async def test_not_found_propagates(self) -> None:
        """Race: the anchor message was deleted between post and
        anchor. ``discord.NotFound`` must propagate so the tool can
        surface a recoverable error."""
        resolver, _, _ = _real_body_resolver()

        channel = _text_channel(name="a2a-audit", channel_id=555)
        channel.create_thread = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "gone")
        )
        resolver._sender.client.fetch_channel = AsyncMock(return_value=channel)

        with pytest.raises(discord.NotFound):
            await resolver.create_anchored_thread(555, 12345, name="t")

    async def test_http_exception_propagates(self) -> None:
        """Discord 5xx during thread creation must propagate so the
        tool's infra-error handler can re-raise rather than the tool
        returning a happy-path id with no thread on the other side."""
        resolver, _, _ = _real_body_resolver()

        channel = _text_channel(name="a2a-audit", channel_id=555)
        channel.create_thread = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "boom")
        )
        resolver._sender.client.fetch_channel = AsyncMock(return_value=channel)

        with pytest.raises(discord.HTTPException):
            await resolver.create_anchored_thread(555, 12345, name="t")

    async def test_channel_fetch_forbidden_propagates(self) -> None:
        """If the bot loses access between resolution and anchor (or
        was misconfigured for ``View Channel`` from the start),
        ``fetch_channel`` raises ``Forbidden`` and the tool must see
        it — not a silent ``None`` return."""
        resolver, _, _ = _real_body_resolver()
        resolver._sender.client.fetch_channel = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "view")
        )

        with pytest.raises(discord.Forbidden):
            await resolver.create_anchored_thread(555, 12345, name="t")

    async def test_non_text_channel_raises_type_error(self) -> None:
        """Defense in depth: if an operator points
        ``CALFKIT_A2A_CHANNEL_NAME`` at the id of a non-text channel
        (or some future Discord refactor reshapes the return type),
        ``create_thread`` doesn't exist and the resolver surfaces a
        clear ``TypeError`` rather than an opaque ``AttributeError``."""
        resolver, _, _ = _real_body_resolver()
        # Return a CategoryChannel instead of TextChannel.
        not_a_text = _category(name="oops", channel_id=555)
        resolver._sender.client.fetch_channel = AsyncMock(return_value=not_a_text)

        with pytest.raises(TypeError, match="expected TextChannel"):
            await resolver.create_anchored_thread(555, 12345, name="t")


class TestCategoryResolution:
    """Behavior of ``_resolve_category`` — unchanged from the per-pair
    design, but re-verified under the unified-channel callsite."""

    async def test_unconfigured_returns_none_without_io(self) -> None:
        """Default opt-out path must not touch Discord at all — the
        feature's zero-cost-when-unused promise lives here."""
        resolver, guild, _ = _real_body_resolver(category_name=None)
        category = await resolver._resolve_category()
        assert category is None
        guild.fetch_channels.assert_not_called()
        guild.create_category.assert_not_called()

    async def test_finds_existing_category_by_name(self) -> None:
        """When a category with the configured name already exists in
        the guild, reuse it rather than creating a duplicate."""
        existing = _category(name="private-a2a", channel_id=12345)
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[existing],
        )
        result = await resolver._resolve_category()
        assert result is existing
        guild.create_category.assert_not_called()

    async def test_creates_category_when_missing(self) -> None:
        """Lazy creation: first call creates the category if no
        matching one exists in the guild."""
        resolver, guild, created_category = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[],
        )
        result = await resolver._resolve_category()
        assert result is created_category
        guild.create_category.assert_awaited_once()
        kwargs = guild.create_category.await_args.kwargs
        assert kwargs["name"] == "private-a2a"
        assert "a2a" in kwargs["reason"].lower()

    async def test_existing_category_ignores_same_name_non_category(self) -> None:
        """A text/voice channel with the same name as the configured
        category must not be mistaken for the category — only
        ``discord.CategoryChannel`` instances match."""
        decoy = _text_channel(name="private-a2a", channel_id=55555)
        resolver, guild, created_category = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[decoy],
        )
        result = await resolver._resolve_category()
        assert result is created_category
        guild.create_category.assert_awaited_once()

    async def test_category_cached_across_calls(self) -> None:
        """Once resolved, subsequent invocations short-circuit without
        further Discord I/O — the resolver is intended to live for the
        process lifetime."""
        existing = _category(name="private-a2a", channel_id=12345)
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[existing],
        )
        first = await resolver._resolve_category()
        second = await resolver._resolve_category()
        assert first is second
        assert guild.fetch_channels.await_count == 1
        assert resolver._sender.client.fetch_guild.await_count == 1

    async def test_category_create_forbidden_propagates(self) -> None:
        """Operator misconfiguration (no Manage Channels) must abort
        the A2A turn rather than silently fall back to root-level
        channel creation, which would defeat the category's purpose."""
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[],
        )
        guild.create_category = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "manage channels")
        )
        with pytest.raises(discord.Forbidden):
            await resolver._resolve_category()
