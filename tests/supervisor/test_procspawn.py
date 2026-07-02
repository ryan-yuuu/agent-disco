"""Unit tests for the detached-process primitives (:mod:`calfcord.supervisor.procspawn`).

These exercise the mechanism against **real** child processes (short-lived
``python -c`` invocations) in ``tmp_path`` homes — the point of a mechanism-only
module is that there is nothing to fake below it. Every child is spawned into its
own session (``start_new_session``) and reaped by the test so no stray processes
leak; the terminate paths use tight, injected timeouts so the whole file stays
well under a couple of seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

from calfcord.supervisor import compose, procspawn

# --- helpers ----------------------------------------------------------------


def _wait(pid: int) -> None:
    """Reap ``pid`` (a direct child) so the test leaves no zombie behind."""
    with contextlib.suppress(OSError):
        os.waitpid(pid, 0)


def _spawn_py(
    code: str,
    home: Path,
    slot: str = "job",
    *,
    env: dict | None = None,
    cwd: str | None = None,
) -> procspawn.SpawnedProcess:
    """Spawn a short ``python -c <code>`` child through the primitive under test."""
    return procspawn.spawn_detached(
        [sys.executable, "-c", code],
        log_path=procspawn.log_path_for(home, slot),
        pidfile=procspawn.pidfile_for(home, slot),
        env=env,
        cwd=cwd,
    )


def _write_pidfile_json(pidfile: Path, record: dict) -> None:
    """Write a hand-crafted pidfile payload (for the not-ours / stale cases)."""
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps(record), encoding="utf-8")


# --- spawn_detached ---------------------------------------------------------


def test_spawn_detached_starts_new_session(tmp_path: Path) -> None:
    spawned = _spawn_py("import os; print(os.getsid(0))", tmp_path)
    _wait(spawned.pid)
    reported_sid = int(procspawn.log_path_for(tmp_path, "job").read_text().strip())
    # start_new_session makes the child a session leader, so its session id equals
    # its own pid — and differs from the test runner's session.
    assert reported_sid == spawned.pid
    assert reported_sid != os.getsid(0)


def test_spawn_detached_log_captures_stdout_and_stderr(tmp_path: Path) -> None:
    spawned = _spawn_py(
        "import sys; print('to-out'); print('to-err', file=sys.stderr)", tmp_path
    )
    _wait(spawned.pid)
    body = procspawn.log_path_for(tmp_path, "job").read_text()
    assert "to-out" in body
    assert "to-err" in body


def test_spawn_detached_log_is_appended_not_truncated(tmp_path: Path) -> None:
    log = procspawn.log_path_for(tmp_path, "job")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("prior-line\n")
    spawned = _spawn_py("print('new-line')", tmp_path)
    _wait(spawned.pid)
    body = log.read_text()
    assert "prior-line" in body
    assert "new-line" in body


def test_spawn_detached_stdin_is_devnull(tmp_path: Path) -> None:
    spawned = _spawn_py(
        "import sys; print('EOF' if sys.stdin.read() == '' else 'DATA')", tmp_path
    )
    _wait(spawned.pid)
    assert "EOF" in procspawn.log_path_for(tmp_path, "job").read_text()


def test_spawn_detached_creates_parent_dirs(tmp_path: Path) -> None:
    # Neither state/run nor state/logs exists yet; spawn must create both.
    assert not (tmp_path / "state" / "run").exists()
    assert not (tmp_path / "state" / "logs").exists()
    spawned = _spawn_py("print('ok')", tmp_path)
    _wait(spawned.pid)
    assert procspawn.pidfile_for(tmp_path, "job").exists()
    assert procspawn.log_path_for(tmp_path, "job").exists()


def test_spawn_detached_honors_env(tmp_path: Path) -> None:
    env = {**os.environ, "CALF_PROCSPAWN_MARKER": "sentinel-value"}
    spawned = _spawn_py(
        "import os; print(os.environ.get('CALF_PROCSPAWN_MARKER', ''))",
        tmp_path,
        env=env,
    )
    _wait(spawned.pid)
    assert "sentinel-value" in procspawn.log_path_for(tmp_path, "job").read_text()


def test_spawn_detached_rotates_an_oversized_log(tmp_path: Path) -> None:
    """A log at/over the threshold is shifted to ``.log.1`` before the new spawn
    writes, so a crash-looped or chatty slot cannot grow one file forever."""
    log = procspawn.log_path_for(tmp_path, "job")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"old" + b"x" * procspawn._LOG_ROTATE_AT_BYTES)
    spawned = _spawn_py("print('fresh-line')", tmp_path)
    _wait(spawned.pid)
    rotated = log.with_name("job.log.1")
    assert rotated.exists()
    assert rotated.read_bytes().startswith(b"old")
    body = log.read_text()
    assert "fresh-line" in body
    assert "old" not in body  # the new log starts fresh


def test_spawn_detached_rotation_shifts_backups_and_drops_the_oldest(tmp_path: Path) -> None:
    log = procspawn.log_path_for(tmp_path, "job")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"gen0" + b"x" * procspawn._LOG_ROTATE_AT_BYTES)
    for i in range(1, procspawn._LOG_ROTATE_BACKUPS + 1):
        log.with_name(f"job.log.{i}").write_text(f"gen{i}")
    spawned = _spawn_py("print('ok')", tmp_path)
    _wait(spawned.pid)
    # Every backup shifted one slot down; the oldest (gen5) fell off the end.
    assert log.with_name("job.log.1").read_bytes().startswith(b"gen0")
    for i in range(2, procspawn._LOG_ROTATE_BACKUPS + 1):
        assert log.with_name(f"job.log.{i}").read_text() == f"gen{i - 1}"
    assert not log.with_name(f"job.log.{procspawn._LOG_ROTATE_BACKUPS + 1}").exists()


def test_spawn_detached_does_not_rotate_a_small_log(tmp_path: Path) -> None:
    log = procspawn.log_path_for(tmp_path, "job")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("prior-line\n")
    spawned = _spawn_py("print('new-line')", tmp_path)
    _wait(spawned.pid)
    assert not log.with_name("job.log.1").exists()
    assert "prior-line" in log.read_text()  # still appended, not rotated


def test_spawn_detached_honors_cwd(tmp_path: Path) -> None:
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    spawned = _spawn_py("import os; print(os.getcwd())", tmp_path, cwd=str(workdir))
    _wait(spawned.pid)
    reported = procspawn.log_path_for(tmp_path, "job").read_text().strip()
    assert os.path.realpath(reported) == os.path.realpath(str(workdir))


def test_spawn_detached_writes_identity_pidfile(tmp_path: Path) -> None:
    spawned = _spawn_py("import time; time.sleep(5)", tmp_path)
    try:
        record = procspawn.read_pidfile(procspawn.pidfile_for(tmp_path, "job"))
        assert record is not None
        assert record.pid == spawned.pid
        assert record.argv[0] == sys.executable
        # A real OS start-token is captured on both supported platforms.
        assert record.start_token
        assert record.argv_hash
    finally:
        os.killpg(spawned.pid, signal.SIGKILL)
        _wait(spawned.pid)


# --- read_pidfile -----------------------------------------------------------


def test_read_pidfile_missing_returns_none(tmp_path: Path) -> None:
    assert procspawn.read_pidfile(tmp_path / "nope.pid") is None


def test_read_pidfile_corrupt_returns_none(tmp_path: Path) -> None:
    pidfile = tmp_path / "corrupt.pid"
    pidfile.write_text("this is not json {")
    assert procspawn.read_pidfile(pidfile) is None


def test_read_pidfile_missing_required_field_returns_none(tmp_path: Path) -> None:
    pidfile = tmp_path / "partial.pid"
    pidfile.write_text(json.dumps({"argv": ["x"]}))  # no pid
    assert procspawn.read_pidfile(pidfile) is None


def test_read_pidfile_roundtrips_a_spawned_record(tmp_path: Path) -> None:
    spawned = _spawn_py("import time; time.sleep(5)", tmp_path)
    try:
        record = procspawn.read_pidfile(spawned.pidfile)
        assert record == spawned.record
    finally:
        os.killpg(spawned.pid, signal.SIGKILL)
        _wait(spawned.pid)


# --- is_ours_and_alive ------------------------------------------------------


def test_is_ours_and_alive_true_for_matching_record(tmp_path: Path) -> None:
    spawned = _spawn_py("import time; time.sleep(5)", tmp_path)
    try:
        assert procspawn.is_ours_and_alive(spawned.record) is True
    finally:
        os.killpg(spawned.pid, signal.SIGKILL)
        _wait(spawned.pid)


def test_is_ours_and_alive_false_on_token_mismatch(tmp_path: Path) -> None:
    # A live pid (this test process) with a NON-matching start-token models pid
    # reuse: a recycled pid must never be mistaken for ours.
    record = procspawn.PidRecord(
        pid=os.getpid(),
        argv=("whatever",),
        start_token="not-the-real-token",
        spawn_ts=0.0,
        argv_hash="deadbeef",
    )
    assert procspawn.is_ours_and_alive(record) is False


def test_is_ours_and_alive_true_when_token_matches_live_pid(tmp_path: Path) -> None:
    token = procspawn._process_start_token(os.getpid())
    assert token  # the running platform exposes a start-token
    record = procspawn.PidRecord(
        pid=os.getpid(),
        argv=("whatever",),
        start_token=token,
        spawn_ts=0.0,
        argv_hash="deadbeef",
    )
    assert procspawn.is_ours_and_alive(record) is True


def test_is_ours_and_alive_false_when_dead(tmp_path: Path) -> None:
    spawned = _spawn_py("import sys; sys.exit(0)", tmp_path)
    _wait(spawned.pid)
    assert procspawn.is_ours_and_alive(spawned.record) is False


def test_is_ours_and_alive_false_without_a_start_token(tmp_path: Path) -> None:
    # An empty start-token means no re-queryable OS identity was captured; a KILL
    # primitive must not claim ownership it cannot prove, so this reads not-ours.
    record = procspawn.PidRecord(
        pid=os.getpid(),
        argv=("whatever",),
        start_token="",
        spawn_ts=0.0,
        argv_hash="deadbeef",
    )
    assert procspawn.is_ours_and_alive(record) is False


# --- terminate --------------------------------------------------------------


class _FakeClock:
    """A monotonic clock advanced only by the injected sleep (lifecycle pattern)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _advancing_sleep(clock: _FakeClock, calls: list[float]):
    async def _sleep(seconds: float) -> None:
        calls.append(seconds)
        clock.t += seconds
        # Yield + a sliver of real time so a just-signalled real child makes
        # progress toward death without the fake clock racing infinitely.
        await asyncio.sleep(0.005)

    return _sleep


