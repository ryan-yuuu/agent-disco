"""Unit tests for the substrate lifecycle orchestration (design §13.1-§13.3).

These exercise ``start`` / ``stop`` / ``status`` and their building blocks with
**no real process-compose binary and no broker**: the REST client, the process
spawner, and the clock are all injected. The spawn seam records every argv so the
detached-launch contract (``up -f ... -D -t=false -p <port> -L <log>``) and the
teardown-on-failure (``down -p <port>``) are pinned by test; a stub client drives
the idempotency probe, the priming reconcile (#494), and the readiness gate down
each branch. No wall-clock waits: ``sleep`` advances a fake monotonic clock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from calfcord.supervisor import _workspace, lifecycle, procspawn

# --- fakes ------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock whose only advance is driven by the injected sleep.

    ``start``'s readiness poll measures elapsed time with ``clock()`` and waits
    between polls with ``sleep()``; wiring ``sleep`` to advance this clock makes
    the timeout deterministic with zero real time elapsed.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _RecordingSpawn:
    """Records every argv it is asked to launch (the process spawner seam)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> None:
        self.calls.append(list(argv))


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient.

    ``project_state_results`` / ``bridge_states`` are pulled one per call; a
    ``RuntimeError`` sentinel models the REST server being unreachable (the real
    client raises RuntimeError on a transport failure). ``update_project`` records
    its (byte-exact) body so the priming reconcile can be asserted exactly once.
    """

    def __init__(
        self,
        *,
        project_state_results: list | None = None,
        bridge_states: list | None = None,
        list_processes_result: list | None = None,
        list_processes_raises: Exception | None = None,
        update_project_raises: Exception | None = None,
        process_info: object = None,
        process_info_raises: Exception | None = None,
        restart_process_raises: Exception | None = None,
    ) -> None:
        self._project_state_results = list(project_state_results or [])
        self._bridge_states = list(bridge_states or [])
        self._list_processes_result = list_processes_result or []
        # When set, the process-list read fails the way the real client signals a
        # transport error (the supervisor died between two REST calls): a raise.
        self._list_processes_raises = list_processes_raises
        # When set, the priming reconcile (the buggy first project-update) fails the
        # way the real client signals a PC reconcile / transport error: a raise.
        self._update_project_raises = update_project_raises
        # The declared config the idempotency home-ownership check reads back. A
        # real `get_process_info` returns a process config that embeds the
        # home-specific log path; the check confirms the answering supervisor is
        # THIS home's. `process_info_raises` models the info route being
        # unavailable (the verdict is then "cannot determine").
        self._process_info = process_info
        self._process_info_raises = process_info_raises
        # When set, `restart_process` fails the way the real client signals a
        # transport / non-2xx error: a raise (ProcessComposeError <: RuntimeError).
        self._restart_process_raises = restart_process_raises
        self.update_project_calls: list[str] = []
        self.project_state_call_count = 0
        self.get_process_calls: list[str] = []
        self.get_process_info_calls: list[str] = []
        self.restart_process_calls: list[str] = []

    async def project_state(self):
        self.project_state_call_count += 1
        if not self._project_state_results:
            raise RuntimeError("project_state: connection refused")
        result = self._project_state_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def update_project(self, yaml_text: str):
        self.update_project_calls.append(yaml_text)
        if self._update_project_raises is not None:
            raise self._update_project_raises
        return {}

    async def get_process(self, name: str):
        self.get_process_calls.append(name)
        if not self._bridge_states:
            raise RuntimeError("get_process: connection refused")
        result = self._bridge_states.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def get_process_info(self, name: str):
        self.get_process_info_calls.append(name)
        if self._process_info_raises is not None:
            raise self._process_info_raises
        return self._process_info

    async def restart_process(self, name: str):
        self.restart_process_calls.append(name)
        if self._restart_process_raises is not None:
            raise self._restart_process_raises
        return {}

    async def list_processes(self):
        if self._list_processes_raises is not None:
            raise self._list_processes_raises
        return self._list_processes_result


async def _reachable_broker() -> bool:
    """A broker probe that always reports reachable, so start's §13.2 fast-fail
    precondition passes without touching a real broker."""
    return True


def _home(tmp_path: Path) -> str:
    return str(tmp_path)


def _define_agent(tmp_path: Path, name: str = "assistant") -> None:
    """Drop an ``agents/<name>.md`` under the test home.

    Phase 3 dropped ``start``'s ``agent_ids`` param: the success banners' create-
    vs-start signpost now reads the home's agents dir directly (a glob — the file
    is never parsed), so tests that want the "agent start" steer define one.
    """
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n")


@pytest.fixture(autouse=True)
def _no_agents_dir_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signpost honours $CALFKIT_AGENTS_DIR; keep the host env out of tests."""
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)


class _StubProbe:
    """A scriptable mesh probe for status (returns agent NAMES or raises)."""

    def __init__(self, names: list[str] | None = None, *, raises: Exception | None = None) -> None:
        self._names = list(names or [])
        self._raises = raises
        self.calls: list[str] = []

    async def __call__(self, server_urls: str) -> list[str]:
        self.calls.append(server_urls)
        if self._raises is not None:
            raise self._raises
        return list(self._names)


def _write_self_pidfile(home, slot: str) -> Path:
    """Write a pidfile naming THIS (alive, ours) process for ``slot`` — safe to scan,
    NEVER pass to a terminate path (it would signal the test runner)."""
    record = procspawn._identity_for(os.getpid(), ("self",))
    pidfile = procspawn.pidfile_for(home, slot)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps(procspawn._record_to_dict(record)), encoding="utf-8")
    return pidfile


