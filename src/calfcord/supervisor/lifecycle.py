"""Substrate lifecycle orchestration: ``start`` / ``stop`` / ``status`` (§13.1-§13.3).

This is the imperative glue above the two pure-ish seams — :mod:`compose` (renders
the project) and :mod:`client` (the REST wire) — that brings the *office* up and
down. Everything that touches the world (the binary path, the process launcher,
the wall clock) is injected, so the whole flow is unit-testable with no real
``process-compose`` binary and no broker.

The hard-won contract from the Phase-0 spike (design §13) lives here:

* **Detached launch** is ``up -f <yaml> -D -t=false -p <port> -L <log>`` — never
  ``--no-server`` (that kills the REST API the readiness gate polls). The REST
  port is *derived from the home path* (:func:`pc_port_for`) so two installs on
  one host do not both grab the supervisor default :8080.
* **Priming reconcile for upstream bug #494** (§13.1): immediately after ``up``,
  issue exactly one no-op ``update_project`` with the byte-identical rendered
  YAML, so the buggy first project-update lands on a no-op instead of bouncing the
  substrate.
* **Readiness gate** (§12.6 / §13.3): ``up -D`` returning 0 does NOT mean healthy,
  so ``start`` polls the **bridge** to ``is_ready``/Running with a timeout and, on
  timeout, tears the substrate down and returns non-zero — a green light that
  lies is worse than a red one.
* **Lock + idempotency** (§12.4): an exclusive ``flock`` serializes start/stop so
  two concurrent starts cannot race two supervisors; ``start`` probes first and
  short-circuits if the office is already open, and ``stop`` is a no-op if nothing
  is running.

Import-light like the rest of this package.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from calfcord.health.check import BrokerProbe, default_broker_probe
from calfcord.supervisor import _workspace, procspawn
from calfcord.supervisor._workspace import (
    iter_process_dicts,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient
from calfcord.supervisor.compose import (
    SUPERVISOR_LOG_STEM,
    broker_is_compose_managed,
    render_compose,
)
from calfcord.supervisor.procspawn import TerminateResult

# A process launcher: hand it an argv and it starts the process. Production wires
# this to a detached ``subprocess.Popen`` (the ``up`` must outlive ``start``); a
# blocking variant is fine for the short-lived ``down``. Tests record the argv.
Spawn = Callable[[Sequence[str]], None]

# Monotonic seconds, for measuring the readiness-gate budget. Injected so a test
# clock can advance instantly in lockstep with ``sleep``.
Clock = Callable[[], float]

# The inter-poll wait. Injected so tests drive the poll loop with zero real time.
Sleep = Callable[[float], Awaitable[None]]

# Derived REST-port range (§12.4 multi-home): a documented high band that avoids
# the supervisor default :8080 and the broker's :9092, so a second $CALFCORD_HOME
# on the same host gets its own stable, non-colliding port. 800 ports is ample
# headroom against hash collisions for the handful of installs on one machine.
_PORT_RANGE_START = 8100
_PORT_RANGE_END = 8899
_PORT_RANGE_WIDTH = _PORT_RANGE_END - _PORT_RANGE_START + 1

# Readiness gate cadence (§13.2/§13.3): poll the bridge every few seconds until it
# is ready or the budget is spent. A modest default budget covers a cold broker
# provision + Discord connect without hanging the CLI forever.
_DEFAULT_READY_TIMEOUT_SECONDS = 90
_READINESS_POLL_INTERVAL_SECONDS = 2.0

# Bounded wait for the REST server itself to answer after a detached ``up`` (the
# socket binds a beat after the process forks). Separate from the readiness gate:
# this only proves the supervisor is talking, not that the bridge is healthy.
_SERVER_UP_TIMEOUT_SECONDS = 30
_SERVER_UP_POLL_INTERVAL_SECONDS = 0.5

# Bounded wait for a blocking ``down`` to complete (§13.2 shutdown grace is 10s;
# this caps the synchronous teardown a little above that so an orderly stop is
# never cut short, while a wedged supervisor still fails loudly instead of
# hanging the CLI forever).
_DOWN_TIMEOUT_SECONDS = 20

_LOCK_FILENAME = "calfcord-lifecycle.lock"
_COMPOSE_FILENAME = "process-compose.yaml"
# Derive from the single shared stem so the writer (this module's ``up -L``) and
# the reader (``disco logs``) can never drift apart (review #19).
_SUPERVISOR_LOG_FILENAME = f"{SUPERVISOR_LOG_STEM}.log"

# Substrate processes, for the status board's substrate-vs-roster split.
_SUBSTRATE = frozenset({"broker", "bridge"})

# The bridge's per-process log — where process-compose captures the bridge
# process's own stdout/stderr, beside the supervisor log (``compose._log_location``
# renders ``<home>/state/logs/<name>.log``). When the readiness gate times out the
# actual cause (e.g. a discord.py crash traceback) lands here, not in the
# supervisor log, so this is the file :func:`_diagnose_start_failure` reads.
_BRIDGE_LOG_FILENAME = "bridge.log"

# How much of the bridge log's TAIL the diagnosis reads. A crashing bridge prints
# its traceback last, so the tail carries the signal; bounding the read keeps a
# runaway/rotated log from being slurped whole into memory. 64 KiB comfortably
# spans a discord.py traceback plus surrounding noise.
_LOG_TAIL_BYTES = 64 * 1024

# Known bridge-startup failure signatures, matched against the log tail, most
# specific first. Each maps a discord.py exception name to an actionable, cause-
# specific fix that replaces the generic "broker down or intents off" guess.
# ``PrivilegedIntentsRequired`` is the common first-run miss: **Message Content**
# is the only privileged intent the bridge requests, so name it exactly rather
# than "intents" generically. ``LoginFailure`` ("Improper token") means the token
# itself was rejected.
_START_FAILURE_SIGNATURES: tuple[tuple[str, str], ...] = (
    (
        "PrivilegedIntentsRequired",
        "the bridge crashed because the Message Content privileged intent is off. "
        "Enable it under Developer Portal -> your app -> Bot -> Privileged Gateway "
        "Intents (toggle Message Content on) and Save, then re-run `disco start`.",
    ),
    (
        "LoginFailure",
        "the Discord bot token was rejected -- re-run `disco init` to re-enter it.",
    ),
)

# The pydantic-settings ValidationError header for the bridge's Discord settings
# model — a missing/invalid DISCORD_* value crashes the bridge before it ever
# reaches Discord, so the diagnosis is "complete the config", not the generic
# broker/intents guess.
_DISCORD_SETTINGS_ERROR_MARKER = "ValidationError"
_DISCORD_SETTINGS_MODEL = "DiscordSettings"

# A pydantic error detail is a bare lowercase field name on its own line followed
# by an indented explanation ("Field required", "Input should be …"). Cheap,
# tail-tolerant extraction — when it finds nothing the message stays generic.
_SETTINGS_FIELD_RE = re.compile(r"(?m)^([a-z][a-z0-9_]*)\r?\n\s+\S")


def _diagnose_settings_validation(tail: str) -> str | None:
    """The Discord-settings diagnosis for a pydantic ValidationError, or ``None``.

    Matches only the bridge's own settings model (``DiscordSettings``) so an
    unrelated model's ValidationError never claims a Discord config gap. Field
    names are mapped back to the ``DISCORD_*`` env vars the operator actually
    sets (pydantic prints the lowercase attribute names); when none are cheaply
    extractable from the tail the message stays generic but still actionable.
    """
    if _DISCORD_SETTINGS_ERROR_MARKER not in tail or _DISCORD_SETTINGS_MODEL not in tail:
        return None
    # Only look past the LAST ValidationError header so stray lowercase log
    # lines earlier in the tail cannot masquerade as field names.
    section = tail[tail.rindex(_DISCORD_SETTINGS_ERROR_MARKER) :]
    fields = [f"DISCORD_{name.upper()}" for name in _SETTINGS_FIELD_RE.findall(section)]
    if fields:
        named = ", ".join(dict.fromkeys(fields))  # de-dup, order kept
        return (
            f"the bridge is missing required Discord settings ({named}) -- "
            "re-run `disco init` to complete them."
        )
    return (
        "the bridge is missing required Discord settings -- "
        "re-run `disco init` to complete them."
    )


def _read_log_tail(path: str, *, max_bytes: int = _LOG_TAIL_BYTES) -> str | None:
    """Return the last ``max_bytes`` of ``path`` as text, or ``None`` if unreadable.

    Bounded (seeks to the tail rather than reading the whole file) so a large or
    rotated log cannot blow up memory. A missing/unreadable file yields ``None``
    instead of raising — diagnosis is best-effort and must never itself fail the
    caller. Bytes are decoded leniently (``errors="replace"``) since a truncated
    tail may slice a multibyte sequence.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _diagnose_start_failure(log_dir: str) -> str | None:
    """Diagnose the likeliest bridge-startup failure from its per-process log.

    Reads only the TAIL of ``<log_dir>/bridge.log`` and returns a cause-specific,
    actionable message for the first known signature it matches, or ``None`` when
    the log is missing/unreadable or carries no recognised signature (the caller
    then falls back to the generic hint). Pure and total: it never raises.
    """
    tail = _read_log_tail(os.path.join(log_dir, _BRIDGE_LOG_FILENAME))
    if tail is None:
        return None
    for signature, message in _START_FAILURE_SIGNATURES:
        if signature in tail:
            return message
    return _diagnose_settings_validation(tail)


