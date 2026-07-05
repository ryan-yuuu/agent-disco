"""Unit tests for :class:`MessageNormalizer` and :func:`extract_mention_ids`.

Post-0.12 the normalizer is pure of any agent roster (C6): it PARSES a Discord
message into a :class:`WireMessage` and extracts the ordered ``!<id>`` mention
tokens, but never validates them against a registry — that resolution moved to
the live mesh at dispatch time. So there is no ``SlashNormalizer`` and no
``UnknownAgentMentionError`` here anymore; an unknown ``!mention`` is a valid
wire, not an error.
"""

from __future__ import annotations

import pytest

from calfcord.agents.identifier import MENTION_PREFIX
from calfcord.bridge.normalizer import MessageNormalizer, extract_mention_ids

_BOT_USER_ID = 99
_OWNER_USER_ID = 1234


class TestExtractMentionIds:
    """The standalone ``!<id>`` token scanner the gateway gates ambient
    messages on and the normalizer derives ``slash_target`` from."""

    def test_no_mention_returns_empty(self) -> None:
        assert extract_mention_ids("hello world") == ()

    def test_single_mention(self) -> None:
        assert extract_mention_ids("!scribe do the thing") == ("scribe",)

    def test_scanner_is_built_from_the_mention_prefix_constant(self) -> None:
        # The regex is derived from ``identifier.MENTION_PREFIX`` so the trigger
        # char cannot silently drift from the constant (mirrors the charset-drift
        # guard in ``tests/agents/test_identifier.py``).
        assert extract_mention_ids(f"{MENTION_PREFIX}scribe hi") == ("scribe",)

    def test_old_at_prefix_no_longer_triggers(self) -> None:
        # Hard cutover: ``@name`` is inert now; only ``!name`` addresses an agent.
        assert extract_mention_ids("@scribe do the thing") == ()

    def test_mention_need_not_lead(self) -> None:
        # ``!<id>`` need not be the first token; it just has to START a
        # whitespace-delimited token.
        assert extract_mention_ids("hey !scribe what's up") == ("scribe",)

    def test_leading_whitespace_allowed(self) -> None:
        assert extract_mention_ids("   !scribe hi") == ("scribe",)

    def test_lower_cases_tokens(self) -> None:
        # Agent ids are lower-case, so tokens are normalized to match the mesh.
        assert extract_mention_ids("!SCRIBE and !Echo") == ("scribe", "echo")

    def test_order_is_preserved(self) -> None:
        # The handler invokes the FIRST online mention (R-A2), so order matters.
        assert extract_mention_ids("!bravo then !alpha then !charlie") == ("bravo", "alpha", "charlie")

    def test_duplicates_are_collapsed_preserving_first_position(self) -> None:
        # A double-mention must not distort the "no agent online" notice, and a
        # case-variant duplicate collapses to one lower-cased entry at its first
        # position.
        assert extract_mention_ids("!echo !scribe !Echo again") == ("echo", "scribe")

    def test_embedded_bang_is_not_a_mention(self) -> None:
        # A ``!`` mid-token (e.g. ``wait5!echo``) is preceded by a non-whitespace
        # char, so it does not START a token and is excluded — the same
        # token-boundary rule that kept ``foo@bar.com`` from matching under ``@``.
        assert extract_mention_ids("that is wait5!echo though") == ()

    def test_trailing_exclamation_is_not_a_mention(self) -> None:
        # Ordinary exclamation punctuation (``nice work! thanks``) is a ``!``
        # followed by whitespace, so there are no id characters to capture — this
        # guards the most common false-positive risk of the ``!`` prefix.
        assert extract_mention_ids("nice work! thanks everyone") == ()

    def test_bare_bang_is_not_a_mention(self) -> None:
        # ``!`` followed by whitespace has no id characters to capture.
        assert extract_mention_ids("! hi") == ()

    def test_id_charset_hyphen_underscore_digits(self) -> None:
        # The capture uses the canonical agent-id charset ``[a-z0-9_-]``.
        assert extract_mention_ids("!agent-1_x go") == ("agent-1_x",)

    def test_trailing_punctuation_stops_the_capture(self) -> None:
        # A ``:`` (or any non-id char) terminates the token but keeps what precedes.
        assert extract_mention_ids("!scribe: please help") == ("scribe",)

    def test_reserved_command_names_are_not_mentions(self) -> None:
        assert extract_mention_ids("!new !scribe hello") == ("scribe",)
        assert extract_mention_ids("!unstick !scribe hello") == ("scribe",)
        assert extract_mention_ids("!new") == ()


