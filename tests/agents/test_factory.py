"""Unit tests for AgentFactory.

The factory constructs a calfkit ``Worker`` over a single vanilla, **name-
addressed** ``Agent`` node. These tests verify the wiring without invoking a
real LLM: the ``model_client_factory`` constructor argument lets us inject a
fake so no provider client is constructed.

Name-addressing (calfkit 0.12, ADR-0017) means the built agent declares no
channel ``subscribe_topics`` and no addressing gate — it is reached by name on
its automatic private input topic. A2A/handoff reach is declared natively via
``peers`` from the ``a2a``/``handoff`` frontmatter fields.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from calfkit import Handoff, Messaging, Toolboxes
from calfkit.mcp import MCPToolbox
from calfkit.nodes import Agent
from calfkit.nodes.tool import Tools
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfcord.agents.definition import AgentDefinition, Provider
from calfcord.agents.factory import AgentFactory, resolve_provider
from calfcord.agents.memory import MEMORY_PROMPT_DEPS_KEY


def _definition(
    *,
    agent_id: str = "scheduler",
    description: str = "A test agent.",
    provider: Provider | None = None,
    model: str | None = None,
    tools: tuple[str, ...] = (),
    mcp: bool | tuple[str, ...] = (),
    thinking_effort: str | None = None,
    a2a: bool | tuple[str, ...] = True,
    handoff: bool | tuple[str, ...] = True,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        description=description,
        provider=provider,
        model=model,
        tools=tools,
        mcp=mcp,
        thinking_effort=thinking_effort,  # type: ignore[arg-type]
        a2a=a2a,
        handoff=handoff,
        system_prompt="You are a test agent.",
    )


def _memory_definition(
    *,
    agent_id: str = "scribe",
    tools: tuple[str, ...] | None = (),
    mcp: tuple[str, ...] = (),
) -> AgentDefinition:
    """A ``memory: true`` definition (built against the real TOOL_REGISTRY)."""
    return AgentDefinition(
        agent_id=agent_id,
        description="A test agent.",
        tools=tools,
        mcp=mcp,
        memory=True,
        system_prompt="You are a test agent.",
    )


def _model_factory_spy() -> tuple[list[tuple[str, str]], Any]:
    """Return ``(calls, factory)`` where ``calls`` collects ``(provider, model)`` tuples."""
    calls: list[tuple[str, str]] = []

    def factory(provider: Provider, model_name: str) -> PydanticModelClient:
        calls.append((provider, model_name))
        return MagicMock(spec=PydanticModelClient)

    return calls, factory


def _factory(**kwargs: Any) -> AgentFactory:
    """Construct an AgentFactory with a spy model-client factory by default."""
    kwargs.setdefault("model_client_factory", _model_factory_spy()[1])
    return AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock(), **kwargs)


def _registered_before_node_seams(node: Agent) -> list[Any]:
    """Return the agent's ``before_node`` seam chain (the 0.12 gate successor).

    A freshly built node only grows the lazy ``_seam_chains`` dict once a seam
    is registered, so an agent with no gate has either no attribute or an empty
    chain — both collapse to ``[]`` here.
    """
    return getattr(node, "_seam_chains", {}).get("before_node", [])


def _registered_on_callee_error_seams(node: Agent) -> list[Any]:
    """Return the agent's ``on_callee_error`` seam chain.

    ``on_tool_error`` is a promoted surface that is sugar over ``on_callee_error``
    (calfkit 0.12.7): a handler passed to ``on_tool_error=`` lands here wrapped in
    a ``functools.wraps`` adapter that preserves the handler's ``__qualname__``, so
    the chain identifies which policy was wired.
    """
    return getattr(node, "_seam_chains", {}).get("on_callee_error", [])


class TestConstruction:
    def test_constructs_with_required_args(self) -> None:
        factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
        assert factory is not None


class TestBuild:
    def test_returns_worker_with_one_node(self) -> None:
        worker = _factory().build(_definition())
        # Worker stores nodes in ``_nodes`` (internal; verified by reading
        # calfkit/worker/worker.py).
        assert len(worker._nodes) == 1
        assert isinstance(worker._nodes[0], Agent)

    def test_node_name_matches_definition(self) -> None:
        """The agent is addressed by name: ``Agent(name=...)`` -> ``node_id``."""
        worker = _factory().build(_definition(agent_id="scheduler"))
        assert worker._nodes[0].node_id == "scheduler"

    def test_description_is_wired_into_agent(self) -> None:
        """``description=`` must reach the Agent or every AgentCard.description
        is ``None`` and both the mesh roster and the message_agent peer
        directory render blank."""
        node = _factory().build_node(_definition(description="Books and preps meetings"))
        assert node._description == "Books and preps meetings"

    def test_no_channel_subscribe_topics(self) -> None:
        """Name-addressing: the agent declares no channel subscriptions; calfkit
        reaches it on its automatic private input topic."""
        node = _factory().build_node(_definition())
        assert node.subscribe_topics == []

    def test_no_publish_topic_steps_mirror(self) -> None:
        """The old ``publish_topic=AGENT_STEPS_TOPIC`` steps mirror is gone —
        live progress now rides the caller's run stream."""
        node = _factory().build_node(_definition())
        assert node.publish_topic is None

    def test_no_addressing_gates_registered(self) -> None:
        """The addressable / addressed-to-me gates are removed: a name-addressed
        agent registers no ``before_node`` seam."""
        node = _factory().build_node(_definition())
        assert _registered_before_node_seams(node) == []