def resolve_pc_binary() -> str:
    """Locate the ``process-compose`` binary, or raise an actionable error.

    Precedence (design §12.4): an explicit ``$CALFCORD_PROCESS_COMPOSE_BIN`` (dev
    override / packaging) → the install's ``$CALFCORD_HOME/bin/process-compose``
    (the ``ensure_process_compose`` bootstrap target) → a ``process-compose`` on
    ``PATH``. Each candidate must be an existing,
    executable file; a stale env var pointing at nothing falls through rather than
    masking a working PATH binary.
    """
    explicit = os.environ.get("CALFCORD_PROCESS_COMPOSE_BIN")
    if explicit and _is_executable_file(explicit):
        return explicit

    home = os.environ.get("CALFCORD_HOME")
    if home:
        candidate = os.path.join(home, "bin", "process-compose")
        if _is_executable_file(candidate):
            return candidate

    on_path = shutil.which("process-compose")
    if on_path:
        return on_path

    raise RuntimeError(
        "process-compose binary not found "
        "(checked $CALFCORD_PROCESS_COMPOSE_BIN, $CALFCORD_HOME/bin/process-compose, "
        "and PATH); re-run the Agent Disco installer to bootstrap it, or set "
        "$CALFCORD_PROCESS_COMPOSE_BIN to a process-compose v1.110.0 binary."
    )


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def pc_port_for(home: str | os.PathLike[str]) -> int:
    """A deterministic Process Compose REST port derived from the home path.

    Two ``$CALFCORD_HOME`` installs on one host must not both grab the supervisor
    default :8080 (§12.4). We hash the *absolute* home — so a relative invocation
    picks the same port as the absolute one — with a stable digest (NOT Python's
    per-process-salted ``hash()``) into a documented high band. Same home always
    yields the same port, across processes and reboots, so every REST call and the
    ``up -p`` flag agree.
    """
    absolute = os.path.abspath(os.fspath(home))
    digest = hashlib.sha256(absolute.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:8], "big") % _PORT_RANGE_WIDTH
    return _PORT_RANGE_START + offset


def _lock_path(home: str | os.PathLike[str]) -> str:
    return os.path.join(os.fspath(home), "state", _LOCK_FILENAME)


