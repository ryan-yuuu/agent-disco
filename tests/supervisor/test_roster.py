"""Unit tests for the agent-roster operations (design §3.4, §3.5; Phase 2 spawn model).

Since Phase 2 the roster lives OFF Process Compose: ``agent_start`` etc. SPAWN a
detached process per agent (a pidfile under ``state/run``) instead of a ``POST
/process/start``. These tests exercise that flow with **no real process-compose
binary, no broker, and no real child processes**: the broker-wide live-roster probe
(a read of calfkit's native mesh) and the REST client (used only for the substrate
workspace check) are injected, and the shared spawn/terminate/scan primitives on
:mod:`calfcord.supervisor._workspace` are replaced with an in-memory fake.

The contracts pinned here are the distributed-correct duplicate guard (§3.5 —
refuse to start a name already live anywhere, CLI-side, without a bridge change),
the brand-new-agent path (Phase 2 — an agent authored after ``disco start`` just
spawns, no reload), and the three-way ps union (§3.4 — physical∩logical,
physical-only "not yet registered", logical-only "running on another host").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from calfkit.exceptions import MeshUnavailableError

from calfcord.supervisor import _workspace, roster
from calfcord.supervisor.procspawn import TerminateResult

# --- fakes ------------------------------------------------------------------


class _StubProbe:
    """A scriptable stand-in for the broker-wide live-roster probe.

    Records the ``server_urls`` it was called with so a test can assert the probe
    fired (duplicate guard / ps) or did NOT (workspace-down short-circuit). The
    calfkit 0.12 mesh carries agent NAMES (presence), so it returns a fixed list of
    live agent names. ``raises`` models a broker-down probe.
    """

    def __init__(self, roster_result: list[str] | None = None, *, raises: Exception | None = None) -> None:
        self._roster = list(roster_result or [])
        self._raises = raises
        self.calls: list[str] = []

    async def __call__(self, server_urls: str):
        self.calls.append(server_urls)
        if self._raises is not None:
            raise self._raises
        return list(self._roster)


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient — only the workspace check.

    ``workspace_up`` drives the ``project_state`` probe (False → the real client's
    RuntimeError-on-transport-failure, i.e. supervisor unreachable). The roster no
    longer issues start/stop/restart against PC, so no lifecycle methods are needed.
    """

    def __init__(self, *, workspace_up: bool = True) -> None:
        self._workspace_up = workspace_up

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}


