"""Agent-path MCP toolbox selection — schema-free, ``mcp.json``-free.

calfkit resolves an agent's MCP tools per turn by calling each declared
``ToolSelector`` against the capability view (the control-plane projection of
the ``calf.capabilities`` topic the ``Worker`` auto-registers whenever a hosted
agent declares selectors). This module maps calfcord's tri-state ``mcp:``
frontmatter field onto calfkit's public
:class:`~calfkit.nodes.toolbox.Toolboxes` selector (0.13):

* ``mcp: true`` (the default) → ``Toolboxes(discover=True)`` — every live MCP
  server on the network, resolved fresh each turn.
* ``mcp: false`` → no selector at all.
* ``mcp: [github, gmail/search]`` → one ``Toolboxes`` naming per-server
  :class:`~calfkit.mcp.MCPToolbox` entries. Each is an identity-only handle
  constructible with just the server name — no connection params, no secrets —
  so on a distributed deploy the agent host needs neither ``mcp.json`` nor the
  secrets inside it.

Policy: named entries and discover mode are both **non-strict** (the upstream
default, never overridden here). An agent whose MCP server is down (or not yet
started) boots and answers normally; the affected tools drop out of that turn
with calfkit logging the degradation. This matches the roster's "nothing runs
that you didn't start" property — declaring ``mcp: [github]`` must not hold the
agent hostage to the github server's uptime — and discover mode goes further:
it binds only what is actually live, so a never-started server is simply absent.
"""

from __future__ import annotations

from collections.abc import Iterable

from calfkit import Toolboxes
from calfkit.mcp import MCPToolbox

from calfcord.mcp.selector import parse_mcp_selector


def toolbox_selector(mcp: bool | tuple[str, ...]) -> Toolboxes | None:
    """Map an agent's normalized tri-state ``mcp:`` field to a ``Toolboxes`` selector.

    - ``True`` → ``Toolboxes(discover=True)`` — bind every live MCP server.
    - ``False`` / ``()`` → ``None`` — no MCP toolbox surface.
    - a non-empty tuple → a named ``Toolboxes`` (see :func:`_named_toolboxes`).

    The ``()`` case is defensive: :class:`~calfcord.agents.definition.AgentDefinition`
    normalizes an empty grant list to ``False``, but a ``model_construct`` that
    skips validation could still present ``()`` — treating it as off keeps this a
    total function that never builds an entryless ``Toolboxes`` (which calfkit
    would reject).

    Args:
        mcp: The agent's normalized ``mcp`` field value.

    Raises:
        ValueError: For a malformed named entry (message names the entry
            verbatim, via :func:`parse_mcp_selector`).
    """
    if mcp is True:
        return Toolboxes(discover=True)
    if not mcp:  # False or ()
        return None
    return _named_toolboxes(mcp)


def _named_toolboxes(entries: Iterable[str]) -> Toolboxes:
    """Collapse an agent's canonical ``mcp:`` entries into one named ``Toolboxes``.

    Merge semantics match the old schema-build resolution: a bare ``<server>``
    subsumes that server's explicit ``<server>/<tool>`` entries; explicit-only
    selections dedupe into a sorted ``include`` tuple; servers come back sorted so
    the agent's tool surface is deterministic regardless of frontmatter order.

    Args:
        entries: Canonical ``mcp:`` grant strings — a non-empty iterable.

    Raises:
        ValueError: For a non-MCP or malformed entry (message names the entry
            verbatim, via :func:`parse_mcp_selector`).
    """
    wildcard: set[str] = set()
    explicit: dict[str, set[str]] = {}
    for entry in entries:
        server, tool = parse_mcp_selector(entry)
        if tool is None:
            wildcard.add(server)
        else:
            explicit.setdefault(server, set()).add(tool)
    boxes = [
        # A bare <server> wildcard subsumes that server's explicit picks.
        MCPToolbox(
            server,
            include=None if server in wildcard else tuple(sorted(explicit[server])),
        )
        for server in sorted(wildcard | set(explicit))
    ]
    return Toolboxes(entries=boxes)
