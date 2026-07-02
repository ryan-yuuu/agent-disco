"""Per-server MCP lifecycle: ``mcp-<server>`` roster slots clocking in/out.

Each ``mcp.json`` server runs as its own roster slot. Since Phase 2 the roster lives
OFF Process Compose (PC cannot hot-add a process), so a server is SPAWNED as a
detached process (a pidfile under ``state/run/mcp-<server>.pid``) via the shared
:mod:`calfcord.supervisor._workspace` primitives — the same spawn/terminate shape as
the agent roster. A server added to ``mcp.json`` *after* ``disco start`` no longer
needs a workspace reload: as long as the substrate is up, it simply spawns (the
Phase 2 win, mirroring the brand-new-agent path).

What these verbs deliberately lack is the agent-only broker-wide duplicate guard:
two hosts hosting the same toolbox id are competing consumers on one dispatch topic
— a legitimate (if unusual) scale-out, not the agent split-brain where two same-id
agents double-reply in Discord.

``mcp start <server>`` of a live slot restarts it in place (behavior #2), which is
also the documented way to re-apply an edited ``mcp.json`` entry; ``mcp start
--all`` sweeps every *configured* server, making it the "re-pick up mcp.json"
command. Stop/restart sweeps operate on the *running* ``mcp-`` slots instead —
stopping what exists, not what is configured.
"""

from __future__ import annotations

import os