class _FakeSlots:
    """In-memory stand-in for the ``_workspace`` roster-slot primitives.

    Tracks which slots are "live" and records every launch/terminate so a test can
    assert the exact process actions without a real child. Installs itself over
    ``_workspace.launch_slot`` / ``terminate_slot`` / ``slot_is_live`` /
    ``live_agent_slots`` / ``broker_gate`` — the names roster calls through the
    module. ``launch_dead`` scripts a crash-on-boot (launch confirms the process
    already exited); ``gate_ok`` scripts the broker-reachability gate.
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

        def slot_is_live(home, slot):
            return slot in self.live

        def live_agent_slots(home):
            return {s for s in self.live if _workspace.is_agent_slot(s)}

        async def broker_gate(server_urls=None, probe=None):
            self.gate_calls.append(server_urls)
            return self._gate_ok

        monkeypatch.setattr(_workspace, "launch_slot", launch_slot)
        monkeypatch.setattr(_workspace, "terminate_slot", terminate_slot)
        monkeypatch.setattr(_workspace, "slot_is_live", slot_is_live)
        monkeypatch.setattr(_workspace, "live_agent_slots", live_agent_slots)
        monkeypatch.setattr(_workspace, "broker_gate", broker_gate)


_SERVERS = "localhost:9092"


def _home(tmp_path) -> str:
    return str(tmp_path)


def _agent_argv(tmp_path, name: str) -> list[str]:
    return [_workspace.launcher_for(str(tmp_path)), "run", "agent", name]


# --- agent_start: duplicate guard (§3.5) ------------------------------------


async def test_agent_start_refuses_duplicate_when_probe_shows_live(tmp_path, capsys, monkeypatch):
    """Name already live anywhere → no duplicate, clear message, exit 0 (§3.5)."""
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=probe
    )

    assert rc == 0
    assert slots.spawned == []  # the whole point: no duplicate spawned
    assert probe.calls == [_SERVERS]
    out = capsys.readouterr().out
    assert "already running in the organization" in out


# --- agent_start: already-running-here is a restart (behavior #2) ------------


async def test_agent_start_local_running_restarts_not_duplicate_refusal(tmp_path, capsys, monkeypatch):
    """`start` on a name live on THIS host → terminate + re-spawn, never the refusal."""
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)
    probe = _StubProbe(["assistant"])  # would refuse if we ever reached it

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=probe
    )

    assert rc == 0
    assert slots.terminated == ["assistant"]
    assert slots.spawned == [("assistant", _agent_argv(tmp_path, "assistant"))]
    assert probe.calls == []  # never reached the org probe
    assert "restarted" in capsys.readouterr().out


async def test_agent_start_remote_running_keeps_duplicate_refusal(tmp_path, capsys, monkeypatch):
    """A name running only on ANOTHER host (not locally) still hits the refusal."""
    slots = _FakeSlots(live=["other"])  # something else local, not "assistant"
    slots.install(monkeypatch)
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=probe
    )

    assert rc == 0
    assert slots.spawned == []
    assert "already running in the organization" in capsys.readouterr().out


# --- agent_start: happy spawn ------------------------------------------------


async def test_agent_start_happy_path_spawns_when_roster_empty(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe([])

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=probe
    )

    assert rc == 0
    assert slots.spawned == [("assistant", _agent_argv(tmp_path, "assistant"))]
    out = capsys.readouterr().out
    # "started" is the honest claim — the spawn survived its confirmation window.
    # Presence ("online"/registered) is the callers' watchers' job, never printed here.
    assert "agent assistant started" in out
    assert "online" not in out


async def test_agent_start_spawns_with_home_derived_launcher(tmp_path, monkeypatch):
    """No launcher passed (init/agent_create call site) → derived from home."""
    slots = _FakeSlots()
    slots.install(monkeypatch)

    await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )

    (_, argv) = slots.spawned[0]
    assert argv[0] == str(tmp_path / "shims" / "disco")
    assert argv[1:] == ["run", "agent", "assistant"]


async def test_agent_start_ignores_other_live_agents(tmp_path, monkeypatch):
    """A different agent live locally must not make ``name`` look already-running."""
    slots = _FakeSlots(["scribe"])
    slots.install(monkeypatch)

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )
    assert rc == 0
    assert ("assistant", _agent_argv(tmp_path, "assistant")) in slots.spawned


async def test_agent_start_workspace_down_short_circuits(tmp_path, capsys, monkeypatch):
    """Substrate down → hint, exit 1, and NO spawn / NO probe."""
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=_StubClient(workspace_up=False),
        probe=probe,
    )

    assert rc == 1
    assert slots.spawned == []
    assert probe.calls == []
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


async def test_agent_start_tolerates_probe_failure_and_proceeds(tmp_path, capsys, monkeypatch):
    """Broker-down probe → warn and spawn anyway (guard is best-effort, §3.5)."""
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe(raises=MeshUnavailableError("down", reason="reader_dead"))

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=probe
    )

    assert rc == 0
    assert slots.spawned == [("assistant", _agent_argv(tmp_path, "assistant"))]
    out = capsys.readouterr().out
    assert "could not verify org-wide duplicates" in out


async def test_agent_start_reuses_injected_live_without_probing(tmp_path, monkeypatch):
    """When ``live`` is threaded in (the bulk sweep) the single-start never re-probes."""
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe(["assistant"])  # must NOT be consulted

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=probe,
        live=[],
    )
    assert rc == 0
    assert probe.calls == []
    assert len(slots.spawned) == 1


# --- reserved-name guard -----------------------------------------------------


@pytest.mark.parametrize("reserved", ["broker", "bridge", "tools"])
async def test_agent_start_single_op_refuses_reserved_name(tmp_path, capsys, monkeypatch, reserved):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start(
        _home(tmp_path), name=reserved, server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )
    assert rc == 1
    assert slots.spawned == []
    assert "reserved component" in capsys.readouterr().out


async def test_agent_start_refuses_mcp_slot_name(tmp_path, capsys, monkeypatch):
    """`mcp-` is a reserved prefix: an agent named mcp-github would be reclassified
    as an MCP slot (excluded from agent sweeps, mis-rendered in status)."""
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start(
        _home(tmp_path), name="mcp-github", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )
    assert rc == 1
    assert slots.spawned == []
    assert "disco mcp start github" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tools", "disco tools stop"),
        ("broker", "disco start"),
        ("bridge", "disco start"),
        ("mcp-github", "disco mcp stop github"),
    ],
)
async def test_agent_stop_refuses_non_agent_slots(tmp_path, capsys, monkeypatch, name, expected):
    """`agent stop tools` must not kill the tools singleton; each refusal names the
    verb that actually manages the slot."""
    slots = _FakeSlots([name])
    slots.install(monkeypatch)

    rc = await roster.agent_stop(_home(tmp_path), name=name, client=_StubClient())

    assert rc == 1
    assert slots.terminated == []
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tools", "disco tools restart"),
        ("broker", "disco start"),
        ("bridge", "disco start"),
        ("mcp-github", "disco mcp restart github"),
    ],
)
async def test_agent_restart_refuses_non_agent_slots(tmp_path, capsys, monkeypatch, name, expected):
    """`agent restart tools` used to kill the singleton and plant a bogus
    `run agent tools` process into its pidfile — refuse at the chokepoint."""
    slots = _FakeSlots([name])
    slots.install(monkeypatch)

    rc = await roster.agent_restart(_home(tmp_path), name=name, client=_StubClient())

    assert rc == 1
    assert slots.terminated == []
    assert slots.spawned == []
    assert expected in capsys.readouterr().out


# --- crash-on-boot confirmation ------------------------------------------------


async def test_agent_start_reports_crash_on_boot_honestly(tmp_path, capsys, monkeypatch):
    """A spawn that exits within the confirmation window is a FAILURE (rc 1) naming
    the slot's log path — never a success line followed by a vanished agent."""
    from calfcord.supervisor.procspawn import log_path_for

    slots = _FakeSlots(launch_dead={"assistant"})
    slots.install(monkeypatch)

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "exited" in out
    assert str(log_path_for(_home(tmp_path), "assistant")) in out
    assert "started" not in out


