"""Single source of truth for agent_id format constraints.

The pattern ``[a-z0-9_-]{1,32}`` appears in several places across the
codebase (notably :class:`AgentDefinition.agent_id` and the bridge
normalizer's mention scanner). This module is the canonical
definition; importers should reach for :data:`AGENT_ID_PATTERN` rather
than re-declaring the regex.

This module is intentionally a leaf — only stdlib imports — so any
package in the codebase can import it without risking cycles.
"""

from __future__ import annotations

import re

AGENT_ID_CHARSET = "a-z0-9_-"
"""Raw character class (without brackets) for agent_id characters.
Use for building related regexes (e.g. the bridge's mention
scanner) where the surrounding pattern shape differs but the
character set must match."""

MENTION_PREFIX = "!"
"""The single character that prefixes an agent mention in a Discord message
(e.g. ``!scribe``). The bridge normalizer builds its mention scanner from this
constant, so the trigger char has one home and cannot drift between call sites.

Deliberately NOT a member of :data:`AGENT_ID_CHARSET`: the prefix must never be
a legal agent-id character, or the scanner could not tell where the prefix ends
and the id begins."""

_AGENT_ID_REGEX_STR = rf"[{AGENT_ID_CHARSET}]{{1,32}}"

AGENT_ID_PATTERN = re.compile(_AGENT_ID_REGEX_STR)
"""Compiled regex matching the canonical agent_id format. Use
``.fullmatch(value)`` for membership checks; the character class
also appears verbatim inside the bridge normalizer's mention scanner."""

RESERVED_AGENT_IDS = frozenset({"broker", "bridge", "tools", "process-compose", "unstick", "new"})
"""Workspace slot names an agent id may never take.

Agents share one process/slot namespace with the substrate (the ``broker`` and
``bridge`` Process Compose processes) and the ``tools`` singleton (a detached
roster slot under ``state/run/<slot>.pid``); an agent named after any of them
would collide with that process. ``process-compose`` is reserved for a LOG
collision instead of a slot one: every roster slot logs to
``state/logs/<slot>.log``, and the supervisor's own ``-L`` log is
``state/logs/process-compose.log`` — an agent by that name would interleave with
it, and rotate-at-spawn would rename the live supervisor's log out from under
it. Canonically homed here (the leaf id module) so the parse-time validator
(:class:`~calfcord.agents.definition.AgentDefinition`) and the supervisor's slot
set (:mod:`calfcord.supervisor.compose`) import ONE definition and cannot
drift."""

MCP_SLOT_PREFIX = "mcp-"
"""The slot-name prefix reserved for MCP servers (``mcp-<server>``).

Shares this module with :data:`RESERVED_AGENT_IDS` for the same reason: agent
ids and MCP slots live in the same pidfile namespace, so the prefix is both the
supervisor's slot convention (re-exported by :mod:`calfcord.supervisor.compose`)
and a reserved prefix for agent names."""


def reserved_agent_id_error(agent_id: str) -> str | None:
    """Why ``agent_id`` is a reserved workspace-slot name, or ``None`` if it's free.

    The one create-/parse-time chokepoint message: both ``disco agent create``'s
    name prompt and :class:`AgentDefinition`'s validator surface exactly this
    text, so the operator hears the same reason wherever the collision is caught.
    (The roster verbs keep their own runtime refusal as defense-in-depth.)
    """
    if agent_id == "process-compose":
        # The collision here is the LOG file, not a process slot: the supervisor's
        # own log is state/logs/process-compose.log, exactly where an agent by
        # this name would write (and rotate at spawn).
        return (
            "'process-compose' is reserved for the workspace supervisor — an "
            "agent by this name would share (and rotate) the supervisor's log "
            "file; pick another agent name"
        )
    if agent_id == "unstick":
        return (
            "'unstick' is reserved for the Discord !unstick routing command — "
            "pick another agent name"
        )
    if agent_id == "new":
        return (
            "'new' is reserved for the Discord !new routing command — "
            "pick another agent name"
        )
    if agent_id in RESERVED_AGENT_IDS:
        role = "substrate" if agent_id in ("broker", "bridge") else "tools host"
        return (
            f"{agent_id!r} is reserved for the workspace's {role} process — "
            "agents share its process namespace; pick another agent name"
        )
    if agent_id.startswith(MCP_SLOT_PREFIX):
        return (
            f"{agent_id!r} starts with 'mcp-', which is reserved for MCP server "
            "slots (mcp-<server>) in the workspace's process namespace; "
            "pick another agent name"
        )
    return None