from calfcord.health.check import BrokerProbe
from calfcord.mcp.selector import is_valid_server_name
from calfcord.supervisor import _slot_ops, _workspace, procspawn
from calfcord.supervisor._workspace import (
    BROKER_UNREACHABLE_HINT,
    WORKSPACE_NOT_RUNNING_HINT,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient
from calfcord.supervisor.compose import MCP_SLOT_PREFIX
from calfcord.supervisor.compose import mcp_slot_name as slot_name
from calfcord.supervisor.procspawn import TerminateResult

_NOT_RUNNING_HINT = WORKSPACE_NOT_RUNNING_HINT

# The one shared broker-gate refusal, aliased like the not-running hint.
_BROKER_UNREACHABLE_HINT = BROKER_UNREACHABLE_HINT


def _mcp_argv(launcher: str, server: str) -> list[str]:
    """The argv that spawns MCP server ``server`` — the SAME command the PC slot ran
    (``<launcher> run mcp <server>``)."""
    return [launcher, "run", "mcp", server]


def _label(server: str) -> str:
    """The operator-facing noun for ``server`` in every printed outcome line."""
    return f"mcp server {server}"


def _check_server_name(server: str) -> bool:
    """Refuse a name that could never be a valid slot, pre-spawn."""
    if is_valid_server_name(server):
        return True
    print(
        f"error: invalid MCP server name {server!r}; "
        "must match [a-z0-9_]{1,64} (an mcp.json key)"
    )
    return False


def _live_mcp_slots(home: str | os.PathLike[str]) -> set[str]:
    """This host's ``mcp-`` slots with an ours-and-alive pidfile."""
    return {slot for slot in _workspace.live_slots(home) if slot.startswith(MCP_SLOT_PREFIX)}


def running_servers(home: str | os.PathLike[str]) -> set[str]:
    """Bare server names of this host's running MCP slots.

    The public read for anything outside this module (``mcp list``'s state column):
    callers get server names, never slot names, so the ``mcp-`` prefix convention
    stays encapsulated here. Reads the pidfile namespace directly (no REST), so the
    caller is responsible for its own workspace-up gating if it wants one.
    """
    return {slot.removeprefix(MCP_SLOT_PREFIX) for slot in _live_mcp_slots(home)}


async def _start_or_restart(home: str, launcher: str, server: str) -> int:
    """Spawn (or terminate + re-spawn) one server; workspace + broker already gated.

    The shared locked choreography (:func:`_slot_ops.start_slot`) does the work;
    this adapter only maps the bare server name onto its ``mcp-<server>`` slot,
    argv, and printed noun.
    """
    return await _slot_ops.start_slot(
        home, slot_name(server), _mcp_argv(launcher, server), label=_label(server)
    )


async def mcp_start(
    home: str | os.PathLike[str],
    *,
    server: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Bring MCP server ``server`` up; a live slot is restarted in place.

    Returns a POSIX exit code. Workspace check first, then the broker gate (the
    same probe ``lifecycle.start`` uses — the old PC slot's broker dependency,
    re-imposed off PC); start-of-running is a restart (behavior #2 — also the
    edited-entry pickup). A server added to ``mcp.json`` after ``disco start``
    simply spawns (no reload needed off Process Compose).
    """
    if not _check_server_name(server):
        return 1
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Legacy-workspace guard (fail-open): an old-main supervisor still runs the
    # mcp- slots under PC — spawning beside it would duplicate the server.
    if await _workspace.legacy_pc_roster(client):
        print(_workspace.LEGACY_WORKSPACE_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    return await _start_or_restart(home, launcher, server)


async def mcp_stop(
    home: str | os.PathLike[str],
    *,
    server: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take MCP server ``server`` offline (terminate its process).

    The terminate runs under the same slot-mutation locks the spawn verbs take,
    so a stop racing a start's confirmation window never unlinks the fresh
    pidfile mid-confirm; a busy slot is reported honestly and skipped (benign).
    """
    if not _check_server_name(server):
        return 1
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    return await _slot_ops.stop_slot(home, slot_name(server), label=_label(server))


async def mcp_restart(
    home: str | os.PathLike[str],
    *,
    server: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Reload MCP server ``server`` after an mcp.json edit (terminate + re-spawn).

    Terminates any live instance and spawns a fresh one (a stopped slot restarting
    back up is the expected effect). Broker-gated like every spawn verb, run under
    the slot-mutation locks, and the spawn must survive its confirmation window —
    a crash-on-boot reports the log path and returns ``1``.
    """
    if not _check_server_name(server):
        return 1
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Legacy-workspace guard (fail-open), like every spawn verb.
    if await _workspace.legacy_pc_roster(client):
        print(_workspace.LEGACY_WORKSPACE_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    return await _slot_ops.restart_slot(
        home, slot_name(server), _mcp_argv(launcher, server), label=_label(server)
    )


async def mcp_start_all(
    home: str | os.PathLike[str],
    *,
    servers: list[str],
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Spawn (or restart-in-place) every *configured* server — the
    "re-pick up mcp.json" sweep.

    ``servers`` is the caller-enumerated mcp.json name list (the CLI reads it via the
    no-secrets ``list_server_names``). No per-name validation here: those names come
    from the validated readers. The broker is gated ONCE for the whole sweep.
    """
    if not servers:
        print("no MCP servers configured; add one with `disco mcp add`")
        return 0
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Legacy-workspace guard and broker gate, ONCE for the whole sweep.
    if await _workspace.legacy_pc_roster(client):
        print(_workspace.LEGACY_WORKSPACE_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    worst = 0
    for server in servers:
        worst = max(worst, await _start_or_restart(home, launcher, server))
    return worst


async def mcp_stop_all(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
) -> int:
    """Stop every *running* ``mcp-`` slot on this host."""
    return await _sweep_running(home, launcher=None, client=client, verb="stop")


async def mcp_restart_all(
    home: str | os.PathLike[str],
    *,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Restart every *running* ``mcp-`` slot on this host."""
    return await _sweep_running(home, launcher=launcher, client=client, verb="restart", broker_probe=broker_probe)


async def _sweep_running(
    home: str | os.PathLike[str],
    *,
    launcher: str | None,
    client: ProcessComposeClient | None,
    verb: str,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Stop or restart every live ``mcp-`` slot (the shared sweep body).

    The restart sweep is broker-gated ONCE (a spawn verb); the stop sweep is not
    (termination needs no broker). Both stops and restarts run under the per-slot
    mutation locks; a BUSY slot is reported and skipped (benign, never a counted
    failure), and each restart's spawn must survive its confirmation window — a
    crash-on-boot is reported per server and turns the sweep's exit code non-zero.
    A contended LIFECYCLE lock (a ``disco start``/``stop`` in flight) aborts the
    stop sweep with ONE honest error (agent-sweep semantics) — it is
    workspace-wide, so every remaining slot would refuse identically.
    """
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Restart is a spawn verb, so it carries the spawn pre-flights (the stop sweep
    # needs neither: termination stays usable on a legacy workspace).
    if verb == "restart":
        if await _workspace.legacy_pc_roster(client):
            print(_workspace.LEGACY_WORKSPACE_HINT)
            return 1
        if not await _workspace.broker_gate(None, broker_probe):
            print(_BROKER_UNREACHABLE_HINT)
            return 1

    try:
        running = sorted(_live_mcp_slots(home))
    except _workspace.SlotScanError as exc:
        # An unreadable state/run is a scan failure — never "no servers running".
        print(f"error: {exc}")
        return 1
    if not running:
        print("no MCP servers running on this host")
        return 0
    worst = 0
    for slot in running:
        server = slot.removeprefix(MCP_SLOT_PREFIX)
        if verb == "stop":
            try:
                with _workspace.slot_mutation(home, slot):
                    result = await _workspace.terminate_slot(home, slot)
            except _workspace.SlotBusyError:
                print(f"mcp server {server} is being started/stopped by another disco command; skipped.")
                continue
            except _workspace.WorkspaceBusyError as exc:
                # Workspace-wide: every remaining slot would refuse identically,
                # so one honest error closes the sweep (agent-sweep semantics).
                print(f"error: {exc}")
                return 1
            if result is TerminateResult.KILL_UNCONFIRMED:
                print(
                    f"error: mcp server {server} did not die after SIGKILL — still "
                    f"running (see {procspawn.log_path_for(home, slot)})"
                )
                worst = 1
                continue
            if result is TerminateResult.INDETERMINATE:
                # terminate_slot already printed the cannot-verify warning.
                worst = 1
                continue
            if result is not None and result.process_was_stopped:
                print(f"mcp server {server} stopped")
            else:
                print(f"mcp server {server} is not running here")
            continue
        worst = max(worst, await _start_or_restart(home, launcher, server))
    return worst