@contextlib.contextmanager
def lifecycle_lock(home: str | os.PathLike[str]):
    """Hold an exclusive ``flock`` over ``<home>/state/calfcord-lifecycle.lock``.

    Serializes ``start``/``stop`` so two concurrent invocations cannot race two
    supervisors against one home (§12.4). Uses ``LOCK_EX | LOCK_NB`` so a second
    holder fails *immediately* instead of blocking indefinitely. CONTENTION
    surfaces as :class:`~calfcord.supervisor._workspace.WorkspaceBusyError` — a
    domain refusal ``start``/``stop`` translate into one clean error line (the
    holder may be another start/stop OR a roster verb holding the lock SHARED for
    its spawn-confirm window, so the message names neither). Any other ``OSError``
    (permissions, a broken state dir) propagates as the IO problem it actually is.
    The parent ``state/`` dir is created on demand; the lock file itself is kept
    (its presence is harmless — the advisory lock, not the file, is the guard)
    and the fd is always closed on exit, releasing the lock.
    """
    path = _lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _workspace.WorkspaceBusyError(
                "another disco command is in progress for this workspace "
                f"(could not acquire {path}); retry in a moment."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# A workspace-readiness alias kept for the internal call sites here; the body is
# the one shared :func:`_workspace.workspace_is_up` (Fix #14 consolidation).
_supervisor_is_up = workspace_is_up


def _home_marker(home: str) -> str:
    """The home-specific path the rendered project embeds in every process.

    Each declared process's ``log_location`` is ``<home>/state/logs/<name>.log``
    (see :func:`compose._log_location`), and that absolute path opens every
    quoted string value in which it appears — so ``<home>/state`` is a prefix of
    one of the answering supervisor's quoted config paths iff that supervisor was
    launched for THIS home (see :func:`_supervisor_belongs_to_home`).
    """
    return os.path.join(home, "state")


async def _supervisor_belongs_to_home(
    client: ProcessComposeClient, home: str
) -> bool | None:
    """Whether the answering supervisor was launched for ``home`` (Fix #11).

    :func:`pc_port_for` maps two homes into an 800-port band, so a collision is
    possible: two installs can hash to one REST port. The bare ``project_state``
    idempotency probe only proves *something* answers that port, not *whose*
    supervisor it is — so before trusting an "already up" verdict we read back a
    declared process's config (which embeds the home-specific log path) and check
    for this home's marker.

    Returns ``True`` (this home's), ``False`` (a DIFFERENT home colliding on the
    port — the caller must fail loudly, never a false "already open"), or ``None``
    when it cannot be determined (the info route is unavailable), in which case the
    caller keeps the prior best-effort idempotent behavior.
    """
    try:
        # ``bridge`` is always a declared substrate process, so its config is the
        # stable place to read the home-specific log path back from.
        info = await client.get_process_info("bridge")
    except RuntimeError:
        return None
    if not info:
        return None
    # Robust to whatever JSON key Process Compose uses for the log path (the API
    # shape is version-fragile, §12.4 Risk #2): scan the whole serialized config
    # rather than pinning a single field name. Match the marker only where it
    # OPENS a quoted path value (repr quotes string values, and the absolute
    # home path starts the log_location/working_dir it appears in) so a home
    # whose path is a *suffix* of another's — e.g. "/calf" vs "/data/calf" —
    # cannot false-positive on a bare substring scan and silently adopt the
    # other install's colliding supervisor.
    marker = _home_marker(home)
    serialized = repr(info)
    return f"'{marker}" in serialized or f'"{marker}' in serialized


def _bridge_is_ready(state: object) -> bool:
    """Whether a ``get_process('bridge')`` state object reports its probe passed.

    The bridge declares a readiness probe (§13.2), so its *health* is the probe
    verdict, not mere liveness: Process Compose v1.110.0 sets ``is_ready: "Ready"``
    only once that exec probe passes. We gate STRICTLY on it — ``status: "Running"``
    while ``is_ready`` is anything other than ``"Ready"`` is exactly the
    green-light-that-lies the readiness gate exists to reject (§12.6/§13.3), so a
    Running-but-not-yet-ready bridge must NOT read healthy.
    """
    return isinstance(state, dict) and state.get("is_ready") == "Ready"


def _process_pid(state: object) -> int | None:
    """The OS pid Process Compose reports for a process, or ``None`` if absent/malformed.

    Used by the in-place restart gate to tell the NEW bridge instance apart from the
    pre-restart one (a restart always yields a fresh pid): see :func:`_await_bridge_ready`.
    """
    if isinstance(state, dict) and isinstance(state.get("pid"), int):
        return state["pid"]
    return None


async def _await_supervisor_up(
    client: ProcessComposeClient, *, clock: Clock, sleep: Sleep
) -> bool:
    """Poll the REST server until it answers, bounded by the server-up timeout."""
    deadline = clock() + _SERVER_UP_TIMEOUT_SECONDS
    while True:
        if await _supervisor_is_up(client):
            return True
        if clock() >= deadline:
            return False
        await sleep(_SERVER_UP_POLL_INTERVAL_SECONDS)


async def _await_bridge_ready(
    client: ProcessComposeClient,
    *,
    timeout_s: float,
    clock: Clock,
    sleep: Sleep,
    restarted_from_pid: int | None = None,
) -> bool:
    """Poll the bridge until it is ready, bounded by ``timeout_s`` (§13.3).

    A transport error mid-poll (the supervisor restarting the bridge under
    ``restart: always``) is treated as "not ready yet", not a fatal error, so a
    transient bounce does not abort the gate before the budget is spent.

    ``restarted_from_pid`` gates the in-place restart path (:func:`_restart_bridge_to_ready`):
    when set, ``Ready`` is accepted only once the reported pid DIFFERS from it, so a
    poll that races Process Compose cannot latch the pre-restart instance's stale
    ``Ready`` and lie (§12.6 — the bridge is ``Ready`` right up until it bounces, and
    its heartbeat-freshness probe can keep reading ``Ready`` off the old beat for a
    beat). The cold-start path leaves it ``None`` — there is no prior instance, and
    the bridge begins not-``Ready`` there anyway. A restarted bridge that never
    reports a pid degrades to the plain readiness gate rather than hanging.
    """
    deadline = clock() + timeout_s
    while True:
        try:
            state = await client.get_process("bridge")
        except RuntimeError:
            state = None
        if _bridge_is_ready(state) and (restarted_from_pid is None or _process_pid(state) != restarted_from_pid):
            return True
        if clock() >= deadline:
            return False
        await sleep(_READINESS_POLL_INTERVAL_SECONDS)


async def _restart_bridge_to_ready(
    client: ProcessComposeClient,
    home: str,
    *,
    ready_timeout_s: float,
    clock: Clock,
    sleep: Sleep,
) -> str | None:
    """Restart the bridge slot in place and wait until the NEW instance is Ready.

    The shared seam behind ``disco bridge restart`` and ``start``'s already-open
    path. Restarts ONLY the ``bridge`` process inside the running supervisor —
    never relaunches the supervisor, so the §12.4 "no second supervisor" invariant
    holds for both callers.

    Returns ``None`` on success (the restarted bridge reached Ready). On failure it
    returns a ready-to-print operator message; the caller prints ``error: <msg>``
    and returns non-zero. §12.6 honest fail-fast: the caller must NOT report success
    on a non-``None`` return. Two failure modes are distinguished because they need
    different guidance:

    * the restart REST call itself failed (the bridge never bounced) — surface the
      :class:`ProcessComposeError` (its HTTP status + Process Compose's reason), the
      way ``stop`` echoes its teardown error, instead of a misleading "not ready";
    * the bridge bounced but never reached Ready within ``ready_timeout_s`` —
      diagnose the cause from ``bridge.log`` (a rejected token / disabled intent /
      missing ``DISCORD_*`` field is the likely culprit exactly when a restart is
      picking up new config), reusing :func:`_diagnose_start_failure`, else a
      generic "check the log".

    Readiness is gated on the reported pid CHANGING from the pre-restart value (via
    :func:`_await_bridge_ready`), so a poll racing Process Compose cannot latch the
    old instance's stale ``Ready``. The caller owns the lifecycle lock; this must
    NOT re-acquire it — the ``flock`` is non-reentrant, so a second acquire refuses.
    """
    try:
        pid_before = _process_pid(await client.get_process("bridge"))
    except RuntimeError:
        pid_before = None  # can't read it (e.g. slot not declared yet) → plain readiness gate
    try:
        await client.restart_process("bridge")
    except RuntimeError as exc:
        # The restart request itself failed — the bridge never bounced, so "not
        # ready — check the bridge log" would misdirect. Surface the actual cause
        # (HTTP status + PC's reason) the way stop() echoes its teardown error.
        return f"the bridge restart request failed ({exc})."
    if await _await_bridge_ready(
        client, timeout_s=ready_timeout_s, clock=clock, sleep=sleep, restarted_from_pid=pid_before
    ):
        return None
    # Bounced but never became Ready: prefer the cause-specific diagnosis the
    # cold-start path gives (bad token / disabled intent / bad DISCORD_* field) —
    # the restart path is where a freshly-edited config most often breaks.
    diagnosis = _diagnose_start_failure(os.path.join(home, "state", "logs"))
    return diagnosis or "the bridge didn't come back ready — check `disco logs bridge`."


def _default_spawn(argv: Sequence[str]) -> None:
    """Launch a detached child that outlives this process (the production spawn).

    ``start_new_session=True`` puts the child in its own session so the supervisor
    keeps running after the CLI exits (and is not felled by a Ctrl-C delivered to
    the CLI's terminal group). stdout/stderr are discarded — the supervisor writes
    its own ``-L`` log file — so no pipe fills up and wedges the child.
    """
    import subprocess

    # argv is built from a pinned binary path + literal flags (no shell, no user
    # string interpolation), so the child launch is safe.
    subprocess.Popen(
        list(argv),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _default_spawn_blocking(argv: Sequence[str]) -> None:
    """Run ``argv`` to completion, bounded by a timeout (the blocking spawn).

    Unlike :func:`_default_spawn` (which detaches a child that must outlive the
    CLI), this is for the short-lived ``down`` teardown: ``stop`` and the start
    readiness-timeout teardown must wait for the supervisor to actually stop
    before returning, so a later ``start`` cannot collide with a supervisor that
    is still shutting down. stdout/stderr are discarded (the supervisor logs to
    its ``-L`` file); a bounded ``timeout`` turns a wedged ``down`` into a loud
    failure rather than an indefinite hang.
    """
    import subprocess

    # argv is a pinned binary path + literal flags (no shell, no user string
    # interpolation), so the launch is safe.
    subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_DOWN_TIMEOUT_SECONDS,
        check=False,
    )


# A per-home client resolver alias for the internal call sites; the body is the
# one shared :func:`_workspace.resolve_client` (Fix #14 consolidation).
_resolve_client = resolve_client


def _attempt_teardown(spawn_blocking: Spawn, binary: str, port: int) -> bool:
    """Blocking ``down``; ``True`` iff it completed without raising.

    ``start``'s failure paths must tear the substrate back down, but the teardown
    itself can fail (a wedged supervisor's ``down`` hits its bounded timeout).
    That failure is CAPTURED, never suppressed into a "tore it down" overclaim:
    the caller words its message on this verdict, and no exception (including
    ``TimeoutExpired``) escapes as a raw traceback.
    """
    try:
        spawn_blocking([binary, "down", "-p", str(port)])
    except Exception:
        return False
    return True


# The honest suffix for a failure path whose own teardown ALSO failed.
_TEARDOWN_UNCERTAIN = (
    "teardown may not have completed — run `disco stop` or check `disco status`."
)


def _agents_defined(home: str) -> bool:
    """Whether any agent ``.md`` is defined for this install (banner signpost only).

    Phase 3 dropped ``start``'s vestigial ``agent_ids`` param (the manifest is
    substrate-only), so the success banners' create-vs-start steer reads the
    agents dir directly: ``$CALFKIT_AGENTS_DIR`` when set (the same override the
    shim, the runners, and ``init.resolve_paths`` honour) else ``<home>/agents``.
    ``detect_agents`` is the CLI's one definition of "which ``.md`` files are live
    agents" — reused rather than re-declared so the skip rules cannot drift;
    imported lazily because its module pulls the agents package (heavy), which
    the supervisor must not pay at import time.
    """
    from calfcord.cli._agents import detect_agents

    agents_dir = os.environ.get("CALFKIT_AGENTS_DIR") or os.path.join(home, "agents")
    return bool(detect_agents(Path(agents_dir)))


async def start(
    home: str | os.PathLike[str],
    *,
    server_urls: str,
    launcher: str,
    ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_SECONDS,
    client: ProcessComposeClient | None = None,
    spawn: Spawn | None = None,
    spawn_blocking: Spawn | None = None,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    broker_probe: BrokerProbe | None = None,
    banner: bool = True,
) -> int:
    """Open the workspace: render, launch detached, prime, gate on readiness.

    Returns a POSIX exit code: ``0`` once the substrate (broker + bridge) is up
    and the bridge is ready; non-zero if the broker precondition fails fast, or
    (after tearing the substrate back down) if the bridge does not become ready
    within ``ready_timeout_s`` — never a green light that lies (§12.6).

    An **external** broker is a **fast-fail precondition** (§13.2): before
    rendering or launching anything, ``start`` probes it via ``broker_probe``
    (default derived from ``server_urls``); a down remote broker returns non-zero
    immediately with an actionable hint instead of burning the full
    bridge-readiness budget waiting for a bridge that can never connect. A
    **compose-managed** (loopback) broker is EXEMPT from this probe: it is a cold
    autostart process ``up`` itself launches (with its own readiness probe +
    ``depends_on`` graph), so pre-probing it would fast-fail the very broker being
    started. The same :func:`broker_is_compose_managed` predicate also drives
    whether the rendered manifest declares a local ``broker`` slot, so the two
    decisions cannot diverge.

    ``banner`` governs the **terminal next-step signpost ONLY** — the closing
    "workspace open … -> disco agent start <name>" line (and its already-open and
    empty-org variants). Errors and warnings print regardless, on every branch: they
    are composed where their context lives, every caller wants them verbatim, and
    ``init``'s finish explicitly relies on ``start`` having already printed the
    specific cause. This is emphatically **not** a ``quiet`` flag; widening it to one
    would strand the wizard on a silent failure.

    Pass ``banner=False`` when the caller is about to *take* the next step the
    signpost would name. ``disco start`` leaves it on — the operator is being handed
    back the prompt with a decision to make, and §12.6 requires that banner to always
    name what's next. But ``disco init`` and ``agent create``'s start-now go on to
    start the agent themselves, so for them the signpost tells the operator to run a
    command the very next line executes for them, and they narrate their own progress
    in their own voice besides.

    ``client`` / ``spawn`` / ``spawn_blocking`` / ``clock`` / ``sleep`` /
    ``broker_probe`` are injected for testing; in production they default to a
    per-home REST client, a detached subprocess spawner (``up`` must outlive the
    CLI), a blocking spawner (for the synchronous ``down`` teardown),
    ``time.monotonic``, ``asyncio.sleep``, and the real broker metadata probe.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)
    spawn = spawn or _default_spawn
    spawn_blocking = spawn_blocking or _default_spawn_blocking
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    # Read the defined roster once so the success banners can tell an empty org
    # from a defined one — an org with zero agents is steered to `agent create`,
    # not the pointless `agent start`. The banners are its ONLY consumer, and the
    # read lazily imports the heavy agents package, so a caller that suppressed the
    # signpost pays neither the import nor the filesystem scan.
    agents_defined = _agents_defined(home) if banner else False

    with contextlib.ExitStack() as stack:
        # A contended lock (another start/stop, or a roster verb holding it SHARED
        # for its spawn-confirm window) is a domain refusal, not a traceback: one
        # clean error line, exit 1, and the holder is left undisturbed.
        try:
            stack.enter_context(lifecycle_lock(home))
        except _workspace.WorkspaceBusyError as exc:
            print(f"error: {exc}")
            return 1
        # Idempotency (§12.4): if the office is already open, do NOT launch a
        # second supervisor. We DO cycle the bridge in place (below) so a re-run of
        # `start`/`init` picks up a new build/config and clears a wedged mesh reader
        # — restarting one slot within the existing supervisor keeps the "no second
        # supervisor" invariant intact. But the port can collide across homes (Fix
        # #11): verify the answering supervisor is THIS home's before touching it. A
        # DIFFERENT home colliding on the port must fail loudly — never a false
        # "already open", and never restart the other install's bridge.
        if await _supervisor_is_up(client):
            belongs = await _supervisor_belongs_to_home(client, home)
            if belongs is False:
                print(
                    f"error: another Agent Disco install is already using REST port "
                    f"{pc_port_for(home)} on this host (a port collision between "
                    "two $CALFCORD_HOME installs). Stop the other install, or run "
                    "this one under a different $CALFCORD_HOME, then re-run "
                    "`disco start`."
                )
                return 1
            # Honest fail-fast (§12.6): if the in-place bridge restart doesn't come
            # back Ready, surface the specific cause and return non-zero rather than
            # print a green "already open" banner that lies. `restart: always` keeps
            # retrying the bridge underneath.
            reason = await _restart_bridge_to_ready(
                client, home, ready_timeout_s=ready_timeout_s, clock=clock, sleep=sleep
            )
            if reason is not None:
                print(f"error: {reason}")
                return 1
            if banner:
                if agents_defined:
                    print("workspace already open — bridge restarted. Next: disco agent start <name>")
                else:
                    print(
                        "workspace already open — bridge restarted. "
                        "No agents defined yet -> disco agent create <name>"
                    )
            return 0

        # One predicate governs both the pre-launch probe and the manifest's broker
        # slot, so they can never diverge (§13.2). A loopback URL means the broker
        # is a compose-managed AUTOSTART process that `up` itself launches; an
        # external URL means the operator's broker lives elsewhere.
        broker_managed = broker_is_compose_managed(server_urls)

        # Broker fast-fail precondition (§13.2) — EXTERNAL brokers ONLY. For a
        # remote broker the bridge cannot reach Ready without it, so probe it
        # BEFORE rendering/launching: a down broker fails here in a heartbeat
        # instead of after the full bridge readiness timeout, and the workspace is
        # left untouched (no `up`). A compose-managed loopback broker is EXEMPT: it
        # is a cold autostart process `up` brings up (with its own readiness probe
        # + depends_on graph), so probing it here would fast-fail the very broker we
        # are about to start — the P0 cold-start bug.
        if not broker_managed:
            probe = broker_probe or default_broker_probe(server_urls)
            if not await probe():
                print(
                    f"error: broker not reachable at {server_urls}; "
                    "start it with `disco broker`, then re-run `disco start`."
                )
                return 1

        port = pc_port_for(home)
        # The manifest is SUBSTRATE-ONLY: the roster (agents, ``tools``,
        # ``mcp-<server>``) spawns off Process Compose per slot, so nothing
        # roster-shaped is threaded into the compose render.
        yaml_text = render_compose(
            home=home,
            launcher=launcher,
            broker_managed=broker_managed,
        )
        yaml_path = _write_compose(home, yaml_text)
        log_path = _ensure_log_path(home)
        binary = resolve_pc_binary()

        # Detached launch — §13.2 flags exactly; NEVER --no-server.
        spawn(
            [
                binary,
                "up",
                "-f",
                yaml_path,
                "-D",
                "-t=false",
                "-p",
                str(port),
                "-L",
                log_path,
            ]
        )

        if not await _await_supervisor_up(client, clock=clock, sleep=sleep):
            print(
                "error: process-compose REST server did not come up "
                f"within {_SERVER_UP_TIMEOUT_SECONDS}s; "
                f"check {log_path}"
            )
            return 1

        # Priming reconcile for upstream #494 (§13.1): exactly one no-op
        # project-update with the byte-identical YAML, so the buggy first update
        # lands on a no-op instead of bouncing the substrate. This runs AFTER the
        # detached supervisor is already up, so a raise here (a PC reconcile error
        # / transport failure) must NOT be left bare: an unhandled exception would
        # orphan the supervisor and dump a traceback — and since `start` is the
        # wizard's start_fn, it would crash `disco init`. Fail like the
        # readiness-gate path below: tear the substrate back down via the BLOCKING
        # seam (a racy detached `down` could let a retried `start` collide with a
        # supervisor still stopping), report actionably, and return non-zero.
        try:
            await client.update_project(yaml_text)
        except RuntimeError:
            torn_down = _attempt_teardown(spawn_blocking, binary, port)
            outcome = "tore it down." if torn_down else _TEARDOWN_UNCERTAIN
            print(
                f"error: workspace failed to prime; {outcome} "
                f"See {log_path} or run: disco doctor"
            )
            return 1

        if not await _await_bridge_ready(
            client, timeout_s=ready_timeout_s, clock=clock, sleep=sleep
        ):
            # No green light that lies (§12.6): tear the substrate down and report
            # the specific failure + the likeliest cause. Use the BLOCKING seam so
            # the supervisor is actually stopped before we return — a fire-and-
            # forget detached `down` could let a retried `start` collide with a
            # supervisor still shutting down (§13.3). The teardown's own failure
            # is captured too: "tore down" is only claimed when the down completed.
            torn_down = _attempt_teardown(spawn_blocking, binary, port)
            outcome = "tore down the workspace." if torn_down else _TEARDOWN_UNCERTAIN
            # Diagnose from the bridge's OWN log (the supervisor log only records
            # the readiness timeout; the real cause — e.g. a discord.py crash — is
            # in <home>/state/logs/bridge.log). If a known signature matches, print
            # the cause-specific fix instead of guessing; otherwise keep the generic
            # message. Both messages point at the BRIDGE log — the file the
            # diagnosis reads and where the real traceback lands — never the
            # supervisor log, which only echoes the timeout.
            bridge_log = os.path.join(os.path.dirname(log_path), _BRIDGE_LOG_FILENAME)
            diagnosis = _diagnose_start_failure(os.path.dirname(log_path))
            if diagnosis is not None:
                print(
                    "error: bridge did not become ready within "
                    f"{ready_timeout_s:g}s; {outcome} "
                    f"{diagnosis} See {bridge_log} or run: disco doctor"
                )
            else:
                print(
                    "error: bridge did not become ready within "
                    f"{ready_timeout_s:g}s; {outcome} "
                    "Likely the broker could not be reached or Discord privileged "
                    f"intents are off. See {bridge_log} or run: disco doctor"
                )
            return 1

    if banner:
        if agents_defined:
            print(
                "workspace open (broker + bridge). No agents running yet "
                "-> disco agent start <name>"
            )
        else:
            print(
                "workspace open (broker + bridge). "
                "No agents defined yet -> disco agent create <name>"
            )
    return 0


async def _sweep_roster_pidfiles(home: str) -> tuple[int, int]:
    """Terminate every live roster process and clear stale pidfiles; return
    ``(stopped, still_running)`` — the honest split of what actually died.

    ``disco stop`` closes the WHOLE workspace, roster included — but the roster lives
    off Process Compose (Phase 2), so a ``down`` of the substrate leaves the detached
    agent/tools/mcp processes running. This sweeps ``state/run/*.pid``: a live-and-ours
    slot is terminated (SIGTERM→SIGKILL via :func:`_workspace.terminate_slot`, which
    also clears its pidfile), and a stale/torn/not-ours pidfile is swept without a
    signal. Run BEFORE the substrate ``down`` so agents can still publish their
    departure while the broker is up.

    The survivor count comes straight from the terminate ENUM — both keep-the-
    pidfile outcomes leave a still-live pid behind: ``KILL_UNCONFIRMED`` (the
    reap window closed on it) and ``INDETERMINATE`` (identity unreadable, left
    untouched; ``terminate_slot`` already printed its per-slot warning).
    ``TERMINATED`` / ``KILLED`` are confirmed deaths (``process_was_stopped`` —
    the one membership definition). No liveness re-probe: the enum already
    carries the verdict, and a second probe could only disagree with it racily.
    An unreadable ``state/run`` propagates :class:`_workspace.SlotScanError` —
    the caller must NOT read that as "nothing to sweep".
    """
    stopped = 0
    wedged = 0
    for slot, _pidfile in list(_workspace.iter_slot_pidfiles(home)):
        result = await _workspace.terminate_slot(home, slot)
        if result in (TerminateResult.KILL_UNCONFIRMED, TerminateResult.INDETERMINATE):
            wedged += 1
        elif result is not None and result.process_was_stopped:
            stopped += 1
    return stopped, wedged


async def restart_bridge(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_SECONDS,
) -> int:
    """``disco bridge restart`` — restart the bridge slot in place; honest exit code.

    Restarts ONLY the bridge process within the running supervisor (never the
    supervisor itself, so the §12.4 invariant holds), then gates on readiness.
    Refuses cleanly, one error line + exit ``1``, when:

    * the workspace isn't open (nothing to restart → points at ``disco start``);
    * a DIFFERENT ``$CALFCORD_HOME`` install answers this home's REST port (a Fix
      #11 collision — never restart the other install's bridge);
    * the bridge doesn't come back Ready within ``ready_timeout_s`` (§12.6 — no
      green light that lies);
    * a concurrent lifecycle verb holds the lock (a domain refusal, not a
      traceback).

    Returns ``0`` and prints ``bridge restarted.`` on success. ``client`` /
    ``clock`` / ``sleep`` are injected for testing; production defaults are the
    per-home REST client, ``time.monotonic``, and ``asyncio.sleep``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep

    try:
        with lifecycle_lock(home):
            if not await _supervisor_is_up(client):
                print("error: the workspace isn't open — run `disco start` first.")
                return 1
            # A colliding port could answer for ANOTHER install; never restart its
            # bridge from here (Fix #11). ``None`` (info route unavailable) keeps the
            # best-effort "it's ours" behaviour, mirroring ``start``'s idempotency gate.
            if await _supervisor_belongs_to_home(client, home) is False:
                print(
                    f"error: another Agent Disco install is using REST port "
                    f"{pc_port_for(home)} on this host (a $CALFCORD_HOME port collision). "
                    "Restart the bridge from that install's home."
                )
                return 1
            reason = await _restart_bridge_to_ready(
                client, home, ready_timeout_s=ready_timeout_s, clock=clock, sleep=sleep
            )
            if reason is not None:
                print(f"error: {reason}")
                return 1
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1
    print("bridge restarted.")
    return 0


async def stop(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
    spawn_blocking: Spawn | None = None,
) -> int:
    """Close the WHOLE workspace — substrate AND roster; idempotent (§12.4).

    Two teardowns under one lock: the roster is swept first
    (:func:`_sweep_roster_pidfiles` — terminate every live detached agent/tools/mcp
    process while the broker is still up so departures publish, and clear stale
    pidfiles), then the substrate is brought down. ``down`` is issued through the
    **blocking** seam so ``stop`` returns only after the supervisor has actually
    stopped — a fire-and-forget detached ``down`` would let a racing ``start`` collide
    with a supervisor still shutting down (§13.3).

    A no-op reports honestly: with the substrate down AND nothing swept, "nothing to
    stop"; otherwise "workspace closed", noting how many roster processes were
    stopped — and, separately, any survivor (a wedged ``KILL_UNCONFIRMED`` or an
    unverifiable ``INDETERMINATE`` slot) so "closed" never silently overclaims.
    "Closed" is also VERIFIED: a ``down`` that
    raises (e.g. its bounded timeout) or a supervisor still answering afterwards
    reports "teardown may not have completed" and returns ``1``. An unreadable
    ``state/run`` aborts before touching anything (the sweep saw nothing — "closed"
    would strand every process behind it). A contended lifecycle lock (a roster
    verb mid-spawn, or another start/stop) is a clean one-line refusal.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)
    spawn_blocking = spawn_blocking or _default_spawn_blocking

    try:
        with lifecycle_lock(home):
            pc_up = await _supervisor_is_up(client)
            # Sweep the roster first (broker still up → clean departures), then the
            # substrate. The sweep also runs when the substrate is already down, so an
            # orphaned roster (a crashed supervisor) is still cleaned up by `disco stop`.
            try:
                terminated, wedged = await _sweep_roster_pidfiles(home)
            except _workspace.SlotScanError as exc:
                # The sweep saw NOTHING — claiming "closed" here would strand every
                # process behind the unreadable dir. Leave the world untouched.
                print(f"error: {exc}")
                print(
                    "error: not stopping the workspace under unknown roster state — "
                    "fix the state/run permissions and re-run `disco stop`."
                )
                return 1
            teardown_uncertain = False
            if pc_up:
                binary = resolve_pc_binary()
                try:
                    spawn_blocking([binary, "down", "-p", str(pc_port_for(home))])
                except Exception as exc:  # incl. a wedged down's TimeoutExpired
                    print(f"error: workspace teardown failed ({exc}).")
                    teardown_uncertain = True
                # `down` returning is not proof: re-probe before claiming closed.
                if not teardown_uncertain and await _supervisor_is_up(client):
                    teardown_uncertain = True
    except _workspace.WorkspaceBusyError as exc:
        print(f"error: {exc}")
        return 1

    if teardown_uncertain:
        print(
            "error: teardown may not have completed — run `disco stop` again "
            "or check `disco status`."
        )
        return 1
    if not pc_up and terminated == 0 and wedged == 0:
        print("nothing to stop (workspace not running).")
        return 0
    if wedged:
        print(
            f"workspace closed ({terminated} roster process(es) stopped, "
            f"{wedged} still running — see `disco logs`)."
        )
    elif terminated:
        print(f"workspace closed ({terminated} roster process(es) stopped).")
    else:
        print("workspace closed.")
    return 0


async def status(
    home: str | os.PathLike[str],
    *,
    server_urls: str,
    client: ProcessComposeClient | None = None,
    probe: Callable[[str], Awaitable[list[str]]] | None = None,
) -> int:
    """Render a glanceable org board, or a "not running" hint (§12.6).

    The **substrate** rows come from Process Compose (broker + bridge). The
    **roster** rows are the Phase-2 reconciliation of two truths: this host's live
    pidfiles (``state/run/*.pid``) and the broker-wide mesh presence (the same probe
    ``agent_ps`` uses). For an AGENT slot the cross-product yields three states —

    * pidfile-alive **and** mesh-registered → ``running``;
    * pidfile-alive **only** → ``started, not registered (see disco logs)`` (up here
      but not answering — just starting, or wedged);
    * mesh-registered **only** (no local pidfile) → ``running (another host)``.

    A slot whose pidfile names a DEAD process (a crash or clean exit — no
    auto-respawn off PC) is rendered honestly as ``not running (exited — see
    <log>)`` rather than omitted; ``disco stop``'s sweep is the acknowledge-and-
    clear point that removes those files. The ``tools`` singleton and
    ``mcp-<server>`` slots carry no mesh presence, so their pidfile is the whole
    truth: live renders ``running``, dead renders the exited row. An unreadable
    mesh roster view degrades to the pidfile-only view with a note (read-only
    status must never crash).

    ``probe`` / ``client`` are injected for testing.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _supervisor_is_up(client):
        print("workspace not running (start it with: disco start)")
        # The detached roster outlives a dead supervisor — say so rather than
        # implying the host is idle.
        _workspace.note_local_survivors(home)
        return 0

    try:
        payload = await client.list_processes()
    except RuntimeError:
        # The supervisor died between the up-probe above and this read; read-only
        # status must degrade (the docstring's never-crash promise), and "no longer
        # answering" is exactly the not-running state.
        print("workspace not running (start it with: disco start)")
        _workspace.note_local_survivors(home)
        return 0
    substrate = [p for p in _process_rows(payload) if p["name"] in _SUBSTRATE]

    try:
        # ONE snapshot feeds every roster set below (live/dead/unverifiable are
        # projections of it): one scan, one identity read per slot — and a slot
        # dying mid-render can never appear in two disagreeing sets.
        verdicts = _workspace.classify_slots(home)
    except _workspace.SlotScanError as exc:
        # Unreadable state/run: the roster half of the board is UNKNOWN, not
        # empty — warn and render what can still be read (the substrate).
        print(f"warning: {exc}")
        verdicts = {}
    live = {s for s, v in verdicts.items() if v is procspawn.Identity.OURS}
    dead = {
        s
        for s, v in verdicts.items()
        if v not in (procspawn.Identity.OURS, procspawn.Identity.INDETERMINATE)
    }
    unverifiable = {
        s for s, v in verdicts.items() if v is procspawn.Identity.INDETERMINATE
    }
    agent_slots = {slot for slot in live if _workspace.is_agent_slot(slot)}
    other_slots = sorted(live - agent_slots)  # tools + mcp-<server>

    probe = _workspace.resolve_probe(probe)
    mesh_unreadable = False
    try:
        # Mesh names are broker-wide input: screened (with one aggregate
        # warning for any malformed name) before they can reach a printed row.
        logical = _workspace.screen_mesh_names(await probe(server_urls))
    except Exception:
        logical = set()
        mesh_unreadable = True

    print("workspace is open.")
    print("substrate:")
    for row in substrate:
        print(_format_row(row))
    print("roster:")
    _render_roster_board(
        home=home,
        agent_slots=agent_slots,
        other_slots=other_slots,
        dead_slots=dead,
        unverifiable_slots=unverifiable,
        logical=logical,
    )
    if mesh_unreadable:
        # Honest label: it is the mesh VIEW we could not read — the substrate
        # rows above may well show the broker itself Running.
        print("note: mesh roster view unreadable; roster shown from local pidfiles only.")
    # Reboot non-survival, stated honestly (§12.6): the daemon is session-scoped.
    print("note: the workspace does not survive a reboot; re-run `disco start`.")
    return 0


def _render_roster_board(
    *,
    home: str,
    agent_slots: set[str],
    other_slots: list[str],
    dead_slots: set[str],
    unverifiable_slots: set[str],
    logical: set[str],
) -> None:
    """Print the reconciled roster rows (see :func:`status`).

    Agents reconcile pidfile-presence against mesh-presence into the three §3.4
    states; ``tools`` / ``mcp-<server>`` slots have no mesh presence, so a live
    pidfile renders ``running``. Dead-but-pidfile-present slots render the honest
    ``not running (exited — see <log>)`` row instead of vanishing (crashes are the
    board's whole job to surface under no-auto-respawn); an UNVERIFIABLE slot (a
    live pid whose identity read failed) renders ``state unknown`` — neither the
    ``running`` claim nor the ``exited`` lie. An entirely empty roster prints the
    create/start signpost so the board is never a confusing blank.
    """
    dead_agents = {slot for slot in dead_slots if _workspace.is_agent_slot(slot)}
    dead_others = sorted(dead_slots - dead_agents)
    unverifiable_agents = {slot for slot in unverifiable_slots if _workspace.is_agent_slot(slot)}
    unverifiable_others = sorted(unverifiable_slots - unverifiable_agents)
    agent_names = sorted(agent_slots | logical | dead_agents | unverifiable_agents)
    if not agent_names and not other_slots and not dead_others and not unverifiable_others:
        print("  (none running -> disco agent start <name>, or disco agent create <name> to add one)")
        return

    unverifiable_state = "state unknown (cannot verify its recorded process)"
    for name in agent_names:
        here = name in agent_slots
        answering = name in logical
        log_path = procspawn.log_path_for(home, name)
        if here and answering:
            state = "running"
        elif here:
            state = "started, not registered (see disco logs)"
        elif name in unverifiable_agents:
            state = unverifiable_state
        elif answering and name in dead_agents:
            # Both truths: the org still answers for the name elsewhere, but the
            # local instance died and its pidfile is awaiting the stop-sweep.
            state = f"running (another host); exited here — see {log_path}"
        elif answering:
            state = "running (another host)"
        else:
            state = f"not running (exited — see {log_path})"
        print(f"  {name:<16} {state}")
    for slot in other_slots:
        print(f"  {slot:<16} running")
    for slot in unverifiable_others:
        print(f"  {slot:<16} {unverifiable_state}")
    for slot in dead_others:
        print(f"  {slot:<16} not running (exited — see {procspawn.log_path_for(home, slot)})")


def _process_rows(payload: object) -> list[dict]:
    """Normalize ``list_processes()`` into row dicts (name/status/is_ready).

    The wire-shape tolerance (bare list vs ``{"data": [...]}``, skip non-dicts) is
    the one shared :func:`_workspace.iter_process_dicts` (Fix #14); this only
    projects the board's three columns onto each entry.
    """
    return [
        {
            "name": item.get("name", "?"),
            "status": item.get("status", "?"),
            "is_ready": item.get("is_ready", "-"),
        }
        for item in iter_process_dicts(payload)
    ]


def _format_row(row: dict) -> str:
    return f"  {row['name']:<16} {row['status']:<10} ready={row['is_ready']}"


def _write_compose(home: str, yaml_text: str) -> str:
    path = os.path.join(home, "state", _COMPOSE_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(yaml_text, encoding="utf-8")
    return path


def _ensure_log_path(home: str) -> str:
    # Owner-only, matching the spawn path: `disco start` usually creates
    # state/logs first, and every detached slot's log lands in it too.
    logs_dir = procspawn.ensure_private_dir(os.path.join(home, "state", "logs"))
    return os.path.join(logs_dir, _SUPERVISOR_LOG_FILENAME)