class TestNormalizeMentionAndKind:
    """``normalize`` sets ``kind``/``slash_target`` from the mention scan and
    keeps the original content verbatim (the ``!`` prefix is NOT stripped)."""

    def test_plain_message_is_kind_message(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="hello world"))
        assert wire.kind == "message"
        assert wire.slash_target is None
        assert wire.content == "hello world"

    def test_mention_is_kind_slash_with_first_target(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="!scribe book a haircut"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scribe"
        # The prefix is intentionally retained so the agent sees the full text.
        assert wire.content == "!scribe book a haircut"

    def test_slash_target_is_lower_cased(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="!SCRIBE hi"))
        assert wire.slash_target == "scribe"

    def test_first_of_multiple_mentions_is_the_target(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="!scribe please loop in !echo"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scribe"

    def test_unknown_mention_is_a_valid_wire_not_an_error(self, fake_message) -> None:
        # The registry gate is gone (C6): an unrecognized ``!mention`` normalizes
        # cleanly; the mesh roster decides at dispatch whether it is reachable.
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="!nobody_here hi"))
        assert wire.kind == "slash"
        assert wire.slash_target == "nobody_here"


class TestNormalizeChannelAndThread:
    """Channel flattening: threads collapse to the parent for topic routing while
    ``source_channel_id`` preserves the un-flattened id for history fetching."""

    def test_top_level_message_source_equals_channel(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(channel_id=200))
        assert wire.channel_id == 200
        assert wire.source_channel_id == 200
        assert wire.thread_id is None

    def test_thread_message_collapses_to_parent_channel(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(channel_id=500, thread_parent_id=200))
        # channel_id is the flattened parent; source_channel_id is the thread itself.
        assert wire.channel_id == 200, "thread messages must route on the parent channel id"
        assert wire.source_channel_id == 500
        assert wire.thread_id == 500

    def test_wire_carries_message_and_guild_ids(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        msg = fake_message(message_id=777, guild_id=42)
        wire = normalizer.normalize(msg)
        assert wire.message_id == 777
        assert wire.guild_id == 42
        assert wire.event_id  # a non-empty uuid7 hex


class TestNormalizeAuthor:
    """Author identity resolution: owner, bot/webhook flags, avatar, and the
    (now always ``None``) ``agent_id``."""

    def test_human_owner_is_flagged(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(author_id=_OWNER_USER_ID, author_name="ryan"))
        assert wire.author.is_human_owner is True
        assert wire.author.is_bot is False
        assert wire.author.is_webhook is False

    def test_non_owner_human_is_not_owner(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(author_id=_OWNER_USER_ID + 1))
        assert wire.author.is_human_owner is False

    def test_owner_unset_means_no_one_is_owner(self, fake_message) -> None:
        normalizer = MessageNormalizer(human_owner_id=None)
        wire = normalizer.normalize(fake_message(author_id=_OWNER_USER_ID))
        assert wire.author.is_human_owner is False

    def test_persona_webhook_flags_but_agent_id_is_none(self, fake_message) -> None:
        # A persona webhook post used to resolve ``agent_id`` from a registry; the
        # registry is gone, so ``agent_id`` is always ``None`` (history recognizes
        # agent turns by bot-owned ``webhook_id`` instead, R-A3).
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(
            fake_message(author_display_name="Aksel (Scheduler)", author_is_bot=True, webhook_id=777)
        )
        assert wire.author.is_webhook is True
        assert wire.author.webhook_id == 777
        assert wire.author.is_bot is True
        assert wire.author.agent_id is None
        # A bot/webhook author is never the human owner.
        assert wire.author.is_human_owner is False

    def test_bot_own_non_webhook_message(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(
            fake_message(author_id=_BOT_USER_ID, author_name="calfkit-bot", author_is_bot=True, webhook_id=None)
        )
        assert wire.author.is_bot is True
        assert wire.author.is_webhook is False
        assert wire.author.agent_id is None
        assert wire.author.is_human_owner is False

    def test_display_name_and_avatar_round_trip(self, fake_message) -> None:
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(author_display_name="Alice A.", author_avatar_url="https://cdn/x.png"))
        assert wire.author.display_name == "Alice A."
        assert wire.author.avatar_url == "https://cdn/x.png"

    def test_missing_display_avatar_yields_none_avatar(self) -> None:
        # ``_resolve_avatar_url`` is duck-typed: an author without ``display_avatar``
        # (a minimal fake) resolves to ``None`` rather than raising.
        from datetime import UTC, datetime
        from types import SimpleNamespace

        author = SimpleNamespace(id=4000, name="bob", display_name="bob", bot=False)
        message = SimpleNamespace(
            id=1,
            channel=SimpleNamespace(id=2000),
            guild=SimpleNamespace(id=3000),
            author=author,
            webhook_id=None,
            content="hi",
            created_at=datetime.now(UTC),
        )
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        wire = normalizer.normalize(message)
        assert wire.author.avatar_url is None


class TestNormalizeGuards:
    def test_dm_raises(self, fake_message) -> None:
        # DMs have no guild; callers filter them out, but the normalizer enforces
        # it defensively.
        normalizer = MessageNormalizer(_OWNER_USER_ID)
        with pytest.raises(ValueError, match="DM"):
            normalizer.normalize(fake_message(guild_id=None))
