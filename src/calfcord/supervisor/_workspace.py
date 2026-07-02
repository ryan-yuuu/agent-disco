"""The shared seam every supervisor surface builds on (DRY consolidation, Fix #14).

``lifecycle`` / ``roster`` / ``component`` and ``cli.doctor`` each grew their own
copy of four identical building blocks: resolve a per-home REST client, decide
whether the workspace REST server answers, the one not-running hint, and the
``{"data": [...]}``-vs-bare-list process-list normalizer. Four copies are four
chances to drift (a different hint string, a different wire-shape tolerance), so
they live here once and the surfaces re-export thin aliases for the names their
tests reference.

Since Phase 2 (the roster moved OFF Process Compose — see :mod:`procspawn`), this
module ALSO owns the shared roster-slot primitives: the home-derived shim
launcher, the pidfile-namespace scan of ``state/run/*.pid``, and the
spawn/terminate glue over :mod:`procspawn` that ``roster`` / ``component`` /
``mcp_roster`` all drive. The slot conventions (agent name, ``tools``,
``mcp-<server>``) stay with their surfaces; what lives here is the mechanism-
agnostic "which slots are alive here" + "spawn/kill this slot" seam, so the four
surfaces cannot drift on how a roster member clocks in, out, or is reconciled.

Import-light like the rest of :mod:`calfcord.supervisor`, so every CLI-side
surface that consumes it stays cheap to import.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path

from calfcord.supervisor import procspawn
from calfcord.supervisor.client import ProcessComposeClient
from calfcord.supervisor.compose import _RESERVED_PROCESS_NAMES as _NON_AGENT_SLOTS
from calfcord.supervisor.compose import MCP_SLOT_PREFIX

# The single hint shown when an op needs a running workspace and there isn't one.
# Centralized so every lifecycle surface (substrate, agent roster, components)
# speaks with exactly one voice.
WORKSPACE_NOT_RUNNING_HINT = "workspace not running (start it with: disco start)"


def resolve_client(
    client: ProcessComposeClient | None, home: str | os.PathLike[str]
) -> ProcessComposeClient:
    """Resolve the REST client, defaulting to a per-home supervisor client.

    The port is derived from ``$CALFCORD_HOME`` (:func:`lifecycle.pc_port_for`) —
    the same port the ``up -p`` flag pinned — so a second install on one host talks
    to its own supervisor and does not collide. ``pc_port_for`` is imported lazily
    (it lives in :mod:`lifecycle`, which imports this leaf module) so the seam has
    no module-level dependency on ``lifecycle`` and the two cannot form an import
    cycle.
    """
    if client is not None:
        return client
    from calfcord.supervisor.lifecycle import pc_port_for

    return ProcessComposeClient(port=pc_port_for(home))


async def workspace_is_up(client: ProcessComposeClient) -> bool:
    """Whether the supervisor REST server answers — a successful ``project_state``.

    The client raises ``RuntimeError`` on a transport failure (server not up /
    wrong port), which is exactly "the workspace isn't open" here; any other error
    is a real bug and is left to propagate (it is not swallowed into "down").
    """
    try:
        await client.project_state()
    except RuntimeError:
        return False
    return True


def iter_process_dicts(payload: object) -> Iterator[dict]:
    """Yield the dict process entries from a ``list_processes`` payload.

    Process Compose returns either a bare list or ``{"data": [...]}`` depending on
    version (the wire shape wobbles across versions), so accept both, and skip
    non-dict entries defensively so a stray wire-shape wobble never crashes a
    caller (the status board / the ps physical view / the drift read).
    """
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    for item in items or []:
        if isinstance(item, dict):
            yield item


# --- legacy-workspace guard (upgrade over a live old-style workspace) --------

# The refusal every roster SPAWN verb prints when the answering supervisor was
# started by an older calfcord (see :func:`legacy_pc_roster`). Centralized like
# the not-running hint so the surfaces speak with one voice.
LEGACY_WORKSPACE_HINT = (
    "error: this workspace was started by an older calfcord — run `disco stop` "
    "then `disco start`, then re-run this command."
)

# The only processes a MODERN (detached-roster) project declares. Anything else
# in the supervisor's process list means the roster still runs under PC.
_SUBSTRATE_PROCESS_NAMES = frozenset({"broker", "bridge"})


async def legacy_pc_roster(client: ProcessComposeClient) -> bool:
    """Whether the answering supervisor still supervises ROSTER processes (old main).

    ``disco self update`` does not stop a running workspace, so after an upgrade the
    old supervisor can still be running the roster as PC-declared slots while
    ``state/run/`` is empty. A detached spawn beside that would DUPLICATE the agent
    (split-brain: double replies in Discord), so the spawn verbs consult this and
    refuse with :data:`LEGACY_WORKSPACE_HINT`; stops and status stay usable so the
    operator can wind the old workspace down.

    Cheap (one REST read of the project the caller already reached) and FAIL-OPEN:
    any failure to read or parse the process list returns ``False`` — detection is
    best-effort and must never become a new way for a healthy start to fail. A
    row missing its name is skipped for the same reason.
    """
    try:
        payload = await client.list_processes()
    except Exception:
        return False
    for item in iter_process_dicts(payload):
        name = item.get("name")
        if isinstance(name, str) and name not in _SUBSTRATE_PROCESS_NAMES:
            return True
    return False


# --- roster-slot primitives (Phase 2: roster off Process Compose) -----------
#
# The substrate (broker + bridge) stays on Process Compose; the roster (agents,
# ``tools``, and each ``mcp-<server>``) is instead a detached process per slot,
# spawned via :mod:`procspawn` with a pidfile under ``state/run/<slot>.pid``. The
# helpers below are the one place that scans that pidfile namespace and drives
# spawn/terminate, so ``roster`` / ``component`` / ``mcp_roster`` (and
# ``lifecycle`` stop/status) share exactly one notion of "alive here".


def launcher_for(home: str | os.PathLike[str]) -> str:
    """The install shim every roster process is spawned under: ``<home>/shims/disco``.

    The SAME path :mod:`compose` rendered into each Process Compose ``command`` and
    that ``lifecycle.start`` / ``init`` / ``agent_create`` pass explicitly, derived
    here from ``home`` so a roster op that spawns a slot need not thread it through
    (Phase 3 may drop the now-vestigial ``launcher`` params on the callers).
    """
    return str(Path(os.fspath(home)) / "shims" / "disco")


def _run_dir(home: str | os.PathLike[str]) -> Path:
    return Path(os.fspath(home)) / "state" / "run"


class SlotScanError(RuntimeError):
    """``state/run`` exists but cannot be read — roster state on this host is UNKNOWN.

    Distinct from the dir simply not existing yet (no slots — the benign
    pre-first-spawn case): an unreadable scan must never collapse to "no slots",
    because that empty answer is what would let ``disco stop`` claim "workspace
    closed" over processes it never saw.
    """

    def __init__(self, run_dir: Path, exc: OSError) -> None:
        super().__init__(f"cannot read {run_dir}: {exc} — roster state unknown")


def iter_slot_pidfiles(home: str | os.PathLike[str]) -> Iterator[tuple[str, Path]]:
    """Yield ``(slot, pidfile)`` for every ``<slot>.pid`` under ``state/run`` (sorted).

    The slot is the filename stem, so it round-trips the naming convention the
    surfaces write (agent name, ``tools``, ``mcp-<server>``). A missing ``run`` dir
    yields nothing rather than raising, so the scan is safe before the first spawn
    — but a run dir that EXISTS and cannot be read raises :class:`SlotScanError`
    (roster state unknown ≠ no slots; consumers must surface it, not swallow it).
    """
    run_dir = _run_dir(home)
    try:
        entries = os.listdir(run_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SlotScanError(run_dir, exc) from exc
    for name in sorted(entries):
        if not name.endswith(".pid"):
            continue
        stem = name[: -len(".pid")]
        try:
            procspawn.require_safe_slot(stem)
        except ValueError:
            # A stray file whose stem could never be a valid slot (dot-leading
            # editor/tmp artifacts, traversal shapes): skip it rather than let a
            # nonsense stem flow into the path helpers downstream.
            continue
        yield stem, run_dir / name


def slot_is_live(home: str | os.PathLike[str], slot: str) -> bool:
    """Whether ``slot``'s pidfile names an ours-and-alive process (the truth of "up here").

    A missing/torn/stale pidfile — or one naming a recycled pid — reads ``False``
    (:func:`procspawn.is_ours_and_alive` is the reuse-proof gate). An
    INDETERMINATE identity also reads ``False`` (never acted on as ours);
    consumers that must preserve those slots use :func:`slot_identity` /
    :func:`indeterminate_slots` instead.
    """
    record = procspawn.read_pidfile(procspawn.pidfile_for(home, slot))
    return record is not None and procspawn.is_ours_and_alive(record)


def slot_identity(home: str | os.PathLike[str], slot: str) -> procspawn.Identity | None:
    """The tri-state identity verdict for ``slot``'s pidfile, or ``None`` if there
    is no usable record (missing or torn file)."""
    record = procspawn.read_pidfile(procspawn.pidfile_for(home, slot))
    if record is None:
        return None
    return procspawn.identity(record)


def live_slots(home: str | os.PathLike[str]) -> set[str]:
    """Every slot with an ours-and-alive pidfile under ``state/run`` (any slot type)."""
    return {slot for slot, _ in iter_slot_pidfiles(home) if slot_is_live(home, slot)}


def is_agent_slot(slot: str) -> bool:
    """Whether ``slot`` is an AGENT slot — not the substrate/``tools`` reserved set,
    and not an ``mcp-<server>`` slot. The single classifier the agent-only sweeps
    (``stop --all`` / ``restart --all``) and the status board's agent/other split
    share, so "which pidfile is an agent" is defined once."""
    return slot not in _NON_AGENT_SLOTS and not slot.startswith(MCP_SLOT_PREFIX)


def live_agent_slots(home: str | os.PathLike[str]) -> set[str]:
    """Every AGENT slot alive here — the local membership set for the agent sweeps."""
    return {slot for slot in live_slots(home) if is_agent_slot(slot)}


def dead_slots(home: str | os.PathLike[str]) -> set[str]:
    """Every slot with a pidfile but PROVABLY no ours-and-alive process — crashed
    or exited (a dead pid, a recycled pid, or a torn record).

    The status board renders these honestly (``not running (exited …)``) instead
    of omitting them; ``disco stop``'s sweep is the acknowledge-and-clear point
    that removes the files. A slot whose identity could not be READ is *not* dead
    — it lands in :func:`indeterminate_slots` so nothing sweeps or misrenders a
    process that may well be alive and ours.
    """
    return {
        slot
        for slot, _ in iter_slot_pidfiles(home)
        if slot_identity(home, slot)
        not in (procspawn.Identity.OURS, procspawn.Identity.INDETERMINATE)
    }


def note_local_survivors(home: str | os.PathLike[str]) -> None:
    """Note any detached roster processes still alive locally (supervisor down).

    Shared by the ``agent ps`` and ``disco status`` early returns: "workspace not
    running" alone would imply an idle host, but the detached roster outlives a
    dead/stopped supervisor — ``disco stop`` is what terminates it. Counts the
    provably-live slots AND the indeterminate ones (their pid IS alive; only the
    ownership evidence is momentarily unreadable — omitting them would imply an
    idle host over a running process). An unreadable scan degrades to its own
    warning instead of silence.
    """
    try:
        survivors = sorted(live_slots(home) | indeterminate_slots(home))
    except SlotScanError as exc:
        print(f"warning: {exc}")
        return
    if survivors:
        print(
            f"note: {len(survivors)} detached roster process(es) still running "
            f"locally — `disco stop` stops the ones it can verify: {', '.join(survivors)}"
        )


def indeterminate_slots(home: str | os.PathLike[str]) -> set[str]:
    """Every slot whose pidfile names a LIVE pid whose identity could not be read.

    The unknown middle of the tri-state: not provably ours (never signalled), not
    provably stale (never swept). Status renders these as unverifiable so the
    operator knows the tooling is degraded rather than seeing a lie either way.
    """
    return {
        slot
        for slot, _ in iter_slot_pidfiles(home)
        if slot_identity(home, slot) is procspawn.Identity.INDETERMINATE
    }


def spawn_slot(
    home: str | os.PathLike[str], slot: str, argv: Sequence[str]
) -> procspawn.SpawnedProcess:
    """Spawn ``argv`` as the detached process for ``slot`` (pidfile + log by convention).

    The child inherits this CLI's environment (the shim that invoked ``disco agent
    start`` already exported ``$CALFCORD_HOME`` etc., and the launcher shim in
    ``argv`` re-derives the rest via its ``--env-file``), so no env is passed. The
    working directory is pinned to ``home``: the shim defaults
    ``CALFCORD_WORKSPACE_DIR`` to ``$PWD``, so an inherited cwd would make a slot's
    workspace depend on *where the operator ran the verb* — pinning it matches
    where the Process Compose daemon effectively ran the old slots.
    """
    return procspawn.spawn_detached(
        argv,
        log_path=procspawn.log_path_for(home, slot),
        pidfile=procspawn.pidfile_for(home, slot),
        cwd=os.fspath(home),
    )


# The post-spawn liveness-confirmation window: long enough for a crash-on-boot
# (bad flag, import error, unreadable config) to have exited, short enough that
# every start verb stays snappy. Presence/registration is NOT confirmed here —
# that is the callers' watchers' job; this only rejects the lie of printing
# success for a process that is already dead.
_SPAWN_CONFIRM_WINDOW_S = 1.5
_SPAWN_CONFIRM_POLL_S = 0.25

# Injectable time seams for :func:`launch_slot`, mirroring lifecycle's pattern.
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


async def launch_slot(
    home: str | os.PathLike[str],
    slot: str,
    argv: Sequence[str],
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> bool:
    """Spawn ``slot`` and confirm it survives a short grace window.

    Returns ``True`` when the process is still ours-and-alive at the end of the
    window ("started" — nothing more). Returns ``False`` when it exited during the
    window (crash-on-boot): the fresh-but-dead pidfile is cleaned so the failed
    start leaves no stale record, and the caller reports the failure with the
    slot's log path. ``clock``/``sleep`` are injected so tests drive the window
    without real time.
    """
    spawned = spawn_slot(home, slot, argv)
    deadline = clock() + _SPAWN_CONFIRM_WINDOW_S
    while True:
        # The just-spawned process is OUR child and nothing else waits on it, so a
        # crash-on-boot leaves a zombie: it still answers ``os.kill(pid, 0)`` and
        # (on Linux) keeps its /proc start-token, so without a reap the liveness
        # check below would read True for the whole window and the caller would
        # print "started" for a dead process.
        procspawn.reap(spawned.pid)
        if not slot_is_live(home, slot):
            procspawn.cleanup_stale(procspawn.pidfile_for(home, slot))
            return False
        if clock() >= deadline:
            return True
        await sleep(_SPAWN_CONFIRM_POLL_S)


# --- spawn-critical-section locks --------------------------------------------


class WorkspaceBusyError(RuntimeError):
    """A ``disco start``/``disco stop`` holds the lifecycle lock — no spawning now."""


class SlotBusyError(RuntimeError):
    """Another CLI invocation is mid-mutation on this slot (a concurrent start)."""


@contextlib.contextmanager
def _flock_nb(path: str | os.PathLike[str], operation: int) -> Iterator[None]:
    """Hold a non-blocking ``flock`` on ``path``.

    CONTENTION — the lock is held elsewhere — surfaces as ``BlockingIOError``
    (the ``EWOULDBLOCK``/``EAGAIN`` the kernel returns for ``LOCK_NB``; both
    supported platforms' ``flock`` reports contention that way and Python maps
    those errnos to ``BlockingIOError``). Callers translate exactly that to
    their domain busy-error. Any OTHER ``OSError`` — an unmakeable parent dir, an
    unwritable lock file — propagates untouched: a permissions problem must read
    as a permissions problem, never as "another disco command is in progress"."""
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def slot_mutation(home: str | os.PathLike[str], slot: str) -> Iterator[None]:
    """The spawn critical section for one slot: check-alive → spawn → confirm.

    Two locks, ALWAYS in this order (the documented ordering — ``disco start``/
    ``stop`` take only the lifecycle lock, so no cycle is possible):

    1. the **lifecycle lock, SHARED** (the same ``state/calfcord-lifecycle.lock``
       ``disco stop`` takes exclusively for its roster sweep) — so a spawn can
       never land *behind* a concurrent stop sweep, and a stop can never sweep
       mid-spawn. Concurrent spawns share it freely. Contention (a start/stop in
       flight) raises :class:`WorkspaceBusyError`.
    2. an **exclusive per-slot lock** (``state/run/<slot>.lock``) — so two
       concurrent ``agent start <name>`` cannot both pass the check-alive and
       double-spawn. Contention raises :class:`SlotBusyError`.

    Both are non-blocking: a second holder fails immediately with a clear domain
    error rather than queueing behind a window it cannot see.
    """
    # The per-slot lock path embeds the slot name (and _flock_nb makedirs its
    # parent), so a traversal-shaped name must be refused before any path exists.
    procspawn.require_safe_slot(slot)
    # Lazy import: lifecycle imports this leaf module at module level, so the
    # lock-path constant is fetched at call time to avoid an import cycle.
    from calfcord.supervisor.lifecycle import _lock_path

    try:
        lifecycle_guard = _flock_nb(_lock_path(home), fcntl.LOCK_SH)
        lifecycle_guard.__enter__()
    except BlockingIOError as exc:
        # ONLY contention reads as busy; any other OSError (permissions, a broken
        # state dir) propagates as the IO problem it actually is.
        raise WorkspaceBusyError(
            "a `disco start`/`disco stop` is in progress for this workspace; retry once it finishes."
        ) from exc
    try:
        try:
            slot_guard = _flock_nb(_run_dir(home) / f"{slot}.lock", fcntl.LOCK_EX)
            slot_guard.__enter__()
        except BlockingIOError as exc:
            raise SlotBusyError(
                f"another disco command is already starting/stopping {slot!r} here."
            ) from exc
        try:
            yield
        finally:
            slot_guard.__exit__(None, None, None)
    finally:
        lifecycle_guard.__exit__(None, None, None)


# --- broker reachability gate --------------------------------------------------

# The one refusal printed when the gate fails, centralized (like the not-running
# hint) so every roster surface speaks with one voice.
BROKER_UNREACHABLE_HINT = (
    "error: broker not reachable — is the workspace healthy? "
    "Check `disco status` and `disco logs`."
)


# The broker-wide live-roster probe shape: hand it ``server_urls``, get the NAMES
# of every agent currently online across the org (see roster._probe_live_roster).
LiveRosterProbe = Callable[[str], Awaitable[list[str]]]


def resolve_probe(probe: LiveRosterProbe | None) -> LiveRosterProbe:
    """Resolve the broker-wide live-roster probe, defaulting to the native-mesh read.

    The shared seam ``roster`` (start/restart/ps) and ``lifecycle.status`` both
    consume, homed here beside :func:`broker_gate` so a surface never has to
    reach into another surface's privates for it. The default adapts
    :func:`calfcord.supervisor.roster._probe_live_roster` (imported lazily —
    ``roster`` imports this leaf module at module level, and its calfkit client
    dependency must not become an import-time cost here) to the injectable
    ``(server_urls) -> [names]`` shape, so tests can stub a plain async callable.
    """
    if probe is not None:
        return probe

    async def _default_probe(server_urls: str) -> list[str]:
        from calfcord.supervisor.roster import _probe_live_roster

        return await _probe_live_roster(server_urls)

    return _default_probe


async def broker_gate(
    server_urls: str | None, probe: Callable[[], Awaitable[bool]] | None
) -> bool:
    """Whether the broker answers — the roster-start precondition.

    The old Process Compose slots carried ``depends_on: broker process_healthy``;
    off PC that gate is re-imposed here with the SAME probe ``lifecycle.start``
    uses (metadata reachability, not bare TCP), so a roster start during a broker
    bounce fails honestly up front instead of crash-landing a doomed spawn.
    ``server_urls`` falls back to the effective ``CALF_HOST_URL`` (the one default
    every runner shares); ``probe`` is the injection seam for tests.
    """
    if probe is None:
        from calfcord.health.check import default_broker_probe

        probe = default_broker_probe(server_urls or os.getenv("CALF_HOST_URL") or "localhost")
    return await probe()


async def terminate_slot(
    home: str | os.PathLike[str], slot: str
) -> procspawn.TerminateResult | None:
    """Stop ``slot``'s process and clear its pidfile; ``None`` if there was no record.

    Reads the pidfile, terminates the process group it names (SIGTERM→SIGKILL via
    :func:`procspawn.terminate`), then removes the now-dead pidfile
    (:func:`procspawn.cleanup_stale` keeps only a still-alive one, so a
    ``NOT_OURS`` stale file is swept too). Returns the terminate outcome, or ``None``
    when there was no usable record (nothing spawned here, or a torn file — the
    torn case is swept with a printed warning, since whatever process it named may
    survive the removal unseen).

    Two outcomes deliberately KEEP the pidfile: ``INDETERMINATE`` (identity
    unreadable — never signal, never unlink; a warning tells the operator the
    slot is unverifiable) and ``KILL_UNCONFIRMED`` (the process shrugged off
    SIGKILL within the reap window; its pidfile stays so it remains visible to
    status/stop rather than becoming an invisible survivor).
    """
    pidfile = procspawn.pidfile_for(home, slot)
    record = procspawn.read_pidfile(pidfile)
    if record is None:
        # No usable record: clear a torn file if present, report "nothing here".
        if procspawn.cleanup_stale(pidfile):
            print(
                f"warning: removed unreadable pidfile for slot {slot!r}; "
                "the process it named (if any) may still be running — check ps/logs."
            )
        return None
    result = await procspawn.terminate(record)
    if result is procspawn.TerminateResult.INDETERMINATE:
        print(
            f"warning: cannot verify process {record.pid} for slot {slot!r} "
            "(identity read failed); leaving it untouched."
        )
        return result
    procspawn.cleanup_stale(pidfile)
    return result
