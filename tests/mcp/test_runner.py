"""Tests for the ``calfkit-mcp`` bridge runner's node-resolution guard.

:func:`calfcord.mcp.runner._resolve_mcp_nodes` is the empty-registry guard
extracted from ``_amain`` so it can be exercised without standing up Kafka. An
empty registry must fail fast — the worker would otherwise boot inert,
subscribing to no topics while appearing healthy.

The former key/``name=`` mismatch guard is gone: :func:`calfcord.mcp.config.
load_mcp_servers` builds every server with ``name=<config key>`` via
``McpServers.from_file``, so a mismatch is structurally impossible.

``McpServer`` construction is I/O-free for ``$VAR``-free args, so real servers
are safe to build in-process for this guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from calfkit.mcp import McpServer, McpToolDef

from calfcord.mcp.runner import _resolve_mcp_nodes

_CONFIG_PATH = Path("mcp.json")


def test_empty_registry_raises_system_exit() -> None:
    with pytest.raises(SystemExit, match="no MCP servers configured"):
        _resolve_mcp_nodes({}, _CONFIG_PATH)


def test_non_empty_registry_returns_values() -> None:
    """A non-empty registry resolves to its values in insertion order, suitable
    for passing to a calfkit ``Worker``."""
    server_a = McpServer.stdio("npx", "-y", "a", tools=[McpToolDef(name="t")], name="a")
    server_b = McpServer.stdio("npx", "-y", "b", tools=[McpToolDef(name="t")], name="b")
    servers = {"a": server_a, "b": server_b}

    nodes = _resolve_mcp_nodes(servers, _CONFIG_PATH)

    assert nodes == [server_a, server_b]