def _write_stale_pidfile(home, slot: str) -> Path:
    """Write a pidfile naming a reaped (dead) pid for ``slot``."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pidfile = procspawn.pidfile_for(home, slot)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(
        json.dumps({"v": 1, "pid": proc.pid, "argv": ["gone"], "start_token": "x", "spawn_ts": 0.0, "argv_hash": "x"}),
        encoding="utf-8",
    )
    return pidfile


def _write_not_ours_pidfile(home, slot: str) -> Path:
    """Write a pidfile naming an alive-but-NOT-ours pid (init, pid 1) — a recycled-pid
    stale file. Safe: terminate refuses to signal a pid it cannot prove is ours."""
    pidfile = procspawn.pidfile_for(home, slot)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(
        json.dumps({"v": 1, "pid": 1, "argv": ["init"], "start_token": "bogus", "spawn_ts": 0.0, "argv_hash": "x"}),
        encoding="utf-8",
    )
    return pidfile


def _make_fake_binary(path: Path) -> str:
    """An executable file standing in for the process-compose binary."""
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake_pc_bin(tmp_path, monkeypatch) -> str:
    """Point binary resolution at a dummy executable so start/stop never touch a
    real process-compose; the spawn seam is injected so it is never executed."""
    binary = _make_fake_binary(tmp_path / "fake-process-compose")
    monkeypatch.setenv("CALFCORD_PROCESS_COMPOSE_BIN", binary)
    return binary


# --- resolve_pc_binary ------------------------------------------------------


def test_resolve_pc_binary_prefers_explicit_env(tmp_path, monkeypatch) -> None:
    explicit = _make_fake_binary(tmp_path / "custom-pc")
    monkeypatch.setenv("CALFCORD_PROCESS_COMPOSE_BIN", explicit)
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    assert lifecycle.resolve_pc_binary() == explicit


def test_resolve_pc_binary_falls_back_to_home_bin(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CALFCORD_PROCESS_COMPOSE_BIN", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pc = bin_dir / "process-compose"
    pc.write_text("")
    pc.chmod(0o755)
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    assert lifecycle.resolve_pc_binary() == str(pc)


def test_resolve_pc_binary_falls_back_to_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CALFCORD_PROCESS_COMPOSE_BIN", raising=False)
    # No $CALFCORD_HOME/bin binary; a process-compose on PATH must win.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    on_path = tmp_path / "pathdir" / "process-compose"
    on_path.parent.mkdir()
    on_path.write_text("")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert lifecycle.resolve_pc_binary() == str(on_path)


def test_resolve_pc_binary_raises_actionable_when_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CALFCORD_PROCESS_COMPOSE_BIN", raising=False)
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError) as excinfo:
        lifecycle.resolve_pc_binary()
    message = str(excinfo.value)
    assert "process-compose" in message
    # Actionable: point the user back at the installer to recover.
    assert "install" in message.lower()


# --- pc_port_for ------------------------------------------------------------


def test_pc_port_for_is_deterministic() -> None:
    home = "/srv/calfcord"
    assert lifecycle.pc_port_for(home) == lifecycle.pc_port_for(home)


def test_pc_port_for_differs_per_home() -> None:
    # Two installs on one host must not collide on the default :8080.
    assert lifecycle.pc_port_for("/srv/one") != lifecycle.pc_port_for("/srv/two")


def test_pc_port_for_is_in_documented_high_range() -> None:
    for home in ("/a", "/b/c", "/srv/calfcord", "/Users/x/.agent-disco"):
        port = lifecycle.pc_port_for(home)
        assert lifecycle._PORT_RANGE_START <= port <= lifecycle._PORT_RANGE_END
        # Never the supervisor default, which is the whole point of deriving it.
        assert port != 8080


def test_pc_port_for_uses_absolute_path(tmp_path, monkeypatch) -> None:
    # The same install reached via a relative vs absolute path must hash the same,
    # so a CWD-relative invocation does not pick a different port.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "home").mkdir()
    abs_port = lifecycle.pc_port_for(str(tmp_path / "home"))
    rel_port = lifecycle.pc_port_for("home")
    assert abs_port == rel_port


# --- lockfile guard ---------------------------------------------------------


def test_lock_guard_creates_parent_and_acquires(tmp_path) -> None:
    home = _home(tmp_path)
    with lifecycle.lifecycle_lock(home):
        assert (tmp_path / "state").is_dir()


def _acquire_and_release(home: str) -> None:
    """Acquire the lifecycle lock and immediately release it (a single call so a
    contention test can assert the *second* acquire raises without nesting two
    ``with`` blocks)."""
    with lifecycle.lifecycle_lock(home):
        pass


def test_lock_guard_raises_on_contention(tmp_path) -> None:
    home = _home(tmp_path)
    with lifecycle.lifecycle_lock(home), pytest.raises(RuntimeError) as excinfo:
        _acquire_and_release(home)
    assert "in progress" in str(excinfo.value)


def test_lock_guard_releases_after_exit(tmp_path) -> None:
    home = _home(tmp_path)
    with lifecycle.lifecycle_lock(home):
        pass
    # A second acquire after the first releases must succeed (no leaked lock).
    with lifecycle.lifecycle_lock(home):
        pass


# --- start: idempotency -----------------------------------------------------


async def test_start_already_open_restarts_the_bridge(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    _define_agent(tmp_path)  # a defined roster => the "agent start" next-step signpost
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    # project_state succeeds on the first probe => supervisor already up, and the
    # declared config embeds THIS home's marker path, so the home-ownership check
    # (Fix #11) confirms the answering supervisor is ours. The already-open path
    # then cycles the bridge in place (picking up a new build/config or clearing a
    # wedged reader) WITHOUT relaunching the supervisor (§12.4).
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(home, "state", "logs", "bridge.log")},
        bridge_states=[{"is_ready": "Ready", "pid": 100}, {"is_ready": "Ready", "pid": 200}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
    )

    assert code == 0
    assert spawn.calls == []  # no second `up` — supervisor not relaunched
    assert client.update_project_calls == []  # no reconcile when already open
    assert client.restart_process_calls == ["bridge"]  # bridge cycled in place
    out = capsys.readouterr().out.lower()
    assert "already open" in out
    assert "restarted" in out
    assert "disco agent start" in out  # the defined-roster next-step signpost


async def test_start_idempotency_rejects_a_different_home_on_a_colliding_port(
    tmp_path, capsys
) -> None:
    # Fix #11: two homes can hash to the same REST port. The idempotency probe
    # verifies only that SOMETHING answers the port, not WHICH home's supervisor —
    # so install B would see install A's supervisor and skip its own launch. Before
    # trusting an "already up" verdict, confirm the answering supervisor is THIS
    # home's via its declared home-specific paths; a DIFFERENT home must fail
    # loudly, never return a false "already open".
    home = _home(tmp_path / "B")
    other_home = str(tmp_path / "A")
    spawn = _RecordingSpawn()
    # The supervisor answers (port collision), but its declared config embeds
    # ANOTHER home's path — it belongs to install A, not B.
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={
            "log_location": os.path.join(other_home, "state", "logs", "bridge.log")
        },
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=_FakeClock(),
        broker_probe=_reachable_broker,
    )

    assert code == 1
    # It must NOT have launched a second supervisor, restarted the other install's
    # bridge, or claimed "already open".
    assert spawn.calls == []
    assert client.restart_process_calls == []
    out = capsys.readouterr().out.lower()
    assert "already open" not in out
    assert "error" in out
    # Actionable: it names the port collision so the operator can re-home / repick.
    assert "port" in out


# --- bridge restart: the shared mechanism + the `disco bridge restart` verb --


def test_process_pid_extracts_int_pid_or_none() -> None:
    """The pid extractor used by the restart gate: an int pid, or None for a state
    that lacks one / is malformed (which degrades the gate to a plain readiness check)."""
    assert lifecycle._process_pid({"pid": 42, "is_ready": "Ready"}) == 42
    assert lifecycle._process_pid({"is_ready": "Ready"}) is None  # no pid key
    assert lifecycle._process_pid({"pid": "42"}) is None  # non-int pid
    assert lifecycle._process_pid(None) is None  # not a dict


async def test_restart_bridge_to_ready_restarts_then_confirms_new_instance_ready(tmp_path) -> None:
    """The shared mechanism restarts the bridge then returns None once the NEW
    instance (a CHANGED pid) reports Ready."""
    clock = _FakeClock()
    # pre-restart pid read (pid 100), then the new instance is Ready under pid 200.
    client = _StubClient(bridge_states=[{"is_ready": "Ready", "pid": 100}, {"is_ready": "Ready", "pid": 200}])

    reason = await lifecycle._restart_bridge_to_ready(
        client, _home(tmp_path), ready_timeout_s=90, clock=clock, sleep=clock.sleep
    )

    assert reason is None
    assert client.restart_process_calls == ["bridge"]
    # one pre-restart read (to capture the old pid) + one readiness poll.
    assert client.get_process_calls == ["bridge", "bridge"]


async def test_restart_bridge_to_ready_surfaces_the_restart_call_failure(tmp_path) -> None:
    """A failed restart REST call returns a message naming the ACTUAL cause (HTTP
    status + PC reason), not a misleading 'not ready', and never polls a bridge it
    did not restart."""
    clock = _FakeClock()
    client = _StubClient(
        bridge_states=[{"is_ready": "Ready", "pid": 100}],  # the pre-restart pid read
        restart_process_raises=RuntimeError("restart: HTTP 503 process not running"),
    )

    reason = await lifecycle._restart_bridge_to_ready(
        client, _home(tmp_path), ready_timeout_s=90, clock=clock, sleep=clock.sleep
    )

    assert reason is not None
    assert "restart request failed" in reason
    assert "503" in reason  # the ProcessComposeError detail is surfaced, not dropped
    assert client.restart_process_calls == ["bridge"]
    # only the pre-restart pid read; NO readiness poll after a failed restart.
    assert client.get_process_calls == ["bridge"]


async def test_restart_bridge_to_ready_times_out_when_bridge_never_becomes_ready(tmp_path) -> None:
    """Restart succeeds but the new instance never reaches Ready → a non-None
    'didn't come back ready' message, only AFTER the readiness budget is spent."""
    clock = _FakeClock()
    # one pre-restart pid read, then every readiness poll signals not-ready.
    client = _StubClient(bridge_states=[{"is_ready": "Ready", "pid": 100}])

    reason = await lifecycle._restart_bridge_to_ready(
        client, _home(tmp_path), ready_timeout_s=4, clock=clock, sleep=clock.sleep
    )

    assert reason is not None
    assert "didn't come back ready" in reason
    assert client.restart_process_calls == ["bridge"]
    assert clock() >= 4  # the budget was actually spent polling, not a premature give-up


async def test_restart_bridge_to_ready_waits_past_a_stale_pre_restart_ready(tmp_path) -> None:
    """The gate must not latch the pre-restart instance's stale Ready: it waits until
    a CHANGED pid reports Ready — guarding the §12.6 stale-Ready race (a real Process
    Compose behaviour: after POST /restart the old pid can keep reading Ready for a
    beat off a not-yet-stale heartbeat)."""
    clock = _FakeClock()
    client = _StubClient(
        bridge_states=[
            {"is_ready": "Ready", "pid": 100},  # pre-restart pid read
            {"is_ready": "Ready", "pid": 100},  # stale: same pid still shows Ready
            {"is_ready": "Ready", "pid": 200},  # the NEW instance is finally Ready
        ]
    )

    reason = await lifecycle._restart_bridge_to_ready(
        client, _home(tmp_path), ready_timeout_s=90, clock=clock, sleep=clock.sleep
    )

    assert reason is None
    # pid read + the stale poll (rejected) + the fresh poll (accepted): it did NOT
    # return on the stale Ready.
    assert client.get_process_calls == ["bridge", "bridge", "bridge"]


