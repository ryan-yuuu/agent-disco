"""Unit tests for the shared supervisor seam (``_workspace.py``, Fix #14).

``lifecycle`` / ``roster`` / ``component`` / ``cli.doctor`` all build on these four
consolidated primitives, so they are pinned here once: the per-home client
resolver, the workspace-up probe, the one not-running hint, and the
``{"data": [...]}``-vs-bare-list process-list normalizer. The surfaces keep thin
re-export aliases (``roster._resolve_client``, ``component._workspace_is_up``,
``lifecycle._process_rows`` …) whose own tests cover the wiring; these cover the
seam directly so the shared behavior is not only ever exercised second-hand.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from calfcord.supervisor import _workspace, procspawn
from calfcord.supervisor.client import ProcessComposeClient


class _StubClient:
    """A scriptable stand-in: ``project_state`` raises iff the workspace is down."""

    def __init__(self, *, up: bool) -> None:
        self._up = up

    async def project_state(self):
        if not self._up:
            # Mirrors ProcessComposeClient: a transport failure surfaces as
            # RuntimeError, which the up-probe reads as "not running".
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}


def test_one_hint_string_is_shared_by_every_surface() -> None:
    # The hint must be byte-identical everywhere so the lifecycle surfaces speak
    # with one voice (the whole point of consolidating it).
    from calfcord.supervisor import component, roster

    assert roster._NOT_RUNNING_HINT is _workspace.WORKSPACE_NOT_RUNNING_HINT
    assert component._NOT_RUNNING_HINT is _workspace.WORKSPACE_NOT_RUNNING_HINT
    assert "disco start" in _workspace.WORKSPACE_NOT_RUNNING_HINT


def test_resolve_client_passes_through_an_injected_client() -> None:
    injected = ProcessComposeClient(port=1234)
    assert _workspace.resolve_client(injected, "/srv/home") is injected


def test_resolve_client_defaults_to_a_per_home_client(tmp_path) -> None:
    # With no client injected the resolver builds a per-home ProcessComposeClient
    # on the port pc_port_for derives from the home (the port `up -p` pinned).
    from calfcord.supervisor.lifecycle import pc_port_for

    home = str(tmp_path)
    client = _workspace.resolve_client(None, home)
    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    # Equal base URLs prove equal ports without a live call.
    assert client._base_url == expected._base_url


async def test_workspace_is_up_true_when_project_state_answers() -> None:
    assert await _workspace.workspace_is_up(_StubClient(up=True)) is True


async def test_workspace_is_up_false_on_transport_runtimeerror() -> None:
    assert await _workspace.workspace_is_up(_StubClient(up=False)) is False


def test_iter_process_dicts_handles_bare_list() -> None:
    payload = [{"name": "broker"}, {"name": "bridge"}]
    assert list(_workspace.iter_process_dicts(payload)) == payload


def test_iter_process_dicts_unwraps_data_envelope() -> None:
    # Process Compose's process-list shape wobbles across versions; the
    # ``{"data": [...]}`` envelope must be unwrapped exactly like the bare list.
    inner = [{"name": "broker"}]
    assert list(_workspace.iter_process_dicts({"data": inner})) == inner


def test_iter_process_dicts_skips_non_dicts_and_none() -> None:
    # A stray non-dict entry (or a None payload) must be skipped, never crash a
    # caller (the status board / ps physical view / drift read).
    assert list(_workspace.iter_process_dicts(["junk", {"name": "broker"}, 7])) == [
        {"name": "broker"}
    ]
    assert list(_workspace.iter_process_dicts(None)) == []


# --- legacy-workspace guard (upgrade over a live old-style workspace) --------


class _ProcessListClient:
    """A stub whose ``list_processes`` returns a scripted payload or raises."""

    def __init__(self, payload: object = None, *, raises: Exception | None = None) -> None:
        self._payload = payload
        self._raises = raises

    async def list_processes(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


async def test_legacy_pc_roster_false_for_a_substrate_only_project() -> None:
    client = _ProcessListClient([{"name": "broker"}, {"name": "bridge"}])
    assert await _workspace.legacy_pc_roster(client) is False


async def test_legacy_pc_roster_true_when_pc_still_supervises_roster_processes() -> None:
    # An old-style workspace declared the roster (agents/tools/mcp) as PC slots;
    # any non-substrate process means spawning beside it would split-brain.
    client = _ProcessListClient(
        {"data": [{"name": "broker"}, {"name": "bridge"}, {"name": "assistant"}]}
    )
    assert await _workspace.legacy_pc_roster(client) is True


async def test_legacy_pc_roster_true_for_a_legacy_tools_slot() -> None:
    client = _ProcessListClient([{"name": "broker"}, {"name": "bridge"}, {"name": "tools"}])
    assert await _workspace.legacy_pc_roster(client) is True


async def test_legacy_pc_roster_fails_open_when_the_read_raises() -> None:
    # The check is best-effort: an unreadable process list must NOT block the
    # spawn verbs (proceed as today).
    client = _ProcessListClient(raises=RuntimeError("connection refused"))
    assert await _workspace.legacy_pc_roster(client) is False


async def test_legacy_pc_roster_fails_open_on_a_client_without_the_route() -> None:
    class _Bare:
        pass

    assert await _workspace.legacy_pc_roster(_Bare()) is False


async def test_legacy_pc_roster_skips_entries_without_a_name() -> None:
    # A wire-shape wobble (a dict row missing "name") must not read as legacy.
    client = _ProcessListClient([{"status": "Running"}, {"name": "bridge"}])
    assert await _workspace.legacy_pc_roster(client) is False


def test_legacy_workspace_hint_names_the_remedy() -> None:
    assert "older calfcord" in _workspace.LEGACY_WORKSPACE_HINT
    assert "disco stop" in _workspace.LEGACY_WORKSPACE_HINT
    assert "disco start" in _workspace.LEGACY_WORKSPACE_HINT


# --- roster-slot primitives (Phase 2) ---------------------------------------


def _write_self_pidfile(home, slot: str) -> Path:
    """Write a pidfile naming THIS (alive, ours) test process for ``slot``."""
    record = procspawn._identity_for(os.getpid(), ("self",))
    pidfile = procspawn.pidfile_for(home, slot)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps(procspawn._record_to_dict(record)), encoding="utf-8")
    return pidfile


def _write_dead_pidfile(home, slot: str) -> Path:
    """Write a pidfile naming a definitely-dead pid (a reaped child) for ``slot``."""
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    record = {
        "v": 1,
        "pid": proc.pid,
        "argv": ["gone"],
        "start_token": "stale-token",
        "spawn_ts": time.time(),
        "argv_hash": "x",
    }
    pidfile = procspawn.pidfile_for(home, slot)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps(record), encoding="utf-8")
    return pidfile


def test_launcher_for_is_the_home_shim(tmp_path) -> None:
    assert _workspace.launcher_for(tmp_path) == str(tmp_path / "shims" / "disco")


def test_iter_slot_pidfiles_empty_before_any_spawn(tmp_path) -> None:
    # No state/run dir yet — the scan is safe and yields nothing, never raises.
    assert list(_workspace.iter_slot_pidfiles(tmp_path)) == []


def test_iter_slot_pidfiles_yields_stems_sorted(tmp_path) -> None:
    _write_self_pidfile(tmp_path, "scribe")
    _write_self_pidfile(tmp_path, "assistant")
    _write_self_pidfile(tmp_path, "mcp-github")
    slots = [slot for slot, _ in _workspace.iter_slot_pidfiles(tmp_path)]
    assert slots == ["assistant", "mcp-github", "scribe"]


def test_slot_is_live_true_for_our_alive_process(tmp_path) -> None:
    _write_self_pidfile(tmp_path, "assistant")
    assert _workspace.slot_is_live(tmp_path, "assistant") is True


def test_slot_is_live_false_when_absent(tmp_path) -> None:
    assert _workspace.slot_is_live(tmp_path, "nobody") is False


def test_slot_is_live_false_when_stale(tmp_path) -> None:
    _write_dead_pidfile(tmp_path, "assistant")
    assert _workspace.slot_is_live(tmp_path, "assistant") is False


def test_live_slots_returns_only_alive(tmp_path) -> None:
    _write_self_pidfile(tmp_path, "assistant")
    _write_dead_pidfile(tmp_path, "scribe")
    assert _workspace.live_slots(tmp_path) == {"assistant"}


def test_is_agent_slot_classification() -> None:
    assert _workspace.is_agent_slot("assistant") is True
    assert _workspace.is_agent_slot("tools") is False
    assert _workspace.is_agent_slot("broker") is False
    assert _workspace.is_agent_slot("bridge") is False
    assert _workspace.is_agent_slot("mcp-github") is False
    # The supervisor's own log stem: never an agent (a pre-guard `.md` must not
    # let a roster verb spawn a slot that shares the supervisor's log file).
    assert _workspace.is_agent_slot("process-compose") is False


def test_live_agent_slots_excludes_tools_and_mcp(tmp_path) -> None:
    _write_self_pidfile(tmp_path, "assistant")
    _write_self_pidfile(tmp_path, "tools")
    _write_self_pidfile(tmp_path, "mcp-github")
    assert _workspace.live_agent_slots(tmp_path) == {"assistant"}


def test_spawn_slot_writes_pidfile_and_log(tmp_path) -> None:
    spawned = _workspace.spawn_slot(tmp_path, "job", [sys.executable, "-c", "print('hi')"])
    os.waitpid(spawned.pid, 0)
    assert procspawn.pidfile_for(tmp_path, "job").exists()
    assert procspawn.log_path_for(tmp_path, "job").exists()
    assert _workspace.slot_is_live(tmp_path, "job") is False  # already exited + reaped


async def test_terminate_slot_stops_process_and_clears_pidfile(tmp_path) -> None:
    _workspace.spawn_slot(tmp_path, "job", [sys.executable, "-c", "import time; time.sleep(30)"])
    assert _workspace.slot_is_live(tmp_path, "job") is True
    result = await _workspace.terminate_slot(tmp_path, "job")
    assert result in (procspawn.TerminateResult.TERMINATED, procspawn.TerminateResult.KILLED)
    assert not procspawn.pidfile_for(tmp_path, "job").exists()


async def test_terminate_slot_none_when_no_pidfile(tmp_path) -> None:
    assert await _workspace.terminate_slot(tmp_path, "nobody") is None


def test_dead_slots_lists_pidfiles_without_a_live_process(tmp_path) -> None:
    """A slot that crashed (pidfile present, process gone) must be reportable, not
    invisible — the status board renders these as `not running (exited …)`."""
    _write_self_pidfile(tmp_path, "alive")
    _write_dead_pidfile(tmp_path, "crashed")
    assert _workspace.dead_slots(tmp_path) == {"crashed"}


def test_dead_slots_empty_when_nothing_spawned(tmp_path) -> None:
    assert _workspace.dead_slots(tmp_path) == set()


# --- spawn cwd determinism (detached slots must not inherit the CLI's cwd) ---


def test_spawn_slot_pins_the_child_cwd_to_home(tmp_path, monkeypatch) -> None:
    """The shim defaults CALFCORD_WORKSPACE_DIR to $PWD, so a detached slot's cwd
    must be deterministic (the home dir) — not wherever the operator happened to
    run the verb (the PC daemon effectively ran from the home too)."""
    monkeypatch.chdir(tmp_path / ".." if (tmp_path / "..").exists() else tmp_path)
    spawned = _workspace.spawn_slot(
        tmp_path, "job", [sys.executable, "-c", "import os; print(os.getcwd())"]
    )
    os.waitpid(spawned.pid, 0)
    reported = procspawn.log_path_for(tmp_path, "job").read_text().strip()
    assert os.path.realpath(reported) == os.path.realpath(os.fspath(tmp_path))


# --- launch_slot: spawn + bounded liveness confirmation (crash-on-boot) ------


class _StepClock:
    """A clock returning scripted instants (repeating the last one)."""

    def __init__(self, *instants: float) -> None:
        self._instants = list(instants)

    def __call__(self) -> float:
        if len(self._instants) > 1:
            return self._instants.pop(0)
        return self._instants[0]


async def _no_sleep(_s: float) -> None:
    return None


async def test_launch_slot_true_when_the_process_survives_the_window(tmp_path) -> None:
    ok = await _workspace.launch_slot(
        tmp_path,
        "job",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        clock=_StepClock(0.0, 999.0),  # window elapses on the first re-check
        sleep=_no_sleep,
    )
    assert ok is True
    assert _workspace.slot_is_live(tmp_path, "job") is True
    await _workspace.terminate_slot(tmp_path, "job")


async def test_launch_slot_false_and_cleans_pidfile_when_it_exits_immediately(tmp_path) -> None:
    """Crash-on-boot must not read as success: the window catches the exit, the
    pidfile is cleaned, and the caller gets False to report honestly."""
    ok = await _workspace.launch_slot(
        tmp_path,
        "job",
        ["/bin/sh", "-c", "exit 7"],
    )
    assert ok is False
    assert not procspawn.pidfile_for(tmp_path, "job").exists()


async def test_launch_slot_false_on_a_mid_window_death(tmp_path, monkeypatch) -> None:
    """A process alive on the FIRST liveness poll but dead on a later one (a
    mid-window death) must read False — driven deterministically: scripted
    liveness plus a clock pinned before the deadline, so the loop is forced
    through the sleep-and-recheck arm with no real child and no real time."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        _workspace, "spawn_slot", lambda home, slot, argv: SimpleNamespace(pid=os.getpid())
    )
    liveness = iter([True, False])
    monkeypatch.setattr(_workspace, "slot_is_live", lambda home, slot: next(liveness))
    cleaned: list = []
    monkeypatch.setattr(procspawn, "cleanup_stale", lambda pidfile: cleaned.append(pidfile))
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)

    ok = await _workspace.launch_slot(
        tmp_path,
        "job",
        ["cmd"],
        clock=lambda: 0.0,  # never past the deadline: only death can end the loop
        sleep=recording_sleep,
    )

    assert ok is False
    assert slept == [_workspace._SPAWN_CONFIRM_POLL_S]  # one poll between checks
    assert cleaned == [procspawn.pidfile_for(tmp_path, "job")]


