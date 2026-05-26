"""Mutable single-slot holder for an agent's current AgentDefinition.

AgentDefinition is frozen, so the agent process can't mutate fields in place
when the control sink applies a command. Instead, the sink builds a new
definition (via md_writer or model_copy) and swaps it into this ref. Code that
needs the "current" definition reads ``ref.current``.

Kept deliberately minimal (no events, no thread-safety primitives): the agent
process is single-threaded over its asyncio event loop, so a plain reassignment
is race-free for our access patterns.
"""
from __future__ import annotations

from dataclasses import dataclass

from calfkit_organization.agents.definition import AgentDefinition


@dataclass
class AgentDefinitionRef:
    """Single-slot mutable container for an agent's current definition."""

    current: AgentDefinition

    def swap(self, new_def: AgentDefinition) -> None:
        """Replace the current definition. New value must have the same agent_id."""
        if new_def.agent_id != self.current.agent_id:
            raise ValueError(
                f"AgentDefinitionRef.swap: agent_id mismatch "
                f"(current={self.current.agent_id!r}, new={new_def.agent_id!r})"
            )
        self.current = new_def
