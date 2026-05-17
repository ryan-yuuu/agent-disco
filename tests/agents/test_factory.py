"""Unit tests for AgentFactory.

The factory body is a stub; these tests pin the contract (construction
succeeds, build() raises NotImplementedError with the agent name in the
message) so the runner integration won't silently regress when the real
implementation lands.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.agents.state import AgentRuntimeState


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="scheduler",
        slash="/scheduler",
        display_name="Aksel (Scheduler)",
        description="Calendar.",
        system_prompt="You are Aksel.",
    )


def test_factory_constructs() -> None:
    factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
    assert factory is not None


def test_build_raises_not_implemented_with_agent_name() -> None:
    factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
    definition = _definition()
    state = AgentRuntimeState(channels=[1])
    # The stub raises before touching the store, so a bare mock is enough.
    store = MagicMock()

    with pytest.raises(NotImplementedError, match="scheduler"):
        factory.build(definition, state, store)
