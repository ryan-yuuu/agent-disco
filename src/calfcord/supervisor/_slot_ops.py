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


def report_wedged_survivor(home: str | os.PathLike[str], slot: str, *, label: str) -> None:
    """The refusal for a pre-spawn terminate that did not actually clear the slot:
    spawning a replacement beside a surviving process would double-run it (and
    overwrite the survivor's pidfile, orphaning it from the tooling)."""
    print(
        f"error: {label} is still running (its process could not be stopped or "
        f"verified) — not spawning a replacement; see {procspawn.log_path_for(home, slot)}"
    )


def report_unverifiable_slot(home: str | os.PathLike[str], slot: str, *, label: str) -> None:
    """The refusal for a check-alive that read ``Identity.INDETERMINATE``: the
    slot's recorded process is live but could not be verified as ours (a
    transient identity-read failure). Acting on the unknown either way would be
    destructive — terminating could kill a stranger, spawning would overwrite
    the survivor's pidfile and double-run it — so the slot is left untouched,
    exactly like the wedged-survivor refusal."""
    print(
        f"error: cannot verify {label}'s recorded process (its identity could not "
        f"be read) — leaving it untouched and not spawning beside it; "
        f"see {procspawn.log_path_for(home, slot)}"
    )


def report_missing_launcher(argv: Sequence[str], *, label: str) -> None:
    """The one actionable line for a spawn whose launcher binary does not exist."""
    print(
        f"error: cannot start {label}: launcher not found ({argv[0]}) — "
        "re-run the Agent Disco installer, or check the install's shims directory."
    )


def _terminate_left_slot_running(
    home: str | os.PathLike[str], slot: str, result: TerminateResult | None
) -> bool:
    """Whether a pre-spawn terminate failed to clear ``slot``.

    True for the two keep-the-pidfile outcomes (``KILL_UNCONFIRMED`` — the process
    shrugged off SIGKILL; ``INDETERMINATE`` — it could not even be verified) and,
    belt-and-braces, whenever the slot still READS live afterwards.
    """
    if result in (TerminateResult.KILL_UNCONFIRMED, TerminateResult.INDETERMINATE):
        return True
    return _workspace.slot_is_live(home, slot)


async def _launch_or_report(
    home: str | os.PathLike[str], slot: str, argv: Sequence[str], *, label: str
) -> int:
    """Launch ``slot`` inside an already-held critical section; print the honest
    failure (crash-on-boot, missing launcher) and return the exit code."""
    try:
        launched = await _workspace.launch_slot(home, slot, argv)
    except FileNotFoundError:
        report_missing_launcher(argv, label=label)
        return 1
    if not launched:
        report_boot_crash(home, slot, label=label)
        return 1
    return 0


async def start_slot(
    home: str | os.PathLike[str], slot: str, argv: Sequence[str], *, label: str
) -> int:
    """Spawn (or terminate + re-spawn) ``slot``; workspace/broker already gated.

    The whole check-alive → terminate → spawn → confirm section runs under the
    slot-mutation locks (no double-spawn from concurrent starts; no interleave
    with a ``disco stop`` sweep). Check-alive reads the TRI-STATE identity
    (:func:`_workspace.slot_identity`), never the collapsed boolean, because the
    three verdicts demand three different actions:

    * ``OURS`` — **start of a running slot is a restart** (behavior #2 — decided
      inside the lock so a local instance is never misread): terminate first; if
      that terminate cannot clear the slot (a wedged survivor), the spawn is
      REFUSED (``1``) rather than doubling the process.
    * ``INDETERMINATE`` — a live pid whose identity read failed: REFUSED (``1``)
      without terminating or spawning, mirroring :func:`restart_slot`'s honesty
      (there the same verdict surfaces via the terminate). Collapsing it to "not
      running" would skip both guards, overwrite the survivor's pidfile, and
      spawn a duplicate beside a process that may well be ours.
    * ``NOT_OURS``/no record — a fresh start; the stale record (if any) is
      simply superseded by the spawn.

    The spawn must survive its confirmation window: a crash-on-boot prints an
    honest failure naming the log path and returns ``1``; success says
    ``started``/``restarted`` (presence/registration is the callers' watchers'
    job, never claimed here). A busy slot is a benign duplicate action (``0``);
    a busy workspace is a real refusal (``1``).
    """
    try:
        with _workspace.slot_mutation(home, slot):
            verdict = _workspace.slot_identity(home, slot)
            if verdict is procspawn.Identity.INDETERMINATE:
                report_unverifiable_slot(home, slot, label=label)
                return 1
            restarted = verdict is procspawn.Identity.OURS
            if restarted:
                result = await _workspace.terminate_slot(home, slot)
                if _terminate_left_slot_running(home, slot, result):
                    report_wedged_survivor(home, slot, label=label)
                    return 1
            if await _launch_or_report(home, slot, argv, label=label) != 0:
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
    if result is TerminateResult.KILL_UNCONFIRMED:
        # SIGKILL was sent but the pid never read dead within the reap window: the
        # process survives and its pidfile is kept — claiming success here would
        # leave an invisible runaway.
        print(
            f"error: {label} did not die after SIGKILL — still running "
            f"(see {procspawn.log_path_for(home, slot)})"
        )
        return 1
    if result is TerminateResult.INDETERMINATE:
        # terminate_slot already printed the cannot-verify warning; the verb's
        # verdict is an honest non-success, not a fake "not running here".
        print(f"error: {label} was left untouched (its process could not be verified).")
        return 1
    if result is not None and result.process_was_stopped:
        print(f"{label} stopped")
    else:
        print(f"{label} is not running here")
    return 0


async def restart_slot(
    home: str | os.PathLike[str], slot: str, argv: Sequence[str], *, label: str
) -> int:
    """Terminate + re-spawn ``slot``; workspace/broker already gated.

    A restart of a not-running slot simply brings it up — the expected effect.
    The terminate + spawn runs under the slot-mutation locks; a terminate that
    cannot clear the slot (a wedged survivor) refuses the re-spawn (``1``), and
    the spawn must survive its confirmation window — a crash-on-boot reports the
    log path and returns ``1``; otherwise ``<label> restarted``, ``0``. A busy
    slot is benign (``0``); a busy workspace is a real refusal (``1``).
    """
    try:
        with _workspace.slot_mutation(home, slot):
            result = await _workspace.terminate_slot(home, slot)
            if _terminate_left_slot_running(home, slot, result):
                report_wedged_survivor(home, slot, label=label)
                return 1
            if await _launch_or_report(home, slot, argv, label=label) != 0:
                return 1
            print(f"{label} restarted")
            return 0
    except _workspace.SlotBusyError:
        print(f"{label} is already being restarted here (another disco command holds its slot).")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1