async def test_agent_restart_reports_crash_on_boot_honestly(tmp_path, capsys, monkeypatch):
    from calfcord.supervisor.procspawn import log_path_for

    slots = _FakeSlots(["assistant"], launch_dead={"assistant"})
    slots.install(monkeypatch)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=_StubClient())
    assert rc == 1
    out = capsys.readouterr().out
    assert "exited" in out
    assert str(log_path_for(_home(tmp_path), "assistant")) in out


# --- broker-reachability gate ----------------------------------------------------


async def test_agent_start_blocked_when_broker_unreachable(tmp_path, capsys, monkeypatch):
    """The old PC slots depended on broker-healthy; the gate re-imposes it so a
    start during a broker bounce fails honestly instead of crash-landing."""
    slots = _FakeSlots(gate_ok=False)
    slots.install(monkeypatch)

    rc = await roster.agent_start(
        _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )
    assert rc == 1
    assert slots.spawned == []
    out = capsys.readouterr().out
    assert "broker not reachable" in out
    assert "disco status" in out


async def test_agent_restart_blocked_when_broker_unreachable(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"], gate_ok=False)
    slots.install(monkeypatch)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=_StubClient())
    assert rc == 1
    assert slots.terminated == []
    assert "broker not reachable" in capsys.readouterr().out


