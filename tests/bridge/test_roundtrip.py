"""Unit tests for ``BridgeRoundTrip.handle``.

Mocks the calfkit ``Client.execute_node`` to return a constructed
``NodeResult``, asserts ``DiscordPersonaSender.send`` is (or isn't) called
with the right arguments. No Kafka, no Discord, no LLM.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.client import NodeResult
from calfkit.models import State

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.roundtrip import BridgeRoundTrip
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage


def _wire(
    *,
    slash_target: str | None = "scheduler",
    kind: str = "slash",
) -> WireMessage:
    return WireMessage(
        event_id="evt-1",
        kind=kind,  # type: ignore[arg-type]
        slash_target=slash_target,
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content="book me a haircut",
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )


def _registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                slash="/scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                avatar_url="https://example.com/aksel.png",
                system_prompt="Test scheduler.",
            )
        ]
    )


def _node_result(
    *,
    output: str = "Booked.",
    emitter_node_id: str | None = "scheduler",
    emitter_node_kind: str | None = "agent",
) -> NodeResult[Any]:
    return NodeResult(
        output=output,
        state=State(),
        correlation_id="evt-1",
        emitter_node_id=emitter_node_id,
        emitter_node_kind=emitter_node_kind,
    )


@pytest.fixture
def persona_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=SentMessage(id=99999, channel_id=6789))
    return sender


@pytest.fixture
def client() -> MagicMock:
    """A calfkit Client mock with ``execute_node`` as an AsyncMock by default."""
    c = MagicMock()
    c.execute_node = AsyncMock()
    return c


class TestHappyPath:
    async def test_posts_under_resolved_persona(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())

        persona_sender.send.assert_awaited_once()
        kwargs = persona_sender.send.call_args.kwargs
        assert kwargs["persona"].name == "Aksel (Scheduler)"
        assert kwargs["persona"].avatar_url == "https://example.com/aksel.png"
        assert kwargs["channel_id"] == 6789
        assert kwargs["content"] == "Booked."
        # ReplyContext built from wire — anchored to the inbound message id.
        assert kwargs["reply_to"].message_id == 12345
        assert kwargs["reply_to"].guild_id == 4242

    async def test_invokes_with_in_suffix_topic(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """Ingress topic must use the .in suffix to match the agent's subscribe."""
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())

        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["topic"] == "discord.channel.6789.in"
        assert kwargs["correlation_id"] == "evt-1"
        # The full wire round-trips as a dep so the agent's gate can inspect it.
        assert kwargs["deps"]["discord"]["channel_id"] == 6789
        assert kwargs["deps"]["discord"]["slash_target"] == "scheduler"

    async def test_strips_whitespace_around_output(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        client.execute_node.return_value = _node_result(output="  Booked.\n\n")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())

        assert persona_sender.send.call_args.kwargs["content"] == "Booked."


class TestDropPaths:
    async def test_timeout_drops_silently_with_warning(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client.execute_node.side_effect = asyncio.TimeoutError()
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()
        assert any("timed out" in r.message for r in caplog.records)

    async def test_non_agent_emitter_kind_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Defense-in-depth: ignore replies that aren't from an agent (e.g.
        accidental client emissions)."""
        client.execute_node.return_value = _node_result(emitter_node_kind="client")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()
        assert any("non-agent emitter" in r.message for r in caplog.records)

    async def test_missing_emitter_id_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        client.execute_node.return_value = _node_result(emitter_node_id=None)
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()

    async def test_unknown_emitter_id_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Agent emitter not in the registry: bug somewhere, but don't crash."""
        client.execute_node.return_value = _node_result(emitter_node_id="ghost")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()
        assert any("unknown agent emitter" in r.message for r in caplog.records)

    async def test_empty_output_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """Discord rejects empty webhook executes (400); skip the post."""
        client.execute_node.return_value = _node_result(output="   \n  ")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()


def _make_registry(
    *,
    scheduler_effort: str | None = None,
    scribe_effort: str | None = None,
) -> AgentRegistry:
    """Two-provider registry with optional baked-in thinking_effort values."""
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                slash="/scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                avatar_url="https://example.com/aksel.png",
                provider="anthropic",
                thinking_effort=scheduler_effort,  # type: ignore[arg-type]
                system_prompt="Anthropic scheduler.",
            ),
            AgentDefinition(
                agent_id="scribe",
                slash="/scribe",
                display_name="Scribe",
                description="Notes.",
                avatar_url="https://example.com/scribe.png",
                provider="openai",
                thinking_effort=scribe_effort,  # type: ignore[arg-type]
                system_prompt="OpenAI scribe.",
            ),
        ]
    )


