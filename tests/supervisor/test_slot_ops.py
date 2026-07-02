"""Unit tests for the shared locked verb choreography (:mod:`_slot_ops`).

The surfaces' own test files (``test_roster`` / ``test_component`` /
``test_mcp_roster``) exercise start/stop/restart end-to-end through their verbs
with an in-memory fake of the ``_workspace`` primitives. Pinned HERE is the one
contract those fakes cannot express: ``start_slot``'s check-alive must read the
slot's REAL tri-state identity. A live owned process whose identity read flakes
(``Identity.INDETERMINATE`` — e.g. a transient darwin ``ps`` failure) must be
REFUSED, never collapsed to "not running" — the collapse skips the terminate AND
the wedged-survivor guard, overwrites the survivor's pidfile, and spawns a
duplicate beside it (violating INDETERMINATE's "never remove its pidfile"
contract). The three parametrized shapes are the exact argv each surface hands
``start_slot``, so the refusal is pinned for every caller.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from calfcord.supervisor import _slot_ops, _workspace, procspawn

# --- helpers (the test_workspace pidfile idioms) ------------------------------


def _write_self_pidfile(home, slot: str):
    """Write a pidfile naming THIS (alive, ours) test process for ``slot``."""
    record = procspawn._identity_for(os.getpid(), ("self",))
    pidfile = procspawn.pidfile_for(home, slot)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(json.dumps(procspawn._record_to_dict(record)), encoding="utf-8")
    return pidfile


def _write_dead_pidfile(home, slot: str):
    """Write a pidfile naming a definitely-dead pid (a reaped child) for ``slot``."""
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


class _SpiedPrimitives:
    """Recording stand-ins for launch/terminate so no real process is spawned or
    signalled; everything BELOW them (identity, pidfiles, locks) stays real."""

    def __init__(self, monkeypatch) -> None:
        self.launched: list[tuple[str, list[str]]] = []
        self.terminated: list[str] = []

        async def launch_slot(home, slot, argv, **_kwargs):
            self.launched.append((slot, list(argv)))
            return True

        async def terminate_slot(home, slot):
            self.terminated.append(slot)
            procspawn.pidfile_for(home, slot).unlink(missing_ok=True)
            return procspawn.TerminateResult.TERMINATED

        monkeypatch.setattr(_workspace, "launch_slot", launch_slot)
        monkeypatch.setattr(_workspace, "terminate_slot", terminate_slot)


# The exact argv shape each surface hands start_slot (roster._agent_argv /
# component._component_argv / mcp_roster._mcp_argv) with its slot and label.
_LAUNCHER = "/h/shims/disco"
_SURFACE_SHAPES = [
    pytest.param(
        "assistant", [_LAUNCHER, "run", "agent", "assistant"], "agent assistant", id="agent"
    ),
    pytest.param("tools", [_LAUNCHER, "run", "tools"], "tools", id="tools"),
    pytest.param(
        "mcp-github", [_LAUNCHER, "run", "mcp", "github"], "mcp server github", id="mcp"
    ),
]


# --- the verifier's repro: INDETERMINATE at check-alive ------------------------


@pytest.mark.parametrize(("slot", "argv", "label"), _SURFACE_SHAPES)
async def test_start_slot_refuses_an_unverifiable_live_slot(
    tmp_path, capsys, monkeypatch, slot, argv, label
):
    """A live owned process whose token read flakes (pid-selective — only the
    recorded pid's read fails) must refuse the start: no spawn, no terminate,
    pidfile untouched, rc 1 with the honest cannot-verify line."""
    pidfile = _write_self_pidfile(tmp_path, slot)
    before = pidfile.read_text(encoding="utf-8")
    record = procspawn.read_pidfile(pidfile)
    assert record is not None and record.start_token  # a provable record...
    real_token = procspawn._process_start_token
    monkeypatch.setattr(  # ...whose re-read flakes, for this pid only
        procspawn,
        "_process_start_token",
        lambda pid: None if pid == record.pid else real_token(pid),
    )
    spies = _SpiedPrimitives(monkeypatch)

    rc = await _slot_ops.start_slot(str(tmp_path), slot, argv, label=label)

    assert rc == 1
    assert spies.launched == []  # never a duplicate beside the survivor
    assert spies.terminated == []  # never signal what cannot be verified
    assert pidfile.read_text(encoding="utf-8") == before  # record untouched
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "verif" in out  # "cannot ... verified"
    assert f"{slot}.log" in out  # points at the slot's log, like the wedged path
    assert "started" not in out


# --- the two honest neighbours of the unknown middle ---------------------------


@pytest.mark.parametrize(("slot", "argv", "label"), _SURFACE_SHAPES)
async def test_start_slot_restarts_a_provably_ours_live_slot(
    tmp_path, capsys, monkeypatch, slot, argv, label
):
    """OURS at check-alive is the documented start-of-running-is-a-restart path:
    terminate first, then spawn, reported as ``restarted``."""
    _write_self_pidfile(tmp_path, slot)
    spies = _SpiedPrimitives(monkeypatch)

    rc = await _slot_ops.start_slot(str(tmp_path), slot, argv, label=label)

    assert rc == 0
    assert spies.terminated == [slot]
    assert spies.launched == [(slot, list(argv))]
    assert f"{label} restarted" in capsys.readouterr().out


@pytest.mark.parametrize(("slot", "argv", "label"), _SURFACE_SHAPES)
async def test_start_slot_fresh_starts_over_a_provably_stale_pidfile(
    tmp_path, capsys, monkeypatch, slot, argv, label
):
    """NOT_OURS (a dead/recycled pid) is a fresh start: nothing to terminate,
    the stale record is simply superseded by the spawn."""
    _write_dead_pidfile(tmp_path, slot)
    spies = _SpiedPrimitives(monkeypatch)

    rc = await _slot_ops.start_slot(str(tmp_path), slot, argv, label=label)

    assert rc == 0
    assert spies.terminated == []
    assert spies.launched == [(slot, list(argv))]
    assert f"{label} started" in capsys.readouterr().out
