"""Static agent roster loaded from a TOML config file.

The registry is the single source of truth for which agents exist, what slash
command each one owns, and what display name to attribute a persona webhook
message to. It is loaded once at daemon startup; mutations require a restart.

Duplicate ``agent_id``, ``slash``, or ``display_name`` are rejected at load
time — these fields must be unique across the roster.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator

_NAME_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")


class AgentSpec(BaseModel):
    """One agent's identity. Validated to match Discord's slash-command constraints."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    slash: str
    display_name: str
    avatar_url: str | None = None
    description: str

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        if not _NAME_PATTERN.fullmatch(v):
            raise ValueError(f"agent_id must match [a-z0-9_-]{{1,32}}, got {v!r}")
        return v

    @field_validator("slash")
    @classmethod
    def _validate_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"slash must start with '/', got {v!r}")
        if not _NAME_PATTERN.fullmatch(v[1:]):
            raise ValueError(f"slash name (after '/') must match [a-z0-9_-]{{1,32}}, got {v!r}")
        return v

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, v: str) -> str:
        if not (1 <= len(v) <= 80):
            raise ValueError(f"display_name must be 1-80 chars, got {len(v)}")
        if v.lower() == "clyde":
            raise ValueError("display_name 'Clyde' is rejected by Discord webhooks")
        return v

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str) -> str:
        if not (1 <= len(v) <= 100):
            raise ValueError(f"description must be 1-100 chars (Discord slash limit), got {len(v)}")
        return v


class AgentRegistry:
    """In-memory index of :class:`AgentSpec`s with O(1) lookups by id, slash, and display name."""

    def __init__(self, specs: Sequence[AgentSpec]) -> None:
        self._by_id: dict[str, AgentSpec] = {}
        self._by_slash: dict[str, AgentSpec] = {}
        self._by_display_name: dict[str, AgentSpec] = {}
        self._all: list[AgentSpec] = list(specs)

        for spec in self._all:
            if spec.agent_id in self._by_id:
                raise ValueError(f"duplicate agent_id: {spec.agent_id!r}")
            if spec.slash in self._by_slash:
                raise ValueError(f"duplicate slash: {spec.slash!r}")
            if spec.display_name in self._by_display_name:
                raise ValueError(f"duplicate display_name: {spec.display_name!r}")
            self._by_id[spec.agent_id] = spec
            self._by_slash[spec.slash] = spec
            self._by_display_name[spec.display_name] = spec

    @classmethod
    def from_toml(cls, path: Path) -> Self:
        """Load an :class:`AgentRegistry` from a TOML file.

        Expected schema::

            [[agents]]
            agent_id = "scheduler"
            slash = "/scheduler"
            display_name = "Aksel (Scheduler)"
            description = "Calendar mechanics; book and prep meetings"

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError: If the TOML is malformed or any agent entry fails
                validation (including duplicate detection).
        """
        with path.open("rb") as f:
            data = tomllib.load(f)
        raw_agents = data.get("agents", [])
        if not isinstance(raw_agents, list):
            raise ValueError(f"expected [[agents]] array of tables in {path}, got {type(raw_agents).__name__}")
        specs = [AgentSpec(**entry) for entry in raw_agents]
        return cls(specs)

    def by_id(self, agent_id: str) -> AgentSpec | None:
        return self._by_id.get(agent_id)

    def by_slash(self, slash: str) -> AgentSpec | None:
        return self._by_slash.get(slash)

    def by_display_name(self, name: str) -> AgentSpec | None:
        return self._by_display_name.get(name)

    def all(self) -> Sequence[AgentSpec]:
        return tuple(self._all)
