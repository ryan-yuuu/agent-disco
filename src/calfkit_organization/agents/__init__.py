"""Agent definitions, runtime state, factory, and process runner.

Public surface:
    AgentDefinition    — parsed agent identity + runtime hints + system prompt
    parse_agent_md     — parse one ``.md`` file into an AgentDefinition
    load_agents_dir    — parse all ``.md`` files in a directory
    AgentRuntimeState  — persisted per-agent runtime state (channels, etc.)
    AgentStateStore    — atomic read/write for one agent's state file
    AgentFactory       — constructs a calfkit Worker from a definition (stubbed)
    bootstrap_env_var  — derives the bootstrap env var name from an agent_id
"""

from calfkit_organization.agents.definition import AgentDefinition, parse_agent_md
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.agents.loader import load_agents_dir
from calfkit_organization.agents.runner import bootstrap_env_var
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore

__all__ = [
    "AgentDefinition",
    "AgentFactory",
    "AgentRuntimeState",
    "AgentStateStore",
    "bootstrap_env_var",
    "load_agents_dir",
    "parse_agent_md",
]
