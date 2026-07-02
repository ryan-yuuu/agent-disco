"""Detached roster-process primitives: spawn, identify, terminate.

The roster (agents, tools, MCP servers) runs OFF Process Compose: PC cannot
hot-add a process to a live project — a ``POST /project`` bounces every PID on
both pinned and latest builds (an inherent limitation confirmed by an empirical
spike). The substrate (broker + bridge) stays on PC; each roster member is instead
spawned directly as a **detached** process with **no auto-respawn** (a crash makes
it go offline in the mesh-based status; an operator restarts it). The decision
record is ``docs/adr/0014-roster-detached-off-process-compose.md``.

This module is that mechanism — and *only* the mechanism. It is deliberately:

* **Policy-free.** It launches, identifies, and stops one process given explicit
  paths. It does not decide *which* processes exist, read the agents dir, resolve
  ``$CALFCORD_HOME``, or know what a "slot" means beyond a filename stem. That
  policy lives above: :mod:`calfcord.supervisor._workspace` (the slot scan +
  spawn/terminate glue) and :mod:`calfcord.supervisor._slot_ops` (the locked verb
  choreography).
* **Silent.** Nothing here prints. The lifecycle/roster callers already own the
  print-based UX (the not-running hint, the per-agent lines); a primitive that
  printed would double-speak. Outcomes are returned as values, never stdout.
* **Injectable at the seams that matter.** Only :func:`terminate` waits on the
  world, so only it takes an injected ``clock`` / ``sleep`` — the same testability
  pattern :mod:`lifecycle` uses for its readiness gate — letting the kill/escalate
  timing be driven deterministically with no real wall-clock elapsed.
* **Dependency-free.** Stdlib only (no ``psutil``); Darwin + Linux supported.

**The pid-reuse guard (why an identity record, not a bare pid).** A pidfile that
stored only a pid is a footgun: pids recycle, so a stale pidfile could name a
*different*, unrelated process that the OS has since assigned the same number —
and ``terminate`` would then SIGKILL an innocent bystander. So every pidfile
carries a :class:`PidRecord`: the pid **plus** a re-queryable OS **start-token**
(Linux ``/proc/<pid>/stat`` field 22 — start time in clock ticks since boot;
Darwin ``ps -o lstart`` — the process start timestamp). Ownership is confirmed by
re-reading that token for the live pid and requiring an exact match, so a recycled
pid (same number, later start time) can never be mistaken for ours. The record
also carries the argv and a ``spawn_ts`` + ``argv_hash``, for human inspection of
the pidfile. A token-less record can never prove ownership, so :func:`identity`
permanently *refuses* it rather than risk signalling a stranger — which is why
:func:`spawn_detached` treats a failed token capture (a platform with no cheap
source, or a transient read failure — darwin's ``ps`` can fail even for a live
pid) as a failed spawn: the child is killed and the spawn raises, because a live
process no stop/status/sweep path could ever verify would be an untrackable
orphan.

**Log rotation happens only AT SPAWN.** Process Compose rotated every process log
in-flight (10 MB / 7 days / 5 backups); a detached slot has no supervisor watching
its file, so the ONLY rotation point left is the moment a slot is (re)spawned:
:func:`spawn_detached` shifts an at-threshold ``<slot>.log`` to ``.log.1`` …
``.log.5`` (oldest dropped) before opening the fresh append handle. **Known
limitation:** a long-running chatty slot still grows its *current* file without
bound between restarts — in-run rotation is deliberately NOT provided here (it
would need a size-watching thread or a logging pipe per slot); the trade-off is
recorded in ``docs/adr/0014-roster-detached-off-process-compose.md``.

**Why kill the process GROUP.** :func:`spawn_detached` launches with
``start_new_session=True``, so the child is a session + process-group leader (its
pgid equals its pid) and every worker it forks under the launcher shim inherits
that group. :func:`terminate` therefore signals the whole group
(:func:`os.killpg`) — the entire point of the new session — falling back to the
bare pid only if the group signal fails. That tears down the shim's child workers
too, rather than orphaning them.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from calfcord._atomic import atomic_write_text
from calfcord.supervisor.compose import _log_location

# A monotonic clock for :func:`terminate`'s bounded waits. Injected so a test
# clock can advance in lockstep with the injected ``sleep`` (the lifecycle
# pattern), making the graceful/escalate timing deterministic.
Clock = Callable[[], float]

# The inter-poll wait for :func:`terminate`. Awaitable so the roster's async
# callers never block the event loop during the (up to ``term_timeout_s``)
# graceful window; injected so tests drive the loop without real time.
Sleep = Callable[[float], Awaitable[None]]

# Pidfile on-disk schema version, so a future record shape can be recognised and
# migrated rather than silently misread.
_PIDFILE_SCHEMA_VERSION = 1

# How often :func:`terminate` re-checks liveness while waiting for a signalled
# process to die, and how long it waits for the reap after an (unignorable)
# SIGKILL before returning best-effort.
_REAP_POLL_INTERVAL_S = 0.05
_KILL_REAP_TIMEOUT_S = 2.0

# Bound on the Darwin ``ps`` start-token read, so a wedged ``ps`` can never hang
# a caller. Generous — ``ps`` answers in milliseconds — but finite.
_PS_TIMEOUT_S = 5.0

# Rotate-at-spawn policy, mirroring the Process Compose rotation the roster lost
# when it moved off PC (10 MB, 5 backups; no compression — the files are small
# and plain text keeps `disco logs`-adjacent tooling trivial). See the module
# docstring for the honest limitation: rotation happens ONLY at spawn.
_LOG_ROTATE_AT_BYTES = 10 * 1024 * 1024
_LOG_ROTATE_BACKUPS = 5


@dataclass(frozen=True)
class PidRecord:
    """The identity of one spawned process, persisted to its pidfile.

    ``pid`` + ``start_token`` are the reuse-proof identity (see the module
    docstring); ``argv`` / ``spawn_ts`` / ``argv_hash`` are recorded for
    inspection and as the degraded-platform fallback identity.
    """

    pid: int
    argv: tuple[str, ...]
    start_token: str
    spawn_ts: float
    argv_hash: str


@dataclass(frozen=True)
class SpawnedProcess:
    """What :func:`spawn_detached` hands back: the pid, its record, and its paths."""

    pid: int
    record: PidRecord
    pidfile: Path
    log_path: Path


class Identity(Enum):
    """The tri-state verdict on "is the live process at ``record.pid`` ours?".

    * ``OURS`` — alive AND the re-read start-token matches the record exactly.
    * ``NOT_OURS`` — provably not our live process: the pid is dead, the token
      mismatches (a recycled pid), or the record carries no token at all (an
      unprovable claim is refused — see :func:`identity`).
    * ``INDETERMINATE`` — the pid is alive and the record carries a token, but the
      current token could not be read (e.g. a transient darwin ``ps`` failure).
      Callers must treat this as "unknown": never signal the pid AND never remove
      its pidfile — either action on a live OWNED process would be destructive
      (an innocent kill, or an invisible slot that double-spawns on next start).
    """

    OURS = "ours"
    NOT_OURS = "not_ours"
    INDETERMINATE = "indeterminate"


class TerminateResult(Enum):
    """The outcome of a :func:`terminate` call.

    * ``TERMINATED`` — died on SIGTERM within the graceful window.
    * ``KILLED`` — survived SIGTERM; SIGKILL was sent AND the pid was observed
      dead within the bounded reap window.
    * ``KILL_UNCONFIRMED`` — SIGKILL was sent but the pid was STILL alive when the
      reap window closed (a wedged/unkillable process); callers must not claim it
      stopped, and must keep its pidfile.
    * ``ALREADY_DEAD`` — the pid was already gone before any signal.
    * ``NOT_OURS`` — the pid is alive but its identity does not match; NOT signalled.
    * ``INDETERMINATE`` — the pid is alive but its identity could not be READ
      (:data:`Identity.INDETERMINATE`); NOT signalled — we do not kill what we
      cannot verify.
    """

    TERMINATED = "terminated"
    KILLED = "killed"
    KILL_UNCONFIRMED = "kill_unconfirmed"
    ALREADY_DEAD = "already_dead"
    NOT_OURS = "not_ours"
    INDETERMINATE = "indeterminate"

    @property
    def process_was_stopped(self) -> bool:
        """Whether the process actually died at our hand — the ONE membership
        definition the verb/sweep callers share, so "stopped" cannot drift."""
        return self in (TerminateResult.TERMINATED, TerminateResult.KILLED)


# --- path conventions (pure) ------------------------------------------------


def require_safe_slot(slot: str) -> str:
    """Validate ``slot`` as a bare filename stem; return it, or raise ``ValueError``.

    The slot is interpolated into pidfile/lock/log paths under ``state/``, so a
    traversal-shaped name (``../../x``) would make the path helpers construct —
    and the verbs create/unlink/rename — files OUTSIDE the state tree. This is
    the narrowest waist every path construction goes through: reject empty names,
    dot-leading names, ``..`` sequences, and any path separator.
    """
    if (
        not slot
        or slot.startswith(".")
        or ".." in slot
        or "/" in slot
        or "\\" in slot
        or os.sep in slot
        or (os.altsep is not None and os.altsep in slot)
    ):
        raise ValueError(f"unsafe slot name {slot!r}: must be a bare filename stem")
    return slot


def ensure_private_dir(path: str | os.PathLike[str]) -> Path:
    """Create ``path`` (and parents) if missing, owner-only (``0700``); return it.

    ``state/run`` holds pidfiles (trusted input to a kill path) and ``state/logs``
    holds the slot logs, so the dirs THIS code creates are tightened to the owner
    regardless of umask. A pre-existing dir is left exactly as the operator set
    it — creating is ours to harden, re-modding user dirs is not.
    """
    path = Path(os.fspath(path))
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        # chmod (not mkdir's mode) so the target mode lands regardless of umask;
        # best-effort — a chmod-refusing filesystem must not fail the caller.
        with contextlib.suppress(OSError):
            path.chmod(0o700)
    return path


def pidfile_for(home: str | os.PathLike[str], slot: str) -> Path:
    """The pidfile path for ``slot`` under ``home``: ``<home>/state/run/<slot>.pid``."""
    require_safe_slot(slot)
    return Path(os.fspath(home)) / "state" / "run" / f"{slot}.pid"


def log_path_for(home: str | os.PathLike[str], slot: str) -> Path:
    """The log path for ``slot``: ``<home>/state/logs/<slot>.log``.

    Delegates to :func:`compose._log_location` so a detached roster process writes
    to the SAME file Process Compose used for the substrate — ``disco logs`` reads
    one convention and the two can never drift.
    """
    require_safe_slot(slot)
    return Path(_log_location(os.fspath(home), slot))


# --- spawn ------------------------------------------------------------------


def spawn_detached(
    argv: Sequence[str],
    *,
    log_path: str | os.PathLike[str],
    pidfile: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> SpawnedProcess:
    """Launch ``argv`` as a detached process and persist its identity pidfile.

    The child is fully detached from the caller's terminal: ``start_new_session``
    puts it in its own session (so a Ctrl-C to the CLI's process group never reaches
    it and it outlives the CLI), stdin is ``/dev/null``, and stdout+stderr are
    MERGED and **appended** to ``log_path`` (binary append, parent dirs created) so
    successive runs accumulate rather than clobber. ``env`` (when given) REPLACES
    the child environment wholesale — the mechanism passes it straight to
    :class:`subprocess.Popen`; ``cwd`` sets the working directory.

    The pidfile is written atomically (same-dir tmp + rename, via
    :func:`calfcord._atomic.atomic_write_text`) **before returning**, carrying the
    pid and a re-queryable start-token, so a caller that reads it back the instant
    this returns can already prove ownership (and never mistake a recycled pid for
    ours — see the module docstring).
    """
    argv = [os.fspath(a) for a in argv]
    log_path = Path(log_path)
    pidfile = Path(pidfile)
    ensure_private_dir(log_path.parent)
    _rotate_log_at_spawn(log_path)

    # Binary append: never truncate an existing log, and let the child's own dup'd
    # fd carry the writes after the parent closes its handle. Opened with
    # O_NOFOLLOW + mode 0600: a symlink planted at the log path would otherwise
    # redirect the append (and the rotate-rename) to an arbitrary file, so it is
    # refused HERE, before any child exists — a kill-nothing failure. Not a
    # `with` block — the handle must stay open across the Popen (which dup's it)
    # and is closed explicitly in the finally, so a context manager would not fit.
    try:
        log_fd = os.open(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK) or log_path.is_symlink():
            raise RuntimeError(
                f"refusing to spawn: log path {log_path} is a symlink — "
                "remove it and retry"
            ) from exc
        raise
    log_handle = os.fdopen(log_fd, "ab")
    try:
        # argv is caller-controlled but passed as a list (no shell), so there is no
        # interpolation surface; the child is detached into its own session.
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=dict(env) if env is not None else None,
            cwd=os.fspath(cwd) if cwd is not None else None,
        )
    finally:
        # Popen dup'd the fd into the child; the parent's handle is now redundant.
        log_handle.close()

    record = _identity_for(proc.pid, argv)
    if not record.start_token:
        # An empty captured token can never prove ownership later — identity()
        # permanently refuses token-less records — so every stop/status/sweep
        # path would treat the live child as a stranger: sweep its pidfile and
        # never signal it, an invisible CLI-unstoppable orphan. Same remedy as
        # the failed pidfile write below: kill rather than leak.
        _kill_child_to_avoid_orphan(proc.pid)
        raise RuntimeError(
            f"could not capture the process identity token for pid {proc.pid} "
            "(start-token read failed at spawn); the child was killed rather "
            "than leak an untrackable process — retry the start"
        )
    try:
        ensure_private_dir(pidfile.parent)
        atomic_write_text(pidfile, json.dumps(_record_to_dict(record)))
    except OSError as exc:
        # Without a pidfile NOTHING can ever find or stop this child again (every
        # stop/status/sweep path reads state/run), so a failed write must not
        # leave a live orphan behind either.
        _kill_child_to_avoid_orphan(proc.pid)
        raise RuntimeError(
            f"failed to write pidfile {pidfile} ({exc}); "
            f"the spawned process (pid {proc.pid}) was killed to avoid an orphan"
        ) from exc
    return SpawnedProcess(pid=proc.pid, record=record, pidfile=pidfile, log_path=log_path)


def _kill_child_to_avoid_orphan(pid: int) -> None:
    """SIGKILL the just-spawned group and reap the direct child.

    The shared abort for a spawn whose identity can never be tracked (no
    captured start-token, or no pidfile written): a live child nothing can ever
    find or stop again must not outlive the failed spawn."""
    _signal_group_or_pid(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.waitpid(pid, 0)


def _rotate_log_at_spawn(log_path: Path) -> None:
    """Shift an at-threshold ``<slot>.log`` into the ``.log.1``…``.log.N`` chain.

    Rotation is best-effort and total: any filesystem hiccup (a vanished file, a
    permission oddity) leaves the log where it is and lets the spawn proceed —
    losing rotation is strictly better than failing a start over housekeeping.
    The oldest backup is dropped first so the keep-count is a hard cap; in-run
    growth is NOT bounded here (see the module docstring).
    """
    try:
        # lstat, not stat: sizing THROUGH a planted symlink would let an
        # attacker-chosen huge target trigger renames around the link (the open
        # below refuses the symlink itself via O_NOFOLLOW).
        if log_path.lstat().st_size < _LOG_ROTATE_AT_BYTES:
            return
    except OSError:
        return
    with contextlib.suppress(OSError):
        log_path.with_name(f"{log_path.name}.{_LOG_ROTATE_BACKUPS}").unlink(missing_ok=True)
        for i in range(_LOG_ROTATE_BACKUPS - 1, 0, -1):
            backup = log_path.with_name(f"{log_path.name}.{i}")
            if backup.exists():
                backup.rename(log_path.with_name(f"{log_path.name}.{i + 1}"))
        log_path.rename(log_path.with_name(f"{log_path.name}.1"))


def _identity_for(pid: int, argv: Sequence[str]) -> PidRecord:
    """Build the :class:`PidRecord` for a just-spawned ``pid`` running ``argv``."""
    start_token = _process_start_token(pid) or ""
    argv_hash = hashlib.sha256("\x00".join(argv).encode("utf-8")).hexdigest()
    return PidRecord(
        pid=pid,
        argv=tuple(argv),
        start_token=start_token,
        spawn_ts=time.time(),
        argv_hash=argv_hash,
    )


def _record_to_dict(record: PidRecord) -> dict:
    return {
        "v": _PIDFILE_SCHEMA_VERSION,
        "pid": record.pid,
        "argv": list(record.argv),
        "start_token": record.start_token,
        "spawn_ts": record.spawn_ts,
        "argv_hash": record.argv_hash,
    }


# --- read -------------------------------------------------------------------


def read_pidfile(pidfile: str | os.PathLike[str]) -> PidRecord | None:
    """Parse ``pidfile`` into a :class:`PidRecord`, or ``None`` — never raises.

    A missing file, unreadable bytes, non-JSON content, an unknown schema version
    (``v``), or a payload lacking the required ``pid``/``argv`` all yield ``None``
    (there is simply no usable record), so a torn or hand-mangled pidfile degrades
    to "no record" rather than crashing a caller mid-cleanup.
    """
    try:
        raw = Path(pidfile).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, Mapping):
        return None
    if data.get("v") != _PIDFILE_SCHEMA_VERSION:
        # A future record shape must be recognised and refused, not silently
        # misread under today's field meanings (the ``v`` field's whole job).
        return None
    try:
        return PidRecord(
            pid=int(data["pid"]),
            argv=tuple(str(a) for a in data["argv"]),
            start_token=str(data.get("start_token", "")),
            spawn_ts=float(data.get("spawn_ts", 0.0)),
            argv_hash=str(data.get("argv_hash", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --- identity / liveness ----------------------------------------------------


def identity(record: PidRecord) -> Identity:
    """The tri-state ownership verdict for ``record`` (see :class:`Identity`).

    Two gates. First liveness (``os.kill(pid, 0)`` semantics: a bare signal-0
    probe, treating ``EPERM`` as alive — the process exists, we simply may not
    signal it); a dead pid is provably not our live process → ``NOT_OURS``. Then
    identity: the recorded start-token is re-read for the live pid and must match
    EXACTLY, so a recycled pid (same number, a later start time) reads
    ``NOT_OURS``. A read that FAILS on a live pid (a wedged/transient ``ps`` on
    darwin, a ``/proc`` race on Linux) is ``INDETERMINATE`` — distinct from a
    mismatch, because the process may well be ours and only the *evidence* is
    momentarily unavailable.

    A record with an empty ``start_token`` means no re-queryable OS identity was
    captured at spawn (a platform exposing no cheap start-token source). Because
    this primitive's callers go on to *kill* the pid, that unprovable claim is
    permanently refused (``NOT_OURS``), never parked as indeterminate: no later
    re-read can ever prove it.
    """
    if not _pid_alive(record.pid):
        return Identity.NOT_OURS
    if not record.start_token:
        return Identity.NOT_OURS
    current = _process_start_token(record.pid)
    if current is None:
        # The token read failed — but if the pid died BETWEEN the liveness gate
        # and the read (the common reason a read fails), that is not an unknown:
        # a provably-dead pid can never be our live process. Only a read that
        # fails while the pid stays alive is genuinely indeterminate.
        if not _pid_alive(record.pid):
            return Identity.NOT_OURS
        return Identity.INDETERMINATE
    return Identity.OURS if current == record.start_token else Identity.NOT_OURS


def is_ours_and_alive(record: PidRecord) -> bool:
    """Whether ``record``'s pid is alive AND provably the process we spawned.

    The strict boolean projection of :func:`identity`: only ``OURS`` reads
    ``True``. ``INDETERMINATE`` reads ``False`` here (an unverifiable claim is
    never acted on as ours), so callers that must *preserve* an indeterminate
    slot rather than treat it as absent should consult :func:`identity` directly.
    """
    return identity(record) is Identity.OURS


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process, via ``os.kill(pid, 0)`` semantics.

    ``ESRCH`` (no such process) is dead; ``EPERM`` means the process exists but is
    not ours to signal — still alive. Non-positive pids are rejected outright
    because ``os.kill`` treats ``0`` / ``-1`` as broadcast/group targets, never a
    single-process liveness probe.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_token(pid: int) -> str | None:
    """The OS process start-token for ``pid``, or ``None`` if unavailable.

    A stable, re-queryable value that changes when a pid is recycled: Linux reads
    ``/proc/<pid>/stat`` field 22 (start time in clock ticks since boot); Darwin
    shells out to ``ps -o lstart`` (the process start timestamp). Other platforms
    return ``None`` (no cheap source), which forces :func:`is_ours_and_alive` down
    its conservative refuse-ownership path.
    """
    if sys.platform == "darwin":
        return _darwin_start_token(pid)
    return _linux_start_token(pid)


def _linux_start_token(pid: int) -> str | None:
    """Field 22 (starttime) of ``/proc/<pid>/stat``, or ``None`` if unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    # The comm field (2) is parenthesised and may itself contain spaces or ')', so
    # split AFTER the final ')'. The tokens that follow begin at field 3 (state),
    # making field 22 (starttime) index 19.
    rparen = data.rfind(b")")
    if rparen == -1:
        return None
    rest = data[rparen + 1 :].split()
    if len(rest) < 20:
        return None
    return rest[19].decode("ascii", errors="replace")