class TestPeers:
    """``a2a``/``handoff`` frontmatter -> native ``peers`` (Messaging/Handoff)."""

    def test_default_both_a2a_and_handoff_discover(self) -> None:
        """Both fields default ``True`` -> a discovering Messaging + Handoff."""
        node = _factory().build_node(_definition())
        assert node._peers == (Messaging(discover=True), Handoff(discover=True))

    def test_a2a_false_omits_messaging(self) -> None:
        node = _factory().build_node(_definition(a2a=False))
        assert node._peers == (Handoff(discover=True),)

    def test_handoff_false_omits_handoff(self) -> None:
        node = _factory().build_node(_definition(handoff=False))
        assert node._peers == (Messaging(discover=True),)

    def test_both_false_yields_no_peers(self) -> None:
        """No A2A and no handoff -> ``peers=None`` reaches the Agent (empty tuple)."""
        node = _factory().build_node(_definition(a2a=False, handoff=False))
        assert node._peers == ()

    def test_empty_peer_lists_yield_no_peers_without_crashing(self) -> None:
        """`a2a: []` / `handoff: []` normalize to False at the definition layer, so
        the factory builds no peers rather than a bare Messaging()/Handoff() (which
        calfkit rejects — the boot-crash this guards against)."""
        node = _factory().build_node(_definition(a2a=[], handoff=[]))
        assert node._peers == ()

    def test_factory_guard_holds_when_validation_is_bypassed(self) -> None:
        """Defense-in-depth: a definition built bypassing the empty-tuple validator
        (``model_copy`` does not re-validate) keeps ``a2a``/``handoff`` as ``()`` —
        the factory's OWN truthiness guard (not just the definition normalizer)
        must still yield no peers rather than a bare, calfkit-rejected handle."""
        bypassed = _definition().model_copy(update={"a2a": (), "handoff": ()})
        assert bypassed.a2a == () and bypassed.handoff == ()  # bypass confirmed: still empty tuples
        node = _factory().build_node(bypassed)
        assert node._peers == ()

    def test_a2a_list_restricts_to_named_peers(self) -> None:
        node = _factory().build_node(_definition(a2a=("scribe", "researcher")))
        assert Messaging("scribe", "researcher") in node._peers
        # The named-peer Messaging does not discover.
        messaging = next(p for p in node._peers if isinstance(p, Messaging))
        assert messaging.names == ("scribe", "researcher")
        assert messaging.discover is False

    def test_handoff_list_restricts_to_named_targets(self) -> None:
        node = _factory().build_node(_definition(handoff=("scribe",)))
        handoff = next(p for p in node._peers if isinstance(p, Handoff))
        assert handoff.names == ("scribe",)
        assert handoff.discover is False


