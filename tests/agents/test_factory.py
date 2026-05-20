"""Unit tests for AgentFactory.

The factory constructs a calfkit ``Worker`` over a single vanilla
``Agent`` node. These tests verify the wiring without invoking a real LLM:
the ``model_client_factory`` constructor argument lets us inject a fake
so no provider client is constructed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from calfkit.nodes import Agent
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfkit_organization.agents.definition import AgentDefinition, Provider
from calfkit_organization.agents.factory import AgentFactory, resolve_provider
from calfkit_organization.agents.state import AgentRuntimeState


def _definition(
    *,
    agent_id: str = "scheduler",
    provider: Provider | None = None,
    model: str | None = None,
    tools: tuple[str, ...] = (),
    thinking_effort: str | None = None,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        slash=f"/{agent_id}",
        display_name=f"Test ({agent_id})",
        description="A test agent.",
        provider=provider,
        model=model,
        tools=tools,
        thinking_effort=thinking_effort,  # type: ignore[arg-type]
        system_prompt="You are a test agent.",
    )


def _model_factory_spy() -> tuple[list[tuple[str, str]], Any]:
    """Return ``(calls, factory)`` where ``calls`` collects ``(provider, model)`` tuples."""
    calls: list[tuple[str, str]] = []

    def factory(provider: Provider, model_name: str) -> PydanticModelClient:
        calls.append((provider, model_name))
        return MagicMock(spec=PydanticModelClient)

    return calls, factory


class TestConstruction:
    def test_constructs_with_required_args(self) -> None:
        factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
        assert factory is not None


class TestBuild:
    def test_returns_worker_with_one_node(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        # Worker stores nodes in ``_nodes`` (internal; verified by reading
        # calfkit/worker/worker.py).
        assert len(worker._nodes) == 1
        assert isinstance(worker._nodes[0], Agent)

    def test_node_identity_matches_definition(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="scheduler"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].node_id == "scheduler"

    def test_subscribe_topics_use_in_suffix(self) -> None:
        """Bridge publishes to ``discord.channel.{cid}.in``; agent must match.
        Per-agent inbox ``agent.{id}.in`` is always appended for A2A."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(),
            AgentRuntimeState(channels=[100, 200, 300]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics == [
            "discord.channel.100.in",
            "discord.channel.200.in",
            "discord.channel.300.in",
            "agent.scheduler.in",
        ]

    def test_subscribe_topic_template_override(self) -> None:
        """The template is configurable for tests / alternate deployments.
        The per-agent inbox is independent of the channel template."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            subscribe_topic_template="my.test.channel.{cid}",
        )
        worker = factory.build(
            _definition(),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics == [
            "my.test.channel.100",
            "agent.scheduler.in",
        ]

    def test_per_agent_inbox_uses_agent_id(self) -> None:
        """Inbox suffix derives from agent_id, not display_name or slash."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="researcher"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics[-1] == "agent.researcher.in"

    def test_gates_registered_in_short_circuit_order(self) -> None:
        """Addressable gate first (cheap), addressed-to-me second (content-based)."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="scheduler"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        node = worker._nodes[0]
        assert len(node.gates) == 2
        assert node.gates[0].__name__ == "addressable_scheduler"
        assert node.gates[1].__name__ == "addressed_to_me_scheduler"

    def test_empty_channels_raises(self) -> None:
        """An inert worker (no subscriptions) is a configuration bug; fail fast."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with pytest.raises(ValueError, match="no channels"):
            factory.build(_definition(), AgentRuntimeState(channels=[]), MagicMock())


class TestProviderResolution:
    def test_default_provider_is_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "anthropic"

    def test_definition_provider_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_provider="anthropic",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="openai", model="gpt-5"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "openai"

    def test_env_provider_used_when_definition_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_provider="anthropic",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider=None, model="gpt-5"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "openai"

    def test_ctor_default_used_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_provider="openai",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider=None, model="gpt-5"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "openai"

    def test_unknown_env_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var can carry a typo; surface it at build time."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            factory.build(
                _definition(provider=None),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )


