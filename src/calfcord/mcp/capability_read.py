"""Best-effort CLI read of the live MCP capability view.

Reads calfkit's public ``client.mesh.get_tools()`` — the caller-side view over
the org-wide capability plane — for which MCP toolboxes exist *right now*,
including servers hosted by other machines that this host's ``mcp.json`` knows
nothing about. The tools editor projects each toolbox to per-tool
``mcp/<server>/<tool>`` rows.

Strictly best-effort: the CLI must work offline (broker down, workspace
closed, dev laptop on a plane), so every failure path degrades to ``None`` —
the editor then falls back to server-level rows from the local ``mcp.json``.
The short catch-up timeout keeps the editor snappy; a partial catch-up just
means fewer rows, never an error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Bounded so the interactive editor stays snappy on a slow/filtered broker;
# a healthy local broker replays the tiny capability view well inside this.
_DEFAULT_TIMEOUT_SECONDS = 1.5


def snapshot_capability_tools(
    server_urls: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> dict[str, list[str]] | None:
    """``{server: [tool, ...]}`` from the live capability view, or ``None``.

    Opens the mesh view with a ``timeout``-bounded catch-up and returns each
    advertised MCP toolbox's tool names (sorted). Any failure — unreachable
    broker, missing directory topic, catch-up timeout — returns ``None`` (NOT
    ``{}``): callers can tell "the view answered and is empty" apart from "the
    view was unreachable" and tell the operator which one happened.
    """
    try:
        return asyncio.run(_snapshot(server_urls, timeout))
    except Exception as exc:
        logger.debug("capability view unavailable (%s); offline rows only", exc)
        return None


def _toolbox_rows(tools: Mapping[str, object]) -> dict[str, list[str]]:
    """Project ``client.mesh.get_tools()`` to ``{toolbox: [tool, ...]}`` rows.

    Keeps only :class:`~calfkit.client.ToolboxInfo` (MCP toolboxes) — a
    ``ToolNodeInfo`` is a function-tool node, which the tools editor offers
    from the local builtin registry, not as an ``mcp/`` row. Bare tool names,
    sorted for a stable checkbox order.
    """
    from calfkit.client import ToolboxInfo

    return {
        name: sorted(spec.name for spec in info.tools) for name, info in tools.items() if isinstance(info, ToolboxInfo)
    }


async def _snapshot(server_urls: str, timeout: float) -> dict[str, list[str]]:
    from calfkit.client import Client, MeshViewConfig

    # calfkit's public caller-side view over the capability plane: it opens a
    # naive (never-creating) reader on the canonical topic, so the CLI peek can
    # never provision anything, and the mesh owns the topic name. ``aclose``
    # tears the view down on every path — a best-effort peek must not leak a
    # consumer.
    client = Client.connect(server_urls, mesh_config=MeshViewConfig(catchup_timeout=timeout))
    try:
        tools = await client.mesh.get_tools()
    finally:
        await client.aclose()
    return _toolbox_rows(tools)
