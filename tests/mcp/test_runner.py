"""Tests for the ``calfkit-mcp`` bridge runner's node-resolution guard.

:func:`calfcord.mcp.runner._resolve_mcp_nodes` is the empty-registry guard
extracted from ``_amain`` so it can be exercised without standing up Kafka. An
empty registry must fail fast — the worker would otherwise boot inert,
subscribing to no topics while appearing healthy.

The former key/``name=`` mismatch guard is gone: ``load_mcp_servers`` requires
every config key to exist in ``MCP_CATALOG`` (keys constrained to ``[a-z0-9_]``,
which name normalization leaves untouched), so ``server.name == key`` holds by
construction.

``McpServer`` construction is I/O-free for ``$VAR``-free args, so real servers
are safe to build in-process for this guard.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.mcp import McpServer, McpServers, McpToolDef
from calfkit.mcp.exceptions import McpConfigError

from calfcord.mcp import runner as mcp_runner
from calfcord.mcp.runner import _amain, _resolve_mcp_nodes

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


async def test_amain_exits_cleanly_on_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config-load failure becomes a clean SystemExit with an actionable
    message — never a raw traceback, and never a broker connection (the guard
    precedes Client.connect)."""

    def _raise(*_a: object, **_k: object) -> McpServers:
        raise McpConfigError("boom")

    monkeypatch.setattr("calfcord.mcp.runner.load_mcp_servers", _raise)
    with pytest.raises(SystemExit) as excinfo:
        await _amain()
    message = str(excinfo.value)
    assert "failed to load MCP servers" in message
    assert "calfcord-mcp-codegen" in message


async def test_amain_exits_when_no_servers_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (but valid) registry fails fast before any broker connection."""
    monkeypatch.setattr(
        "calfcord.mcp.runner.load_mcp_servers",
        lambda *_a, **_k: McpServers({}),
    )
    with pytest.raises(SystemExit, match="nothing to host"):
        await _amain()


def _one_server() -> McpServers:
    server = McpServer.stdio("npx", "-y", "a", tools=[McpToolDef(name="t")], name="a")
    return McpServers({"a": server})


async def test_amain_provisions_infra_before_worker_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 0.5.4 boot contract: ``_amain`` provisions the client reply topic
    (``provision_infra`` — the calfkit#180 blind spot) BEFORE the managed
    ``worker.run()`` takes the broker live. Running before provisioning would
    hang the reply dispatcher on a no-auto-create broker."""
    order: list[str] = []

    @asynccontextmanager
    async def _fake_connect(*_a, **_k):
        yield MagicMock()

    worker = MagicMock()

    async def _run() -> None:
        order.append("run")

    worker.run = _run

    async def _provision(_client) -> None:
        order.append("provision")

    monkeypatch.setattr(mcp_runner, "load_mcp_servers", lambda *_a, **_k: _one_server())
    monkeypatch.setattr(mcp_runner.Client, "connect", lambda *a, **k: _fake_connect(*a, **k))
    monkeypatch.setattr(mcp_runner, "provision_infra", _provision)
    monkeypatch.setattr(mcp_runner, "Worker", lambda *_a, **_k: worker)

    await _amain()

    assert order == ["provision", "run"]


async def test_amain_worker_run_crash_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash inside the managed ``worker.run()`` must escape ``_amain`` so the
    process exits non-zero and the supervisor restarts it — the lifecycle
    guarantee is now native to ``Worker.run()``; the runner must not swallow it."""

    @asynccontextmanager
    async def _fake_connect(*_a, **_k):
        yield MagicMock()

    worker = MagicMock()
    worker.run = AsyncMock(side_effect=ValueError("simulated kafka drop"))

    monkeypatch.setattr(mcp_runner, "load_mcp_servers", lambda *_a, **_k: _one_server())
    monkeypatch.setattr(mcp_runner.Client, "connect", lambda *a, **k: _fake_connect(*a, **k))
    monkeypatch.setattr(mcp_runner, "provision_infra", AsyncMock())
    monkeypatch.setattr(mcp_runner, "Worker", lambda *_a, **_k: worker)

    with pytest.raises(ValueError, match="simulated kafka drop"):
        await _amain()