class TestProviderResolution:
    def test_default_provider_is_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider=None))
        assert calls[0][0] == "anthropic"

    def test_definition_provider_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_provider="anthropic", model_client_factory=model_factory)
        factory.build(_definition(provider="openai", model="gpt-5"))
        assert calls[0][0] == "openai"

    def test_env_provider_used_when_definition_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_provider="anthropic", model_client_factory=model_factory)
        factory.build(_definition(provider=None, model="gpt-5"))
        assert calls[0][0] == "openai"

    def test_ctor_default_used_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_provider="openai", model_client_factory=model_factory)
        factory.build(_definition(provider=None, model="gpt-5"))
        assert calls[0][0] == "openai"

    def test_unknown_env_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var can carry a typo; surface it at build time."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            _factory().build(_definition(provider=None))


class TestModelResolution:
    def test_definition_model_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Definition takes precedence over env var, ctor default, and provider default."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_model="claude-from-ctor", model_client_factory=model_factory)
        factory.build(_definition(model="claude-from-defn"))
        assert calls[0][1] == "claude-from-defn"

    def test_env_var_used_when_definition_model_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_model="claude-from-ctor", model_client_factory=model_factory)
        factory.build(_definition(model=None))
        assert calls[0][1] == "claude-from-env"

    def test_ctor_default_used_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_model="claude-from-ctor", model_client_factory=model_factory)
        factory.build(_definition(model=None))
        assert calls[0][1] == "claude-from-ctor"

    def test_provider_default_used_as_final_fallback_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any model hint, anthropic agents fall back to the project's
        default Claude model."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider="anthropic", model=None))
        assert calls[0] == ("anthropic", "claude-sonnet-4-5")

    def test_provider_default_used_as_final_fallback_openai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any model hint, openai agents fall back to the OpenAI default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider="openai", model=None))
        assert calls[0] == ("openai", "gpt-5-mini")

    def test_openai_codex_resolves_to_none_when_no_model_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """openai-codex has no static default: with no model hint, ``None`` is
        passed through so the Codex client resolves a live-catalog default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider="openai-codex", model=None))
        assert calls[0] == ("openai-codex", None)


class TestRequireModelGuard:
    """``_default_model_client_factory`` must reject ``None`` for providers that
    have no catalog-resolved default — only openai-codex tolerates it."""

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_none_model_raises_for_non_codex(self, provider: str) -> None:
        from calfcord.agents.factory import _default_model_client_factory

        with pytest.raises(ValueError, match="requires a model name"):
            _default_model_client_factory(provider, None)  # type: ignore[arg-type]


class TestThinkingEffortBaking:
    """Factory passes definition.thinking_effort through build_model_settings
    into the calfkit Agent constructor as a tier-2 default."""

    def test_anthropic_high_passes_thinking_dict(self) -> None:
        worker = _factory().build(_definition(provider="anthropic", thinking_effort="high"))
        agent_loop = worker._nodes[0]._agent_loop  # internal access acceptable in tests
        assert agent_loop.model_settings == {"anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}}

    def test_openai_medium_passes_reasoning_effort(self) -> None:
        worker = _factory().build(_definition(provider="openai", thinking_effort="medium"))
        agent_loop = worker._nodes[0]._agent_loop
        # Matches the operator → OpenAI mapping in
        # :mod:`calfcord.agents.thinking`: operator ``medium`` → OpenAI
        # ``"medium"`` after the ramp shift that accompanied the ``minimal``
        # tier addition.
        assert agent_loop.model_settings == {"openai_reasoning_effort": "medium"}

    def test_no_effort_in_definition_no_model_settings(self) -> None:
        """thinking_effort=None → no tier-2 model_settings."""
        worker = _factory().build(_definition(provider="anthropic"))
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings is None

    def test_effort_none_passes_empty_dict(self) -> None:
        """Explicit "none" → empty dict (calfkit merges as no-op)."""
        worker = _factory().build(_definition(provider="openai", thinking_effort="none"))
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings == {}


class TestResolveProviderModuleFunction:
    """``resolve_provider`` is lifted to module scope so the bridge can reuse it."""

    def test_definition_provider_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        assert resolve_provider(_definition(provider="openai"), default_provider="anthropic") == "openai"

    def test_env_var_used_when_definition_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        assert resolve_provider(_definition(provider=None), default_provider="anthropic") == "openai"

    def test_default_used_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        assert resolve_provider(_definition(provider=None), default_provider="openai") == "openai"

    def test_unknown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            resolve_provider(_definition(provider=None))


class TestToolsWiring:
    """``definition.tools`` and ``definition.mcp`` map to calfkit runtime
    *selectors*, resolved per turn against the capability view — never against a
    local registry at build time. Omitted ``tools:`` → ``Tools(discover=True)``
    (every live tool node); an explicit builtin list → one ``Tools(names=[...])``.
    The tri-state ``mcp:`` field → ``Toolboxes(discover=True)`` (default, every
    live MCP server), a named ``Toolboxes`` for a grant list, or nothing when
    ``false``. Because these are deferred selectors, the agent carries no eager
    bindings (``Agent.tools == []``); the surface rides on
    ``Agent._tool_selectors``, which is also what makes the Worker auto-register
    the capability view."""

    def test_omitted_tools_yields_discover_selector(self) -> None:
        """``tools:`` omitted (None) → a single ``Tools(discover=True)`` so the
        agent binds every live tool node at runtime, not a build-time snapshot."""
        agent = _factory().build_node(_definition(tools=None))
        assert agent.tools == []
        assert agent._tool_selectors == [Tools(discover=True)]

    def test_empty_tools_yields_no_selectors(self) -> None:
        """``tools: []`` is the deliberate no-tools opt-out: no selectors, and
        specifically NOT a discover handle (the empty case must not fall through
        to "discover everything")."""
        agent = _factory().build_node(_definition(tools=()))
        assert agent.tools == []
        assert agent._tool_selectors == []

    def test_named_builtins_become_one_tools_names_selector(self) -> None:
        """An explicit builtin list → one ``Tools(names=[...])`` in declared
        order. No local-registry lookup, so no name is rejected here (unknown
        names degrade at runtime per the capability view)."""
        agent = _factory().build_node(_definition(tools=("terminal", "read_file")))
        assert agent.tools == []
        assert agent._tool_selectors == [Tools(names=["terminal", "read_file"])]

    def test_named_builtins_dedupe_without_tripping_duplicate_rail(self) -> None:
        """A duplicate builtin name collapses (calfkit's Tools order-preserving-
        dedupes) rather than tripping the ``_add_tools`` duplicate-name rail."""
        agent = _factory().build_node(_definition(tools=("terminal", "read_file", "terminal")))
        assert agent._tool_selectors == [Tools(names=["terminal", "read_file"])]

    def test_restricted_builtins_and_named_mcp_yield_both_selectors_builtins_first(self) -> None:
        """A builtin list plus a named ``mcp:`` grant list yields a leading
        ``Tools(names=[...])`` for the builtins plus one named ``Toolboxes``."""
        agent = _factory().build_node(_definition(tools=("terminal",), mcp=("gmail",)))
        assert agent._tool_selectors == [Tools(names=["terminal"]), Toolboxes(MCPToolbox("gmail"))]

    def test_mcp_only_agent_has_no_tools_selector(self) -> None:
        """``tools: []`` plus a named ``mcp:`` list yields just the ``Toolboxes`` —
        no ``Tools`` selector is created (an empty ``Tools(names=[])`` would raise)."""
        agent = _factory().build_node(_definition(tools=(), mcp=("gmail/search",)))
        assert agent.tools == []
        assert agent._tool_selectors == [Toolboxes(MCPToolbox("gmail", include=("search",)))]

    def test_named_mcp_entries_collapse_per_server_sorted(self) -> None:
        """Multiple named ``mcp:`` entries collapse to one ``Toolbox`` entry per
        server inside a single ``Toolboxes``; explicit tool picks merge into a
        sorted ``include``; servers come back sorted so the surface is
        deterministic regardless of declaration order."""
        agent = _factory().build_node(
            _definition(tools=(), mcp=("gmail/send", "gmail/search", "docs")),
        )
        assert agent._tool_selectors == [
            Toolboxes(MCPToolbox("docs"), MCPToolbox("gmail", include=("search", "send"))),
        ]

    def test_mcp_true_yields_discover_toolboxes(self) -> None:
        """``mcp: true`` (the default) → a single discover-mode ``Toolboxes`` so the
        agent binds every live MCP server on the network at runtime."""
        agent = _factory().build_node(_definition(tools=(), mcp=True))
        assert agent._tool_selectors == [Toolboxes(discover=True)]

    def test_mcp_false_yields_no_toolbox_selector(self) -> None:
        """``mcp: false`` is the explicit opt-out — no ``Toolboxes`` is created."""
        agent = _factory().build_node(_definition(tools=(), mcp=False))
        assert agent._tool_selectors == []

    def test_default_agent_discovers_builtins_and_mcp(self) -> None:
        """The new default posture for a general agent — omitted ``tools:`` and
        ``mcp: true`` — discovers both live planes: builtins and MCP servers."""
        agent = _factory().build_node(_definition(tools=None, mcp=True))
        assert agent._tool_selectors == [Tools(discover=True), Toolboxes(discover=True)]

    def test_omitted_tools_with_named_mcp_discovers_builtins_plus_named_mcp(self) -> None:
        """Omitted builtins with a named MCP list: "all live builtins plus named
        MCP" without pinning the builtin set in frontmatter."""
        agent = _factory().build_node(_definition(tools=None, mcp=("github",)))
        assert agent._tool_selectors == [Tools(discover=True), Toolboxes(MCPToolbox("github"))]

    def test_build_log_describes_named_selector_surface(self, caplog: pytest.LogCaptureFixture) -> None:
        """The build log records the selector surface for operators: named builtins
        inline and ``mcp:<server>`` per toolbox entry. The MCP label rides on the
        public ``Toolbox.name`` field, so a silent upstream rename must fail here,
        not in production logs."""
        with caplog.at_level(logging.INFO, logger="calfcord.agents.factory"):
            _factory().build_node(_definition(tools=("terminal",), mcp=("gmail/send", "docs")))
        message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("building agent"))
        assert "mcp:docs" in message
        assert "mcp:gmail" in message
        assert "terminal" in message

    def test_build_log_marks_builtin_discover(self, caplog: pytest.LogCaptureFixture) -> None:
        """An omitted-tools agent logs the discover handle explicitly so an
        operator can see at a glance that the agent binds the live tool plane."""
        with caplog.at_level(logging.INFO, logger="calfcord.agents.factory"):
            _factory().build_node(_definition(tools=None, mcp=False))
        message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("building agent"))
        assert "discover:*" in message

    def test_build_log_marks_mcp_discover(self, caplog: pytest.LogCaptureFixture) -> None:
        """A discover-mode ``mcp: true`` agent logs the MCP discover handle so an
        operator sees at a glance that the agent binds the live MCP plane."""
        with caplog.at_level(logging.INFO, logger="calfcord.agents.factory"):
            _factory().build_node(_definition(tools=(), mcp=True))
        message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("building agent"))
        assert "mcp:discover:*" in message


