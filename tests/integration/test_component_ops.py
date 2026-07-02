"""Real-process integration test for the singleton component ops (Phase 2 spawn model).

The unit tests in ``tests/supervisor/test_component.py`` pin the control flow with
the ``_workspace`` primitives faked out; this is the complement that runs
``component_start`` / ``component_stop`` / ``component_restart`` end-to-end against a
**real OS process** through the real ``_workspace`` → ``procspawn`` chain.

Since the roster (the ``tools`` singleton included) moved OFF Process Compose
(Phase 2), no ``process-compose`` binary and no broker are needed: the substrate
workspace check is satisfied with an injected stub client, and the spawned component
is a throwaway launcher script that just sleeps. This test runs unconditionally.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from calfcord.supervisor import _workspace, component
from calfcord.supervisor.procspawn import pidfile_for, read_pidfile

_COMPONENT_SLOT = "tools"
_POLL_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.1


class _WorkspaceUpClient:
    async def project_state(self):
        return {"status": "ok"}


def _fake_launcher(tmp_path: Path) -> str:
    script = tmp_path / "shims" / "disco"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexec sleep 3600\n")
    script.chmod(0o755)
    return str(script)


async def _poll_until(predicate, *, timeout_s: float = _POLL_TIMEOUT_S) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _broker_up() -> bool:
    """The broker-reachability gate passes — a real broker is the substrate
    lifecycle's own concern, not this spawn-mechanics test's."""
    return True


async def test_component_ops_spawn_terminate_and_cycle(tmp_path: Path) -> None:
    home = str(tmp_path)
    launcher = _fake_launcher(tmp_path)
    client = _WorkspaceUpClient()
    pidfile = pidfile_for(home, _COMPONENT_SLOT)
    spawned_pids: list[int] = []

    try:
        rc = await component.component_start(
            home, name=_COMPONENT_SLOT, launcher=launcher, client=client, broker_probe=_broker_up
        )
        assert rc == 0
        assert await _poll_until(lambda: _workspace.slot_is_live(home, _COMPONENT_SLOT))
        started = read_pidfile(pidfile)
        assert started is not None and _pid_alive(started.pid)
        spawned_pids.append(started.pid)

        rc_stop = await component.component_stop(home, name=_COMPONENT_SLOT, client=client)
        assert rc_stop == 0
        assert await _poll_until(lambda: not pidfile.exists())
        assert not _pid_alive(started.pid)

        rc_restart = await component.component_restart(
            home, name=_COMPONENT_SLOT, launcher=launcher, client=client, broker_probe=_broker_up
        )
        assert rc_restart == 0
        assert await _poll_until(lambda: _workspace.slot_is_live(home, _COMPONENT_SLOT))
        restarted = read_pidfile(pidfile)
        assert restarted is not None and _pid_alive(restarted.pid)
        assert restarted.pid != started.pid
        spawned_pids.append(restarted.pid)
    finally:
        for pid in spawned_pids:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
