"""Unit tests for the per-server MCP roster lifecycle (``disco mcp ...``, Phase 2).

Each ``mcp.json`` server is its own roster slot ``mcp-<server>``. Since Phase 2 the
roster lives OFF Process Compose: the verbs SPAWN a detached process per slot instead
of a ``POST /process/start`` — the agent-roster shape minus the agent-only broker-wide
duplicate guard (two hosts hosting the same toolbox id is a legitimate
competing-consumer setup, not the agent split-brain).

Contracts pinned:

* workspace check first (not-running hint, exit 1, no spawn);
* ``start`` of a live slot is a restart in place (behavior #2) — also the "re-pick up
  an edited mcp.json entry" command;
* a server added to mcp.json after ``disco start`` simply spawns (no reload — the
  Phase 2 win);
* ``--all`` sweeps: start over the *configured* names (mcp.json), stop and restart
  over the *running* ``mcp-`` slots on this host;
* server names are validated against the selector grammar before any spawn.
"""

from __future__ import annotations

import pytest

from calfcord.supervisor import _workspace, mcp_roster
from calfcord.supervisor.procspawn import TerminateResult


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient — the workspace check plus
    the declared-process read the legacy-workspace guard consults (``processes``
    defaults to the modern substrate-only project)."""

    def __init__(self, *, workspace_up: bool = True, processes: list | None = None) -> None:
        self._workspace_up = workspace_up
        self._processes = processes if processes is not None else [{"name": "broker"}, {"name": "bridge"}]

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def list_processes(self):
        return self._processes


class _FakeSlots:
    """In-memory stand-in for the ``_workspace`` roster-slot primitives.

    ``live`` holds full slot names (e.g. ``mcp-github``). Records spawn/terminate.
    """

    def __init__(
        self,
        live: list[str] | None = None,
        *,
        spawn_error: dict[str, Exception] | None = None,
        launch_dead: set[str] | None = None,
        gate_ok: bool = True,
    ) -> None:
        self.live: set[str] = set(live or [])
        self.spawned: list[tuple[str, list[str]]] = []
        self.terminated: list[str] = []
        self._spawn_error = spawn_error or {}
        self._launch_dead = launch_dead or set()
        self._gate_ok = gate_ok
        self.gate_calls: list[str | None] = []

    def install(self, monkeypatch) -> None:
        async def launch_slot(home, slot, argv, **_kwargs):
            if slot in self._spawn_error:
                raise self._spawn_error[slot]
            if slot in self._launch_dead:
                return False
            self.spawned.append((slot, list(argv)))
            self.live.add(slot)
            return True

        async def terminate_slot(home, slot):
            self.terminated.append(slot)
            was_live = slot in self.live
            self.live.discard(slot)
            return TerminateResult.TERMINATED if was_live else None

        def live_slots(home):
            return set(self.live)

        def slot_is_live(home, slot):
            return slot in self.live

        async def broker_gate(server_urls=None, probe=None):
            self.gate_calls.append(server_urls)
            return self._gate_ok

        monkeypatch.setattr(_workspace, "launch_slot", launch_slot)
        monkeypatch.setattr(_workspace, "terminate_slot", terminate_slot)
        monkeypatch.setattr(_workspace, "live_slots", live_slots)
        monkeypatch.setattr(_workspace, "slot_is_live", slot_is_live)
        monkeypatch.setattr(_workspace, "broker_gate", broker_gate)


def _home(tmp_path) -> str:
    return str(tmp_path)


def _argv(tmp_path, server: str) -> list[str]:
    return [_workspace.launcher_for(str(tmp_path)), "run", "mcp", server]


# --- mcp_start ---------------------------------------------------------------


async def test_start_when_not_running_spawns_slot(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 0
    assert slots.spawned == [("mcp-github", _argv(tmp_path, "github"))]
    assert slots.terminated == []
    out = capsys.readouterr().out
    # "started" is the honest claim; presence is the watchers' job, never "online".
    assert "github started" in out
    assert "online" not in out


async def test_start_when_running_restarts_in_place(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 0
    assert slots.terminated == ["mcp-github"]
    assert slots.spawned == [("mcp-github", _argv(tmp_path, "github"))]
    assert "restarted" in capsys.readouterr().out


async def test_start_workspace_down_prints_hint(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=_StubClient(workspace_up=False))
    assert rc == 1
    assert slots.spawned == []
    assert "disco start" in capsys.readouterr().out


async def test_start_invalid_server_name_refused_before_spawn(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="Bad-Name", client=_StubClient(workspace_up=False))
    assert rc == 1
    assert slots.spawned == []
    assert "Bad-Name" in capsys.readouterr().out


# --- mcp_stop / mcp_restart ---------------------------------------------------


async def test_stop_stops_slot(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_stop(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 0
    assert slots.terminated == ["mcp-github"]
    assert "github stopped" in capsys.readouterr().out


async def test_stop_not_running_here(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_stop(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 0
    assert "not running here" in capsys.readouterr().out


async def test_restart_restarts_slot(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_restart(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 0
    assert slots.spawned == [("mcp-github", _argv(tmp_path, "github"))]
    assert "restarted" in capsys.readouterr().out


# --- sweeps -------------------------------------------------------------------


async def test_start_all_starts_each_configured_server(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["mcp-alpha"])
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_start_all(_home(tmp_path), servers=["alpha", "beta"], client=_StubClient())
    assert rc == 0
    # alpha was running -> restarted (edited-entry pickup); beta spawned.
    assert "mcp-alpha" in slots.terminated
    assert ("mcp-beta", _argv(tmp_path, "beta")) in slots.spawned


async def test_start_all_with_no_servers_says_so(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_start_all(_home(tmp_path), servers=[], client=_StubClient())
    assert rc == 0
    assert slots.spawned == []
    assert "disco mcp add" in capsys.readouterr().out


async def test_stop_all_stops_only_running_mcp_slots(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["mcp-alpha", "mcp-beta", "assistant", "tools"])
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_stop_all(_home(tmp_path), client=_StubClient())
    assert rc == 0
    assert sorted(slots.terminated) == ["mcp-alpha", "mcp-beta"]


async def test_stop_all_none_running_is_benign(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_stop_all(_home(tmp_path), client=_StubClient())
    assert rc == 0
    assert slots.terminated == []
    assert "no MCP servers running" in capsys.readouterr().out


async def test_restart_all_restarts_only_running_mcp_slots(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["mcp-alpha", "assistant"])
    slots.install(monkeypatch)
    rc = await mcp_roster.mcp_restart_all(_home(tmp_path), client=_StubClient())
    assert rc == 0
    assert slots.terminated == ["mcp-alpha"]
    assert ("mcp-alpha", _argv(tmp_path, "alpha")) in slots.spawned


# --- crash-on-boot / broker gate / concurrency ---------------------------------


async def test_mcp_start_reports_crash_on_boot_honestly(tmp_path, capsys, monkeypatch):
    from calfcord.supervisor.procspawn import log_path_for

    slots = _FakeSlots(launch_dead={"mcp-github"})
    slots.install(monkeypatch)

    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 1
    out = capsys.readouterr().out
    assert "exited" in out
    assert str(log_path_for(_home(tmp_path), "mcp-github")) in out
    assert "started" not in out


async def test_mcp_restart_reports_crash_on_boot_honestly(tmp_path, capsys, monkeypatch):
    from calfcord.supervisor.procspawn import log_path_for

    slots = _FakeSlots(["mcp-github"], launch_dead={"mcp-github"})
    slots.install(monkeypatch)

    rc = await mcp_roster.mcp_restart(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 1
    assert str(log_path_for(_home(tmp_path), "mcp-github")) in capsys.readouterr().out


@pytest.mark.parametrize(
    "call",
    [
        lambda c, h: mcp_roster.mcp_start(h, server="x", client=c),
        lambda c, h: mcp_roster.mcp_restart(h, server="x", client=c),
        lambda c, h: mcp_roster.mcp_start_all(h, servers=["x"], client=c),
        lambda c, h: mcp_roster.mcp_restart_all(h, client=c),
    ],
    ids=["start", "restart", "start_all", "restart_all"],
)
async def test_mcp_spawn_verbs_blocked_when_broker_unreachable(tmp_path, capsys, monkeypatch, call):
    slots = _FakeSlots(["mcp-x"], gate_ok=False)
    slots.install(monkeypatch)

    rc = await call(_StubClient(), _home(tmp_path))
    assert rc == 1
    assert slots.spawned == []
    assert "broker not reachable" in capsys.readouterr().out


async def test_mcp_start_all_gates_the_broker_once(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await mcp_roster.mcp_start_all(_home(tmp_path), servers=["alpha", "beta"], client=_StubClient())
    assert rc == 0
    assert len(slots.gate_calls) == 1


async def test_mcp_start_concurrent_holder_reports_busy(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "mcp-github"):
        rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=_StubClient())
    assert rc == 0
    assert slots.spawned == []
    assert "already" in capsys.readouterr().out


async def test_mcp_stop_respects_a_concurrent_slot_holder(tmp_path, capsys, monkeypatch):
    """A stop racing a start's confirm window must not unlink the fresh pidfile:
    the slot lock refuses, the stop skips honestly, and nothing is terminated."""
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "mcp-github"):
        rc = await mcp_roster.mcp_stop(_home(tmp_path), server="github", client=_StubClient())

    assert rc == 0
    assert slots.terminated == []
    assert "another disco command" in capsys.readouterr().out


@pytest.mark.parametrize(
    "call",
    [
        lambda h, c: mcp_roster.mcp_stop(h, server="Bad Name!", client=c),
        lambda h, c: mcp_roster.mcp_restart(h, server="Bad Name!", client=c),
    ],
    ids=["stop", "restart"],
)
async def test_mcp_stop_and_restart_refuse_an_invalid_server_name(tmp_path, capsys, monkeypatch, call):
    """The selector-grammar guard fires on stop/restart too (not just start):
    a name that could never be an mcp.json key is refused before any slot work."""
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)

    rc = await call(_home(tmp_path), _StubClient())

    assert rc == 1
    assert slots.terminated == []
    assert slots.spawned == []
    assert "invalid MCP server name" in capsys.readouterr().out


async def test_mcp_stop_all_workspace_busy_is_an_error_not_a_skip(tmp_path, capsys, monkeypatch):
    """A `disco start`/`stop` holding the lifecycle lock EXCLUSIVELY while the stop
    sweep runs is a real refusal per slot: an error line and a nonzero sweep exit
    (unlike the benign per-slot busy skip)."""
    from calfcord.supervisor.lifecycle import lifecycle_lock

    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)

    with lifecycle_lock(_home(tmp_path)):
        rc = await mcp_roster.mcp_stop_all(_home(tmp_path), client=_StubClient())

    assert rc == 1
    assert slots.terminated == []
    out = capsys.readouterr().out
    assert out.count("error:") == 1
    assert "in progress" in out


async def test_mcp_restart_concurrent_holder_reports_busy(tmp_path, capsys, monkeypatch):
    """A restart racing another command's slot mutation is benign: the honest busy
    line, exit 0, and NOTHING terminated or spawned."""
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "mcp-github"):
        rc = await mcp_roster.mcp_restart(_home(tmp_path), server="github", client=_StubClient())

    assert rc == 0
    assert slots.terminated == []
    assert slots.spawned == []
    assert "already being restarted" in capsys.readouterr().out


async def test_mcp_restart_all_busy_slot_is_reported_and_benign(tmp_path, capsys, monkeypatch):
    """A busy slot in the restart sweep is skipped with an honest line and does not
    fail the sweep — the other slots still restart."""
    slots = _FakeSlots(["mcp-alpha", "mcp-beta"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "mcp-alpha"):
        rc = await mcp_roster.mcp_restart_all(_home(tmp_path), client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["mcp-beta"]
    assert ("mcp-beta", _argv(tmp_path, "beta")) in slots.spawned
    assert all(slot != "mcp-alpha" for slot, _ in slots.spawned)
    assert "already" in capsys.readouterr().out


# --- legacy-workspace guard (upgrade over a live old-style workspace) ---------

_LEGACY_PROCESSES = [{"name": "broker"}, {"name": "bridge"}, {"name": "mcp-github"}]


@pytest.mark.parametrize(
    "call",
    [
        lambda c, h: mcp_roster.mcp_start(h, server="github", client=c),
        lambda c, h: mcp_roster.mcp_restart(h, server="github", client=c),
        lambda c, h: mcp_roster.mcp_start_all(h, servers=["github"], client=c),
        lambda c, h: mcp_roster.mcp_restart_all(h, client=c),
    ],
    ids=["start", "restart", "start_all", "restart_all"],
)
async def test_mcp_spawn_verbs_refuse_a_legacy_pc_supervised_workspace(
    tmp_path, capsys, monkeypatch, call
):
    """An old-main workspace still supervises mcp- slots under PC; every SPAWN verb
    refuses with the upgrade remedy (stops stay usable)."""
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)

    rc = await call(_StubClient(processes=_LEGACY_PROCESSES), _home(tmp_path))

    assert rc == 1
    assert slots.spawned == []
    assert slots.terminated == []
    assert "older calfcord" in capsys.readouterr().out


async def test_mcp_stop_stays_usable_on_a_legacy_workspace(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["mcp-github"])
    slots.install(monkeypatch)

    rc = await mcp_roster.mcp_stop(
        _home(tmp_path), server="github", client=_StubClient(processes=_LEGACY_PROCESSES)
    )

    assert rc == 0
    assert slots.terminated == ["mcp-github"]
    assert "mcp server github stopped" in capsys.readouterr().out


async def test_mcp_stop_all_busy_slot_is_reported_and_benign(tmp_path, capsys, monkeypatch):
    """A busy slot in the stop sweep is skipped with an honest line and does not
    fail the sweep — matching the restart sweep's benign busy handling."""
    slots = _FakeSlots(["mcp-alpha", "mcp-beta"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "mcp-alpha"):
        rc = await mcp_roster.mcp_stop_all(_home(tmp_path), client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["mcp-beta"]
    assert "another disco command" in capsys.readouterr().out


# ----------------------------------------------------- workspace-down uniformity


@pytest.mark.parametrize(
    "call",
    [
        lambda c, h: mcp_roster.mcp_stop(h, server="x", client=c),
        lambda c, h: mcp_roster.mcp_restart(h, server="x", client=c),
        lambda c, h: mcp_roster.mcp_start_all(h, servers=["x"], client=c),
        lambda c, h: mcp_roster.mcp_stop_all(h, client=c),
        lambda c, h: mcp_roster.mcp_restart_all(h, client=c),
    ],
    ids=["stop", "restart", "start_all", "stop_all", "restart_all"],
)
async def test_workspace_down_hints_and_exits_1(tmp_path, capsys, monkeypatch, call):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    rc = await call(_StubClient(workspace_up=False), _home(tmp_path))
    assert rc == 1
    assert "disco start" in capsys.readouterr().out
    assert slots.spawned == [] and slots.terminated == []


def test_running_servers_returns_bare_names(tmp_path, monkeypatch):
    """The public read strips the slot prefix so callers (mcp list) never learn the
    mcp- convention; it reads the pidfile namespace, no REST client."""
    slots = _FakeSlots(["mcp-github", "mcp-docs", "assistant"])
    slots.install(monkeypatch)
    assert mcp_roster.running_servers(_home(tmp_path)) == {"github", "docs"}