class TestModelResolution:
    def test_definition_model_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Definition takes precedence over env var, ctor default, and provider default."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_model="claude-from-ctor",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(model="claude-from-defn"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][1] == "claude-from-defn"

    def test_env_var_used_when_definition_model_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_model="claude-from-ctor",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][1] == "claude-from-env"

    def test_ctor_default_used_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_model="claude-from-ctor",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][1] == "claude-from-ctor"

    def test_provider_default_used_as_final_fallback_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without any model hint, anthropic agents fall back to the project's
        default Claude model."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="anthropic", model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0] == ("anthropic", "claude-sonnet-4-5")

    def test_provider_default_used_as_final_fallback_openai(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without any model hint, openai agents fall back to the OpenAI default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="openai", model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0] == ("openai", "gpt-5-mini")


class TestThinkingEffortBaking:
    """Factory passes definition.thinking_effort through build_model_settings
    into the calfkit Agent constructor as a tier-2 default."""

    def test_anthropic_high_passes_thinking_dict(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="anthropic", thinking_effort="high"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop  # internal access acceptable in tests
        assert agent_loop.model_settings == {
            "anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}
        }

    def test_openai_medium_passes_reasoning_effort(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="openai", thinking_effort="medium"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings == {"openai_reasoning_effort": "low"}

    def test_no_effort_in_definition_no_model_settings(self) -> None:
        """thinking_effort=None → no tier-2 model_settings."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="anthropic"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings is None

    def test_effort_none_passes_empty_dict(self) -> None:
        """Explicit "none" → empty dict (calfkit merges as no-op)."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="openai", thinking_effort="none"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings == {}


class TestResolveProviderModuleFunction:
    """``resolve_provider`` is lifted to module scope so the bridge can reuse it."""

    def test_definition_provider_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        assert (
            resolve_provider(_definition(provider="openai"), default_provider="anthropic")
            == "openai"
        )

    def test_env_var_used_when_definition_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        assert (
            resolve_provider(_definition(provider=None), default_provider="anthropic")
            == "openai"
        )

    def test_default_used_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        assert (
            resolve_provider(_definition(provider=None), default_provider="openai")
            == "openai"
        )

    def test_unknown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            resolve_provider(_definition(provider=None))


def _fake_tool_node(name: str) -> Any:
    """Build a MagicMock that quacks like a ``ToolNodeDef`` for wiring tests."""
    node = MagicMock()
    node.tool_schema.name = name
    return node


class TestToolsWiring:
    """``definition.tools`` names are resolved against the registry and passed
    to the calfkit ``Agent``. Unknown names raise at build time."""

    def test_empty_tools_passes_none_to_agent(self) -> None:
        """Empty tuple → ``Agent(tools=None)`` (calfkit's no-tools sentinel)."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
        )
        worker = factory.build(
            _definition(tools=()),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].tools == []

    def test_known_tool_name_is_wired_through_registry(self) -> None:
        """A name listed in ``tools:`` resolves to the registry's ToolNodeDef
        and lands in ``Agent.tools``."""
        _, model_factory = _model_factory_spy()
        fake_calendar = _fake_tool_node("calendar")
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": fake_calendar},
        )
        worker = factory.build(
            _definition(tools=("calendar",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].tools == [fake_calendar]

    def test_unknown_tool_name_raises_with_known_list(self) -> None:
        """Typo in ``.md`` fails at build, listing every unknown plus
        what the registry actually contains so the operator can fix it."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": _fake_tool_node("calendar")},
        )
        with pytest.raises(ValueError, match="unknown tool"):
            factory.build(
                _definition(agent_id="scheduler", tools=("calndar",)),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_unknown_tool_error_aggregates_multiple_names(self) -> None:
        """Several typos surface in one message — operator fixes the .md once."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": _fake_tool_node("calendar")},
        )
        with pytest.raises(ValueError) as excinfo:
            factory.build(
                _definition(agent_id="scheduler", tools=("calndar", "emial")),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )
        assert "calndar" in str(excinfo.value)
        assert "emial" in str(excinfo.value)
