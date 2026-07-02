"""Unit tests for the GENERIC component lifecycle (Phase 2 spawn model).

``component_start`` / ``component_stop`` / ``component_restart`` are the DRY base
the ``tools`` SINGLETON clocks in/out through. Since Phase 2 the roster lives OFF
Process Compose: a component is SPAWNED as a detached process (a pidfile under
``state/run``) via the shared ``_workspace`` primitives — the same spawn/terminate
shape as the agent roster, but WITHOUT the agent-only broker-wide duplicate guard (a
singleton cannot duplicate on one host). These exercise the functions with **no real
process-compose binary, no broker, and no real child process**: the REST client
(used only for the substrate workspace check) is injected and the spawn/terminate
primitives are replaced with an in-memory fake.

The contracts pinned here:

* **Workspace check first.** With the substrate unreachable there is nothing to
  start/stop/restart — print the not-running hint and return ``1`` before any spawn.
* **Start of an already-running component is a restart** (behavior #2): a ``start``
  on a slot already live locally re-applies an edited config by terminating and
  re-spawning it, rather than a duplicate spawn.
* **No duplicate guard.** Unlike ``agent_start``, ``component_start`` never takes or
  queries a probe — a singleton component cannot duplicate on one host.
* **Per-home default client.** With no client injected, the default targets the
  port :func:`lifecycle.pc_port_for` derives from ``$CALFCORD_HOME``.
"""

from __future__ import annotations

