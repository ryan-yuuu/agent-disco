"""Unit tests for the /thinking-effort operator slash command callback.

The Discord layer is exercised via the internal ``_on_thinking_effort``
entry point so we don't need a live ``app_commands.CommandTree`` or a
real ``discord.Interaction``. The fake interaction is the conftest
factory extended with an ``AsyncMock`` for ``response.send_message``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.roundtrip import BridgeRoundTrip
from calfkit_organization.bridge.slash import SlashCommandManager


_OWNER_USER_ID = 9999


def _registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scribe",
                slash="/scribe",
                display_name="Scribe",
                description="Notes.",
                provider="openai",
                system_prompt="Test scribe.",
            ),
            AgentDefinition(
                agent_id="echo",
                slash="/echo",
                display_name="Echo",
                description="Echoes.",
                system_prompt="Test echo.",
            ),
        ]
    )


def _interaction(*, user_id: int = _OWNER_USER_ID) -> Any:
    """Build a fake Discord interaction with an AsyncMock response."""
    response = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=user_id, name="alice", display_name="alice")
    return SimpleNamespace(id=42, user=user, response=response)


def _fake_discord_client() -> MagicMock:
    """A discord.Client mock that doesn't trip CommandTree's duplicate-tree guard.

    ``app_commands.CommandTree.__init__`` checks ``client._connection._command_tree``
    is None before allowing construction. A bare ``MagicMock()`` would return a
    fresh ``MagicMock`` for that attribute (truthy), so we wire ``None`` explicitly.
    """
    client = MagicMock()
    client._connection._command_tree = None
    return client


@pytest.fixture
def manager(tmp_path: Path) -> SlashCommandManager:
    """A SlashCommandManager wired with a tmp state dir and the owner id.

    Uses mocks for the discord.Client + BridgeRoundTrip + SlashNormalizer
    dependencies — none of them are touched by the thinking-effort callback.
    """
    return SlashCommandManager(
        client=_fake_discord_client(),
        registry=_registry(),
        roundtrip=MagicMock(spec=BridgeRoundTrip),
        slash_normalizer=MagicMock(),
        state_dir=tmp_path,
        owner_user_id=_OWNER_USER_ID,
    )


class TestAuthorization:
    async def test_non_owner_is_rejected_no_file_write(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        # seed a state file so we can detect any accidental mutation
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[1]))

        interaction = _interaction(user_id=_OWNER_USER_ID + 1)
        await manager._on_thinking_effort(interaction, "scribe", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "owner" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

        # file unchanged
        reloaded = await store.load()
        assert reloaded.thinking_effort is None

    async def test_owner_id_unset_permits_any_caller(
        self, tmp_path: Path
    ) -> None:
        """When ``owner_user_id`` is None, the slash is open to anyone."""
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[1]))

        manager = SlashCommandManager(
            client=_fake_discord_client(),
            registry=_registry(),
            roundtrip=MagicMock(spec=BridgeRoundTrip),
            slash_normalizer=MagicMock(),
            state_dir=tmp_path,
            owner_user_id=None,
        )
        interaction = _interaction(user_id=123456)
        await manager._on_thinking_effort(interaction, "scribe", "low")

        reloaded = await store.load()
        assert reloaded.thinking_effort == "low"


class TestPersistence:
    async def test_writes_thinking_effort_to_state_file(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[7]))

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        reloaded = await store.load()
        assert reloaded.thinking_effort == "high"
        assert reloaded.channels == [7]  # other fields preserved

    async def test_overwrites_existing_value(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[7], thinking_effort="low"))

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "max")

        reloaded = await store.load()
        assert reloaded.thinking_effort == "max"

    async def test_file_format_is_readable_json(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        """The on-disk file remains human-inspectable after the slash writes."""
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[7]))

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "medium")

        data = json.loads((tmp_path / "scribe.json").read_text(encoding="utf-8"))
        assert data["thinking_effort"] == "medium"
        assert data["channels"] == [7]


class TestErrorPaths:
    async def test_unknown_agent_replies_ephemeral_no_write(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "ghost", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "no agent named" in msg[0].lower() or "ghost" in msg[0]
        assert kwargs.get("ephemeral") is True
        assert not (tmp_path / "ghost.json").exists()

    async def test_unknown_effort_replies_ephemeral(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[1]))

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "bananas")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "bananas" in msg[0].lower() or "unknown effort" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        reloaded = await store.load()
        assert reloaded.thinking_effort is None

    async def test_missing_state_file_replies_with_bootstrap_hint(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        """Agent never bootstrapped → no file → helpful error, not silent success."""
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "no state file" in msg[0].lower() or "bootstrap" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_persistence_oserror_replies_apologetically_with_id(
        self,
        manager: SlashCommandManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Disk-full / permission-denied: apology reply + log with interaction_id."""
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[1]))

        async def _raise_oserror(_value: str) -> None:
            raise OSError("simulated disk full")

        # Patch the specific store the manager already cached at __init__.
        cached_store = manager._stores["scribe"]
        monkeypatch.setattr(cached_store, "set_thinking_effort", _raise_oserror)

        interaction = _interaction()
        with caplog.at_level("ERROR"):
            await manager._on_thinking_effort(interaction, "scribe", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "failed to persist" in msg[0].lower()
        # Interaction id flows into both log and reply for traceability.
        assert str(interaction.id) in msg[0]
        assert kwargs.get("ephemeral") is True
        assert any(
            "failed to persist thinking_effort" in r.message and str(interaction.id) in r.message
            for r in caplog.records
        )

    async def test_missing_store_for_known_agent_id_replies_internally(
        self,
        tmp_path: Path,
    ) -> None:
        """Defensive: if _stores ends up missing an entry for a known agent,
        the callback must reply with an internal-error message rather than
        crashing the interaction.
        """
        manager = SlashCommandManager(
            client=_fake_discord_client(),
            registry=_registry(),
            roundtrip=MagicMock(spec=BridgeRoundTrip),
            slash_normalizer=MagicMock(),
            state_dir=tmp_path,
            owner_user_id=_OWNER_USER_ID,
        )
        # Simulate the post-init drift the defensive log defends against.
        manager._stores.pop("scribe")

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "internal error" in msg.lower()
        assert str(interaction.id) in msg


class TestReplyText:
    async def test_success_reply_mentions_agent_and_effort(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[1]))

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "scribe" in msg
        assert "high" in msg
        assert "next" in msg.lower()  # informs about take-effect timing

    async def test_none_effort_reply_says_disabled(
        self, manager: SlashCommandManager, tmp_path: Path
    ) -> None:
        store = AgentStateStore(tmp_path / "scribe.json")
        await store.save(AgentRuntimeState(channels=[1]))

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "none")

        msg = interaction.response.send_message.call_args[0][0]
        assert "disabled" in msg.lower()


