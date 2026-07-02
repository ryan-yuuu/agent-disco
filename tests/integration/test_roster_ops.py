"""Real-process integration test for the roster ops (Phase 2 spawn model).

The unit tests in ``tests/supervisor/test_roster.py`` drive
:mod:`calfcord.supervisor.roster` with the ``_workspace`` spawn/terminate
primitives faked out — they pin the *control flow* (the workspace check, the
duplicate guard, the restart-in-place behavior). This module is the complement: it
runs the ops end-to-end against **real OS processes** through the real
``_workspace`` → ``procspawn`` chain, to prove the property only real processes can
show — that ``agent_start`` spawns a detached child (a pidfile appears, the process
is alive), ``agent_stop`` terminates it (the pidfile is cleared, the process is
gone), and ``agent_restart`` cycles it to a NEW pid.

Since the roster moved OFF Process Compose (Phase 2), no ``process-compose`` binary
and no broker are needed: the SUBSTRATE workspace check is satisfied with an
injected stub client (a real substrate is the substrate lifecycle's own concern,
covered by ``test_lifecycle_supervisor.py``), the §3.5 duplicate guard is a no-op
via a stub probe returning ``[]``, and the spawned "agent" is a throwaway launcher
script that just sleeps. This test therefore runs unconditionally (no env gate).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from calfcord.supervisor import _workspace, roster
from calfcord.supervisor.procspawn import pidfile_for, read_pidfile

_ROSTER_SLOT = "assistant"
_POLL_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.1


class _WorkspaceUpClient:
    """A stub REST client whose ``project_state`` always answers — the substrate is
    "up" so the roster ops proceed to the real spawn/terminate."""

    async def project_state(self):
        return {"status": "ok"}


async def _empty_probe(server_urls: str) -> list[str]:
    """The org has no live agents, so the §3.5 duplicate guard is a no-op."""
    return []


async def _broker_up() -> bool:
    """The broker-reachability gate passes — a real broker is the substrate
    lifecycle's own concern, not this spawn-mechanics test's."""
    return True


def _fake_launcher(tmp_path: Path) -> str:
    """A launcher shim standing in for ``<home>/shims/disco``: it ignores its args
    (``run agent <name>``) and execs a long sleep, so the spawned slot is a real,
    terminable process we fully control."""
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


async def test_roster_ops_spawn_terminate_and_cycle(tmp_path: Path) -> None:
    home = str(tmp_path)
    launcher = _fake_launcher(tmp_path)
    client = _WorkspaceUpClient()
    pidfile = pidfile_for(home, _ROSTER_SLOT)
    spawned_pids: list[int] = []

    try:
        # (a) agent_start spawns a real detached child: pidfile appears + alive.
        rc = await roster.agent_start(
            home,
            name=_ROSTER_SLOT,
            server_urls="unused",
            launcher=launcher,
            client=client,
            probe=_empty_probe,
            broker_probe=_broker_up,
        )
        assert rc == 0
        assert await _poll_until(lambda: _workspace.slot_is_live(home, _ROSTER_SLOT))
        started = read_pidfile(pidfile)
        assert started is not None and _pid_alive(started.pid)
        spawned_pids.append(started.pid)

        # (b) agent_stop terminates it: pidfile cleared + process gone.
        rc_stop = await roster.agent_stop(home, name=_ROSTER_SLOT, client=client)
        assert rc_stop == 0
        assert await _poll_until(lambda: not pidfile.exists())
        assert not _pid_alive(started.pid)

        # (c) agent_restart brings it back with a NEW pid.
        rc_restart = await roster.agent_restart(
            home, name=_ROSTER_SLOT, launcher=launcher, client=client, broker_probe=_broker_up
        )
        assert rc_restart == 0
        assert await _poll_until(lambda: _workspace.slot_is_live(home, _ROSTER_SLOT))
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