class TestPublishTopicValidation:
    """A stray ``publish_topic`` on any agent is rejected at validation.

    This exercises :class:`AgentDefinition` validation directly — no factory
    build, no name-addressing. ``publish_topic`` was a reserved field for the
    built-in router (both the field AND its dedicated ``_forbid_publish_topic``
    validator were removed in the 0.12 migration); with no field declared,
    ``model_config extra="forbid"`` now rejects a stale ``publish_topic:`` as an
    unknown field (the ``ValidationError`` still names it), so the
    misconfiguration stays visible without a bespoke validator.
    """

    def test_default_no_publish_topic_builds(self) -> None:
        """A normal agent (no ``publish_topic``) validates and may carry tools."""
        AgentDefinition(
            agent_id="scribe",
            description="x",
            tools=("calendar",),
            system_prompt="x",
        )

    def test_publish_topic_raises(self) -> None:
        """A stale ``publish_topic`` is rejected as an unknown field (extra="forbid")
        so the migration's removal of the field fails loudly, not silently."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="publish_topic"):
            AgentDefinition(
                agent_id="scribe",
                description="x",
                publish_topic="some.topic",
                system_prompt="x",
            )


class TestMemoryFlag:
    """``memory: true`` requires the filesystem tools the memory block tells the
    agent to use; the factory's guard enforces this at build time. These tests
    use the real TOOL_REGISTRY (no override) so read_file/write_file resolve."""

    def test_memory_agent_with_only_mcp_fs_lookalikes_rejected(self) -> None:
        """MCP selectors cannot satisfy the memory guard: their tools resolve
        at runtime, so the factory cannot prove read_file/write_file exist.
        memory: true therefore requires the BUILTIN fs tools explicitly."""
        with pytest.raises(ValueError, match="memory needs read_file and"):
            _factory().build(_memory_definition(tools=(), mcp=("files",)))

    def test_memory_agent_with_explicit_fs_tools_builds(self) -> None:
        worker = _factory().build(_memory_definition(tools=("read_file", "write_file")))
        assert worker._nodes[0].node_id == "scribe"

    def test_memory_agent_with_all_tools_builds(self) -> None:
        """``tools`` omitted (None) grants every builtin — includes the fs
        tools, so the guard passes."""
        worker = _factory().build(_memory_definition(tools=None))
        assert worker._nodes[0].node_id == "scribe"

    def test_memory_true_without_tools_raises(self) -> None:
        with pytest.raises(ValueError, match="memory: true"):
            _factory().build(_memory_definition(tools=()))

    def test_memory_true_missing_write_file_raises(self) -> None:
        with pytest.raises(ValueError, match="write_file"):
            _factory().build(_memory_definition(tools=("read_file",)))

    def test_non_memory_agent_unaffected_by_guard(self) -> None:
        """A ``memory=False`` agent with no tools builds fine (guard skipped)."""
        worker = _factory().build(_definition(tools=()))
        assert worker._nodes[0].system_prompt == "You are a test agent."

    def test_memory_agent_registers_the_instructions_hook(self) -> None:
        """The factory wires the runtime hook onto memory agents — not just the
        guard. Registered dynamic-instructions functions land in pydantic-ai's
        ``_agent_loop._instructions`` (alongside the literal system prompt). Without
        this, the template would reach ``deps`` but never be injected — a silent
        no-op the guard alone can't catch."""
        node = _factory().build_node(
            _memory_definition(agent_id="scribe", tools=("read_file", "write_file")),
        )
        hooks = [i for i in node._agent_loop._instructions if callable(i)]
        assert len(hooks) == 1, "memory agent should register exactly one instructions hook"
        # The registered hook localizes the bridge-shipped template for THIS agent.
        ctx = SimpleNamespace(deps={MEMORY_PROMPT_DEPS_KEY: "block {{MEMORY_DIR}}"})
        assert hooks[0](ctx) == "block memory/scribe/"

    def test_non_memory_agent_registers_no_instructions_hook(self) -> None:
        """A memory=False agent must NOT carry the hook — only the literal
        system prompt is in ``_instructions``."""
        node = _factory().build_node(_definition(tools=("read_file",)))
        assert [i for i in node._agent_loop._instructions if callable(i)] == []