def _darwin_start_token(pid: int) -> str | None:
    """The ``ps -o lstart`` start timestamp for ``pid``, or ``None`` if unreadable.

    Pinned to ``/bin/ps`` by absolute path: the token feeds a kill decision, so
    it must come from the system binary, never a ``ps`` resolved off a caller's
    ``$PATH`` (where a planted binary could forge a matching token).
    """
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None


# --- terminate --------------------------------------------------------------


async def terminate(
    record: PidRecord,
    *,
    term_timeout_s: float = 10.0,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> TerminateResult:
    """Stop the process ``record`` names: SIGTERM the group, escalate to SIGKILL.

    The sequence, matching the no-auto-respawn model (stop it; do not restart it):

    1. Reap first (a prior kill may have left our own child a zombie whose pid still
       answers ``os.kill(pid, 0)``), then check identity. If the pid is not alive →
       ``ALREADY_DEAD``; if alive but provably not ours → ``NOT_OURS``; if alive
       but the identity could not be READ → ``INDETERMINATE``. Neither of the last
       two is EVER signalled — we do not kill what we cannot prove is ours.
    2. SIGTERM the **process group** (:func:`os.killpg` on the session-leader pid —
       the reason for ``start_new_session`` — so the shim's child workers die too),
       falling back to the bare pid if the group signal fails.
    3. Poll for death up to ``term_timeout_s`` (bounded by the injected ``clock`` /
       ``sleep``). Died → ``TERMINATED``.
    4. Otherwise RE-VERIFY identity (the entry proof is stale after a long
       graceful window): a recycled pid reads ``TERMINATED`` (our process died
       post-SIGTERM), an unreadable identity reads ``INDETERMINATE`` — neither is
       ever SIGKILLed. Only a still-provably-ours process is escalated: SIGKILL
       the group and wait (bounded) for the reap: observed dead → ``KILLED``;
       still alive when the window closes → ``KILL_UNCONFIRMED`` (a
       wedged/unkillable process — callers must not claim it stopped).
    """
    pid = record.pid
    # A zombie of our own child still answers os.kill(pid, 0); reap it so liveness
    # reads truthfully before we branch on it.
    reap(pid)

    if not _pid_alive(pid):
        return TerminateResult.ALREADY_DEAD
    verdict = identity(record)
    if verdict is Identity.INDETERMINATE:
        return TerminateResult.INDETERMINATE
    if verdict is not Identity.OURS:
        return TerminateResult.NOT_OURS

    _signal_group_or_pid(pid, signal.SIGTERM)
    if await _wait_until_dead(pid, timeout_s=term_timeout_s, clock=clock, sleep=sleep):
        return TerminateResult.TERMINATED

    # Re-verify identity IMMEDIATELY before the SIGKILL escalation: the graceful
    # window can be long, and the entry-gate proof is stale by now. A mismatch
    # means our process died after the SIGTERM and the pid was recycled — the
    # truthful outcome is TERMINATED, and the stranger is never signalled. An
    # unreadable identity is returned as such: we do not SIGKILL what we can no
    # longer prove is ours.
    verdict = identity(record)
    if verdict is Identity.INDETERMINATE:
        return TerminateResult.INDETERMINATE
    if verdict is not Identity.OURS:
        return TerminateResult.TERMINATED

    _signal_group_or_pid(pid, signal.SIGKILL)
    if await _wait_until_dead(pid, timeout_s=_KILL_REAP_TIMEOUT_S, clock=clock, sleep=sleep):
        return TerminateResult.KILLED
    return TerminateResult.KILL_UNCONFIRMED


async def _wait_until_dead(pid: int, *, timeout_s: float, clock: Clock, sleep: Sleep) -> bool:
    """Poll until ``pid`` dies or ``timeout_s`` elapses; reaps a zombie child each pass."""
    deadline = clock() + timeout_s
    while True:
        reap(pid)
        if not _pid_alive(pid):
            return True
        if clock() >= deadline:
            return False
        await sleep(_REAP_POLL_INTERVAL_S)


def _signal_group_or_pid(pid: int, sig: int) -> None:
    """Signal ``pid``'s process group, falling back to the bare pid.

    ``killpg`` on the session-leader pid (== pgid) reaches the shim's child workers
    too. If that fails for any reason (the group already gone, or the child somehow
    reparented its group), fall back to signalling the pid directly. Every failure
    is swallowed: a process that vanished between the liveness check and the signal
    is exactly the success case, not an error.
    """
    with contextlib.suppress(OSError):
        os.killpg(pid, sig)
        return
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


def reap(pid: int) -> None:
    """Best-effort non-blocking reap of ``pid`` if it is our child.

    Clears a zombie left by an exited-but-unwaited direct child so the pid stops
    answering ``os.kill(pid, 0)`` (a zombie also keeps its Linux ``/proc``
    start-token, so without a reap even the identity gate reads it as ours-and-
    alive). :func:`terminate` reaps before judging liveness, and any caller that
    polls liveness on a child IT spawned (``_workspace.launch_slot``'s confirm
    window) must do the same — nothing else waits on a detached child. ``ECHILD``
    (the process is not our child — the common cross-invocation case) and any
    other ``OSError`` are ignored: reaping is a courtesy, never load-bearing.
    Non-positive pids are rejected outright: ``waitpid(0, …)`` / ``waitpid(-1,
    …)`` wait on *any* child (group/broadcast semantics), so a corrupt pidfile
    must never let a bogus pid quietly reap an unrelated child of this process.
    """
    if pid <= 0:
        return
    with contextlib.suppress(OSError):
        os.waitpid(pid, os.WNOHANG)


# --- cleanup ----------------------------------------------------------------


def cleanup_stale(pidfile: str | os.PathLike[str]) -> bool:
    """Remove ``pidfile`` iff it is PROVABLY stale; return whether it was removed.

    Stale means: the file exists but its record is missing/corrupt, or names a
    process that is dead or provably not ours (a recycled pid). A live-and-ours
    pidfile is kept (``False``); an absent file is a no-op (``False``). An
    ``INDETERMINATE`` identity (a live pid whose token read failed) is also KEPT:
    unlinking it could make a live OWNED process invisible, and the next start
    would double-spawn beside it — the unknown case must degrade to "leave it
    alone", never to "sweep it". Removal races (another cleaner won) collapse to
    ``False`` rather than raising.
    """
    pidfile = Path(pidfile)
    if not pidfile.exists():
        return False
    record = read_pidfile(pidfile)
    if record is not None and identity(record) in (Identity.OURS, Identity.INDETERMINATE):
        return False
    try:
        pidfile.unlink()
    except OSError:
        return False
    return True