class TestRegisterGuards:
    def test_register_without_state_dir_raises(self) -> None:
        """Misconfiguration must surface at boot, not at slash invocation."""
        manager = SlashCommandManager(
            client=_fake_discord_client(),
            registry=_registry(),
            roundtrip=MagicMock(spec=BridgeRoundTrip),
            slash_normalizer=MagicMock(),
            state_dir=None,
            owner_user_id=_OWNER_USER_ID,
        )
        with pytest.raises(RuntimeError, match="state_dir"):
            manager.register_thinking_effort()

    def test_register_adds_thinking_effort_command_to_tree(
        self, manager: SlashCommandManager
    ) -> None:
        """The happy path: registration adds a single ``thinking-effort`` command."""
        from calfkit_organization.bridge.slash import _THINKING_EFFORT_COMMAND_NAME

        manager.register_thinking_effort()
        cmd = manager._tree.get_command(_THINKING_EFFORT_COMMAND_NAME)
        assert cmd is not None
        assert cmd.name == _THINKING_EFFORT_COMMAND_NAME

    def test_construction_eagerly_builds_one_store_per_agent(
        self, manager: SlashCommandManager
    ) -> None:
        """Stores are cached at __init__ so concurrent slashes don't race on sweep."""
        assert set(manager._stores) == {"scribe", "echo"}
