"""Tests for AgentDefinitionRef (the agent's mutable single-slot definition holder)."""

from __future__ import annotations

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.control_plane.definition_ref import AgentDefinitionRef


def _make_definition(agent_id: str = "scribe", **overrides: object) -> AgentDefinition:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "slash": f"/{agent_id}",
        "display_name": agent_id.capitalize(),
        "description": "Test.",
        "provider": "anthropic",
        "thinking_effort": "low",
        "system_prompt": "You are.",
    }
    base.update(overrides)
    return AgentDefinition(**base)  # type: ignore[arg-type]


def test_current_returns_initial_definition() -> None:
    defn = _make_definition()
    ref = AgentDefinitionRef(current=defn)
    assert ref.current is defn


def test_swap_with_same_agent_id_updates_current() -> None:
    defn = _make_definition(thinking_effort="low")
    ref = AgentDefinitionRef(current=defn)

    new_defn = _make_definition(thinking_effort="high")
    ref.swap(new_defn)

    assert ref.current is new_defn
    assert ref.current.thinking_effort == "high"


def test_swap_with_different_agent_id_raises() -> None:
    defn = _make_definition(agent_id="scribe")
    ref = AgentDefinitionRef(current=defn)

    other = _make_definition(agent_id="scheduler")
    with pytest.raises(ValueError, match="agent_id mismatch"):
        ref.swap(other)

    # Current is unchanged after the rejected swap.
    assert ref.current is defn