async def test_terminate_graceful_within_timeout(tmp_path: Path) -> None:
    spawned = _spawn_py("import time; time.sleep(30)", tmp_path)
    calls: list[float] = []
    outcome = await procspawn.terminate(
        spawned.record,
        term_timeout_s=5.0,
        sleep=_advancing_sleep(_FakeClock(), calls),
        clock=time.monotonic,
    )
    assert outcome is procspawn.TerminateResult.TERMINATED
    assert not procspawn.is_ours_and_alive(spawned.record)


def _await_log_marker(home: Path, slot: str, marker: str, timeout_s: float = 5.0) -> None:
    """Block until ``marker`` appears in the child's log (its readiness signal)."""
    log = procspawn.log_path_for(home, slot)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if log.exists() and marker in log.read_text():
            return
        time.sleep(0.01)
    raise AssertionError(f"child did not reach {marker!r} within {timeout_s}s")


async def test_terminate_kills_after_timeout(tmp_path: Path) -> None:
    # A child that ignores SIGTERM survives the graceful window and forces SIGKILL.
    # It announces readiness only AFTER the ignore handler is installed, so the test
    # cannot send SIGTERM during the startup window (before the handler is armed).
    spawned = _spawn_py(
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ARMED', flush=True); time.sleep(30)",
        tmp_path,
    )
    _await_log_marker(tmp_path, "job", "ARMED")
    clock = _FakeClock()
    calls: list[float] = []
    outcome = await procspawn.terminate(
        spawned.record,
        term_timeout_s=1.0,
        sleep=_advancing_sleep(clock, calls),
        clock=clock,
    )
    assert outcome is procspawn.TerminateResult.KILLED
    assert calls  # the injected sleep was actually driven
    assert not procspawn.is_ours_and_alive(spawned.record)


