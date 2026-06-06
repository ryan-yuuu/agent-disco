"""Unit tests for ``calfkit-tools`` runner helpers.

Covers the pure helpers (``_resolve_timeout``, ``_resolve_channel_name``,
``_resolve_category_name``, ``_resolve_tool_nodes``) and the boot wiring
(``provision_infra`` then ``worker.run()``). The full ``_amain`` requires
Discord auth, a Kafka broker, and an agents directory — too heavy for a unit
test. Operators will see boot failures of those in stderr; the contracts worth
pinning are the local validation helpers, the init-call shape (so private_chat
receives the fetcher), and the provision-before-run lifecycle ordering.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfcord.bridge.egress import A2AChannelResolver
from calfcord.tools import runner
from calfcord.tools.builtin import private_chat


class TestResolveTimeout:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", raising=False)
        assert runner._resolve_timeout() == private_chat.DEFAULT_TIMEOUT_SECONDS

    def test_numeric_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "15.5")
        assert runner._resolve_timeout() == 15.5

    def test_non_numeric_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd env var must fail boot rather than silently use the
        default — a 60s timeout when the operator typed something different
        would be very confusing."""
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "abc")
        with pytest.raises(SystemExit, match="must be a number"):
            runner._resolve_timeout()

    def test_zero_or_negative_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-positive timeout is a misconfiguration — without this
        guard, ``execute_node(timeout=0)`` would either always fail or
        block depending on calfkit's interpretation."""
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "0")
        with pytest.raises(SystemExit, match="must be positive"):
            runner._resolve_timeout()


class TestResolveCategoryName:
    """``CALFKIT_A2A_CHANNEL_CATEGORY`` reading. Opt-in, empty-as-unset."""

    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_A2A_CHANNEL_CATEGORY", raising=False)
        assert runner._resolve_category_name() is None

    def test_set_returns_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "private-a2a")
        assert runner._resolve_category_name() == "private-a2a"

    def test_empty_string_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who leaves the line blank in ``.env`` should
        get the default uncategorized behavior, not a category literally
        named ``""``."""
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "")
        assert runner._resolve_category_name() is None

    def test_whitespace_only_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "   ")
        assert runner._resolve_category_name() is None

    def test_leading_trailing_whitespace_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quoting in shell/env files commonly leaves stray whitespace;
        normalize so ``"  private-a2a "`` and ``"private-a2a"`` are
        equivalent rather than two different Discord categories."""
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "  private-a2a  ")
        assert runner._resolve_category_name() == "private-a2a"


class TestResolveChannelName:
    """``CALFKIT_A2A_CHANNEL_NAME`` reading. Has a default
    (``"private-a2a-chats"``) — operators don't need to set it for the system
    to work, but they can override for multi-tenant deployments."""

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_A2A_CHANNEL_NAME", raising=False)
        assert runner._resolve_channel_name() == "private-a2a-chats"

    def test_env_var_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_NAME", "foo")
        assert runner._resolve_channel_name() == "foo"

    def test_default_constant_value(self) -> None:
        """Pin the literal default — a refactor that silently changes
        the default channel name would split existing operators' deploys
        without warning."""
        assert runner._DEFAULT_CHANNEL_NAME == "private-a2a-chats"

    def test_empty_string_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty / whitespace-only treated as unset (same posture as
        ``_resolve_category_name``) so a blank line in ``.env`` doesn't
        create a literally-named channel."""
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_NAME", "")
        assert runner._resolve_channel_name() == "private-a2a-chats"

    def test_whitespace_only_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_NAME", "   ")
        assert runner._resolve_channel_name() == "private-a2a-chats"

    def test_leading_trailing_whitespace_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_NAME", "  team-a2a  ")
        assert runner._resolve_channel_name() == "team-a2a"


class TestResolveToolNodes:
    def test_returns_nodes_from_populated_registry(self) -> None:
        node = MagicMock()
        result = runner._resolve_tool_nodes({"private_chat": node})
        assert result == [node]

    def test_empty_registry_fails_fast(self) -> None:
        """The empty-registry guard exists specifically to prevent the
        worker from starting in an inert state — subscribed to nothing,
        responding to nothing, but otherwise looking healthy in logs."""
        with pytest.raises(SystemExit, match="empty"):
            runner._resolve_tool_nodes({})

    def test_empty_registry_message_names_include_filter_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty-registry is most often caused by a typo in
        ``CALFCORD_TOOLS_INCLUDE`` (per-tool images). The SystemExit
        message must NAME the env var and surface its value so the
        operator can short-circuit a ``why is my registry empty`` hunt.
        A regression that strips the env-var attribution would pass
        the broader ``match='empty'`` guard above but silently revert
        the cleanup's UX improvement — this test pins it."""
        monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "definitely_not_a_real_tool")
        with pytest.raises(SystemExit) as exc_info:
            runner._resolve_tool_nodes({})
        message = str(exc_info.value)
        assert "CALFCORD_TOOLS_INCLUDE=" in message
        assert "definitely_not_a_real_tool" in message

    def test_empty_registry_message_handles_unset_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``CALFCORD_TOOLS_INCLUDE`` is unset, the message must
        still surface the env var with an explicit ``<unset>`` marker
        rather than an ambiguous empty string — a future log-aggregation
        regex that anchors on ``CALFCORD_TOOLS_INCLUDE=\\S+`` would
        otherwise miss the unset case silently."""
        monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            runner._resolve_tool_nodes({})
        message = str(exc_info.value)
        assert "CALFCORD_TOOLS_INCLUDE=<unset>" in message


class TestDefaultTimeoutValue:
    def test_default_is_60_seconds(self) -> None:
        """Pin the literal — the design discussion settled on 60s. A future
        change to e.g. 600s should be a deliberate decision the test forces
        a reader to confirm, not a silent edit that passes existing tests
        because they only compared against the constant."""
        assert private_chat.DEFAULT_TIMEOUT_SECONDS == 60.0


class TestInitWiringFromRunner:
    """``private_chat.init`` is the boot-time wiring contract from runner
    to tool. These tests pin the kwargs the runner passes so a refactor
    that drops the fetcher (or renames the channel-name kwarg on the
    resolver) breaks here, not in production where A2A would silently
    skip history projection.
    """

    def test_init_signature_accepts_discord_client(self) -> None:
        """``private_chat.init`` must accept ``discord_client`` as a
        keyword argument — pinning the signature catches a future
        rename that would break the runner."""
        import inspect

        sig = inspect.signature(private_chat.init)
        assert "discord_client" in sig.parameters
        # All four singletons must be kwargs-only so call-site renames
        # don't silently swap them.
        for name in ("client", "persona_sender", "resolver", "discord_client"):
            assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY

    def test_init_binds_discord_client_into_module_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end binding: after init() returns, the module-level
        ``_discord_client`` is the client we passed in."""
        import discord
        from calfkit.client import Client as _Client

        from calfcord.discord.persona import (
            DiscordPersonaSender as _PersonaSender,
        )

        monkeypatch.setattr(private_chat, "_discord_client", None)
        discord_client = MagicMock(spec=discord.Client)
        private_chat.init(
            client=MagicMock(spec=_Client),
            persona_sender=MagicMock(spec=_PersonaSender),
            resolver=MagicMock(spec=A2AChannelResolver),
            discord_client=discord_client,
            timeout_seconds=1.0,
        )
        assert private_chat._discord_client is discord_client