async def test_agent_start_all_gates_the_broker_once(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["a", "b"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=_StubProbe([]),
    )
    assert rc == 0
    assert slots.gate_calls == [_SERVERS]  # once for the sweep, not per id
    assert len(slots.spawned) == 2


async def test_agent_restart_all_blocked_when_broker_unreachable(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"], gate_ok=False)
    slots.install(monkeypatch)

    rc = await roster.agent_restart_all(_home(tmp_path), client=_StubClient())
    assert rc == 1
    assert slots.terminated == []
    assert "broker not reachable" in capsys.readouterr().out


# --- concurrent-start locking -----------------------------------------------------


async def test_agent_start_concurrent_holder_reports_busy_not_double_spawn(tmp_path, capsys, monkeypatch):
    """With another start mid-flight on the same slot, the second start must not
    double-spawn: it reports the in-progress start and exits benignly."""
    slots = _FakeSlots()
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "assistant"):
        rc = await roster.agent_start(
            _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
        )
    assert rc == 0
    assert slots.spawned == []
    assert "already" in capsys.readouterr().out


async def test_agent_start_refused_while_disco_stop_holds_the_lifecycle_lock(tmp_path, capsys, monkeypatch):
    """A spawn must not land behind a concurrent `disco stop` sweep: the shared
    lifecycle lock refuses while start/stop holds it exclusively."""
    from calfcord.supervisor.lifecycle import lifecycle_lock

    slots = _FakeSlots()
    slots.install(monkeypatch)

    with lifecycle_lock(_home(tmp_path)):
        rc = await roster.agent_start(
            _home(tmp_path), name="assistant", server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
        )
    assert rc == 1
    assert slots.spawned == []
    assert "in progress" in capsys.readouterr().out


# --- mcp-prefixed ids in the --all sweep -------------------------------------------


async def test_agent_start_all_filters_mcp_prefixed_ids(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["mcp-github", "assistant"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=_StubProbe([]),
    )
    assert rc == 0
    assert [s for s, _ in slots.spawned] == ["assistant"]
    assert "1 agent(s) processed" in capsys.readouterr().out


# --- agent_stop --------------------------------------------------------------


async def test_agent_stop_happy_path(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["assistant"]
    assert "assistant stopped" in capsys.readouterr().out


async def test_agent_stop_not_running_here(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()  # nothing live → terminate_slot returns None
    slots.install(monkeypatch)

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=_StubClient())

    assert rc == 0
    assert "not running here" in capsys.readouterr().out


async def test_agent_stop_respects_a_concurrent_slot_holder(tmp_path, capsys, monkeypatch):
    """A stop racing a start's confirm window must not unlink the fresh pidfile:
    the slot lock refuses, the stop skips honestly, and nothing is terminated."""
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "assistant"):
        rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=_StubClient())

    assert rc == 0
    assert slots.terminated == []
    assert "another disco command" in capsys.readouterr().out


async def test_agent_stop_refused_while_disco_stop_holds_the_lifecycle_lock(tmp_path, capsys, monkeypatch):
    """Lock ordering holds for stops too: a roster stop must not interleave with a
    `disco start`/`disco stop` holding the lifecycle lock exclusively."""
    from calfcord.supervisor.lifecycle import lifecycle_lock

    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    with lifecycle_lock(_home(tmp_path)):
        rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=_StubClient())

    assert rc == 1
    assert slots.terminated == []
    assert "in progress" in capsys.readouterr().out


async def test_agent_stop_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=_StubClient(workspace_up=False))

    assert rc == 1
    assert slots.terminated == []
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


# --- agent_restart -----------------------------------------------------------