async def test_restart_bridge_to_ready_diagnoses_a_config_failure_from_the_bridge_log(tmp_path) -> None:
    """On a readiness timeout the mechanism reuses the cold-start diagnosis: a known
    bridge.log signature yields a cause-specific message (here: a rejected token →
    'disco init'), not the generic 'check disco logs bridge'."""
    home = _home(tmp_path)
    _write_bridge_log(home, _LOGIN_FAILURE_TRACEBACK)
    clock = _FakeClock()
    client = _StubClient(bridge_states=[{"is_ready": "Ready", "pid": 100}])  # never a new-pid Ready

    reason = await lifecycle._restart_bridge_to_ready(
        client, home, ready_timeout_s=4, clock=clock, sleep=clock.sleep
    )

    assert reason is not None
    assert "disco init" in reason  # the cause-specific diagnosis, not the generic hint
    assert "didn't come back ready" not in reason


async def test_restart_bridge_to_ready_degrades_to_plain_gate_when_pre_pid_unreadable(tmp_path) -> None:
    """If the pre-restart pid can't be read (get_process raises), the gate degrades
    to a plain readiness check and still SUCCEEDS on the next Ready — it must not
    hang waiting for a pid change it can never compute against a None baseline."""
    clock = _FakeClock()
    # The pre-restart pid read raises; then the bridge reports Ready.
    client = _StubClient(bridge_states=[RuntimeError("get_process: refused"), {"is_ready": "Ready", "pid": 200}])

    reason = await lifecycle._restart_bridge_to_ready(
        client, _home(tmp_path), ready_timeout_s=90, clock=clock, sleep=clock.sleep
    )

    assert reason is None  # accepted the first Ready despite an unknown pre-restart pid
    assert client.restart_process_calls == ["bridge"]


async def test_restart_bridge_reports_ok_when_the_bridge_comes_back_ready(tmp_path, capsys) -> None:
    """``disco bridge restart`` on an open workspace restarts the bridge, waits for
    Ready, and returns 0 with a plain confirmation."""
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(home, "state", "logs", "bridge.log")},
        bridge_states=[{"is_ready": "Ready", "pid": 100}, {"is_ready": "Ready", "pid": 200}],
    )

    code = await lifecycle.restart_bridge(home, client=client, clock=clock, sleep=clock.sleep)

    assert code == 0
    assert client.restart_process_calls == ["bridge"]
    assert "bridge restarted" in capsys.readouterr().out.lower()


async def test_restart_bridge_proceeds_when_ownership_is_undeterminable(tmp_path, capsys) -> None:
    """When the info route is unavailable (``_supervisor_belongs_to_home`` -> None),
    the verb keeps the best-effort 'it's ours' behaviour and DOES restart. Guards
    the documented transient: a refactor flipping ``is False`` to a falsy/``not True``
    check would silently turn the info-route-unavailable case into a refusal."""
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info=None,  # info route unavailable => belongs_to_home returns None
        bridge_states=[{"is_ready": "Ready", "pid": 100}, {"is_ready": "Ready", "pid": 200}],
    )

    code = await lifecycle.restart_bridge(home, client=client, clock=clock, sleep=clock.sleep)

    assert code == 0
    assert client.restart_process_calls == ["bridge"]
    assert "bridge restarted" in capsys.readouterr().out.lower()


async def test_restart_bridge_refuses_when_the_workspace_is_closed(tmp_path, capsys) -> None:
    """With no supervisor answering, there is no bridge to restart: refuse with a
    one-line error that names ``disco start``, and never issue a restart."""
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(project_state_results=[])  # project_state raises => not up

    code = await lifecycle.restart_bridge(home, client=client, clock=clock, sleep=clock.sleep)

    assert code == 1
    assert client.restart_process_calls == []
    out = capsys.readouterr().out.lower()
    assert "error" in out
    assert "disco start" in out


async def test_restart_bridge_rejects_a_different_home_on_a_colliding_port(tmp_path, capsys) -> None:
    """A supervisor answering this home's REST port but belonging to ANOTHER
    install must not have its bridge restarted from here — fail loud, no restart."""
    home = _home(tmp_path / "B")
    other_home = str(tmp_path / "A")
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(other_home, "state", "logs", "bridge.log")},
    )

    code = await lifecycle.restart_bridge(home, client=client, clock=clock, sleep=clock.sleep)

    assert code == 1
    assert client.restart_process_calls == []
    out = capsys.readouterr().out.lower()
    assert "error" in out
    assert "port" in out


async def test_restart_bridge_fails_fast_when_the_bridge_does_not_come_back(tmp_path, capsys) -> None:
    """Honest fail-fast: a restarted bridge that never reaches Ready returns 1 and
    points at the logs — never a green light that lies (§12.6)."""
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(home, "state", "logs", "bridge.log")},
        bridge_states=[],  # never ready
    )

    code = await lifecycle.restart_bridge(
        home, client=client, clock=clock, sleep=clock.sleep, ready_timeout_s=4
    )

    assert code == 1
    assert client.restart_process_calls == ["bridge"]
    out = capsys.readouterr().out.lower()
    assert "error" in out
    assert "logs bridge" in out


async def test_restart_bridge_contended_lock_is_a_clean_error_not_a_traceback(tmp_path, capsys) -> None:
    """A concurrent lifecycle verb holding the lock makes ``disco bridge restart``
    a clean one-line refusal, not a raw traceback — and nothing is restarted."""
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(project_state_results=[{"running": True}])

    with _workspace.slot_mutation(home, "assistant"):
        # Inject the fake clock/sleep so that if the lock ever failed to contend, the
        # flow would fall through to the readiness wait and fail FAST here rather than
        # hang ~90s of real time before the assertion catches the regression.
        code = await lifecycle.restart_bridge(home, client=client, clock=clock, sleep=clock.sleep)

    assert code == 1
    assert client.restart_process_calls == []
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "in progress" in out


# --- home-ownership match (Fix #11 + the round-2 anchoring) -----------------


async def test_supervisor_belongs_to_home_rejects_a_suffix_home_collision() -> None:
    # The crux the quote-anchored match exists for: a bare-substring scan would
    # find "/calf/state" INSIDE "/data/calf/state/logs/bridge.log" and wrongly
    # claim install A's colliding supervisor is ours. The anchored match requires
    # the marker to OPEN the quoted path value, so a suffix home is rejected
    # (False) — while the genuine same-home supervisor is still recognised (True).
    # A revert to the bare-substring scan flips the first assertion, so this pins
    # the fix (the prior different-home test used non-suffix sibling paths, which
    # both the old and new code reject identically).
    other = _StubClient(process_info={"log_location": "/data/calf/state/logs/bridge.log"})
    assert await lifecycle._supervisor_belongs_to_home(other, "/calf") is False
    same = _StubClient(process_info={"log_location": "/calf/state/logs/bridge.log"})
    assert await lifecycle._supervisor_belongs_to_home(same, "/calf") is True


async def test_supervisor_belongs_to_home_returns_none_when_info_unavailable() -> None:
    # Best-effort: when the info route is unreachable or empty the verdict is
    # "cannot determine" (None), so the caller keeps the prior idempotent
    # "already open" behaviour rather than failing a legitimate restart loudly.
    raising = _StubClient(process_info_raises=RuntimeError("info route unavailable"))
    assert await lifecycle._supervisor_belongs_to_home(raising, "/h") is None
    empty = _StubClient(process_info=None)
    assert await lifecycle._supervisor_belongs_to_home(empty, "/h") is None


# --- start: happy path ------------------------------------------------------


