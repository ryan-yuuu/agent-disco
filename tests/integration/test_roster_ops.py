"""Gated REAL-binary integration test for the roster ops (§3.4-§3.5, §13.1).

The unit tests in ``tests/supervisor/test_roster.py`` drive
:mod:`calfcord.supervisor.roster` with an injected fake client + stub probe —
they are the must-pass contract for the *control flow* (the workspace check, the
duplicate guard, the §13.1 not-a-declared-slot steer). This module is the
complement: it runs the PC-backed half of those ops against a real
``process-compose`` v1.110.0 binary to prove the property only a real binary can
show — that clocking a pre-declared ``disabled`` roster slot in/out/restarting it
goes through the real REST handlers AND leaves the substrate PIDs untouched (the
§13.1 / upstream-#494 PID-stable path).

The control-plane probe is NOT exercised here: it needs a live agent answering
``bridge.discovery`` pings, which CI does not have, and the §3.5 duplicate-guard
path is already covered by the unit tests. We therefore inject a **stub probe
returning ``[]``**, so the duplicate guard is a no-op and the ops fall straight
through to the real Process Compose calls. We also inject a
:class:`ProcessComposeClient` bound to the launched port, so the ops' workspace
check + start/stop/restart hit the throwaway supervisor (and never depend on a
broker or on ``$CALFCORD_HOME``'s derived port).

The substrate is a stub: a ``broker`` sleeper plus a ``keepalive`` sleeper that
exists only so the supervisor (and its REST server) stays up after the ``assistant``
roster slot is stopped — process-compose exits once *all* processes are done,
which would otherwise refuse the post-stop queries. ``assistant`` is the
pre-declared ``disabled: true`` roster slot (a sleeper with an ``exec`` readiness
probe of ``true``) that the ops clock in.

Gated behind ``CALF_TEST_PC`` with ``process-compose`` on PATH (mirrors
``tests/integration/test_lifecycle_supervisor.py``); skips cleanly otherwise::

    CALF_TEST_PC=1 PATH="$HOME/.calfcord/bin:$PATH" \
        uv run pytest tests/integration/test_roster_ops.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.supervisor import roster
from calfcord.supervisor.client import ProcessComposeClient

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_PC") or shutil.which("process-compose") is None,
    reason="set CALF_TEST_PC=1 with `process-compose` on PATH to run the real-binary roster test",
)

# The roster slot the ops clock in/out, and the substrate process whose PID must
# stay stable across that (the §13.1 PID-stability assertion).
_ROSTER_SLOT = "assistant"
_SUBSTRATE_PID_ANCHOR = "broker"

# Bounded polling for state transitions / teardown, so a wedged binary fails the
# test loudly instead of hanging it.
_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.2


async def _no_agents(server_urls: str) -> list[AgentDefinition]:
    """A stub control-plane probe: the org has no live agents.

    With an empty live roster the §3.5 duplicate guard is a no-op, so
    ``agent_start`` falls straight through to the real ``POST /process/start`` —
    isolating the PC-backed path this integration test exists to exercise from
    the broker-dependent probe (covered by the roster unit tests).
    """
    return []


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_project(path: Path, logs: Path) -> None:
    """A substrate-plus-disabled-roster-slot stub project (no broker, no Discord).

    ``broker`` is the PID anchor whose stability we assert; ``keepalive`` keeps
    the supervisor's REST server up after the roster slot is stopped (PC exits
    once *all* processes finish). ``assistant`` is the pre-declared ``disabled``
    roster slot — a sleeper with an ``exec`` readiness probe of ``true`` (mirroring
    the real renderer's roster shape) that the ops clock in via ``POST
    /process/start``, the §13.1 GO path that must leave the substrate PIDs intact.
    """
    probe = (
        "    readiness_probe:\n"
        "      exec:\n"
        '        command: "true"\n'
        "      initial_delay_seconds: 1\n"
        "      period_seconds: 1\n"
        "      timeout_seconds: 2\n"
        "      success_threshold: 1\n"
        "      failure_threshold: 3\n"
    )
    path.write_text(
        "version: '0.5'\n"
        "processes:\n"
        "  broker:\n"
        '    command: "sleep 3600"\n'
        f"    log_location: {logs}/broker.log\n"
        "  keepalive:\n"
        '    command: "sleep 3600"\n'
        f"    log_location: {logs}/keepalive.log\n"
        # The pre-declared roster slot, disabled until it clocks in. It depends on
        # nothing and gates nothing, so starting it must leave the substrate PIDs
        # untouched (§13.1 GO path).
        f"  {_ROSTER_SLOT}:\n"
        '    command: "sleep 3600"\n'
        "    disabled: true\n"
        f"    log_location: {logs}/{_ROSTER_SLOT}.log\n"
        f"{probe}"
    )


async def _poll_until(predicate, *, timeout_s: float = _POLL_TIMEOUT_S) -> bool:
    """Await ``predicate()`` becoming truthy within ``timeout_s`` (no fixed sleep)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False


async def _slot_running(client: ProcessComposeClient) -> bool:
    with contextlib.suppress(RuntimeError, KeyError, TypeError):
        return (await client.get_process(_ROSTER_SLOT)).get("is_running") is True
    return False


async def _slot_stopped(client: ProcessComposeClient) -> bool:
    with contextlib.suppress(RuntimeError, KeyError, TypeError):
        return (await client.get_process(_ROSTER_SLOT)).get("is_running") is not True
    return False


async def _slot_pid(client: ProcessComposeClient) -> int | None:
    with contextlib.suppress(RuntimeError, KeyError, TypeError):
        return (await client.get_process(_ROSTER_SLOT)).get("pid")
    return None


async def test_roster_ops_against_real_process_compose(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    logs = Path(home) / "state" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    project = Path(home) / "state" / "process-compose.yaml"
    _stub_project(project, logs)

    port = _free_port()
    proc = subprocess.Popen(
        [
            "process-compose",
            "up",
            "-f",
            str(project),
            "-D",
            "-t=false",
            "-p",
            str(port),
            "-L",
            str(logs / "process-compose.log"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Inject the port-bound client (so the ops' workspace check + PC calls hit
    # THIS throwaway supervisor, not $CALFCORD_HOME's derived port) and the stub
    # probe (so the §3.5 duplicate guard is a no-op).
    client = ProcessComposeClient(port=port)
    try:
        # The supervisor's REST server binds a beat after the detached `up` forks;
        # wait until the ops' workspace check would pass before driving them.
        async def _supervisor_up() -> bool:
            with contextlib.suppress(RuntimeError):
                await client.project_state()
                return True
            return False

        assert await _poll_until(_supervisor_up), (
            "the detached process-compose REST server never came up"
        )

        broker_pid = (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"]
        assert broker_pid, "the substrate anchor must expose a real OS pid"
        assert not await _slot_running(client), (
            f"the `{_ROSTER_SLOT}` slot must start out disabled (not running)"
        )

        # (a) agent_start clocks the disabled slot in: it returns 0, the slot
        # reaches Running with a real PID, and — the decisive §13.1 / #494
        # property — the substrate anchor's PID is UNCHANGED (no substrate bounce).
        rc = await roster.agent_start(
            home,
            name=_ROSTER_SLOT,
            server_urls="unused-by-stub-probe",
            client=client,
            probe=_no_agents,
        )
        assert rc == 0, "agent_start should return 0 once the disabled slot is started"
        assert await _poll_until(lambda: _slot_running(client)), (
            f"agent_start must bring the disabled `{_ROSTER_SLOT}` slot to Running"
        )
        started_pid = await _slot_pid(client)
        assert started_pid, "the started slot must expose a real OS pid"
        assert (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"] == broker_pid, (
            "clocking a disabled roster slot in must not bounce the substrate (§13.1 / #494)"
        )

        # (b) agent_stop clocks it out (the PATCH /process/stop wire): it returns
        # 0 and the slot stops, still without touching the substrate PID.
        rc_stop = await roster.agent_stop(home, name=_ROSTER_SLOT, client=client)
        assert rc_stop == 0, "agent_stop should return 0"
        assert await _poll_until(lambda: _slot_stopped(client)), (
            f"agent_stop must take the `{_ROSTER_SLOT}` slot out of Running"
        )
        assert (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"] == broker_pid, (
            "stopping a roster slot must not bounce the substrate"
        )

        # (c) agent_restart brings it back with a NEW pid (the POST /process/restart
        # wire), proving restart really cycles the process, not just no-ops.
        rc_restart = await roster.agent_restart(home, name=_ROSTER_SLOT, client=client)
        assert rc_restart == 0, "agent_restart should return 0"
        assert await _poll_until(lambda: _slot_running(client)), (
            f"agent_restart must bring the `{_ROSTER_SLOT}` slot back to Running"
        )
        restarted_pid = await _slot_pid(client)
        assert restarted_pid and restarted_pid != started_pid, (
            "agent_restart must cycle the process to a NEW pid"
        )
        assert (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"] == broker_pid, (
            "restarting a roster slot must not bounce the substrate"
        )
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["process-compose", "down", "-p", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=10)