async def test_agent_restart_running_terminates_and_respawns(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["assistant"]
    assert slots.spawned == [("assistant", _agent_argv(tmp_path, "assistant"))]
    assert "assistant restarted" in capsys.readouterr().out


async def test_agent_restart_not_running_still_spawns(tmp_path, capsys, monkeypatch):
    """Restart of a stopped agent brings it up — the expected effect."""
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=_StubClient())

    assert rc == 0
    assert slots.spawned == [("assistant", _agent_argv(tmp_path, "assistant"))]
    assert "assistant restarted" in capsys.readouterr().out


async def test_agent_restart_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=_StubClient(workspace_up=False))

    assert rc == 1
    assert slots.spawned == []
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


# --- agent_start_all ---------------------------------------------------------


async def test_agent_start_all_mixes_running_stopped_and_remote(tmp_path, capsys, monkeypatch):
    """local-running restarts, stopped spawns, remote-only refuses — all successes."""
    slots = _FakeSlots(["running_here"])
    slots.install(monkeypatch)
    probe = _StubProbe(["remote_only"])

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["running_here", "stopped", "remote_only"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=probe,
    )

    assert rc == 0
    assert probe.calls == [_SERVERS]  # probed ONCE
    assert ("stopped", _agent_argv(tmp_path, "stopped")) in slots.spawned
    assert "running_here" in slots.terminated  # restarted in place
    out = capsys.readouterr().out
    assert "start --all: 3 agent(s) processed, 0 failed." in out


