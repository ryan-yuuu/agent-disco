"""Gated REAL-binary integration test for the substrate lifecycle (§13.1-§13.3).

The unit tests in ``tests/supervisor/test_lifecycle.py`` drive
:mod:`calfcord.supervisor.lifecycle` with an injected fake client + spawn — they
are the must-pass contract for the *control flow*. This module is the complement:
it runs the supervisor's actual mechanics against a real ``process-compose``
v1.110.0 binary to prove the Phase-0 properties that only a real binary can show
(substrate PIDs staying stable across the #494 priming reconcile AND a subsequent
real reconcile that adds a process, the detached-up readiness gate, idempotency,
the lock, teardown).

The real bridge needs a Discord connection (unavailable in CI), so we do NOT
start the real substrate. Instead we seam the YAML generation
(:func:`lifecycle.render_compose`, the import the module renders through) to a
**stub project**: ``broker`` and ``bridge`` are long-lived sleepers whose exec
``readiness_probe`` always succeeds (``true``), plus a ``disabled`` ``extra`` slot
that clocks in later. That exercises every real supervisor path — detached
``up``, the priming ``POST /project`` reconcile, the bridge-readiness gate, a real
PID-stable reconcile (the disabled-slot ``POST /process/start``), the lock,
``down`` — with zero external dependencies (a local broker, for ``start``'s §13.2
fast-fail precondition, is the one exception).

Gated behind ``CALF_TEST_PC`` with ``process-compose`` on PATH (mirrors
``tests/integration/test_pc_client.py``); skips cleanly otherwise::

    CALF_TEST_PC=1 PATH="$HOME/.calfcord/bin:$PATH" \
        uv run pytest tests/integration/test_lifecycle_supervisor.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from calfcord.supervisor import lifecycle
from calfcord.supervisor.client import ProcessComposeClient

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_PC") or shutil.which("process-compose") is None,
    reason="set CALF_TEST_PC=1 with `process-compose` on PATH to run the real-binary lifecycle test",
)

# A short readiness gate: the stub probes are instant (`true`), so the bridge is
# Ready within a couple of poll periods — no need for the production 90s budget.
_READY_TIMEOUT_S = 30.0
# Bounded polling for teardown / state transitions, so a wedged binary fails the
# test loudly instead of hanging it.
_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.2


def _stub_compose(home: str) -> str:
    """A substrate-shaped stub project: sleeper broker + bridge + a disabled slot.

    Mirrors the real renderer's substrate shape (restart policy, depends_on,
    exec readiness probe, log locations) so the supervisor mechanics are
    exercised faithfully — but the commands are bare sleepers and the probes are
    ``true``, so neither Discord nor a broker is needed and ``bridge`` reaches
    ``is_ready: Ready`` on its own. Byte-stable across calls, so the priming
    reconcile inside ``start`` is a genuine no-op (the #494 mitigation).

    It also pre-declares an ``extra`` process as ``disabled: true`` — the same
    shape onboarding uses for a roster slot that clocks in later. Starting it via
    ``POST /process/start/{name}`` (NOT a ``project update``) is the §13.1 GO path:
    a *real* state change to the running project that leaves the substrate PIDs
    untouched, which is what the #494 PID-stability assertion exercises. (An
    ``update_project`` that genuinely changes the process set is NOT PID-stable on
    v1.110.0 — it reclassifies every process as "updated" and bounces the
    substrate — so the byte-identical priming reconcile is the only PID-stable
    ``update_project``; the disabled-slot start is the PID-stable real reconcile.)
    """
    logs = os.path.join(home, "state", "logs")
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
    return (
        "version: '0.5'\n"
        "processes:\n"
        "  broker:\n"
        '    command: "sleep 3600"\n'
        f"    log_location: {logs}/broker.log\n"
        f"{probe}"
        "  bridge:\n"
        '    command: "sleep 3600"\n'
        f"    log_location: {logs}/bridge.log\n"
        "    depends_on:\n"
        "      broker:\n"
        "        condition: process_healthy\n"
        f"{probe}"
        # A pre-declared roster slot, disabled until it clocks in. It depends on
        # nothing and gates nothing, so starting it must leave broker/bridge (and
        # their PIDs) untouched (§13.1 GO path).
        "  extra:\n"
        '    command: "sleep 3600"\n'
        "    disabled: true\n"
        f"    log_location: {logs}/extra.log\n"
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


async def _supervisor_down(client: ProcessComposeClient) -> bool:
    """True once the REST server stops answering (the project has been torn down)."""
    try:
        await client.project_state()
    except RuntimeError:
        return True
    return False


def _down(home: str) -> None:
    """Best-effort ``process-compose down`` for the home's derived port."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["process-compose", "down", "-p", str(lifecycle.pc_port_for(home))],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )


async def test_lifecycle_against_real_process_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = str(tmp_path / "home")
    os.makedirs(os.path.join(home, "state", "logs"), exist_ok=True)

    # Seam the renderer to the stub substrate (no Discord, no broker needed). The
    # lifecycle renders through `lifecycle.render_compose`, so patching that one
    # name swaps the project while leaving every real supervisor path intact.
    monkeypatch.setattr(lifecycle, "render_compose", lambda **_: _stub_compose(home))

    client = ProcessComposeClient(port=lifecycle.pc_port_for(home))
    try:
        # (a) start brings the project up detached and the readiness gate passes.
        rc = await lifecycle.start(
            home,
            server_urls="localhost:9092",
            launcher="unused-by-stub",
            ready_timeout_s=_READY_TIMEOUT_S,
        )
        assert rc == 0, "start should return 0 once the bridge readiness gate passes"

        broker = await client.get_process("broker")
        bridge = await client.get_process("bridge")
        assert broker.get("is_running")
        assert bridge.get("is_ready") == "Ready" or bridge.get("status") == "Running"
        broker_pid = broker["pid"]
        assert broker_pid, "broker must expose a real OS pid"

        # (b) the priming reconcile (inside start) plus a subsequent GENUINE
        # state change to the running project leave the broker OS PID UNCHANGED —
        # the decisive #494 / §13.1 PID-stability property. The real reconcile we
        # exercise is the documented GO path: clocking in the pre-declared
        # `disabled` slot via POST /process/start (NOT a project-update — on
        # v1.110.0 an update that genuinely changes the process set bounces the
        # whole substrate, so the disabled-slot start is the PID-stable real path).
        assert not (await client.get_process("extra")).get("is_running"), (
            "the `extra` slot must start out disabled (not running)"
        )
        await client.start_process("extra")

        async def _extra_running() -> bool:
            with contextlib.suppress(RuntimeError, KeyError, TypeError):
                return (await client.get_process("extra")).get("is_running") is True
            return False

        # The start is async server-side; wait until the slot is actually running
        # before asserting PID stability — no fixed sleep that could race it.
        assert await _poll_until(_extra_running), (
            "starting the disabled `extra` slot must bring it up (real reconcile)"
        )
        assert (await client.get_process("broker"))["pid"] == broker_pid, (
            "starting a disabled roster slot must not bounce the broker (§13.1 / #494)"
        )

        # (c) a second start is idempotent: it short-circuits (no second
        # supervisor), returns 0, and the broker pid is unchanged.
        rc2 = await lifecycle.start(
            home,
            server_urls="localhost:9092",
            launcher="unused-by-stub",
            ready_timeout_s=_READY_TIMEOUT_S,
        )
        assert rc2 == 0
        assert (await client.get_process("broker"))["pid"] == broker_pid, (
            "a second start must not relaunch the supervisor (idempotent by home)"
        )

        # (d) the lockfile blocks a concurrent start: while the exclusive
        # lifecycle lock is held, start cannot acquire it and REFUSES cleanly —
        # one error line, exit 1, never a raw traceback (flock is per
        # open-file-description, so a second os.open in-process is blocked too).
        with lifecycle.lifecycle_lock(home):
            rc_blocked = await lifecycle.start(
                home,
                server_urls="localhost:9092",
                launcher="unused-by-stub",
                ready_timeout_s=_READY_TIMEOUT_S,
            )
        assert rc_blocked == 1
        # The supervisor is untouched by the blocked attempt.
        assert (await client.get_process("broker"))["pid"] == broker_pid

        # (e) stop tears the substrate down...
        rc_stop = await lifecycle.stop(home)
        assert rc_stop == 0
        assert await _poll_until(lambda: _supervisor_down(client)), (
            "stop must take the supervisor (and its REST server) down"
        )

        # ...and is idempotent on a now-cold system: a second stop is a clean no-op.
        rc_stop2 = await lifecycle.stop(home)
        assert rc_stop2 == 0
    finally:
        _down(home)