async def test_terminate_already_dead(tmp_path: Path) -> None:
    spawned = _spawn_py("import sys; sys.exit(0)", tmp_path)
    _wait(spawned.pid)
    outcome = await procspawn.terminate(
        spawned.record,
        term_timeout_s=1.0,
        sleep=_advancing_sleep(_FakeClock(), []),
        clock=time.monotonic,
    )
    assert outcome is procspawn.TerminateResult.ALREADY_DEAD


async def test_terminate_not_ours_does_not_signal(tmp_path: Path) -> None:
    # A live pid whose identity does not match ours (here: the test process itself,
    # with a bogus token). terminate must report not-ours and NEVER signal it.
    record = procspawn.PidRecord(
        pid=os.getpid(),
        argv=("whatever",),
        start_token="not-the-real-token",
        spawn_ts=0.0,
        argv_hash="deadbeef",
    )
    outcome = await procspawn.terminate(
        record,
        term_timeout_s=1.0,
        sleep=_advancing_sleep(_FakeClock(), []),
        clock=time.monotonic,
    )
    assert outcome is procspawn.TerminateResult.NOT_OURS


# --- reap ---------------------------------------------------------------------


def test_reap_clears_an_exited_child_so_liveness_reads_dead(tmp_path: Path) -> None:
    """An exited-but-unreaped direct child is a zombie: it still answers
    ``os.kill(pid, 0)`` (and on Linux keeps its ``/proc`` start-token), so a
    liveness poll that never reaps would read it alive forever — the
    ``launch_slot`` confirm-window bug. ``reap`` is the public fix: once the
    child has exited and been reaped, ``_pid_alive`` reads False."""
    spawned = _spawn_py("pass", tmp_path)
    deadline = time.monotonic() + 10.0
    while procspawn._pid_alive(spawned.pid):
        assert time.monotonic() < deadline, "child never read dead despite reaping"
        procspawn.reap(spawned.pid)
        time.sleep(0.01)
    assert procspawn._pid_alive(spawned.pid) is False


