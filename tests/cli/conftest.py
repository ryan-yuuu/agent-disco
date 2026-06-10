"""Shared fixtures for the CLI test package."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_mcp_enumeration_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tool-checkbox surfaces (agent tools editor, create wizard's
    pick_tools) off the network in unit tests: the default MCP enumeration
    reads mcp.json and probes the broker's capability topic, which a unit
    test must never do. Tests exercising MCP rows inject their own
    ``mcp_servers_fn`` / ``live_tools_fn`` (or re-patch these defaults).
    """
    from calfcord.cli import agent_tools

    monkeypatch.setattr(agent_tools, "_default_mcp_servers", lambda: [])
    monkeypatch.setattr(agent_tools, "_default_live_tools", lambda: {})
