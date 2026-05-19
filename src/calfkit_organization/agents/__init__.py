"""Agent definitions, runtime state, factory, and process runner.

Public surface:
    AgentDefinition           — parsed agent identity + runtime hints + system prompt
    parse_agent_md            — parse one ``.md`` file into an AgentDefinition
    load_agents_dir           — parse all ``.md`` files in a directory
    AgentRuntimeState         — persisted per-agent runtime state
    AgentStateStore           — atomic read/write for one agent's state file
    ThinkingEffort            — Literal of operator-facing effort tiers
    AgentFactory              — constructs a calfkit Worker from a definition
    resolve_provider          — shared provider-resolution fallback chain
    bootstrap_env_var         — derives the bootstrap env var name from an agent_id
    make_addressable_gate     — gate factory: reject self/unknown-bot emissions
    make_addressed_to_me_gate — gate factory: require slash target match
"""

from calfkit_organization.agents.definition import (
    AgentDefinition,
    ThinkingEffort,
    parse_agent_md,
)
from calfkit_organization.agents.factory import AgentFactory, resolve_provider
from calfkit_organization.agents.gates import make_addressable_gate, make_addressed_to_me_gate
from calfkit_organization.agents.loader import load_agents_dir
from calfkit_organization.agents.runner import bootstrap_env_var
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore

__all__ = [
    "AgentDefinition",
    "AgentFactory",
    "AgentRuntimeState",
    "AgentStateStore",
    "ThinkingEffort",
    "bootstrap_env_var",
    "load_agents_dir",
    "make_addressable_gate",
    "make_addressed_to_me_gate",
    "parse_agent_md",
    "resolve_provider",
]
