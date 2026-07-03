"""Tests for the CLI's best-effort capability-view snapshot.

The snapshot reads calfkit's public ``client.mesh.get_tools()`` view and
projects the MCP toolboxes to ``{server: [tool, ...]}`` rows. The projection
is unit-testable with real DTOs; the degrade contract (any failure ->
``None``) is exercised against a real unreachable broker.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from calfkit.client import ToolboxInfo, ToolNodeInfo, ToolSpec

from calfcord.mcp.capability_read import _toolbox_rows, snapshot_capability_tools

_SEEN = datetime(2026, 1, 1, tzinfo=UTC)


def test_toolbox_rows_keeps_only_toolboxes_with_sorted_tool_names() -> None:
    """MCP toolboxes project to ``{server: sorted bare tool names}``; a
    ``ToolNodeInfo`` (a function-tool node) is NOT an MCP row — the editor
    covers those via the local builtin registry — so it is dropped."""
    tools = {
        "github": ToolboxInfo(
            name="github",
            tools=(
                ToolSpec(name="search", description=None, parameters_schema={}),
                ToolSpec(name="create_issue", description="", parameters_schema={}),
            ),
            last_seen=_SEEN,
        ),
        "terminal": ToolNodeInfo(name="terminal", description=None, parameters_schema={}, last_seen=_SEEN),
    }

    assert _toolbox_rows(tools) == {"github": ["create_issue", "search"]}


def test_toolbox_rows_keeps_advertising_toolbox_with_no_tools() -> None:
    """A toolbox that advertises zero tools is still a real ``mcp/<server>``
    row (``{name: []}``) — the server exists but exposes nothing — distinct
    from the server being absent entirely."""
    tools = {"empty": ToolboxInfo(name="empty", tools=(), last_seen=_SEEN)}

    assert _toolbox_rows(tools) == {"empty": []}


class _FakeMesh:
    def __init__(self, result: object) -> None:
        self._result = result

    async def get_tools(self) -> object:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeClient:
    """A stand-in for ``calfkit.client.Client`` recording that it was closed."""

    def __init__(self, result: object) -> None:
        self.mesh = _FakeMesh(result)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_snapshot_maps_mesh_toolboxes_and_closes_client(monkeypatch) -> None:
    """The success path reads ``client.mesh.get_tools()``, projects the MCP
    toolboxes to rows, and always closes the client."""
    from calfkit.client import Client

    from calfcord.mcp import capability_read

    fake = _FakeClient(
        {
            "github": ToolboxInfo(
                name="github",
                tools=(ToolSpec(name="search", description=None, parameters_schema={}),),
                last_seen=_SEEN,
            )
        }
    )
    monkeypatch.setattr(Client, "connect", lambda *a, **k: fake)

    result = capability_read.snapshot_capability_tools("broker:9092", timeout=0.5)

    assert result == {"github": ["search"]}
    assert fake.closed is True


def test_snapshot_degrades_to_none_and_closes_on_mesh_unavailable(monkeypatch) -> None:
    """A ``MeshUnavailableError`` (view unreachable / directory absent) degrades
    to ``None``, and the client is still closed (cleanup runs on failure)."""
    from calfkit.client import Client
    from calfkit.exceptions import MeshUnavailableError

    from calfcord.mcp import capability_read

    fake = _FakeClient(MeshUnavailableError("no directory", reason="open_failed"))
    monkeypatch.setattr(Client, "connect", lambda *a, **k: fake)

    result = capability_read.snapshot_capability_tools("broker:9092", timeout=0.5)

    assert result is None
    assert fake.closed is True


def test_snapshot_function_nodes_only_answers_empty_not_none(monkeypatch) -> None:
    """A view that answered with only function-tool nodes (no MCP toolboxes)
    yields ``{}`` — "answered, no MCP servers" — NOT ``None`` (which means the
    view was unreachable). The ``{}``/``None`` distinction is the whole point."""
    from calfkit.client import Client

    from calfcord.mcp import capability_read

    fake = _FakeClient(
        {"terminal": ToolNodeInfo(name="terminal", description=None, parameters_schema={}, last_seen=_SEEN)}
    )
    monkeypatch.setattr(Client, "connect", lambda *a, **k: fake)

    result = capability_read.snapshot_capability_tools("broker:9092", timeout=0.5)

    assert result == {}
    assert result is not None


def test_unreachable_broker_degrades_to_none_quickly() -> None:
    """Failure is ``None`` (NOT ``{}``): callers must be able to tell "the
    view was unreachable" apart from "the view answered and is empty" so
    the editor can say which one happened."""
    started = time.monotonic()
    # An unroutable port on localhost: connection refused, not a hang.
    result = snapshot_capability_tools("localhost:1", timeout=0.5)
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 10  # bounded — never the editor hanging on a dead broker
