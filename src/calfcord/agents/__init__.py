"""Agent definitions, factory, and process runner.

Public surface:
    AgentDefinition  — parsed agent identity + runtime hints + system prompt
    parse_agent_md   — parse one ``.md`` file into an AgentDefinition
    load_agents_dir  — parse all ``.md`` files in a directory
    ThinkingEffort   — Literal of operator-facing effort tiers
    AgentFactory     — constructs a calfkit Worker from a definition
    resolve_provider — shared provider-resolution fallback chain

``AgentFactory`` / ``resolve_provider`` are resolved LAZILY (PEP 562) because
:mod:`calfcord.agents.factory` is this package's only heavy member: it reaches
``calfkit`` -> ``calfkit.nodes.agent`` -> ``calfkit.providers.pydantic_ai``, ~1.2s
of imports. Every other name here is light, so re-exporting factory eagerly meant
that importing *any* of them — down to :mod:`~calfcord.agents.identifier`, which
holds one regex — loaded the entire agent framework. That cost landed on every
``disco`` command and, worse, on the ``disco _healthcheck`` readiness probe
Process Compose re-runs every few seconds for the life of the workspace
(ADR-0023). Deferring keeps the surface above intact while charging the import
only to callers that actually construct agents. Pinned by
``tests/agents/test_lazy_barrel.py``.
"""

from typing import TYPE_CHECKING, Any

from calfcord.agents.definition import (
    AgentDefinition,
    ThinkingEffort,
    parse_agent_md,
)
from calfcord.agents.loader import load_agents_dir

if TYPE_CHECKING:  # import-free for type checkers; never executed at runtime
    from calfcord.agents.factory import AgentFactory, resolve_provider

__all__ = [
    "AgentDefinition",
    "AgentFactory",
    "ThinkingEffort",
    "load_agents_dir",
    "parse_agent_md",
    "resolve_provider",
]

_LAZY = frozenset({"AgentFactory", "resolve_provider"})


def __getattr__(name: str) -> Any:
    """Resolve the deferred factory names on first access (PEP 562).

    Anything else raises the same :class:`AttributeError` a normal module would,
    so a typo stays a typo instead of surfacing as an opaque ImportError from a
    module the caller never named.
    """
    if name in _LAZY:
        from calfcord.agents import factory

        return getattr(factory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
