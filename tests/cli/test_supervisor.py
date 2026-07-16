"""Tests for the shared supervisor-availability CLI helper (:mod:`calfcord.cli._supervisor`)."""

from __future__ import annotations

import pytest

from calfcord.cli import _supervisor


def test_reason_is_none_when_the_binary_resolves() -> None:
    """A resolvable binary means the live finish can run — no reason to degrade."""
    assert _supervisor.supervisor_unavailable_reason(lambda: "/usr/bin/process-compose") is None


def test_reason_is_the_actionable_message_when_the_binary_is_missing() -> None:
    """``resolve_pc_binary`` signals 'missing' by raising an actionable RuntimeError;
    the helper surfaces that text as a value so the caller can name the fix."""

    def _missing() -> str:
        raise RuntimeError("process-compose binary not found; re-run the installer")

    assert (
        _supervisor.supervisor_unavailable_reason(_missing)
        == "process-compose binary not found; re-run the installer"
    )


def test_non_runtimeerror_propagates() -> None:
    """The catch stays narrow: a missing binary is a documented domain RuntimeError, but
    an OSError (e.g. a permissions fault on the bin dir) is a real fault that must
    propagate, not be laundered into a benign 'unavailable' degrade."""

    def _permission_fault() -> str:
        raise OSError("permission denied")

    with pytest.raises(OSError):
        _supervisor.supervisor_unavailable_reason(_permission_fault)


def test_default_pc_binary_delegates_to_supervisor_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """``default_pc_binary`` delegates to the supervisor's own resolver, imported lazily
    so this module stays import-light (the monkeypatch on the module attribute is honored
    because the import happens at call time, not import time)."""
    from calfcord.supervisor import lifecycle

    monkeypatch.setattr(lifecycle, "resolve_pc_binary", lambda: "/opt/pc")
    assert _supervisor.default_pc_binary() == "/opt/pc"


# --------------------------------------------------------------------------- #
# start_tools_host — advisory launch of the singleton tools host
# --------------------------------------------------------------------------- #


async def test_start_tools_host_spawns_the_tools_slot() -> None:
    """The helper spawns the singleton ``tools`` slot under the given launcher via the
    injected start fn, and returns its code on success."""
    calls: list[tuple] = []

    async def _tools(home, *, name, launcher, announce=True) -> int:
        calls.append((home, name, launcher))
        return 0

    rc = await _supervisor.start_tools_host("/home", launcher="/home/shims/disco", tools_start_fn=_tools)
    assert rc == 0
    assert calls == [("/home", "tools", "/home/shims/disco")]


async def test_start_tools_host_defaults_to_component_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no injected fn, the helper resolves the same ``component.component_start``
    that ``disco tools start`` runs (lazily, so the module stays import-light)."""
    from calfcord.supervisor import component

    calls: list[tuple] = []

    async def _cs(home, *, name, launcher=None, **_) -> int:
        calls.append((name, launcher))
        return 0

    monkeypatch.setattr(component, "component_start", _cs)
    rc = await _supervisor.start_tools_host("/home", launcher="/l")
    assert rc == 0
    assert calls == [("tools", "/l")]


async def test_start_tools_host_warns_and_returns_nonzero_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """A non-zero return is advisory: warn (pointing at ``disco tools start``) and return
    the code, never raise — the caller keeps going."""

    async def _tools(home, *, name, launcher, announce=True) -> int:
        return 1

    rc = await _supervisor.start_tools_host("/home", launcher="/l", tools_start_fn=_tools)
    assert rc != 0
    assert "disco tools start" in capsys.readouterr().out


async def test_start_tools_host_degrades_a_raise_instead_of_propagating(capsys: pytest.CaptureFixture[str]) -> None:
    """A tools-host start that RAISES (e.g. an OSError spawning the process — a lockfile
    PermissionError, ENOSPC) must NOT escape: the advisory contract requires it degrade to
    a warning + non-zero, never crash the caller. Crashing would skip the agent in
    ``disco init`` and fail an otherwise-open workspace in ``disco start`` — the exact
    guarantee this helper exists to provide. The cause is named (not swallowed), mirroring
    ``init._await_presence``."""

    async def _boom(home, *, name, launcher, announce=True) -> int:
        raise OSError("permission denied: state/run/tools.lock")

    rc = await _supervisor.start_tools_host("/home", launcher="/l", tools_start_fn=_boom)
    assert rc != 0
    out = capsys.readouterr().out
    assert "disco tools start" in out
    assert "permission denied" in out  # the cause is surfaced, not silently dropped


# --------------------------------------------------------------------------- #
# open_workspace — the one "open the workspace" (substrate + tools host)
# --------------------------------------------------------------------------- #


async def test_open_workspace_opens_substrate_then_tools_host() -> None:
    """The one definition of opening the workspace: substrate first, then the tools
    host, returning the substrate code on success."""
    order: list[str] = []

    async def _sub(home, *, server_urls, launcher, banner=True) -> int:
        order.append("substrate")
        return 0

    async def _tools(home, *, name, launcher, announce=True) -> int:
        order.append("tools")
        return 0

    rc = await _supervisor.open_workspace(
        "/h", server_urls="b:9092", launcher="/l", start_fn=_sub, tools_start_fn=_tools
    )
    assert rc == 0
    assert order == ["substrate", "tools"]


async def test_open_workspace_short_circuits_on_substrate_failure() -> None:
    """A substrate failure returns its code before the tools host is ever spawned."""

    async def _sub(home, *, server_urls, launcher, banner=True) -> int:
        return 1

    async def _boom_tools(home, *, name, launcher) -> int:
        raise AssertionError("the tools host must not start when the substrate failed")

    rc = await _supervisor.open_workspace(
        "/h", server_urls="b", launcher="/l", start_fn=_sub, tools_start_fn=_boom_tools
    )
    assert rc == 1


async def test_open_workspace_tools_failure_does_not_change_substrate_code() -> None:
    """The tools-host start is advisory: a failure leaves the successful substrate code
    intact (the workspace is open)."""

    async def _sub(home, *, server_urls, launcher, banner=True) -> int:
        return 0

    async def _tools(home, *, name, launcher, announce=True) -> int:
        return 1

    rc = await _supervisor.open_workspace(
        "/h", server_urls="b", launcher="/l", start_fn=_sub, tools_start_fn=_tools
    )
    assert rc == 0


async def test_open_workspace_defaults_start_fn_to_lifecycle_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no injected substrate fn, the helper resolves the real ``lifecycle.start``
    (lazily); the tools host resolves ``component.component_start`` via start_tools_host."""
    from calfcord.supervisor import component, lifecycle

    async def _ls(home, *, server_urls, launcher, banner=True) -> int:
        return 0

    async def _cs(home, *, name, launcher=None, **_) -> int:
        return 0

    monkeypatch.setattr(lifecycle, "start", _ls)
    monkeypatch.setattr(component, "component_start", _cs)
    rc = await _supervisor.open_workspace("/h", server_urls="b", launcher="/l")
    assert rc == 0
