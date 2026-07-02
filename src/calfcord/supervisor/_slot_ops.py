"""The shared locked verb choreography every roster surface runs (DRY hoist).

``roster`` (agents), ``component`` (the ``tools`` singleton), and ``mcp_roster``
(``mcp-<server>`` slots) each carried a byte-parallel copy of the SAME
start/stop/restart critical section: take the slot-mutation locks, check-alive /
terminate / spawn, confirm the spawn survives its window, and translate every
outcome (started/restarted/stopped, crash-on-boot, busy slot, busy workspace)
into the one honest line + exit code. Three copies were three chances to drift
on the locking order or an outcome message, so the choreography lives here once,
parameterized by the only things that genuinely differ per surface:

* ``slot`` — the pidfile/lock name (agent name, ``tools``, ``mcp-<server>``);
* ``argv`` — the launcher command the slot runs under;
* ``label`` — the operator-facing noun (``agent alice`` / ``tools`` /
  ``mcp server github``), so every printed line stays exactly what each surface
  printed before the hoist.

The genuinely distinct pre-flights (workspace check, broker gate, the agent
duplicate guard, mcp.json name validation) stay with their surfaces — these
helpers assume the caller already gated and own ONLY the locked section.

All ``_workspace`` primitives are resolved at call time (``_workspace.launch_slot``
etc.), so the tests' module-level stubs govern here exactly as they did in the
inlined copies. Import-light like the rest of :mod:`calfcord.supervisor`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from calfcord.supervisor import _workspace, procspawn
from calfcord.supervisor.procspawn import TerminateResult


def report_boot_crash(home: str | os.PathLike[str], slot: str, *, label: str) -> None:
    """The honest failure line for a spawn that exited within its confirmation
    window — names the slot's log (where the crash traceback landed)."""
    print(
        f"error: {label} exited immediately after start — "
        f"see {procspawn.log_path_for(home, slot)}"
    )


async def start_slot(
    home: str | os.PathLike[str], slot: str, argv: Sequence[str], *, label: str
) -> int:
    """Spawn (or terminate + re-spawn) ``slot``; workspace/broker already gated.

    The whole check-alive → terminate → spawn → confirm section runs under the
    slot-mutation locks (no double-spawn from concurrent starts; no interleave
    with a ``disco stop`` sweep). **Start of a running slot is a restart**
    (behavior #2 — checked inside the lock so a local instance is never misread).
    The spawn must survive its confirmation window: a crash-on-boot prints an
    honest failure naming the log path and returns ``1``; success says
    ``started``/``restarted`` (presence/registration is the callers' watchers'
    job, never claimed here). A busy slot is a benign duplicate action (``0``);
    a busy workspace is a real refusal (``1``).
    """
    try:
        with _workspace.slot_mutation(home, slot):
            restarted = _workspace.slot_is_live(home, slot)
            if restarted:
                await _workspace.terminate_slot(home, slot)
            if not await _workspace.launch_slot(home, slot, argv):
                report_boot_crash(home, slot, label=label)
                return 1
            print(f"{label} {'restarted' if restarted else 'started'}")
            return 0
    except _workspace.SlotBusyError:
        # A concurrent start already holds the slot: a benign duplicate action,
        # reported honestly rather than double-spawned.
        print(f"{label} is already being started here (another disco command holds its slot).")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1


async def stop_slot(home: str | os.PathLike[str], slot: str, *, label: str) -> int:
    """Terminate ``slot``'s process and clear its pidfile; workspace already gated.

    Runs under the SAME slot-mutation locks the spawn verbs take, so a stop
    racing a start's confirmation window can never unlink the fresh pidfile
    mid-confirm (which would make the starter misreport "exited immediately").
    A busy slot is reported honestly and skipped (benign, like every busy
    outcome). Honest messages: a live slot is ``stopped``; a slot with no live
    process here (never started, already gone, or a stale/recycled pidfile)
    reports ``is not running here`` — the stale record, if any, has been swept.
    """
    try:
        with _workspace.slot_mutation(home, slot):
            result = await _workspace.terminate_slot(home, slot)
    except _workspace.SlotBusyError:
        print(f"{label} is being started/stopped by another disco command; skipped.")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1
    if result in (TerminateResult.TERMINATED, TerminateResult.KILLED):
        print(f"{label} stopped")
    else:
        print(f"{label} is not running here")
    return 0


async def restart_slot(
    home: str | os.PathLike[str], slot: str, argv: Sequence[str], *, label: str
) -> int:
    """Terminate + re-spawn ``slot``; workspace/broker already gated.

    A restart of a not-running slot simply brings it up — the expected effect.
    The terminate + spawn runs under the slot-mutation locks, and the spawn must
    survive its confirmation window — a crash-on-boot reports the log path and
    returns ``1``; otherwise ``<label> restarted``, ``0``. A busy slot is benign
    (``0``); a busy workspace is a real refusal (``1``).
    """
    try:
        with _workspace.slot_mutation(home, slot):
            await _workspace.terminate_slot(home, slot)
            if not await _workspace.launch_slot(home, slot, argv):
                report_boot_crash(home, slot, label=label)
                return 1
            print(f"{label} restarted")
            return 0
    except _workspace.SlotBusyError:
        print(f"{label} is already being restarted here (another disco command holds its slot).")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1