def test_reap_tolerates_a_pid_that_is_not_our_child() -> None:
    """Reaping is a courtesy, never load-bearing: a pid we did not spawn (ECHILD)
    must be a silent no-op, not a raise."""
    procspawn.reap(1)  # init/launchd is definitely not our child


# --- cleanup_stale ----------------------------------------------------------


def test_cleanup_stale_missing_file_is_noop(tmp_path: Path) -> None:
    assert procspawn.cleanup_stale(tmp_path / "absent.pid") is False


def test_cleanup_stale_keeps_a_live_pidfile(tmp_path: Path) -> None:
    spawned = _spawn_py("import time; time.sleep(30)", tmp_path)
    try:
        assert procspawn.cleanup_stale(spawned.pidfile) is False
        assert spawned.pidfile.exists()
    finally:
        os.killpg(spawned.pid, signal.SIGKILL)
        _wait(spawned.pid)


def test_cleanup_stale_removes_a_dead_pidfile(tmp_path: Path) -> None:
    spawned = _spawn_py("import sys; sys.exit(0)", tmp_path)
    _wait(spawned.pid)
    assert procspawn.cleanup_stale(spawned.pidfile) is True
    assert not spawned.pidfile.exists()


def test_cleanup_stale_removes_a_corrupt_pidfile(tmp_path: Path) -> None:
    pidfile = procspawn.pidfile_for(tmp_path, "job")
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("garbage {")
    assert procspawn.cleanup_stale(pidfile) is True
    assert not pidfile.exists()


def test_cleanup_stale_removes_a_not_ours_pidfile(tmp_path: Path) -> None:
    pidfile = procspawn.pidfile_for(tmp_path, "job")
    _write_pidfile_json(
        pidfile,
        {
            "v": 1,
            "pid": os.getpid(),  # alive, but a bogus token → not ours
            "argv": ["whatever"],
            "start_token": "not-the-real-token",
            "spawn_ts": 0.0,
            "argv_hash": "deadbeef",
        },
    )
    assert procspawn.cleanup_stale(pidfile) is True
    assert not pidfile.exists()


# --- path conventions -------------------------------------------------------


def test_pidfile_for_path_shape(tmp_path: Path) -> None:
    assert procspawn.pidfile_for(tmp_path, "alice") == tmp_path / "state" / "run" / "alice.pid"


def test_log_path_for_matches_compose_log_location(tmp_path: Path) -> None:
    # MUST equal compose._log_location so `disco logs` keeps finding the file.
    assert str(procspawn.log_path_for(tmp_path, "alice")) == compose._log_location(
        str(tmp_path), "alice"
    )