async def test_agent_start_all_filters_reserved(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["tools", "broker", "assistant"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=_StubProbe([]),
    )
    assert rc == 0
    assert [s for s, _ in slots.spawned] == ["assistant"]
    assert "1 agent(s) processed" in capsys.readouterr().out


async def test_agent_start_all_all_reserved_is_clean_no_op(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["tools", "broker", "bridge"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=_StubProbe([]),
    )
    assert rc == 0
    assert slots.spawned == []
    assert "no agents defined" in capsys.readouterr().out


async def test_agent_start_all_empty_defined_set(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path), agent_ids=[], server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([])
    )
    assert rc == 0
    assert "no agents defined" in capsys.readouterr().out


async def test_agent_start_all_continues_past_hard_failure_returns_1(tmp_path, capsys, monkeypatch):
    """A raised spawn fault on one id must not abort the sweep; summary is non-zero."""
    slots = _FakeSlots(spawn_error={"boom": OSError("launcher missing")})
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["boom", "ok"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=_StubProbe([]),
    )
    assert rc == 1
    assert ("ok", _agent_argv(tmp_path, "ok")) in slots.spawned
    out = capsys.readouterr().out
    assert "boom: failed to start" in out
    assert "2 agent(s) processed, 1 failed." in out


async def test_agent_start_all_broker_down_warns_once_not_per_id(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe(raises=MeshUnavailableError("down", reason="reader_dead"))

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["a", "b"],
        server_urls=_SERVERS,
        client=_StubClient(),
        probe=probe,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("could not verify org-wide duplicates") == 1
    assert "org-wide duplicate check skipped: broker unreachable" in out


async def test_agent_start_all_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["a"],
        server_urls=_SERVERS,
        client=_StubClient(workspace_up=False),
        probe=_StubProbe([]),
    )
    assert rc == 1
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


# --- agent_stop_all / agent_restart_all --------------------------------------


async def test_agent_stop_all_targets_only_local_agents(tmp_path, capsys, monkeypatch):
    """Sweeps live AGENT pidfiles only — never tools or mcp-<server> slots."""
    slots = _FakeSlots(["assistant", "scribe", "tools", "mcp-github"])
    slots.install(monkeypatch)

    rc = await roster.agent_stop_all(_home(tmp_path), client=_StubClient())

    assert rc == 0
    assert sorted(slots.terminated) == ["assistant", "scribe"]
    assert "stop --all: 2 agent(s) processed, 0 failed." in capsys.readouterr().out


async def test_agent_stop_all_empty(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["tools", "mcp-github"])  # no agents live
    slots.install(monkeypatch)

    rc = await roster.agent_stop_all(_home(tmp_path), client=_StubClient())
    assert rc == 0
    assert "no agents running locally" in capsys.readouterr().out


async def test_agent_stop_all_busy_slot_is_reported_and_benign(tmp_path, capsys, monkeypatch):
    """A busy slot in the sweep (a start's confirm window in flight) is skipped
    with an honest line and does NOT count as a failure — the rest is swept."""
    slots = _FakeSlots(["assistant", "scribe"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "assistant"):
        rc = await roster.agent_stop_all(_home(tmp_path), client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["scribe"]
    out = capsys.readouterr().out
    assert "another disco command" in out
    assert "2 agent(s) processed, 0 failed." in out


async def test_agent_stop_all_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    rc = await roster.agent_stop_all(_home(tmp_path), client=_StubClient(workspace_up=False))
    assert rc == 1
    assert slots.terminated == []
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


async def test_agent_restart_all_targets_only_local_agents(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant", "tools", "mcp-github"])
    slots.install(monkeypatch)

    rc = await roster.agent_restart_all(_home(tmp_path), client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["assistant"]
    assert ("assistant", _agent_argv(tmp_path, "assistant")) in slots.spawned
    assert "restart --all: 1 agent(s) processed, 0 failed." in capsys.readouterr().out


async def test_agent_restart_all_empty(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_restart_all(_home(tmp_path), client=_StubClient())
    assert rc == 0
    assert "no agents running locally" in capsys.readouterr().out


async def test_agent_restart_all_continues_past_failure_returns_1(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["boom", "ok"], spawn_error={"boom": OSError("nope")})
    slots.install(monkeypatch)

    rc = await roster.agent_restart_all(_home(tmp_path), client=_StubClient())
    assert rc == 1
    assert "boom: failed to restart" in capsys.readouterr().out


async def test_agent_restart_all_busy_slot_is_reported_and_benign(tmp_path, capsys, monkeypatch):
    """Sweep alignment: a busy slot in restart --all is benign (matching the
    single restart's busy handling and the other sweeps), never a counted
    failure — the sweep skips it honestly and restarts the rest."""
    slots = _FakeSlots(["assistant", "scribe"])
    slots.install(monkeypatch)

    with _workspace.slot_mutation(_home(tmp_path), "assistant"):
        rc = await roster.agent_restart_all(_home(tmp_path), client=_StubClient())

    assert rc == 0
    assert slots.terminated == ["scribe"]
    assert ("scribe", _agent_argv(tmp_path, "scribe")) in slots.spawned
    out = capsys.readouterr().out
    assert "another disco command" in out
    assert "2 agent(s) processed, 0 failed." in out


async def test_agent_restart_all_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)

    rc = await roster.agent_restart_all(_home(tmp_path), client=_StubClient(workspace_up=False))
    assert rc == 1
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


# --- agent_ps ----------------------------------------------------------------


async def test_agent_ps_workspace_down(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_ps(
        _home(tmp_path), server_urls=_SERVERS, client=_StubClient(workspace_up=False), probe=probe
    )
    assert rc == 0
    assert probe.calls == []  # no local view → no probe
    assert roster._NOT_RUNNING_HINT in capsys.readouterr().out


async def test_agent_ps_union_three_cases(tmp_path, capsys, monkeypatch):
    """physical∩logical=running; physical-only=not-yet-registered; logical-only=other host."""
    slots = _FakeSlots(["here_and_answering", "here_only", "tools"])
    slots.install(monkeypatch)
    probe = _StubProbe(["here_and_answering", "remote_only"])

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=_StubClient(), probe=probe)

    assert rc == 0
    out = capsys.readouterr().out
    assert "here_and_answering" in out and "running" in out
    assert "started, not yet registered" in out  # here_only
    assert "running on another host" in out  # remote_only
    assert "tools" not in out  # not an agent


async def test_agent_ps_empty(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots()
    slots.install(monkeypatch)

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=_StubClient(), probe=_StubProbe([]))
    assert rc == 0
    assert "no agents running in the organization." in capsys.readouterr().out


async def test_agent_ps_tolerates_probe_failure(tmp_path, capsys, monkeypatch):
    slots = _FakeSlots(["assistant"])
    slots.install(monkeypatch)
    probe = _StubProbe(raises=MeshUnavailableError("down", reason="reader_dead"))

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=_StubClient(), probe=probe)
    assert rc == 0
    out = capsys.readouterr().out
    assert "broker unreachable" in out
    assert "assistant" in out  # physical view still shown


# --- default wiring ----------------------------------------------------------


def test_agent_start_defaults_to_per_home_process_compose_client(tmp_path):
    """With no ``client`` injected, the per-home ``ProcessComposeClient`` is built."""
    from calfcord.supervisor.client import ProcessComposeClient
    from calfcord.supervisor.lifecycle import pc_port_for

    home = _home(tmp_path)
    client = roster._resolve_client(None, home)

    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    assert client._base_url == expected._base_url


async def test_default_probe_delegates_to_probe_live_roster(monkeypatch):
    """The default probe adapts ``_probe_live_roster`` to the injectable shape."""
    seen: dict[str, str] = {}

    async def _fake_probe_live_roster(server_urls: str):
        seen["server_urls"] = server_urls
        return ["assistant"]

    monkeypatch.setattr(roster, "_probe_live_roster", _fake_probe_live_roster)

    default_probe = roster._resolve_probe(None)
    result = await default_probe(_SERVERS)

    assert seen["server_urls"] == _SERVERS
    assert result == ["assistant"]


# --- _probe_live_roster body (the native-mesh read) --------------------------


class _ProbeFakeClient:
    """Scriptable stand-in for the short-lived Client ``_probe_live_roster`` opens."""

    def __init__(self, *, agents=None, get_agents_error=None) -> None:
        self._agents = agents or {}
        self._get_agents_error = get_agents_error
        self.mesh = self  # client.mesh.get_agents() resolves back here
        self.aclosed = False

    async def get_agents(self):
        if self._get_agents_error is not None:
            raise self._get_agents_error
        return self._agents

    async def aclose(self) -> None:
        self.aclosed = True


def _patch_probe_client(monkeypatch, fake: _ProbeFakeClient) -> None:
    monkeypatch.setattr(roster.Client, "connect", lambda *a, **k: fake)


async def test_probe_returns_sorted_online_names(monkeypatch):
    fake = _ProbeFakeClient(agents={"n2": SimpleNamespace(name="scribe"), "n1": SimpleNamespace(name="assistant")})
    _patch_probe_client(monkeypatch, fake)
    assert await roster._probe_live_roster(_SERVERS) == ["assistant", "scribe"]
    assert fake.aclosed is True


async def test_probe_empty_readable_roster_returns_empty(monkeypatch):
    fake = _ProbeFakeClient(agents={})
    _patch_probe_client(monkeypatch, fake)
    assert await roster._probe_live_roster(_SERVERS) == []
    assert fake.aclosed is True


@pytest.mark.parametrize(
    "error",
    [
        MeshUnavailableError("establishing", reason="establishing"),
        MeshUnavailableError("no topic yet", reason="open_failed"),
        MeshUnavailableError("reader died", reason="reader_dead"),
        ConnectionError("broker down"),
    ],
    ids=["establishing", "open_failed", "reader_dead", "broker_down"],
)
async def test_probe_propagates_when_roster_unreadable(monkeypatch, error):
    fake = _ProbeFakeClient(get_agents_error=error)
    _patch_probe_client(monkeypatch, fake)
    with pytest.raises(type(error)):
        await roster._probe_live_roster(_SERVERS)
    assert fake.aclosed is True