# --- slot_mutation: the spawn-critical-section locks --------------------------


def test_slot_mutation_second_holder_gets_slot_busy(tmp_path) -> None:
    with _workspace.slot_mutation(tmp_path, "assistant"):
        try:
            with _workspace.slot_mutation(tmp_path, "assistant"):
                raise AssertionError("second exclusive slot lock must not be granted")
        except _workspace.SlotBusyError:
            pass


def test_slot_mutation_different_slots_coexist(tmp_path) -> None:
    with _workspace.slot_mutation(tmp_path, "assistant"), _workspace.slot_mutation(tmp_path, "scribe"):
        pass


def test_slot_mutation_blocks_a_concurrent_lifecycle_stop(tmp_path) -> None:
    """A spawn in flight holds the lifecycle lock SHARED, so `disco stop`'s
    exclusive lock (its sweep) cannot land mid-spawn."""
    import pytest

    from calfcord.supervisor.lifecycle import lifecycle_lock

    with (
        _workspace.slot_mutation(tmp_path, "assistant"),
        pytest.raises(RuntimeError, match="in progress"),
        lifecycle_lock(tmp_path),
    ):
        raise AssertionError("stop's exclusive lock must not be granted mid-spawn")


def test_slot_mutation_refused_while_lifecycle_lock_held(tmp_path) -> None:
    """The converse: during `disco start`/`disco stop` (exclusive lifecycle lock),
    a spawn must not slip in behind the sweep."""
    import pytest

    from calfcord.supervisor.lifecycle import lifecycle_lock

    with (
        lifecycle_lock(tmp_path),
        pytest.raises(_workspace.WorkspaceBusyError),
        _workspace.slot_mutation(tmp_path, "assistant"),
    ):
        raise AssertionError("spawn critical section must not open during start/stop")