class TestToolErrorPolicy:
    """Every deployed agent surfaces tool failures to its model.

    The factory wires calfkit's zero-arg ``surface_to_model()`` prebuilt on the
    ``on_tool_error`` seam, so a failing tool becomes a model-visible error the
    agent can see and react to. Without it, calfkit's default is to escalate: the
    run faults and the model sees nothing (calfkit 0.12.7).
    """

    @staticmethod
    def _surfaces_tool_errors(node: Agent) -> bool:
        return any(
            getattr(handler, "__qualname__", "").startswith("surface_to_model")
            for handler in _registered_on_callee_error_seams(node)
        )

    def test_agent_surfaces_tool_errors_to_model(self) -> None:
        node = _factory().build_node(_definition())
        assert self._surfaces_tool_errors(node), (
            "every agent must wire surface_to_model() on the tool-error seam"
        )

    @pytest.mark.parametrize(
        "definition",
        [
            pytest.param(_definition(tools=None), id="discover-all-tools"),
            pytest.param(_definition(tools=()), id="no-tools"),
            pytest.param(_definition(tools=("terminal",), mcp=("gmail",)), id="builtins-and-mcp"),
            pytest.param(_definition(a2a=False, handoff=False), id="no-peers"),
            pytest.param(_memory_definition(tools=("read_file", "write_file")), id="memory-agent"),
        ],
    )
    def test_policy_applies_regardless_of_agent_config(self, definition: AgentDefinition) -> None:
        """The surface is unconditional — every agent gets it whatever its tools,
        MCP grants, peers, or memory flag."""
        node = _factory().build_node(definition)
        assert self._surfaces_tool_errors(node)