from calfcord.supervisor import _workspace, component
from calfcord.supervisor.procspawn import TerminateResult


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient — only the workspace check."""

    def __init__(self, *, workspace_up: bool = True) -> None:
        self._workspace_up = workspace_up

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}


class _FakeSlots:
    """In-memory stand-in for the ``_workspace`` roster-slot primitives."""

    def __init__(
        self,
        live: list[str] | None = None,
        *,
        launch_dead: set[str] | None = None,
        gate_ok: bool = True,
    ) -> None:
        self.live: set[str] = set(live or [])
        self.spawned: list[tuple[str, list[str]]] = []
        self.terminated: list[str] = []
        self._launch_dead = launch_dead or set()
        self._gate_ok = gate_ok
        self.gate_calls: list[str | None] = []

    def install(self, monkeypatch) -> None:
        async def launch_slot(home, slot, argv, **_kwargs):
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

        def slot_is_live(home, slot):
            return slot in self.live

        async def broker_gate(server_urls=None, probe=None):
            self.gate_calls.append(server_urls)
            return self._gate_ok

        monkeypatch.setattr(_workspace, "launch_slot", launch_slot)
        monkeypatch.setattr(_workspace, "terminate_slot", terminate_slot)
        monkeypatch.setattr(_workspace, "slot_is_live", slot_is_live)
        monkeypatch.setattr(_workspace, "broker_gate", broker_gate)


def _home(tmp_path) -> str:
    return str(tmp_path)


def _argv(tmp_path, name: str) -> list[str]:
    return [_workspace.launcher_for(str(tmp_path)), "run", name]


# --- component_start --------------------------------------------------------


async def test_component_start_when_not_running_spawns(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await component.component_start(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert slots.spawned == [("tools", _argv(tmp_path, "tools"))]
    assert slots.terminated == []
    out = capsys.readouterr().out
    # "started" is the honest claim; presence is the watchers' job, never "online".
    assert "tools started" in out
    assert "online" not in out


async def test_component_start_when_already_running_restarts(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools"])
    slots.install(monkeypatch)

    rc = await component.component_start(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["tools"]
    assert slots.spawned == [("tools", _argv(tmp_path, "tools"))]
    assert "tools restarted" in capsys.readouterr().out


async def test_component_start_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await component.component_start(_home(tmp_path), name="tools", client=_StubClient(workspace_up=False))

    assert rc == 1
    assert slots.spawned == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "disco start" in out


# --- component_stop ---------------------------------------------------------


async def test_component_stop_happy_path(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools"])
    slots.install(monkeypatch)

    rc = await component.component_stop(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["tools"]
    assert "tools stopped" in capsys.readouterr().out


async def test_component_stop_not_running_here(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await component.component_stop(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert "not running here" in capsys.readouterr().out


async def test_component_stop_respects_a_concurrent_slot_holder(tmp_path, capsys, monkeypatch):
    """A stop racing a start's confirm window must not unlink the fresh pidfile:
    the slot lock refuses, the stop skips honestly, and nothing is terminated."""
    slots = _FakeSlots(["tools"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "tools"):
        rc = await component.component_stop(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert slots.terminated == []
    assert "another disco command" in capsys.readouterr().out


async def test_component_stop_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools"])
    slots.install(monkeypatch)

    rc = await component.component_stop(_home(tmp_path), name="tools", client=_StubClient(workspace_up=False))

    assert rc == 1
    assert slots.terminated == []
    assert "workspace not running" in capsys.readouterr().out


# --- component_restart ------------------------------------------------------


async def test_component_restart_happy_path(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools"])
    slots.install(monkeypatch)

    rc = await component.component_restart(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["tools"]
    assert slots.spawned == [("tools", _argv(tmp_path, "tools"))]
    assert "tools restarted" in capsys.readouterr().out


async def test_component_restart_not_running_still_spawns(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await component.component_restart(_home(tmp_path), name="tools", client=_StubClient())

    assert rc == 0
    assert slots.spawned == [("tools", _argv(tmp_path, "tools"))]
    assert "tools restarted" in capsys.readouterr().out


async def test_component_restart_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools"])
    slots.install(monkeypatch)

    rc = await component.component_restart(_home(tmp_path), name="tools", client=_StubClient(workspace_up=False))

    assert rc == 1
    assert slots.spawned == []
    assert "workspace not running" in capsys.readouterr().out


# --- crash-on-boot / broker gate / concurrency --------------------------------


async def test_component_start_reports_crash_on_boot_honestly(tmp_path, capsys, monkeypatch):
    from calfcord.supervisor.procspawn import log_path_for

    slots = _FakeSlots(launch_dead={"tools"})
    slots.install(monkeypatch)

    rc = await component.component_start(_home(tmp_path), name="tools", client=_StubClient())
    assert rc == 1
    out = capsys.readouterr().out
    assert "exited" in out
    assert str(log_path_for(_home(tmp_path), "tools")) in out
    assert "started" not in out


async def test_component_restart_reports_crash_on_boot_honestly(tmp_path, capsys, monkeypatch):
    from calfcord.supervisor.procspawn import log_path_for

    slots = _FakeSlots(["tools"], launch_dead={"tools"})
    slots.install(monkeypatch)

    rc = await component.component_restart(_home(tmp_path), name="tools", client=_StubClient())
    assert rc == 1
    assert str(log_path_for(_home(tmp_path), "tools")) in capsys.readouterr().out


async def test_component_start_blocked_when_broker_unreachable(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(gate_ok=False)
    slots.install(monkeypatch)

    rc = await component.component_start(_home(tmp_path), name="tools", client=_StubClient())
    assert rc == 1
    assert slots.spawned == []
    assert "broker not reachable" in capsys.readouterr().out


async def test_component_restart_blocked_when_broker_unreachable(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools"], gate_ok=False)
    slots.install(monkeypatch)

    rc = await component.component_restart(_home(tmp_path), name="tools", client=_StubClient())
    assert rc == 1
    assert slots.terminated == []
    assert "broker not reachable" in capsys.readouterr().out


async def test_component_start_concurrent_holder_reports_busy(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "tools"):
        rc = await component.component_start(_home(tmp_path), name="tools", client=_StubClient())
    assert rc == 0
    assert slots.spawned == []
    assert "already" in capsys.readouterr().out


# --- default wiring ----------------------------------------------------------


def test_component_defaults_to_per_home_process_compose_client(tmp_path):
    from calfcord.supervisor.client import ProcessComposeClient
    from calfcord.supervisor.lifecycle import pc_port_for

    home = _home(tmp_path)
    client = component._resolve_client(None, home)

    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    assert client._base_url == expected._base_url