def test_slot_mutation_lifecycle_guard_io_error_is_not_workspace_busy(tmp_path) -> None:
    """Only lock CONTENTION means "busy". A filesystem problem — here state/ is a
    FILE, so the lock's parent dir cannot be made — must surface as what it is,
    never as the lie "a disco start/stop is in progress"."""
    import pytest

    (tmp_path / "state").write_text("in the way")
    with pytest.raises(FileExistsError), _workspace.slot_mutation(tmp_path, "assistant"):
        raise AssertionError("the critical section must not open over a broken lock dir")


def test_slot_mutation_slot_guard_io_error_is_not_slot_busy(tmp_path) -> None:
    """Same for the per-slot lock: state/run being a FILE is an IO problem, not
    "another disco command is already starting/stopping this slot"."""
    import pytest

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "run").write_text("in the way")
    with pytest.raises(FileExistsError), _workspace.slot_mutation(tmp_path, "assistant"):
        raise AssertionError("the critical section must not open over a broken run dir")


def test_slot_mutation_released_after_exit(tmp_path) -> None:
    with _workspace.slot_mutation(tmp_path, "assistant"):
        pass
    with _workspace.slot_mutation(tmp_path, "assistant"):
        pass  # re-acquirable — the locks were released


# --- broker_gate ---------------------------------------------------------------


async def test_broker_gate_uses_the_injected_probe() -> None:
    async def up() -> bool:
        return True

    async def down() -> bool:
        return False

    assert await _workspace.broker_gate("broker:9092", up) is True
    assert await _workspace.broker_gate("broker:9092", down) is False


async def test_broker_gate_defaults_to_default_broker_probe(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def _builder(server_urls: str):
        seen["server_urls"] = server_urls

        async def _probe() -> bool:
            return True

        return _probe

    monkeypatch.setattr("calfcord.health.check.default_broker_probe", _builder)
    assert await _workspace.broker_gate("broker.example:9092", None) is True
    assert seen["server_urls"] == "broker.example:9092"


async def test_broker_gate_falls_back_to_calf_host_url_env(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def _builder(server_urls: str):
        seen["server_urls"] = server_urls

        async def _probe() -> bool:
            return True

        return _probe

    monkeypatch.setattr("calfcord.health.check.default_broker_probe", _builder)
    monkeypatch.setenv("CALF_HOST_URL", "env-broker:9092")
    assert await _workspace.broker_gate(None, None) is True
    assert seen["server_urls"] == "env-broker:9092"
