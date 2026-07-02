"""Roster operations: a teammate clocking in/out of the running office (§3.4-§3.5).

These are the imperative glue that brings a *defined* agent online, takes it
offline, reloads it, and reports what is running. Since Phase 2 the roster lives
OFF Process Compose: PC cannot hot-add a process (a ``POST /project`` bounces every
PID — proven on the pinned and latest builds), so instead of a ``POST
/process/start`` these ops **spawn a detached process per agent** via
:mod:`procspawn` (a pidfile under ``state/run/<name>.pid``, no auto-respawn: a
crash shows the agent offline and an operator restarts it). The shared
spawn/terminate/scan glue is :mod:`calfcord.supervisor._workspace`.

Every world-touching dependency is still injected — the broker-wide live-roster
probe (a read of calfkit's native mesh, :func:`_probe_live_roster`), the Process
Compose REST client (only for the substrate workspace check), and the clock — so
the whole flow is unit-testable with no real ``process-compose`` binary, no broker,
and (with the procspawn primitives stubbed) no real child processes.

Three design contracts from the redesign live here:

* **Workspace check first.** Every op probes the local supervisor's REST surface
  (``project_state``) before doing anything else: the SUBSTRATE (broker + bridge)
  must be open for a roster member to be worth clocking in/out. The client raises
  ``RuntimeError`` on a transport failure, which is exactly "the office isn't open"
  — mapped to a one-line actionable hint, not a traceback.

* **Distributed-correct duplicate guard, CLI-side only (§3.5).** ``agent_start``
  first asks the *broker-wide* live roster (the probe) whether this name is already
  answering anywhere — including another host. If so it refuses to start a
  duplicate (which the bridge would otherwise accept as a benign re-announce,
  yielding double-replies / split-brain A2A) and returns ``0``: a duplicate start
  is a benign no-op, not a failure. No bridge change is required — the guard reads
  the wire, not the bridge's memory.

* **A brand-new agent just spawns (Phase 2 win).** An agent authored *after*
  ``disco start`` used to need a full workspace reload (PC had no live slot for
  it). Off PC there is no slot to pre-declare: as long as the substrate is up, the
  agent is spawned directly — no reload, no ``update_project``.

``agent_ps`` renders the §3.4 union: the LOGICAL view (agents answering across the
whole org, from the probe) unioned with the PHYSICAL view (this host's live agent
pidfiles). The cross-product yields three states — running+registered,
started-but-not-yet-registered (physical only), and running on another host
(logical only, expected under multi-host, NOT an error).

Import-light like the rest of this package, so it stays cheap to import from the
CLI entry point.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

from calfkit.client import Client

from calfcord.health.check import BrokerProbe
from calfcord.supervisor import _workspace, procspawn
from calfcord.supervisor._workspace import (
    WORKSPACE_NOT_RUNNING_HINT,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient
from calfcord.supervisor.compose import _RESERVED_PROCESS_NAMES as _NON_AGENT_PROCESSES
from calfcord.supervisor.compose import MCP_SLOT_PREFIX
from calfcord.supervisor.procspawn import TerminateResult

# A broker-wide live-roster probe: hand it ``server_urls`` and it returns the
# NAMES of every agent currently online across the org. Injected so tests script
# the roster without a real broker; production wraps :func:`_probe_live_roster`
# (a read of calfkit's native mesh).
Probe = Callable[[str], Awaitable[list[str]]]

# Overall wall-clock bound on a single mesh probe (broker connect + view
# catch-up + read). The old control-plane probe used a ~2s discovery-reply
# window; the mesh read adds a connect + broker-start leg, so this bounds the
# TOTAL and errs slightly higher. A probe that exceeds it degrades exactly like
# a broker-down probe (the callers warn and proceed / show physical-only).
_DEFAULT_PROBE_TIMEOUT_S = 5.0

# A wall clock, accepted for symmetry with ``lifecycle.status`` and future
# freshness reconciliation. Unused today; kept so callers need not special-case
# the roster ops when they grow a time dimension.
Clock = Callable[[], float]

# The single hint shown when an op needs a running workspace and there isn't one;
# the one shared :data:`_workspace.WORKSPACE_NOT_RUNNING_HINT` (Fix #14), aliased
# for the call sites below so every roster op speaks with one voice.
_NOT_RUNNING_HINT = WORKSPACE_NOT_RUNNING_HINT


# A per-home client resolver alias kept for the call sites + the test that pins
# the default wiring (``test_roster._resolve_client``); the body is the one shared
# :func:`_workspace.resolve_client` (Fix #14 consolidation).
_resolve_client = resolve_client

# A workspace-readiness alias kept for the call sites below; the body is the one
# shared :func:`_workspace.workspace_is_up` (Fix #14 consolidation).
_workspace_is_up = workspace_is_up


def _agent_argv(launcher: str, name: str) -> list[str]:
    """The argv that spawns agent ``name`` — the SAME command the compose slot ran.

    Process Compose used ``<launcher> run agent <name>``; the detached spawn
    reproduces it exactly, so an agent runs identically whether it clocked in under
    the old PC slot or the new pidfile-tracked process.
    """
    return [launcher, "run", "agent", name]


async def _probe_live_roster(server_urls: str, *, timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S) -> list[str]:
    """Read the online agent names from calfkit's native mesh (``calf.agents``).

    Opens a short-lived observer :class:`~calfkit.client.Client` and reads
    ``client.mesh.get_agents()`` — the same online-only, heartbeat-staleness-filtered
    view the bridge roster reads — returning the online agent names sorted. The mesh
    carries presence, not full definitions, so this returns NAMES only. ``timeout_s``
    bounds the read.

    No broker pre-flight: the mesh read raises at call time if it can't reach the
    broker, so there is nothing to check up front. A
    :class:`~calfkit.exceptions.MeshUnavailableError` (broker down, the
    ``calf.agents`` topic not yet created, the view still establishing, or a dead
    reader) or a timeout PROPAGATES to the caller, whose ``except Exception``
    degrades ("broker unreachable; …"). A successful read of an empty roster
    returns ``[]`` ("nobody online") — distinct from the raise ("couldn't read
    it"), so the duplicate guard never treats an unreadable roster as a confident
    "nobody online".
    """
    client = Client.connect(server_urls)
    try:
        async with asyncio.timeout(timeout_s):
            agents = await client.mesh.get_agents()
            return sorted(info.name for info in agents.values())
    finally:
        await client.aclose()


def _resolve_probe(probe: Probe | None) -> Probe:
    """Resolve the live-roster probe, defaulting to the native-mesh probe.

    The default adapts :func:`_probe_live_roster` (``(server_urls, *, timeout_s)``)
    to the injectable ``(server_urls) -> ...`` shape, so tests can stub a plain
    async callable.
    """
    if probe is not None:
        return probe

    async def _default_probe(server_urls: str) -> list[str]:
        return await _probe_live_roster(server_urls)

    return _default_probe


# The one shared broker-gate refusal (:data:`_workspace.BROKER_UNREACHABLE_HINT`),
# aliased like the not-running hint: the old Process Compose slots carried
# `depends_on: broker process_healthy`, so a roster start during a broker bounce
# must fail up front, not crash-land a doomed spawn.
_BROKER_UNREACHABLE_HINT = _workspace.BROKER_UNREACHABLE_HINT


def _non_agent_error(name: str, verb: str) -> str | None:
    """The refusal for a roster verb aimed at a NON-agent slot, or ``None`` if ok.

    The single chokepoint for ``agent start|stop|restart``: the substrate
    (``broker``/``bridge``), the ``tools`` singleton, and the ``mcp-<server>``
    slots all live in the same pidfile namespace, so an agent verb pointed at one
    would kill/replace a process it does not own (e.g. ``agent restart tools``
    used to plant a bogus ``run agent tools`` into the singleton's pidfile). Each
    message names the verb that actually manages the slot. These names (and the
    ``mcp-`` prefix) are also rejected at create/parse time
    (:func:`calfcord.agents.identifier.reserved_agent_id_error`); this runtime
    chokepoint stays as defense-in-depth for pre-guard ``.md`` files.
    """
    if name == "tools":
        return (
            f"error: 'tools' is a reserved component, not an agent; "
            f"manage it with `disco tools {verb}`."
        )
    if name in _NON_AGENT_PROCESSES:
        return (
            f"error: {name!r} is a reserved component, not an agent; "
            "the substrate is managed by `disco start` / `disco stop`."
        )
    if name.startswith(MCP_SLOT_PREFIX):
        server = name.removeprefix(MCP_SLOT_PREFIX)
        return (
            f"error: {name!r} is an MCP server slot, not an agent; "
            f"manage it with `disco mcp {verb} {server}`."
        )
    return None


def _report_boot_crash(home: str, name: str) -> None:
    """The honest failure line for a spawn that exited within its confirmation
    window — names the slot's log (where the crash traceback landed)."""
    print(
        f"error: agent {name} exited immediately after start — "
        f"see {procspawn.log_path_for(home, name)}"
    )


async def agent_start(
    home: str | os.PathLike[str],
    *,
    name: str,
    server_urls: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    probe: Probe | None = None,
    live: list[str] | None = None,
    now: Clock | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Bring agent ``name`` up: a teammate clocking into the live org (§3.5).

    Returns a POSIX exit code. The sequence is deliberate:

    1. **Non-agent chokepoint** — the substrate (broker/bridge), the ``tools``
       singleton, and the ``mcp-<server>`` slots are never the agent roster; refuse
       before any work so this single seam closes the exposure for both ``agent
       start <reserved>`` and the ``start --all`` sweep.
    2. **Workspace check** — if the supervisor REST is unreachable the SUBSTRATE is
       down; print the not-running hint and return ``1`` before spending a broker
       probe or a doomed spawn.
    3. **Broker gate** — the same reachability probe ``lifecycle.start`` uses (the
       old PC slot's ``depends_on: broker healthy``, re-imposed off PC). Skipped
       when the bulk sweep pre-gated (``live`` given).
    4. **Duplicate guard (§3.5)** — for a name NOT live here, query the broker-wide
       live roster; if it is already answering on *another host*, do NOT spawn a
       second instance (return ``0`` — a duplicate start is a benign no-op).
    5. **Locked spawn critical section** — under :func:`_workspace.slot_mutation`
       (shared lifecycle lock + exclusive slot lock, so a concurrent start cannot
       double-spawn and a concurrent ``disco stop`` sweep cannot interleave):
       a name live on THIS host is terminated and re-spawned (**start of a running
       agent is a restart**, behavior #2 — checked inside the lock so a local
       instance is never misread as a remote duplicate); otherwise it is spawned
       fresh. Either way the spawn must survive a short confirmation window
       (:func:`_workspace.launch_slot`): a crash-on-boot prints an honest failure
       naming the log path and returns ``1`` — success says ``started``
       (presence/registration is the callers' watchers' job, never claimed here).

    ``launcher`` defaults to the home-derived shim (:func:`_workspace.launcher_for`),
    so callers that only pass ``home`` (``init`` / ``agent_create``) need not thread
    it. ``client`` / ``probe`` / ``broker_probe`` are injected for testing. ``live``
    is the pre-resolved broker-wide roster: when given (the ``start --all`` sweep
    probes AND gates once, threading it in), the duplicate guard reads it directly
    and neither re-probes nor re-gates — so N agents cost ONE probe and ONE
    aggregate warning, not N. ``now`` is accepted for symmetry with the rest of the
    lifecycle surface and is unused today.
    """
    error = _non_agent_error(name, "start")
    if error is not None:
        print(error)
        return 1

    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    if live is None and not await _workspace.broker_gate(server_urls, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    # Duplicate guard (§3.5), read-only so it runs OUTSIDE the locks: the probe is
    # broker-wide, so a name live on ANOTHER host is caught here, CLI-side, with no
    # bridge change. A name live HERE skips it (never misread as a remote
    # duplicate — the locked section below restarts it instead). When the bulk
    # sweep pre-resolved the roster (``live`` given), reuse it — it already probed
    # once and emitted any single aggregate broker-down warning, so no re-probe.
    if not _workspace.slot_is_live(home, name):
        if live is None:
            probe = _resolve_probe(probe)
            try:
                live = await probe(server_urls)
            except Exception:
                # Best-effort (§3.5 concedes a TOCTOU window): if the mesh is
                # unreadable we cannot verify org-wide duplicates, so warn and
                # proceed with the local spawn rather than blocking it — a
                # same-host duplicate is impossible (the locked live-check governs).
                print("warning: could not verify org-wide duplicates (broker unreachable); proceeding.")
                live = []
        if name in live:
            print(f"agent {name} is already running in the organization")
            return 0

    try:
        with _workspace.slot_mutation(home, name):
            restarted = _workspace.slot_is_live(home, name)
            if restarted:
                await _workspace.terminate_slot(home, name)
            if not await _workspace.launch_slot(home, name, _agent_argv(launcher, name)):
                _report_boot_crash(home, name)
                return 1
            print(f"agent {name} {'restarted' if restarted else 'started'}")
            return 0
    except _workspace.SlotBusyError:
        # A concurrent start already holds the slot: a benign duplicate action,
        # reported honestly rather than double-spawned.
        print(f"agent {name} is already being started here (another disco command holds its slot).")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1


async def agent_stop(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take agent ``name`` offline: a teammate clocking out (terminate its process).

    Non-agent chokepoint first (an ``agent stop tools`` must not kill the tools
    singleton; same for ``mcp-<server>`` and the substrate). Then the workspace
    check (the not-running hint + return ``1`` if the office isn't open); otherwise
    terminate the process the pidfile names and clear it — under the SAME
    slot-mutation locks the spawn verbs take, so a stop racing a start's
    confirmation window can never unlink the fresh pidfile mid-confirm (which
    would make the starter misreport "exited immediately"). A busy slot is
    reported honestly and skipped (benign, like every busy outcome). Honest
    messages: a live agent is ``stopped``; a slot with no live process here (never
    started, already gone, or a stale/recycled pidfile) reports ``not running
    here``.
    """
    error = _non_agent_error(name, "stop")
    if error is not None:
        print(error)
        return 1

    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    try:
        with _workspace.slot_mutation(home, name):
            result = await _workspace.terminate_slot(home, name)
    except _workspace.SlotBusyError:
        print(f"agent {name} is being started/stopped by another disco command; skipped.")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1
    if result in (TerminateResult.TERMINATED, TerminateResult.KILLED):
        print(f"agent {name} stopped")
    else:
        # None (no pidfile), ALREADY_DEAD, or NOT_OURS (recycled pid): nothing of
        # ours was running here — the stale record, if any, has been swept.
        print(f"agent {name} is not running here")
    return 0


async def agent_restart(
    home: str | os.PathLike[str],
    *,
    name: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Reload agent ``name`` after an edited ``.md`` (terminate + re-spawn).

    The node bakes its config at construction, so a restart is how a ``.md`` edit
    takes effect: the running instance (if any) is terminated and a fresh process
    is spawned. Restart of a not-running agent simply brings it up — the expected
    effect. Non-agent chokepoint first (an ``agent restart tools`` must not replace
    the singleton with a bogus ``run agent tools`` process), then the workspace
    check and the broker gate (the effective ``CALF_HOST_URL``; killing a healthy
    agent to spawn a doomed one during a broker bounce would be worse than
    refusing). The terminate + spawn runs under the slot-mutation locks, and the
    spawn must survive its confirmation window — a crash-on-boot reports the log
    path and returns ``1``; otherwise ``agent <name> restarted``, return ``0``.
    """
    error = _non_agent_error(name, "restart")
    if error is not None:
        print(error)
        return 1

    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    try:
        with _workspace.slot_mutation(home, name):
            await _workspace.terminate_slot(home, name)
            if not await _workspace.launch_slot(home, name, _agent_argv(launcher, name)):
                _report_boot_crash(home, name)
                return 1
            print(f"agent {name} restarted")
            return 0
    except _workspace.SlotBusyError:
        print(f"agent {name} is already being restarted here (another disco command holds its slot).")
        return 0
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1


async def agent_start_all(
    home: str | os.PathLike[str],
    *,
    agent_ids: list[str],
    server_urls: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    probe: Probe | None = None,
    now: Clock | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Bring every DEFINED agent up on this host (``start --all``, behavior #1).

    ``--all`` targets every *defined* agent (the caller passes ``agent_ids`` from the
    ``.md`` files — roster.py stays off the agents-dir read). Each id runs the SAME
    single-start logic as :func:`agent_start`, so a locally-running one restarts
    (behavior #2), a stopped one spawns, and one only answering on another host hits
    the duplicate refusal — one honest, LOCAL-only sweep of this host.

    Workspace check first (the shared not-running hint + ``1``). An empty defined set
    is a clean no-op (``no agents defined``, ``0``). Otherwise it is **best-effort**:
    a per-item failure is reported and the sweep continues, then a one-line summary
    closes it. Returns ``1`` if any id HARD-failed (a raised fault), else ``0``; the
    restart and duplicate-refuse outcomes are successes, not failures.

    The §3.5 duplicate guard reads the same broker-wide roster for EVERY id, so it is
    probed ONCE up front and threaded into each per-id ``agent_start`` (via its
    ``live`` param) — the broker gate likewise runs ONCE for the sweep. A
    broker-down probe therefore yields ONE aggregate warning for the operator's
    single action.
    """
    # Drop non-agent names BEFORE the empty-check: main.py passes the raw `.md`
    # stems, and while the create/parse-time guard now rejects reserved names, a
    # pre-guard `tools.md` / `broker.md` / `mcp-x.md` may still sit on disk (the
    # stems are globbed, never parsed). The classifier is the same one the
    # sweeps/status use, so "which id is an agent" is defined once. An
    # all-reserved input collapses to the empty set → the clean no-op.
    agent_ids = [n for n in agent_ids if _workspace.is_agent_slot(n)]

    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    if not agent_ids:
        print("no agents defined")
        return 0

    # Gate the broker ONCE for the whole sweep (each per-id ``agent_start`` skips
    # its own gate because ``live`` is threaded in below).
    if not await _workspace.broker_gate(server_urls, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    # Probe the broker-wide roster ONCE for the whole sweep and thread it into each
    # per-id ``agent_start`` via ``live`` so none of them re-probe.
    probe = _resolve_probe(probe)
    try:
        live: list[str] = await probe(server_urls)
        probe_unavailable = False
    except Exception:
        print("warning: could not verify org-wide duplicates (broker unreachable); proceeding for all agents.")
        live = []
        probe_unavailable = True

    failures = 0
    for name in agent_ids:
        try:
            rc = await agent_start(
                home,
                name=name,
                server_urls=server_urls,
                launcher=launcher,
                client=client,
                probe=probe,
                live=live,
            )
        except Exception as exc:
            print(f"agent {name}: failed to start ({exc})")
            failures += 1
            continue
        if rc != 0:
            failures += 1

    summary = f"start --all: {len(agent_ids)} agent(s) processed, {failures} failed."
    if probe_unavailable:
        summary += " (org-wide duplicate check skipped: broker unreachable)"
    print(summary)
    return 1 if failures else 0


async def agent_stop_all(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take every RUNNING local agent offline (``stop --all``, behavior #1).

    LOCAL-only: the target set is exactly this host's live agent pidfiles
    (:func:`_workspace.live_agent_slots` — the ``tools`` singleton and the
    ``mcp-<server>`` slots are never swept). There is no over-the-wire control;
    ``--all`` acts on THIS host.

    Workspace check first (the shared not-running hint + ``1``). Nothing running
    locally is a clean no-op (``no agents running locally``, ``0``). Otherwise
    **best-effort**: each stop runs under its slot-mutation locks (never unlinking
    a pidfile out from under a concurrent start's confirmation window); a BUSY
    slot is reported and skipped — benign, like the other sweeps — while a real
    per-item failure is reported and counted. A one-line summary closes the
    sweep. Returns ``1`` if any stop failed, else ``0``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    targets = sorted(_workspace.live_agent_slots(home))
    if not targets:
        print("no agents running locally")
        return 0

    failures = 0
    for name in targets:
        try:
            with _workspace.slot_mutation(home, name):
                await _workspace.terminate_slot(home, name)
        except _workspace.SlotBusyError:
            # Benign, like every busy outcome: another command is mid-mutation on
            # this slot; skipping beats yanking its fresh pidfile mid-confirm.
            print(f"agent {name} is being started/stopped by another disco command; skipped.")
            continue
        except Exception as exc:
            print(f"agent {name}: failed to stop ({exc})")
            failures += 1
            continue
        print(f"agent {name} stopped")

    print(f"stop --all: {len(targets)} agent(s) processed, {failures} failed.")
    return 1 if failures else 0


async def agent_restart_all(
    home: str | os.PathLike[str],
    *,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Reload every RUNNING local agent (``restart --all``, behavior #1).

    Same LOCAL-only target set as :func:`agent_stop_all` — this host's live agent
    pidfiles — terminated and re-spawned. Useful after a provider/key change that
    affects a whole host's agents at once.

    Workspace check first (the shared not-running hint + ``1``), then ONE broker
    gate for the whole sweep (killing every healthy agent to spawn doomed
    replacements during a broker bounce would be the worst possible outcome).
    Nothing running locally is a clean no-op (``no agents running locally``,
    ``0``). Otherwise **best-effort**: each restart runs under its slot-mutation
    lock and its spawn must survive the confirmation window; a BUSY slot is
    reported and skipped — benign, matching the single restart and the other
    sweeps — while a real per-item failure (raised fault or crash-on-boot) is
    reported and counted, and the sweep continues either way. A one-line summary
    closes it. Returns ``1`` if any restart failed, else ``0``.
    """
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    targets = sorted(_workspace.live_agent_slots(home))
    if not targets:
        print("no agents running locally")
        return 0

    failures = 0
    for name in targets:
        try:
            with _workspace.slot_mutation(home, name):
                await _workspace.terminate_slot(home, name)
                if not await _workspace.launch_slot(home, name, _agent_argv(launcher, name)):
                    _report_boot_crash(home, name)
                    failures += 1
                    continue
        except _workspace.SlotBusyError:
            # Benign, aligned with the single restart's busy handling and the
            # other sweeps: another command already owns this slot's mutation.
            print(f"agent {name} is being started/stopped by another disco command; skipped.")
            continue
        except Exception as exc:
            print(f"agent {name}: failed to restart ({exc})")
            failures += 1
            continue
        print(f"agent {name} restarted")

    print(f"restart --all: {len(targets)} agent(s) processed, {failures} failed.")
    return 1 if failures else 0


async def agent_ps(
    home: str | os.PathLike[str],
    *,
    server_urls: str,
    client: ProcessComposeClient | None = None,
    probe: Probe | None = None,
    now: Clock | None = None,
) -> int:
    """Render the running-roster board: the §3.4 logical-plus-physical union.

    Returns ``0`` always — ps is read-only, and "nothing running" (including no
    workspace at all) is a valid state, not an error. If the supervisor is
    unreachable, print the not-running hint and return ``0`` *without* spending a
    broker probe (there is nothing local to union against).

    Otherwise it unions two views:

    * **LOGICAL** (global): every agent online on the mesh across the whole org —
      true liveness, host-agnostic.
    * **PHYSICAL** (host-local): this host's live agent pidfiles.

    The cross-product yields three rendered states (§3.4):

    * physical **and** logical → ``running`` (online here and registered);
    * physical **only** → ``started, not yet registered`` (up here but not yet
      answering — just starting, or wedged);
    * logical **only** → ``running on another host`` (expected under multi-host —
      this is NOT an error; the physical half is host-local by design).

    ``probe`` / ``client`` are injected for testing; ``now`` is accepted for
    symmetry with the lifecycle surface and is unused today.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 0

    physical = _workspace.live_agent_slots(home)

    probe = _resolve_probe(probe)
    try:
        logical = set(await probe(server_urls))
    except Exception:
        # The probe talks to the broker; a broker hiccup must not crash read-only
        # `ps`. Degrade to the physical (host-local) view with a note.
        logical = set()
        print("note: broker unreachable; showing locally-running agents only.")

    _render_ps_board(physical=physical, logical=logical)
    return 0


def _render_ps_board(*, physical: set[str], logical: set[str]) -> None:
    """Print the three-state roster board for the physical/logical union (§3.4).

    Every name in either view gets exactly one row, sorted for deterministic
    output. The state is decided by which view(s) the name is in (see
    :func:`agent_ps`). An empty union prints an explicit "no agents running" line
    so the board is never a confusing blank.
    """
    everyone = sorted(physical | logical)
    if not everyone:
        print("no agents running in the organization.")
        return

    print("running agents:")
    for name in everyone:
        here = name in physical
        answering = name in logical
        if here and answering:
            state = "running"
        elif here:
            # Up on this host but not answering the probe yet — just starting, or
            # wedged. The drift case the union exists to surface.
            state = "started, not yet registered"
        else:
            # Answering but not a local process — another host is running it.
            # Expected under multi-host (§3.4); NOT an error.
            state = "running on another host"
        print(f"  {name:<16} {state}")