async def test_start_manifest_is_substrate_only(tmp_path, capsys, fake_pc_bin) -> None:
    """Even with agents DEFINED for the install, the written project declares
    ONLY the substrate — the roster (agents, ``tools``, ``mcp-<server>``) is
    spawned off Process Compose."""
    import yaml as _yaml

    _define_agent(tmp_path)
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[
            RuntimeError("not up yet"),
            {"running": True},
        ],
        bridge_states=[{"status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 0
    project = _yaml.safe_load((tmp_path / "state" / "process-compose.yaml").read_text())
    assert set(project["processes"]) == {"broker", "bridge"}



async def test_start_happy_path(tmp_path, capsys, fake_pc_bin) -> None:
    _define_agent(tmp_path)  # a defined roster steers the banner to `agent start`
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    # First probe: not up (RuntimeError) -> launch. Then the REST server answers,
    # and the bridge reports Ready on the first readiness poll.
    client = _StubClient(
        project_state_results=[
            RuntimeError("not up yet"),
            {"running": True},
        ],
        bridge_states=[{"status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 0

    # The rendered YAML is written to <home>/state/process-compose.yaml.
    yaml_path = tmp_path / "state" / "process-compose.yaml"
    assert yaml_path.is_file()

    # Exactly one detached `up` with the §13.2 flags.
    assert len(spawn.calls) == 1
    argv = spawn.calls[0]
    assert argv[1] == "up"
    assert "-D" in argv
    assert "-t=false" in argv
    assert "-p" in argv
    port_value = argv[argv.index("-p") + 1]
    assert int(port_value) == lifecycle.pc_port_for(home)
    assert "-L" in argv
    log_value = argv[argv.index("-L") + 1]
    assert log_value == str(tmp_path / "state" / "logs" / "process-compose.log")
    assert "-f" in argv
    f_value = argv[argv.index("-f") + 1]
    assert f_value == str(yaml_path)
    # NEVER --no-server (it would kill the REST API the readiness gate needs).
    assert "--no-server" not in argv

    # Priming reconcile (#494): update_project called EXACTLY once, byte-identical
    # to the YAML on disk.
    assert len(client.update_project_calls) == 1
    assert client.update_project_calls[0] == yaml_path.read_text()

    # Readiness gate actually polled the bridge.
    assert client.get_process_calls == ["bridge"]

    # Success banner ALWAYS names the next step (§12.6).
    out = capsys.readouterr().out
    assert "agent start" in out


async def test_start_zero_agents_signposts_create_not_start(tmp_path, capsys, fake_pc_bin) -> None:
    """When no agents are DEFINED yet (the home's agents dir is empty — ``start``
    reads it directly for the signpost), the fresh-start success must point at
    ``disco agent create`` (there is nothing to ``agent start``) rather than the
    unconditional ``agent start`` steer — the empty-roster teaching moment."""
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up yet"), {"running": True}],
        bridge_states=[{"status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "disco agent create <name>" in out
    assert "agent start" not in out


async def test_start_already_open_zero_agents_signposts_create(tmp_path, capsys) -> None:
    """The already-open path also steers to ``disco agent create`` when no agents
    are defined, instead of the ``agent start`` next-step — and still restarts the
    bridge in place."""
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(home, "state", "logs", "bridge.log")},
        bridge_states=[{"is_ready": "Ready", "pid": 100}, {"is_ready": "Ready", "pid": 200}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
    )

    assert code == 0
    assert spawn.calls == []
    assert client.restart_process_calls == ["bridge"]
    out = capsys.readouterr().out
    assert "already open" in out.lower()
    assert "disco agent create <name>" in out
    assert "agent start" not in out


async def test_start_already_open_fails_fast_when_the_bridge_wont_come_back(tmp_path, capsys) -> None:
    """Honest fail-fast (§12.6): if the in-place bridge restart doesn't reach Ready,
    ``start`` returns 1 and points at the logs rather than printing a green "already
    open" banner — and never relaunches the supervisor."""
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(home, "state", "logs", "bridge.log")},
        bridge_states=[],  # restarted, but never reaches Ready
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        ready_timeout_s=4,
    )

    assert code == 1
    assert spawn.calls == []  # supervisor NOT relaunched
    assert client.restart_process_calls == ["bridge"]
    out = capsys.readouterr().out.lower()
    assert "already open" not in out
    assert "didn't come back ready" in out
    assert "logs bridge" in out


async def test_start_already_open_restarts_when_ownership_is_undeterminable(tmp_path, capsys) -> None:
    """The start already-open branch mirrors restart_bridge: an undeterminable owner
    (``_supervisor_belongs_to_home`` -> None, the info route unavailable) proceeds to
    restart the bridge in place, not refuse — the same best-effort 'it's ours' path."""
    home = _home(tmp_path)
    _define_agent(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info=None,  # belongs_to_home -> None (info route unavailable)
        bridge_states=[{"is_ready": "Ready", "pid": 100}, {"is_ready": "Ready", "pid": 200}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
    )

    assert code == 0
    assert spawn.calls == []
    assert client.restart_process_calls == ["bridge"]
    assert "bridge restarted" in capsys.readouterr().out.lower()


async def test_start_signpost_honours_agents_dir_env_override(
    tmp_path, capsys, fake_pc_bin, monkeypatch
) -> None:
    """The signpost's agents-dir read honours ``$CALFKIT_AGENTS_DIR`` — the same
    override the shim, the runners, and ``init.resolve_paths`` honour — so an
    install with a pinned agents dir is not misread as an empty org."""
    override = tmp_path / "elsewhere"
    override.mkdir()
    (override / "scribe.md").write_text("---\nname: scribe\n---\nbody\n")
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(override))

    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up yet"), {"running": True}],
        bridge_states=[{"status": "Running", "is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "disco agent start <name>" in out
    assert "agent create" not in out


async def test_start_log_dir_is_created(tmp_path, fake_pc_bin) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"is_ready": "Ready"}],
    )
    await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert (tmp_path / "state" / "logs").is_dir()


async def test_start_waits_for_rest_server_then_primes(tmp_path, fake_pc_bin) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    # First probe: not up. Then two transport errors (server still booting) before
    # the REST server answers; only then does the priming reconcile run.
    client = _StubClient(
        project_state_results=[
            RuntimeError("not up"),
            RuntimeError("booting"),
            RuntimeError("booting"),
            {"running": True},
        ],
        bridge_states=[{"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code == 0
    assert len(client.update_project_calls) == 1


# --- start: broker fast-fail precondition (§13.2) ---------------------------


async def test_start_fails_fast_when_external_broker_unreachable(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # §13.2: for an EXTERNAL (non-loopback) broker the fast-fail precondition
    # stands. If the operator's remote broker is unreachable, start must bail
    # BEFORE rendering/launching — no `up`, no supervisor — so it fails in a
    # heartbeat instead of after a 90s bridge-readiness timeout. (A compose-managed
    # loopback broker is exempt: it is launched by `up` itself, see the test below.)
    home = _home(tmp_path)
    spawn = _RecordingSpawn()

    async def _unreachable() -> bool:
        return False

    # The supervisor is not up (so we pass the idempotency short-circuit), but the
    # broker probe reports unreachable.
    client = _StubClient(project_state_results=[RuntimeError("not up")])

    code = await lifecycle.start(
        home,
        server_urls="broker.example.com:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=_FakeClock(),
        broker_probe=_unreachable,
    )

    assert code == 1
    # Nothing was launched: no `up`, no `down`, no priming reconcile.
    assert spawn.calls == []
    assert client.update_project_calls == []
    # The rendered YAML must NOT have been written (we bail before rendering).
    assert not (tmp_path / "state" / "process-compose.yaml").exists()
    # Actionable error: names the unreachable broker and how to start it.
    out = capsys.readouterr().out.lower()
    assert "broker" in out
    assert "broker.example.com:9092" in out
    assert "disco broker" in out


async def test_start_skips_broker_probe_for_compose_managed_loopback_broker(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # The cold-start regression (P0): on a fresh machine the broker is a
    # compose-managed AUTOSTART process that `up` itself launches. A pre-launch
    # probe would fast-fail the very broker we are about to start, so `start` must
    # SKIP the probe for a loopback URL and proceed to `up` — even when the broker
    # is (as it always is cold) not yet reachable. Here the injected probe reports
    # unreachable, yet start must still render + spawn `up`.
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    probe_calls = {"n": 0}

    async def _unreachable() -> bool:
        probe_calls["n"] += 1
        return False

    client = _StubClient(
        project_state_results=[RuntimeError("not up yet"), {"running": True}],
        bridge_states=[{"is_ready": "Ready"}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_unreachable,
    )

    assert code == 0
    # The pre-launch probe was NOT consulted for a compose-managed broker.
    assert probe_calls["n"] == 0
    # `up` was launched despite the (cold) broker being unreachable.
    assert len(spawn.calls) == 1
    assert spawn.calls[0][1] == "up"


async def test_start_external_broker_manifest_omits_broker_slot(
    tmp_path, fake_pc_bin
) -> None:
    # An external-broker install renders a manifest WITHOUT a local broker process
    # (starting an ephemeral broker nobody talks to is wrong) — the probe governs
    # inclusion and probe-skipping on the same predicate so they cannot diverge.
    import yaml as _yaml

    home = _home(tmp_path)
    clock = _FakeClock()

    async def _reachable() -> bool:
        return True

    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="broker.example.com:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable,
    )
    assert code == 0
    project = _yaml.safe_load((tmp_path / "state" / "process-compose.yaml").read_text())
    procs = project["processes"]
    assert "broker" not in procs
    assert "depends_on" not in procs["bridge"]


# --- start: readiness timeout -> teardown -> non-zero -----------------------


async def test_start_readiness_timeout_tears_down_and_returns_nonzero(
    tmp_path, capsys, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    spawn_blocking = _RecordingSpawn()
    clock = _FakeClock()
    # Server comes up, prime succeeds, but the bridge never becomes Ready: every
    # readiness poll reports Pending until the timeout budget is spent.
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Pending", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        spawn_blocking=spawn_blocking,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code != 0

    # The detached `spawn` launched the `up` but NOT the teardown: a racy
    # fire-and-forget `down` could let a later `start` collide with a supervisor
    # still shutting down, so teardown must use the BLOCKING seam (§13.3 / Fix 3).
    assert all("down" not in c for c in spawn.calls)
    # Teardown: exactly one blocking `down -p <port>`.
    down_calls = [c for c in spawn_blocking.calls if "down" in c]
    assert len(down_calls) == 1
    down = down_calls[0]
    assert "-p" in down
    assert int(down[down.index("-p") + 1]) == lifecycle.pc_port_for(home)

    # The specific failure is printed (no green-light-that-lies, §12.6).
    out = capsys.readouterr().out
    assert "bridge" in out.lower()


async def test_start_running_but_not_ready_bridge_times_out_and_tears_down(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # The bridge process is Running (status=="Running") but its readiness probe
    # has NOT passed (is_ready=="Not Ready"). The strict gate (§13.3) must treat
    # this as a green-light-that-lies: poll until the budget is spent, tear the
    # substrate down, and return non-zero — NEVER accept Running alone.
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Running", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code != 0
    # The bridge was polled (it never went Ready) and a teardown was issued.
    assert client.get_process_calls, "the readiness gate must actually poll the bridge"
    out = capsys.readouterr().out.lower()
    assert "bridge" in out


# --- start-failure diagnosis (bridge-log signature matching) ----------------


# A trimmed but faithful discord.py PrivilegedIntentsRequired traceback, of the
# shape process-compose captures into <home>/state/logs/bridge.log when the
# Message Content portal toggle is off.
_PRIVILEGED_INTENTS_TRACEBACK = """\
Traceback (most recent call last):
  File "/h/.venv/lib/python3.13/site-packages/discord/client.py", line 700, in connect
    await self.ws.poll_event()
discord.errors.PrivilegedIntentsRequired: Shard ID None is requesting privileged \
intents that have not been explicitly enabled in the developer portal. \
It is recommended to go to https://discord.com/developers/applications/ and \
explicitly enable the privileged intents within your application's page. \
If this is not possible, then consider disabling the privileged intents instead.
"""

# discord.py's LoginFailure on a bad token.
_LOGIN_FAILURE_TRACEBACK = """\
Traceback (most recent call last):
  File "/h/.venv/lib/python3.13/site-packages/discord/client.py", line 600, in login
    data = await self.http.static_login(token.strip())
discord.errors.LoginFailure: Improper token has been passed.
"""


def _write_bridge_log(home: str, contents: str) -> str:
    """Write a fake bridge per-process log at <home>/state/logs/bridge.log."""
    logs_dir = os.path.join(home, "state", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    Path(os.path.join(logs_dir, "bridge.log")).write_text(contents, encoding="utf-8")
    return logs_dir


def test_diagnose_start_failure_names_message_content_for_privileged_intents(
    tmp_path,
) -> None:
    # The single most common first-run miss: the Message Content portal toggle is
    # off, so the bridge dies with PrivilegedIntentsRequired. The diagnosis must
    # name the EXACT fix (Message Content specifically — it is the only requested
    # privileged intent), not a generic "intents are off".
    logs_dir = _write_bridge_log(_home(tmp_path), _PRIVILEGED_INTENTS_TRACEBACK)
    msg = lifecycle._diagnose_start_failure(logs_dir)
    assert msg is not None
    assert "Message Content" in msg
    assert "Privileged Gateway Intents" in msg
    assert "disco start" in msg


def test_diagnose_start_failure_names_token_for_login_failure(tmp_path) -> None:
    # A rejected bot token surfaces as discord.py LoginFailure ("Improper token").
    # The diagnosis must point at re-running `disco init` to re-enter it.
    logs_dir = _write_bridge_log(_home(tmp_path), _LOGIN_FAILURE_TRACEBACK)
    msg = lifecycle._diagnose_start_failure(logs_dir)
    assert msg is not None
    assert "token" in msg.lower()
    assert "disco init" in msg


def test_diagnose_start_failure_returns_none_when_log_missing(tmp_path) -> None:
    # No bridge log at all (the process never wrote one) => no diagnosis, never a
    # raise — the caller falls back to the generic hint.
    logs_dir = os.path.join(_home(tmp_path), "state", "logs")
    assert lifecycle._diagnose_start_failure(logs_dir) is None


def test_diagnose_start_failure_returns_none_on_unknown_failure(tmp_path) -> None:
    # A bridge log with no recognised signature must yield None so the caller keeps
    # the existing generic message unchanged.
    logs_dir = _write_bridge_log(_home(tmp_path), "some unrelated shutdown noise\n")
    assert lifecycle._diagnose_start_failure(logs_dir) is None


def test_diagnose_start_failure_reads_only_the_tail(tmp_path) -> None:
    # The signature sits at the very START of a multi-hundred-KB log, well outside
    # the ~64KB tail window. Reading only the tail must miss it (=> None), proving
    # the diagnosis never slurps an unbounded log into memory.
    filler = "x" * (lifecycle._LOG_TAIL_BYTES * 4)
    logs_dir = _write_bridge_log(
        _home(tmp_path), _PRIVILEGED_INTENTS_TRACEBACK + filler
    )
    assert lifecycle._diagnose_start_failure(logs_dir) is None

    # But the same signature WITHIN the tail window is still found, so the tail
    # bound does not blind the common case (the traceback is the last thing a
    # crashing bridge prints).
    logs_dir2 = _write_bridge_log(
        _home(tmp_path / "within"), filler + _PRIVILEGED_INTENTS_TRACEBACK
    )
    assert lifecycle._diagnose_start_failure(logs_dir2) is not None


async def test_start_readiness_timeout_diagnoses_privileged_intents(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # End to end: the bridge never becomes Ready AND its per-process log carries
    # the PrivilegedIntentsRequired traceback. The not-ready branch must surface
    # the specific fix (Message Content) instead of the generic guess, while still
    # pointing at the supervisor log.
    home = _home(tmp_path)
    _write_bridge_log(home, _PRIVILEGED_INTENTS_TRACEBACK)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Pending", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        spawn_blocking=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Message Content" in out
    # Points the user at the BRIDGE log — the file the diagnosis actually read
    # (the supervisor log only records the readiness timeout).
    assert "bridge.log" in out


async def test_start_readiness_timeout_falls_back_to_generic_without_diagnosis(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # No bridge log signature to match => the not-ready branch prints the existing
    # generic message unchanged (broker / privileged intents guess + log pointer).
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Pending", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        spawn_blocking=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code == 1
    out = capsys.readouterr().out.lower()
    assert "likely the broker could not be reached" in out
    # The generic message points at the BRIDGE log too — the file the diagnoser
    # reads and where the real traceback lands (the supervisor log only echoes
    # the readiness timeout).
    assert "bridge.log" in out


async def test_start_priming_reconcile_failure_tears_down_and_returns_nonzero(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # Fix #5: the priming reconcile (`update_project`) runs AFTER the detached
    # supervisor is already up. If it raises (a PC reconcile error / transport
    # failure), an unhandled exception would orphan the supervisor and dump a
    # traceback — crashing `disco init`, since `start` is the wizard's start_fn.
    # It must fail like the readiness-gate path right below it: tear the substrate
    # back down via the BLOCKING seam, print an actionable error, and return 1.
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    spawn_blocking = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        # The bridge would be Ready, but the priming reconcile blows up first, so
        # the readiness gate must never be reached.
        bridge_states=[{"is_ready": "Ready"}],
        update_project_raises=RuntimeError("process-compose POST /project failed"),
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        spawn_blocking=spawn_blocking,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 1
    # The reconcile was attempted exactly once (the buggy first update).
    assert len(client.update_project_calls) == 1
    # No orphan: the supervisor is torn down via the BLOCKING seam (a racy detached
    # `down` could let a retried `start` collide with a supervisor still stopping).
    assert all("down" not in c for c in spawn.calls)
    down_calls = [c for c in spawn_blocking.calls if "down" in c]
    assert len(down_calls) == 1
    down = down_calls[0]
    assert "-p" in down
    assert int(down[down.index("-p") + 1]) == lifecycle.pc_port_for(home)
    # The readiness gate must NOT have been reached (we bailed at the reconcile).
    assert client.get_process_calls == []
    # An actionable, non-traceback error that points at the supervisor log.
    out = capsys.readouterr().out.lower()
    assert "error" in out
    assert "process-compose.log" in out


async def test_start_server_up_timeout_returns_nonzero_without_priming(
    tmp_path, capsys, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    # Not up on the idempotency probe, then the REST server never answers within
    # the server-up budget: every subsequent project_state raises.
    client = _StubClient(
        project_state_results=[RuntimeError("not up")] * 1000,
        bridge_states=[{"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code != 0
    # The REST server never came up, so the priming reconcile must NOT have run.
    assert client.update_project_calls == []
    out = capsys.readouterr().out.lower()
    assert "rest server" in out


async def test_start_readiness_tolerates_transient_bridge_error(
    tmp_path, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    # A transient transport error mid-readiness-poll (the bridge restarting under
    # restart: always) must not abort the gate; the next poll sees it Ready.
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[RuntimeError("bridge bouncing"), {"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code == 0
    assert client.get_process_calls == ["bridge", "bridge"]


def test_bridge_is_ready_rejects_non_dict() -> None:
    assert lifecycle._bridge_is_ready(None) is False
    assert lifecycle._bridge_is_ready("Ready") is False


def test_bridge_is_ready_requires_ready_probe_not_just_running() -> None:
    # A green light that lies (§12.6/§13.3): the bridge HAS a readiness probe, so
    # status=="Running" while the probe is "Not Ready" is exactly the false-green
    # the strict gate must reject. Only is_ready=="Ready" counts.
    assert lifecycle._bridge_is_ready({"status": "Running", "is_ready": "Not Ready"}) is False
    assert lifecycle._bridge_is_ready({"status": "Running"}) is False
    assert lifecycle._bridge_is_ready({"is_ready": "Ready"}) is True
    assert lifecycle._bridge_is_ready({"status": "Running", "is_ready": "Ready"}) is True


def test_default_spawn_launches_detached(monkeypatch) -> None:
    # The production spawn must start a session-detached child (so the supervisor
    # outlives the CLI) without inheriting the CLI's stdio. Assert the Popen call
    # shape instead of launching a real process.
    import subprocess

    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    lifecycle._default_spawn(["process-compose", "up"])
    assert captured["argv"] == ["process-compose", "up"]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL


def test_default_spawn_blocking_runs_to_completion_bounded(monkeypatch) -> None:
    # The blocking spawn must RUN-and-WAIT (subprocess.run, not Popen) with a
    # bounded timeout, so `down` synchronously completes before `stop` returns
    # (§13.3). Assert the call shape instead of launching a real process.
    import subprocess

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "run", fake_run)
    lifecycle._default_spawn_blocking(["process-compose", "down"])
    assert captured["argv"] == ["process-compose", "down"]
    # Bounded so a wedged `down` fails loudly instead of hanging the CLI.
    assert captured["kwargs"]["timeout"] == lifecycle._DOWN_TIMEOUT_SECONDS
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL


# --- stop -------------------------------------------------------------------


async def test_stop_idempotent_when_nothing_running(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()
    # REST unreachable => nothing to stop.
    client = _StubClient(project_state_results=[RuntimeError("not up")])
    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)
    assert code == 0
    assert spawn_blocking.calls == []  # no `down` issued
    assert "nothing to stop" in capsys.readouterr().out.lower()


async def test_stop_issues_down_via_blocking_seam(tmp_path, fake_pc_bin) -> None:
    # `down` must be SYNCHRONOUS (§13.3 / Fix 3): a fire-and-forget detached `down`
    # lets `stop` print "workspace closed" and return before the supervisor has
    # actually stopped, so a racing `start` could collide. `stop` therefore issues
    # `down` through the BLOCKING seam, never the detached `spawn`.
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()
    client = _StubClient(project_state_results=[{"running": True}])
    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)
    assert code == 0
    # Exactly one blocking `down -p <port>`.
    assert len(spawn_blocking.calls) == 1
    argv = spawn_blocking.calls[0]
    assert "down" in argv
    assert "-p" in argv
    assert int(argv[argv.index("-p") + 1]) == lifecycle.pc_port_for(home)


async def test_stop_sweeps_live_roster_and_reports_count(tmp_path, fake_pc_bin, capsys) -> None:
    """`disco stop` closes the roster too: a live detached process is terminated and
    its pidfile cleared, and the count is reported honestly."""
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()
    spawned = _workspace.spawn_slot(home, "assistant", [sys.executable, "-c", "import time; time.sleep(30)"])
    assert _workspace.slot_is_live(home, "assistant")
    client = _StubClient(project_state_results=[{"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)
    assert code == 0
    assert not procspawn.pidfile_for(home, "assistant").exists()
    assert len(spawn_blocking.calls) == 1  # substrate down still issued
    assert "1 roster process(es) stopped" in capsys.readouterr().out
    # No orphan left behind.
    assert not _workspace.slot_is_live(home, "assistant")
    _ = spawned


async def test_stop_sweeps_stale_and_not_ours_pidfiles_silently(tmp_path, fake_pc_bin, capsys) -> None:
    """Stale (dead pid) and not-ours (recycled pid) pidfiles are swept without a
    signal and NOT counted as stopped processes."""
    home = _home(tmp_path)
    _write_stale_pidfile(home, "ghost")
    _write_not_ours_pidfile(home, "stranger")
    client = _StubClient(project_state_results=[{"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=_RecordingSpawn())
    assert code == 0
    assert not procspawn.pidfile_for(home, "ghost").exists()
    assert not procspawn.pidfile_for(home, "stranger").exists()
    out = capsys.readouterr().out
    assert "workspace closed." in out  # no "(N roster process(es) stopped)"


async def test_stop_sweeps_orphaned_roster_when_substrate_down(tmp_path, capsys) -> None:
    """Even with the substrate already down, `disco stop` still reaps an orphaned
    live roster process (a crashed supervisor scenario)."""
    home = _home(tmp_path)
    _workspace.spawn_slot(home, "assistant", [sys.executable, "-c", "import time; time.sleep(30)"])
    client = _StubClient(project_state_results=[RuntimeError("not up")])

    code = await lifecycle.stop(home, client=client, spawn_blocking=_RecordingSpawn())
    assert code == 0
    assert not _workspace.slot_is_live(home, "assistant")
    assert "1 roster process(es) stopped" in capsys.readouterr().out


async def test_stop_contended_lock_is_a_clean_error_not_a_traceback(tmp_path, capsys) -> None:
    """A roster verb holds the lifecycle lock SHARED (for up to its spawn-confirm
    window); `disco stop` racing it must print ONE clean error line and return 1 —
    never dump a raw RuntimeError traceback (the CLI surface catches nothing)."""
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()
    client = _StubClient(project_state_results=[{"running": True}])

    with _workspace.slot_mutation(home, "assistant"):
        code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)

    assert code == 1
    assert spawn_blocking.calls == []  # nothing torn down mid-verb
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "in progress" in out
    # The holder is a roster verb, not a start/stop — the message must not misname it.
    assert "start/stop" not in out


async def test_start_contended_lock_is_a_clean_error_not_a_traceback(tmp_path, capsys) -> None:
    """Same for `disco start`: a contended lifecycle lock is a domain refusal."""
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    client = _StubClient(project_state_results=[RuntimeError("not up")])

    with _workspace.slot_mutation(home, "assistant"):
        code = await lifecycle.start(
            home,
            server_urls="localhost:9092",
            launcher="/h/shims/disco",
            client=client,
            spawn=spawn,
            clock=_FakeClock(),
        )

    assert code == 1
    assert spawn.calls == []  # no second supervisor launched behind the holder
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "in progress" in out


async def test_stop_reports_a_wedged_slot_truthfully(tmp_path, fake_pc_bin, capsys, monkeypatch) -> None:
    """A slot whose terminate ended KILL_UNCONFIRMED (SIGKILL sent, pid never read
    dead) must not be counted 'stopped' — the sweep reports it FROM THE ENUM and
    keeps its live pidfile (mixed sweep: live + stale + wedged)."""
    home = _home(tmp_path)
    _write_self_pidfile(home, "live")  # terminates cleanly (scripted below)
    _write_stale_pidfile(home, "ghost")  # swept silently, never counted
    _write_self_pidfile(home, "wedged")  # SIGKILLed but the process survives

    async def scripted_terminate(home_arg, slot):
        if slot == "live":
            procspawn.pidfile_for(home_arg, slot).unlink()
            return procspawn.TerminateResult.TERMINATED
        if slot == "ghost":
            procspawn.pidfile_for(home_arg, slot).unlink()
            return None
        # "wedged": SIGKILL sent, the reap window closed on a live pid — the
        # (self, alive) pidfile stays and the enum says so.
        return procspawn.TerminateResult.KILL_UNCONFIRMED

    monkeypatch.setattr(_workspace, "terminate_slot", scripted_terminate)
    client = _StubClient(project_state_results=[{"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=_RecordingSpawn())

    assert code == 0
    out = capsys.readouterr().out
    assert "1 roster process(es) stopped" in out
    assert "1 still running" in out
    assert "logs" in out
    # The live pidfile of the survivor is kept — sweeping it would orphan the process.
    assert procspawn.pidfile_for(home, "wedged").exists()
    assert not procspawn.pidfile_for(home, "ghost").exists()


async def test_stop_counts_an_indeterminate_survivor_as_still_running(
    tmp_path, fake_pc_bin, capsys, monkeypatch
) -> None:
    """An INDETERMINATE terminate (identity unreadable; slot left untouched, its
    per-slot warning already printed) is a survivor exactly like
    KILL_UNCONFIRMED: the final summary's "still running" count must include it,
    so "workspace closed" never silently overclaims."""
    home = _home(tmp_path)
    _write_self_pidfile(home, "live")  # terminates cleanly (scripted below)
    _write_self_pidfile(home, "unverifiable")  # left untouched by the sweep

    async def scripted_terminate(home_arg, slot):
        if slot == "live":
            procspawn.pidfile_for(home_arg, slot).unlink()
            return procspawn.TerminateResult.TERMINATED
        return procspawn.TerminateResult.INDETERMINATE

    monkeypatch.setattr(_workspace, "terminate_slot", scripted_terminate)
    client = _StubClient(project_state_results=[{"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=_RecordingSpawn())

    assert code == 0
    out = capsys.readouterr().out
    assert "1 roster process(es) stopped" in out
    assert "1 still running" in out
    # The unverifiable slot's pidfile survives — the sweep never unlinked it.
    assert procspawn.pidfile_for(home, "unverifiable").exists()


# --- status -----------------------------------------------------------------


async def test_status_not_running(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    client = _StubClient(project_state_results=[RuntimeError("not up")])
    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())
    assert code == 0
    out = capsys.readouterr().out.lower()
    assert "not running" in out
    assert "disco start" in out


async def test_status_running_renders_board(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    # Substrate rows come from PC; the agent's row comes from a live pidfile
    # reconciled with the mesh probe.
    _write_self_pidfile(home, "assistant")
    processes = [
        {"name": "broker", "status": "Running", "is_ready": "Ready"},
        {"name": "bridge", "status": "Running", "is_ready": "Ready"},
    ]
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result={"data": processes},
    )
    code = await lifecycle.status(
        home, server_urls="localhost:9092", client=client, probe=_StubProbe(["assistant"])
    )
    assert code == 0
    out = capsys.readouterr().out
    for name in ("broker", "bridge", "assistant"):
        assert name in out
    assert "running" in out
    # Reboot non-survival surfaced honestly somewhere (§12.6): the daemon is
    # session-scoped, so status must say so.
    assert "reboot" in out.lower()


async def test_status_reconciliation_matrix(tmp_path, capsys) -> None:
    """The three agent states + the pidfile-only tools/mcp rows."""
    home = _home(tmp_path)
    _write_self_pidfile(home, "here_and_answering")  # pidfile + mesh -> running
    _write_self_pidfile(home, "here_only")  # pidfile only -> not registered
    _write_self_pidfile(home, "tools")  # non-agent -> running
    _write_self_pidfile(home, "mcp-github")  # non-agent -> running
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )
    probe = _StubProbe(["here_and_answering", "remote_only"])  # remote_only: mesh only

    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=probe)
    assert code == 0
    out = capsys.readouterr().out

    def row_for(slot: str) -> str:
        rows = [line for line in out.splitlines() if line.strip().startswith(slot)]
        assert rows, f"no board row for {slot!r} in:\n{out}"
        return rows[0]

    # Row-bound assertions: each state phrase must sit on ITS slot's row, so a
    # phrase leaking from another row can never satisfy the wrong case.
    assert row_for("here_and_answering").rstrip().endswith("running")
    assert "started, not registered (see disco logs)" in row_for("here_only")
    assert "running (another host)" in row_for("remote_only")
    assert row_for("tools").rstrip().endswith("running")
    assert row_for("mcp-github").rstrip().endswith("running")


async def test_status_broker_unreachable_shows_pidfiles_only(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    _write_self_pidfile(home, "assistant")
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )
    probe = _StubProbe(raises=RuntimeError("broker down"))

    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=probe)
    assert code == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    # Honest degrade label: the MESH VIEW was unreadable — the substrate rows
    # above may still show the broker Running, so "broker unreachable" lied.
    assert "mesh roster view unreadable" in out
    # No mesh -> the local pidfile is "started, not registered".
    assert "started, not registered" in out


async def test_status_renders_dead_slots_instead_of_hiding_them(tmp_path, capsys) -> None:
    """A slot that crashed (pidfile present, process gone) must show as
    `not running (exited — see <log>)` — never silently vanish from the board.
    (`disco stop`'s sweep is the acknowledge-and-clear point for the files.)"""
    home = _home(tmp_path)
    _write_stale_pidfile(home, "assistant")
    _write_stale_pidfile(home, "mcp-github")
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )
    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())
    assert code == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "mcp-github" in out
    assert out.count("not running (exited") == 2
    assert str(procspawn.log_path_for(home, "assistant")) in out


async def test_status_dead_slot_also_running_elsewhere_reports_both_truths(tmp_path, capsys) -> None:
    """Crashed here but answering from another host: both facts are rendered."""
    home = _home(tmp_path)
    _write_stale_pidfile(home, "assistant")
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )
    code = await lifecycle.status(
        home, server_urls="localhost:9092", client=client, probe=_StubProbe(["assistant"])
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "running (another host)" in out
    assert "exited here" in out


async def test_status_empty_roster_signposts_start_and_create(tmp_path, capsys) -> None:
    """An empty roster is a teaching moment: the board must point at BOTH
    ``disco agent start`` and ``disco agent create``, so a fresh org is never a
    dead end."""
    home = _home(tmp_path)
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )
    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())
    assert code == 0
    out = capsys.readouterr().out
    assert "none running" in out
    assert "disco agent start <name>" in out
    assert "disco agent create <name>" in out


async def test_status_supervisor_dying_mid_read_degrades_not_crashes(tmp_path, capsys) -> None:
    """The supervisor can die BETWEEN the up-probe (project_state) and the process
    list read; read-only status must degrade to the not-running hint, never crash
    (the docstring's promise)."""
    home = _home(tmp_path)
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_raises=RuntimeError("list_processes: connection refused"),
    )
    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())
    assert code == 0
    out = capsys.readouterr().out.lower()
    assert "not running" in out
    assert "disco start" in out


def test_process_rows_skips_non_dict_items() -> None:
    # A wire-shape wobble (a stray non-dict entry) must be skipped, not crash the
    # board.
    rows = lifecycle._process_rows(["junk", {"name": "broker", "status": "Running"}])
    assert [r["name"] for r in rows] == ["broker"]


# --- lock interaction with start/stop ---------------------------------------


def test_lockfile_path_is_under_state(tmp_path) -> None:
    home = _home(tmp_path)
    assert lifecycle._lock_path(home) == os.path.join(home, "state", "calfcord-lifecycle.lock")


# --- import isolation -------------------------------------------------------

# The lifecycle now imports ``calfcord.health.check`` for the broker fast-fail
# precondition (§13.2). That import must keep aiokafka lazy (it loads only inside
# ``default_broker_probe``'s coroutine), so importing the supervisor stays
# pure-filesystem. A fresh interpreter gives a clean ``sys.modules`` to assert
# against; mirrors ``tests/health/test_check.py``.
_ISOLATION_SCRIPT = """
import sys

import calfcord.supervisor.lifecycle  # noqa: F401

aiokafka_leaked = any(m == "aiokafka" or m.startswith("aiokafka.") for m in sys.modules)
assert not aiokafka_leaked, "supervisor.lifecycle eagerly imported aiokafka (must be lazy in the probe)"
print("ISOLATION_OK")
"""


def test_lifecycle_does_not_import_aiokafka() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ISOLATION_OK" in result.stdout, (
        "isolation subprocess exited 0 but did not run to completion\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --- stop/status honesty: scan failures, teardown verification, survivors ------


class _FailingSpawn:
    """A blocking-spawn seam that fails (e.g. a wedged `down` hitting its timeout)."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._exc = exc or subprocess.TimeoutExpired(cmd="process-compose down", timeout=20)

    def __call__(self, argv) -> None:
        self.calls.append(list(argv))
        raise self._exc


async def test_stop_scan_error_does_not_claim_closed(tmp_path, capsys, monkeypatch) -> None:
    """An unreadable state/run means the roster sweep saw NOTHING — `disco stop`
    must report the scan failure (rc 1), not claim `workspace closed` over
    processes it never saw. Nothing is torn down under unknown state."""
    home = _home(tmp_path)

    def raising(home_arg):
        raise _workspace.SlotScanError(Path(home) / "state" / "run", OSError("Permission denied"))

    monkeypatch.setattr(_workspace, "iter_slot_pidfiles", raising)
    spawn_blocking = _RecordingSpawn()
    client = _StubClient(project_state_results=[{"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)

    assert code == 1
    assert spawn_blocking.calls == []  # no down under unknown roster state
    out = capsys.readouterr().out
    assert "roster state unknown" in out
    assert "workspace closed" not in out


async def test_stop_down_failure_reports_teardown_uncertain(tmp_path, fake_pc_bin, capsys) -> None:
    """A `down` that raises (here: its bounded timeout) must not read as closed —
    one clean line, rc 1, no raw traceback."""
    home = _home(tmp_path)
    spawn_blocking = _FailingSpawn()
    client = _StubClient(project_state_results=[{"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)

    assert code == 1
    assert len(spawn_blocking.calls) == 1
    out = capsys.readouterr().out
    assert "teardown may not have completed" in out
    assert "disco status" in out
    assert "workspace closed" not in out


async def test_stop_verifies_the_supervisor_actually_stopped(tmp_path, fake_pc_bin, capsys) -> None:
    """`down` returning is not proof: stop re-probes, and a supervisor still
    answering must not be reported closed."""
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()  # succeeds but does nothing
    client = _StubClient(project_state_results=[{"running": True}, {"running": True}])

    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)

    assert code == 1
    out = capsys.readouterr().out
    assert "teardown may not have completed" in out
    assert "workspace closed" not in out


async def test_status_not_running_notes_local_survivors(tmp_path, capsys) -> None:
    """Supervisor down + detached roster processes alive: status must say so
    rather than implying the host is idle."""
    home = _home(tmp_path)
    _write_self_pidfile(home, "assistant")
    client = _StubClient(project_state_results=[RuntimeError("not up")])

    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())

    assert code == 0
    out = capsys.readouterr().out
    assert "not running" in out.lower()
    assert "1 detached roster process(es) still running locally" in out
    assert "assistant" in out
    assert "disco stop" in out


async def test_status_scan_error_degrades_with_a_warning(tmp_path, capsys, monkeypatch) -> None:
    home = _home(tmp_path)

    def raising(home_arg):
        raise _workspace.SlotScanError(Path(home) / "state" / "run", OSError("Permission denied"))

    monkeypatch.setattr(_workspace, "classify_slots", raising)
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())

    assert code == 0  # read-only status still renders the substrate
    out = capsys.readouterr().out
    assert "broker" in out
    assert "roster state unknown" in out


async def test_status_renders_an_unverifiable_slot_honestly(tmp_path, capsys, monkeypatch) -> None:
    """Identity-read failure on a live pid: the row must say the state is unknown
    — neither `running` nor the `exited` lie (which would invite a sweep)."""
    home = _home(tmp_path)
    _write_self_pidfile(home, "assistant")
    monkeypatch.setattr(procspawn, "_process_start_token", lambda pid: None)
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=_StubProbe())

    assert code == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.strip().startswith("assistant")]
    assert rows, out
    assert "cannot verify" in rows[0]
    assert "exited" not in rows[0]


async def test_status_default_probe_is_the_shared_workspace_seam(tmp_path, monkeypatch, capsys) -> None:
    """With no probe injected, status resolves it through _workspace.resolve_probe
    (the shared seam) — not by reaching into roster's privates."""
    seen: list[str] = []

    def fake_resolve(probe):
        assert probe is None

        async def _probe(server_urls: str) -> list[str]:
            seen.append(server_urls)
            return []

        return _probe

    monkeypatch.setattr(_workspace, "resolve_probe", fake_resolve)
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.status(_home(tmp_path), server_urls="localhost:9092", client=client)

    assert code == 0
    assert seen == ["localhost:9092"]


# --- start: teardown honesty on the failure paths -------------------------------


async def test_start_priming_failure_with_failing_teardown_is_honest(
    tmp_path, capsys, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not yet"), {"ok": True}],
        update_project_raises=RuntimeError("reconcile refused"),
    )
    spawn = _RecordingSpawn()
    spawn_blocking = _FailingSpawn()

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        spawn_blocking=spawn_blocking,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 1
    assert len(spawn_blocking.calls) == 1  # the teardown was attempted
    out = capsys.readouterr().out
    assert "teardown may not have completed" in out
    assert "tore it down" not in out
    assert "disco stop" in out or "disco status" in out


async def test_start_readiness_timeout_with_failing_teardown_is_honest(
    tmp_path, capsys, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not yet"), {"ok": True}],
        bridge_states=[{"status": "Running", "is_ready": "Not Ready"}] * 200,
    )
    spawn = _RecordingSpawn()
    spawn_blocking = _FailingSpawn()

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=spawn,
        spawn_blocking=spawn_blocking,
        clock=clock,
        sleep=clock.sleep,
        ready_timeout_s=5,
        broker_probe=_reachable_broker,
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "did not become ready" in out
    assert "teardown may not have completed" in out
    assert "tore down the workspace" not in out


# --- status: one slot snapshot, screened mesh names (fixes Q2 + S6) -------------


async def test_status_takes_one_slot_snapshot(tmp_path, capsys, monkeypatch) -> None:
    """The board's live/dead/unverifiable sets are projections of ONE
    classify_slots snapshot — not three scans (3 x N identity reads, and a slot
    dying between scans could appear in two disagreeing sets)."""
    home = _home(tmp_path)
    calls: list[str] = []

    def fake_classify(home_arg):
        calls.append(str(home_arg))
        return {
            "assistant": procspawn.Identity.OURS,
            "crashed": procspawn.Identity.NOT_OURS,
            "flaky": procspawn.Identity.INDETERMINATE,
        }

    monkeypatch.setattr(_workspace, "classify_slots", fake_classify)
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.status(
        home, server_urls="localhost:9092", client=client, probe=_StubProbe(["assistant"])
    )

    assert code == 0
    assert len(calls) == 1  # ONE snapshot feeds every set on the board
    out = capsys.readouterr().out
    assert out.count("assistant") == 1  # each slot renders exactly one row
    assert "not running (exited" in out
    assert "state unknown" in out


async def test_status_screens_malformed_mesh_names(tmp_path, capsys, monkeypatch) -> None:
    """Mesh-derived names are untrusted broker-wide input: a name carrying
    terminal escapes is dropped from the board with one aggregate warning —
    never printed raw."""
    home = _home(tmp_path)
    _write_self_pidfile(home, "assistant")
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=[{"name": "broker", "status": "Running", "is_ready": "Ready"}],
    )
    evil = "\x1b[2J\x1b[31mpwned"
    probe = _StubProbe(["assistant", evil])

    code = await lifecycle.status(home, server_urls="localhost:9092", client=client, probe=probe)

    assert code == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "pwned" not in out
    assert "assistant" in out
    assert "1 invalid mesh name(s) ignored" in out


# --- start-failure diagnosis: missing Discord settings (fix U2) ------------------


# A trimmed pydantic-settings ValidationError of the shape the bridge prints when
# required DISCORD_* values are absent from the effective environment.
_MISSING_SETTINGS_TRACEBACK = """\
Traceback (most recent call last):
  File "/h/src/calfcord/bridge/main.py", line 31, in main
    settings = DiscordSettings()
pydantic_core._pydantic_core.ValidationError: 2 validation errors for DiscordSettings
bot_token
  Field required [type=missing, input_value={}, input_type=dict]
application_id
  Field required [type=missing, input_value={}, input_type=dict]
"""


def test_diagnose_start_failure_names_missing_discord_settings(tmp_path) -> None:
    """A pydantic ValidationError on the bridge's Discord settings is a config
    gap, not a mystery: the diagnosis names the missing DISCORD_* env vars and
    steers to `disco init`."""
    logs_dir = _write_bridge_log(_home(tmp_path), _MISSING_SETTINGS_TRACEBACK)
    msg = lifecycle._diagnose_start_failure(logs_dir)
    assert msg is not None
    assert "DISCORD_BOT_TOKEN" in msg
    assert "DISCORD_APPLICATION_ID" in msg
    assert "disco init" in msg


def test_diagnose_start_failure_settings_error_without_extractable_fields(tmp_path) -> None:
    """A DiscordSettings ValidationError whose field lines fell outside the tail
    still gets the generic settings diagnosis (never None → the unrelated
    broker/intents guess)."""
    tail = "pydantic_core._pydantic_core.ValidationError: 1 validation error for DiscordSettings\n"
    logs_dir = _write_bridge_log(_home(tmp_path), tail)
    msg = lifecycle._diagnose_start_failure(logs_dir)
    assert msg is not None
    assert "Discord settings" in msg
    assert "disco init" in msg


def test_diagnose_start_failure_ignores_unrelated_validation_errors(tmp_path) -> None:
    # A ValidationError for some OTHER model must not claim Discord settings.
    tail = (
        "pydantic_core._pydantic_core.ValidationError: "
        "1 validation error for SomethingElse\nfield_x\n  Field required\n"
    )
    logs_dir = _write_bridge_log(_home(tmp_path), tail)
    assert lifecycle._diagnose_start_failure(logs_dir) is None


async def test_start_readiness_timeout_generic_message_points_at_bridge_log(
    tmp_path, capsys, fake_pc_bin
) -> None:
    """Even with NO recognised signature, the failure message names the bridge's
    own log — the file the diagnoser reads and where the real traceback lands —
    not the supervisor log."""
    home = _home(tmp_path)
    _write_bridge_log(home, "some unrelated shutdown noise\n")
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Pending", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/disco",
        client=client,
        spawn=_RecordingSpawn(),
        spawn_blocking=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "bridge.log" in out


def test_ensure_log_path_creates_the_logs_dir_owner_only(tmp_path) -> None:
    """`disco start` usually creates state/logs BEFORE any slot spawns; it must
    apply the same owner-only mode the spawn path does (slot logs land here)."""
    home = _home(tmp_path)
    lifecycle._ensure_log_path(home)
    assert (Path(home, "state", "logs").stat().st_mode & 0o777) == 0o700


def test_ensure_log_path_leaves_a_preexisting_dir_mode_alone(tmp_path) -> None:
    home = _home(tmp_path)
    logs_dir = Path(home, "state", "logs")
    logs_dir.mkdir(parents=True)
    logs_dir.chmod(0o755)
    lifecycle._ensure_log_path(home)
    assert (logs_dir.stat().st_mode & 0o777) == 0o755