class TestModelSettings:
    """Per-call model_settings injection driven by registry state + provider mapping."""

    @pytest.mark.parametrize(
        ("effort", "expected"),
        [
            ("low", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 4000}}),
            ("medium", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 10000}}),
            ("high", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}}),
            ("xhigh", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 48000}}),
            ("max", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 63999}}),
        ],
    )
    async def test_anthropic_target_passes_thinking_dict_for_each_tier(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        effort: str,
        expected: dict,
    ) -> None:
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(
            client,
            _make_registry(scheduler_effort=effort),
            persona_sender,
        )
        await rt.handle(_wire(slash_target="scheduler"))
        assert client.execute_node.call_args.kwargs["model_settings"] == expected

    @pytest.mark.parametrize(
        ("effort", "expected_value"),
        [
            ("low", "minimal"),
            ("medium", "low"),
            ("high", "medium"),
            ("xhigh", "high"),
            ("max", "high"),  # OpenAI saturates at high.
        ],
    )
    async def test_openai_target_passes_reasoning_effort_for_each_tier(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        effort: str,
        expected_value: str,
    ) -> None:
        client.execute_node.return_value = _node_result(emitter_node_id="scribe")
        rt = BridgeRoundTrip(
            client,
            _make_registry(scribe_effort=effort),
            persona_sender,
        )
        await rt.handle(_wire(slash_target="scribe"))
        assert client.execute_node.call_args.kwargs["model_settings"] == {
            "openai_reasoning_effort": expected_value
        }

    async def test_effort_none_passes_empty_dict(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """Operator-disabled thinking is an explicit empty dict, not None."""
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(
            client,
            _make_registry(scheduler_effort="none"),
            persona_sender,
        )
        await rt.handle(_wire(slash_target="scheduler"))

        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["model_settings"] == {}

    async def test_no_effort_in_definition_passes_none(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """thinking_effort absent from frontmatter → no override."""
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(client, _make_registry(), persona_sender)
        await rt.handle(_wire(slash_target="scheduler"))

        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["model_settings"] is None

    async def test_ambient_message_passes_none(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """slash_target=None → bridge doesn't know the recipient → no override.

        Documented v1 limitation: ambient messages can't carry per-agent
        effort even when the agent's .md declares one. See
        :mod:`bridge.roundtrip` module docstring.
        """
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(
            client,
            _make_registry(scheduler_effort="max"),
            persona_sender,
        )
        await rt.handle(_wire(slash_target=None, kind="message"))

        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["model_settings"] is None

    async def test_target_missing_from_registry_passes_none(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """slash_target references an unknown agent → log + no override."""
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(client, _make_registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire(slash_target="ghost"))

        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["model_settings"] is None
        assert any("missing from registry" in r.message for r in caplog.records)

    async def test_provider_resolution_failure_degrades_to_no_override(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A bad env-var typo at runtime is caught and degrades to defaults.

        Boot-time validation in ``BridgeRoundTrip.__init__`` catches the
        common case, but if the env var drifts mid-process or
        ``build_model_settings`` ever raises ValueError on an effort tier,
        the per-call resolution path must not blow up the round-trip.
        """
        client.execute_node.return_value = _node_result()

        # Construct the round-trip FIRST (boot validation runs cleanly),
        # then monkeypatch so only the per-call path sees the failure.
        rt = BridgeRoundTrip(
            client,
            _make_registry(scheduler_effort="high"),
            persona_sender,
        )

        def _raise_value_error(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("simulated provider misconfig")

        monkeypatch.setattr(
            "calfkit_organization.bridge.roundtrip.resolve_provider",
            _raise_value_error,
        )

        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire(slash_target="scheduler"))

        # The call still happened — just without an override.
        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["model_settings"] is None
        assert any(
            "model_settings resolution failed" in r.message for r in caplog.records
        )

    async def test_picks_up_runtime_change_after_set_thinking_effort(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Hot reload: a runtime registry mutation flows to the next call's settings."""
        # Use a real on-disk .md so registry.set_thinking_effort can write.
        import frontmatter

        md_path = tmp_path / "scheduler.md"
        post = frontmatter.Post(
            "Body.",
            name="scheduler",
            slash="/scheduler",
            display_name="Aksel (Scheduler)",
            description="Calendar.",
            provider="anthropic",
        )
        md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

        registry = AgentRegistry.from_agents_dir(tmp_path)
        rt = BridgeRoundTrip(client, registry, persona_sender)

        # Before the mutation: no override.
        client.execute_node.return_value = _node_result()
        await rt.handle(_wire(slash_target="scheduler"))
        assert client.execute_node.call_args.kwargs["model_settings"] is None

        # After the mutation: anthropic high (31999) flows through.
        await registry.set_thinking_effort("scheduler", "high")
        await rt.handle(_wire(slash_target="scheduler"))
        assert client.execute_node.call_args.kwargs["model_settings"] == {
            "anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}
        }


class TestConcurrency:
    async def test_semaphore_caps_outstanding(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """With max_in_flight=2, the third concurrent handle waits until one frees."""
        # Block execute_node on an event so handles park inside the semaphore.
        release = asyncio.Event()
        peak_in_flight = 0
        in_flight = 0
        lock = asyncio.Lock()

        async def slow_execute(*_args: Any, **_kwargs: Any) -> NodeResult[Any]:
            nonlocal peak_in_flight, in_flight
            async with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            await release.wait()
            async with lock:
                in_flight -= 1
            return _node_result()

        client.execute_node.side_effect = slow_execute
        rt = BridgeRoundTrip(
            client, _registry(), persona_sender, max_in_flight=2
        )

        tasks = [asyncio.create_task(rt.handle(_wire())) for _ in range(4)]
        # Yield enough times for the semaphore to admit the first two and park
        # the remaining two.
        for _ in range(10):
            await asyncio.sleep(0)
        assert peak_in_flight == 2
        release.set()
        await asyncio.gather(*tasks)
        assert peak_in_flight == 2
