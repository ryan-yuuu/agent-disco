"""Static agent roster, indexed for the bridge's lookups.

The registry is the single source of truth for which agents exist, what
slash command each one owns, and what display name to attribute a
persona-webhook message to. It is loaded once at daemon startup; mutations
require a restart.

The agent definitions themselves live in :mod:`calfkit_organization.agents`
(parsed from ``agents/*.md`` files). This module owns only the *index* the
bridge uses: O(1) lookups by id, slash, and display name, plus rejection
of duplicate ``slash`` or ``display_name`` across agents.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.loader import load_agents_dir


class AgentRegistry:
    """In-memory index of :class:`AgentDefinition`s with O(1) lookups."""

    def __init__(self, definitions: Sequence[AgentDefinition]) -> None:
        self._by_id: dict[str, AgentDefinition] = {}
        self._by_slash: dict[str, AgentDefinition] = {}
        self._by_display_name: dict[str, AgentDefinition] = {}
        self._all: list[AgentDefinition] = list(definitions)

        for d in self._all:
            if d.agent_id in self._by_id:
                raise ValueError(f"duplicate agent_id: {d.agent_id!r}")
            if d.slash in self._by_slash:
                raise ValueError(f"duplicate slash: {d.slash!r}")
            if d.display_name in self._by_display_name:
                raise ValueError(f"duplicate display_name: {d.display_name!r}")
            self._by_id[d.agent_id] = d
            self._by_slash[d.slash] = d
            self._by_display_name[d.display_name] = d

    @classmethod
    def from_agents_dir(cls, path: Path) -> Self:
        """Load an :class:`AgentRegistry` from a directory of agent ``.md`` files.

        Delegates parsing to
        :func:`calfkit_organization.agents.loader.load_agents_dir` and adds
        the cross-agent duplicate-detection of :meth:`__init__`.
        """
        return cls(load_agents_dir(path))

    def by_id(self, agent_id: str) -> AgentDefinition | None:
        return self._by_id.get(agent_id)

    def by_slash(self, slash: str) -> AgentDefinition | None:
        return self._by_slash.get(slash)

    def by_display_name(self, name: str) -> AgentDefinition | None:
        return self._by_display_name.get(name)

    def all(self) -> Sequence[AgentDefinition]:
        return tuple(self._all)
