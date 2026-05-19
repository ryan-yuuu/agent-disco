"""Static agent roster, indexed for the bridge's lookups.

The registry is the single source of truth for which agents exist, what
slash command each one owns, and what display name to attribute a
persona-webhook message to. It is loaded once at daemon startup; new
agents require a restart.

The agent definitions themselves live in :mod:`calfkit_organization.agents`
(parsed from ``agents/*.md`` files). This module owns the *index* the
bridge uses (O(1) lookups by id, slash, and display name, plus rejection
of duplicate ``slash`` or ``display_name`` across agents) and the
in-process mutator for the one frontmatter field that operators can edit
at runtime: ``thinking_effort``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Self

from calfkit_organization.agents.definition import AgentDefinition, ThinkingEffort
from calfkit_organization.agents.loader import load_agents_dir
from calfkit_organization.agents.md_writer import update_thinking_effort


class AgentRegistry:
    """In-memory index of :class:`AgentDefinition`s with O(1) lookups."""

    def __init__(self, definitions: Sequence[AgentDefinition]) -> None:
        self._by_id: dict[str, AgentDefinition] = {}
        self._by_slash: dict[str, AgentDefinition] = {}
        self._by_display_name: dict[str, AgentDefinition] = {}
        self._all: list[AgentDefinition] = list(definitions)
        # Serializes concurrent set_thinking_effort calls. Today's writer
        # (md_writer.update_thinking_effort) is fully synchronous, so on
        # a single-threaded asyncio event loop two concurrent callers
        # would already run end-to-end serially without this lock — it's
        # forward-compat for a future async writer (aiofiles, threaded
        # fsync). Keep it cheap; reintroduce a real interleaving test if
        # the writer ever gains an ``await``.
        self._write_lock = asyncio.Lock()

        for d in self._all:
            self._index(d)

    def _index(self, definition: AgentDefinition) -> None:
        """Insert ``definition`` into all three indexes, rejecting duplicates."""
        if definition.agent_id in self._by_id:
            raise ValueError(f"duplicate agent_id: {definition.agent_id!r}")
        if definition.slash in self._by_slash:
            raise ValueError(f"duplicate slash: {definition.slash!r}")
        if definition.display_name in self._by_display_name:
            raise ValueError(f"duplicate display_name: {definition.display_name!r}")
        self._by_id[definition.agent_id] = definition
        self._by_slash[definition.slash] = definition
        self._by_display_name[definition.display_name] = definition

    def _replace(self, old: AgentDefinition, new: AgentDefinition) -> None:
        """Swap an existing entry. Keys must match; only mutable fields change.

        The asserts guard an internal invariant rather than user input —
        today's only caller is ``set_thinking_effort`` and the writer
        beneath it can't change ``agent_id``, ``slash``, or
        ``display_name``. If you add another caller, audit whether its
        write path can mutate any of those keys before reusing
        ``_replace`` — under ``python -O`` these asserts vanish.
        """
        assert old.agent_id == new.agent_id, "agent_id is immutable"
        assert old.slash == new.slash, "slash is immutable"
        assert old.display_name == new.display_name, "display_name is immutable"
        self._by_id[new.agent_id] = new
        self._by_slash[new.slash] = new
        self._by_display_name[new.display_name] = new
        idx = self._all.index(old)
        self._all[idx] = new

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

    async def set_thinking_effort(
        self, agent_id: str, value: ThinkingEffort
    ) -> AgentDefinition:
        """Rewrite ``thinking_effort`` in the agent's ``.md`` and swap the in-memory copy.

        The returned :class:`AgentDefinition` is the freshly-parsed
        post-write entry. Returned (rather than ``None``) so a caller
        holding the old reference can swap atomically without a second
        ``by_id`` lookup. The same instance is now in all three indexes.

        Raises:
            KeyError: ``agent_id`` is not in the registry.
            ValueError: the registered definition has no ``source_path``
                (in-memory construction without a real file), or the
                existing ``.md`` fails validation, or the rewrite would
                produce an invalid definition.
            FileNotFoundError: the ``.md`` file is missing on disk.
            OSError: a filesystem error during the tmp write or atomic
                rename. Post-rename parent-dir fsync failures are
                swallowed and logged at warning level — see
                :mod:`calfkit_organization.agents.md_writer`.
        """
        async with self._write_lock:
            existing = self._by_id.get(agent_id)
            if existing is None:
                raise KeyError(agent_id)
            if existing.source_path is None:
                raise ValueError(
                    f"agent {agent_id!r} has no source_path; cannot rewrite frontmatter"
                )
            new_definition = update_thinking_effort(existing.source_path, value)
            self._replace(existing, new_definition)
            return new_definition