class TestAmainBootWiring:
    """The 0.5.4 boot contract: ``_amain`` provisions calfcord's blind-spot
    topics (``provision_infra`` — the #180 reply topic) BEFORE handing the
    lifecycle to the managed ``worker.run()``. The Worker now owns signals +
    broker start/stop, so the runner no longer hand-rolls a shutdown loop; what
    must stay pinned is the provision-before-run ordering (a stray run before
    provisioning would hang the reply dispatcher on a no-auto-create broker).
    """

    async def test_provision_infra_runs_before_worker_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_GUILD_ID", "123")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")

        order: list[str] = []
        client = MagicMock()

        @asynccontextmanager
        async def _fake_sender(_settings):
            yield MagicMock()

        @asynccontextmanager
        async def _fake_persona(_settings):
            persona = MagicMock()
            persona.client = MagicMock()
            yield persona

        @asynccontextmanager
        async def _fake_connect(*_a, **_k):
            yield client

        worker = MagicMock()

        async def _run() -> None:
            order.append("run")

        worker.run = _run

        async def _provision(_client) -> None:
            order.append("provision")

        monkeypatch.setattr(runner, "DiscordSender", lambda _s: _fake_sender(_s))
        monkeypatch.setattr(runner, "DiscordPersonaSender", lambda _s: _fake_persona(_s))
        monkeypatch.setattr(runner.Client, "connect", lambda *a, **k: _fake_connect(*a, **k))
        monkeypatch.setattr(runner, "provision_infra", _provision)
        monkeypatch.setattr(runner, "Worker", lambda *_a, **_k: worker)
        monkeypatch.setattr(runner.private_chat, "init", lambda **_k: None)
        monkeypatch.setattr(runner, "A2AChannelResolver", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(runner, "_resolve_tool_nodes", lambda _r: [MagicMock()])

        await runner._amain()

        assert order == ["provision", "run"]

    async def test_worker_run_crash_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A crash inside the managed ``worker.run()`` must escape ``_amain`` so
        the surrounding ``asyncio.run`` exits non-zero and the supervisor
        restarts the process — the lifecycle guarantee is now native to
        ``Worker.run()``, but the runner must not swallow it."""
        monkeypatch.setenv("DISCORD_GUILD_ID", "123")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")

        @asynccontextmanager
        async def _fake_sender(_settings):
            yield MagicMock()

        @asynccontextmanager
        async def _fake_persona(_settings):
            persona = MagicMock()
            persona.client = MagicMock()
            yield persona

        @asynccontextmanager
        async def _fake_connect(*_a, **_k):
            yield MagicMock()

        worker = MagicMock()
        worker.run = AsyncMock(side_effect=ValueError("simulated kafka drop"))

        monkeypatch.setattr(runner, "DiscordSender", lambda _s: _fake_sender(_s))
        monkeypatch.setattr(runner, "DiscordPersonaSender", lambda _s: _fake_persona(_s))
        monkeypatch.setattr(runner.Client, "connect", lambda *a, **k: _fake_connect(*a, **k))
        monkeypatch.setattr(runner, "provision_infra", AsyncMock())
        monkeypatch.setattr(runner, "Worker", lambda *_a, **_k: worker)
        monkeypatch.setattr(runner.private_chat, "init", lambda **_k: None)
        monkeypatch.setattr(runner, "A2AChannelResolver", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(runner, "_resolve_tool_nodes", lambda _r: [MagicMock()])

        with pytest.raises(ValueError, match="simulated kafka drop"):
            await runner._amain()

