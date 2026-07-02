"""Render the Process Compose project that supervises a calfcord host's SUBSTRATE.

The Process Compose YAML is *derived state* — calfcord generates it from config;
the user never edits it (design §3.1). This module is the pure heart of that
generation: home dir + launcher prefix in, a process-compose project ``dict``
out. No filesystem, no broker, no network — so the structure is fully
golden-testable and the same generator works whether the host runs detached, in
dev, or under a future supervisor swap.

**Phase 2 — the substrate is all that lives here now.** Process Compose cannot
hot-add a process to a live project (a ``POST /project`` bounces every PID —
proven on the pinned and latest builds), so the roster (agents, ``tools``, and
each ``mcp-<server>``) moved OFF Process Compose and is now a detached process
per slot, spawned via :mod:`procspawn` with a pidfile under ``state/run``. Only
the **substrate** — ``broker`` (when compose-managed) and ``bridge`` — is
declared here, autostarting under ``disco start``.

The shape is the §13.2 Phase-0 contract, pinned against Process Compose
``v1.110.0`` (config schema ``version: "0.5"``):

* Every ``command`` invokes the calfcord *launcher* (the shim) rather than a
  reconstructed ``uv run`` line, so the venv + ``--env-file`` + default env come
  from one place and no secret literal is ever inlined into the YAML (design
  §12.3). The launcher prefix is a parameter so this generator is mode-agnostic
  and unit-testable.
* ``bridge`` gates on the broker via ``depends_on`` (``process_healthy``);
  readiness is an ``exec`` probe (the bridge has no HTTP server) calling
  ``<launcher> _healthcheck <component>``.
* ``restart: always`` for the substrate (``broker``, ``bridge``): both exit 0 on
  a clean signal-less return, so ``on_failure`` would never fire to recover an
  uncommanded clean exit — they must ``always`` restart. Never ``exit_on_failure``.

The REST port is intentionally absent: it is a flag to ``process-compose up``
(``-p <PC_PORT>``), not a field in the project file (design §13.2).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import yaml

# Process Compose config-file schema version (NOT the binary version). v1.110.0
# reads the "0.5" schema; confirmed against the process-compose docs.
COMPOSE_SCHEMA_VERSION = "0.5"

# Readiness-probe cadence — the §13.2 pinned values. An exec probe (the bridge
# exposes no HTTP endpoint) shells out to the launcher's internal healthcheck.
_PROBE_INITIAL_DELAY_SECONDS = 2
_PROBE_PERIOD_SECONDS = 3
_PROBE_TIMEOUT_SECONDS = 5
_PROBE_SUCCESS_THRESHOLD = 1
_PROBE_FAILURE_THRESHOLD = 3

# Autorestart backoff for the substrate's `always` policy (the only processes
# declared here — the roster is detached, off PC, with NO auto-respawn);
# max_restarts 0 == unlimited retries in Process Compose.
_RESTART_BACKOFF_SECONDS = 2
_RESTART_MAX_RESTARTS = 0

# Per-host log rotation (project-level); §13.2.
_LOG_MAX_SIZE_MB = 10
_LOG_MAX_AGE_DAYS = 7
_LOG_MAX_BACKUPS = 5
_LOG_COMPRESS = True

# Graceful shutdown: SIGTERM with a 10s grace window — comfortably above the
# ~2s an agent needs to publish its AgentDepartureEvent — signalling the whole
# group (parent_only: false) so child workers under the shim also stop.
_SHUTDOWN_SIGNAL = 15
_SHUTDOWN_TIMEOUT_SECONDS = 10
_SHUTDOWN_PARENT_ONLY = False

_HEALTHY = "process_healthy"

# The filename stem of the supervisor's *own* log (``process-compose up -L
# <stem>.log``). It is not a process the generator declares, but it sits beside
# the per-process logs and is a legitimate tail target, so its name must agree
# across the modules that write it (``lifecycle._SUPERVISOR_LOG_FILENAME``) and
# read it (``cli.logs``). Both derive from this one stem so the literal can never
# drift: lifecycle reconstructs the filename as ``SUPERVISOR_LOG_STEM + ".log"``,
# and ``cli.logs`` passes the stem to ``_log_location`` (which appends ``.log``).
# Homed here because both consumers already import ``compose`` and ``compose`` is
# import-light, keeping the logs CLI's decoupling intact.
SUPERVISOR_LOG_STEM = "process-compose"

# Slot names owned by the substrate (``broker``/``bridge``, on Process Compose),
# the ``tools`` singleton (a roster slot spawned off PC), and the supervisor's
# own log stem (``process-compose`` — not a slot, but every roster slot logs to
# ``state/logs/<slot>.log``, and the supervisor's ``-L`` log is
# ``state/logs/process-compose.log``, so an agent by that name would share and
# rotate-at-spawn the live supervisor's log). An agent whose id equalled one of
# these would collide with that process/file, so agent names may never take
# them: the create-/parse-time guard lives in :data:`calfcord.agents.identifier
# .RESERVED_AGENT_IDS` (this module cannot import it — ``calfcord.agents``'s
# package init pulls calfkit, and the supervisor stays import-light — so the two
# literals are pinned equal by test instead: ``test_identifier``).
_RESERVED_PROCESS_NAMES = frozenset({"broker", "bridge", "tools", SUPERVISOR_LOG_STEM})

# The slot-name convention for MCP servers, homed here (like SUPERVISOR_LOG_STEM)
# because the roster surfaces spawn ``mcp-<server>`` slots and the shared
# ``_workspace`` scan classifies them — all must agree on the literal, and compose
# is the import-light module the supervisor package already leans on. The same
# literal is a reserved agent-name prefix (:data:`calfcord.agents.identifier
# .MCP_SLOT_PREFIX`); pinned equal by test for the same import-light reason.
MCP_SLOT_PREFIX = "mcp-"


def mcp_slot_name(server: str) -> str:
    """The roster slot name for MCP server ``server`` (``mcp-<server>``) — the
    stem of its detached process's pidfile (``state/run/mcp-<server>.pid``) and
    log (``state/logs/mcp-<server>.log``)."""
    return f"{MCP_SLOT_PREFIX}{server}"


# Hosts whose broker URL means "a local broker calfcord itself supervises": the
# native install runs Tansu as the compose-managed ``broker`` process bound to
# loopback. Anything else is an EXTERNAL broker the operator runs elsewhere, so
# calfcord must neither declare a local broker slot for it nor start one. The
# empty host (":9092" / bare "") is treated as loopback (no host == localhost).
_LOOPBACK_HOSTS = frozenset({"", "localhost", "::1"})


def _host_of(url: str) -> str:
    """Extract the host from a bare ``host[:port]`` broker URL (no scheme).

    Handles the bracketed IPv6 form (``[::1]:9092`` → ``::1``) and a bare IPv6
    literal (multiple colons, unbracketed, no port — kept whole); a single colon
    is the ordinary ``host:port`` separator.
    """
    url = url.strip()
    if url.startswith("["):
        end = url.find("]")
        if end != -1:
            return url[1:end]
    if url.count(":") == 1:
        return url.rsplit(":", 1)[0]
    return url


def _is_loopback_host(host: str) -> bool:
    """Whether ``host`` names this machine's loopback (localhost / 127.0.0.0/8 / ::1)."""
    host = host.strip().lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def broker_is_compose_managed(server_urls: str) -> bool:
    """Whether ``server_urls`` designates a local broker calfcord supervises.

    This is the single predicate that keeps two decisions from diverging (design
    §13.2): whether the rendered project declares a local ``broker`` process, and
    whether :func:`lifecycle.start` runs its pre-launch broker fast-fail probe.

    A **loopback** URL (``localhost`` / ``127.0.0.0/8`` / ``::1``, with or without
    a port) means calfcord runs Tansu as the compose-managed ``broker`` process:
    ``process-compose up`` launches it (with its own readiness probe + depends_on
    graph), so `start` must NOT pre-probe it — the broker is cold until `up` starts
    it. A non-loopback (external) URL means the operator's broker lives elsewhere,
    so no local slot is declared and the pre-launch fast-fail stands. A
    comma-separated bootstrap list is compose-managed only if EVERY host is
    loopback; an empty string is external (unknown → keep the fast-fail).
    """
    hosts = [h for h in (part.strip() for part in server_urls.split(",")) if h]
    if not hosts:
        return False
    return all(_is_loopback_host(_host_of(h)) for h in hosts)


def _log_location(home: str, name: str) -> str:
    return os.path.join(home, "state", "logs", f"{name}.log")


def _restart(policy: str) -> dict:
    """An ``availability`` block; the substrate always passes ``always`` (the
    only policy left here — no roster process is declared on PC anymore)."""
    return {
        "restart": policy,
        "backoff_seconds": _RESTART_BACKOFF_SECONDS,
        "max_restarts": _RESTART_MAX_RESTARTS,
    }


def _readiness_probe(launcher: str, component: str) -> dict:
    """An exec readiness probe driving ``depends_on: process_healthy``."""
    return {
        "exec": {"command": f"{launcher} _healthcheck {component}"},
        "initial_delay_seconds": _PROBE_INITIAL_DELAY_SECONDS,
        "period_seconds": _PROBE_PERIOD_SECONDS,
        "timeout_seconds": _PROBE_TIMEOUT_SECONDS,
        "success_threshold": _PROBE_SUCCESS_THRESHOLD,
        "failure_threshold": _PROBE_FAILURE_THRESHOLD,
    }


def _process(
    *,
    command: str,
    home: str,
    name: str,
    disabled: bool,
    restart_policy: str,
    depends_on: Mapping[str, str] | None = None,
    readiness_probe: dict | None = None,
) -> dict:
    proc: dict = {
        "command": command,
        "disabled": disabled,
        "availability": _restart(restart_policy),
        "shutdown": {
            "signal": _SHUTDOWN_SIGNAL,
            "timeout_seconds": _SHUTDOWN_TIMEOUT_SECONDS,
            "parent_only": _SHUTDOWN_PARENT_ONLY,
        },
        "log_location": _log_location(home, name),
    }
    if depends_on is not None:
        proc["depends_on"] = {dep: {"condition": condition} for dep, condition in depends_on.items()}
    if readiness_probe is not None:
        proc["readiness_probe"] = readiness_probe
    return proc


def build_compose_project(
    *,
    home: str,
    launcher: str,
    broker_managed: bool = True,
) -> dict:
    """Build the Process Compose project that supervises one calfcord host's SUBSTRATE.

    Only the substrate lives on Process Compose now (Phase 2): ``broker`` (when
    compose-managed) and ``bridge``. The roster — agents, ``tools``, and each
    ``mcp-<server>`` — is spawned off PC as detached processes (see :mod:`procspawn`
    and the ``roster`` / ``component`` / ``mcp_roster`` surfaces), so it is no
    longer declared here.

    ``home`` is ``$CALFCORD_HOME`` — only the per-process ``state/logs/<name>.log``
    paths use it. ``launcher`` is the shim prefix every ``command`` is built on
    (e.g. ``$CALFCORD_HOME/shims/disco``); the generator never reconstructs
    ``uv run`` flags or inlines secrets.

    ``broker_managed`` (default ``True``, the native local install) declares Tansu
    as the autostart ``broker`` process and health-gates the bridge on it. Set
    ``False`` for an EXTERNAL broker (the operator runs Kafka elsewhere): no local
    ``broker`` process is declared — starting an ephemeral broker nobody talks to is
    wrong — and the bridge carries no ``depends_on`` the broker (a dependency on an
    undeclared process would make process-compose reject the project). The caller
    derives this flag from :func:`broker_is_compose_managed` so manifest inclusion
    and `start`'s pre-launch probe cannot diverge (design §13.2).

    Returns a plain ``dict`` (serialize with :func:`render_compose`).
    """
    processes: dict[str, dict] = {}

    # The broker gate the bridge shares — present only when the broker is a local
    # compose process. For an external broker there is no local ``broker`` process
    # to health-gate on, so the dependency is dropped entirely (process-compose
    # rejects a depends_on to an undeclared process).
    broker_dep = {"broker": _HEALTHY} if broker_managed else None

    # Substrate — autostarts, health-gated. The broker (when compose-managed) has a
    # readiness probe checking metadata reachability (not bare TCP); the bridge's
    # checks the Discord heartbeat. `start` gates on the bridge.
    if broker_managed:
        processes["broker"] = _process(
            command=f"{launcher} broker",
            home=home,
            name="broker",
            disabled=False,
            restart_policy="always",
            readiness_probe=_readiness_probe(launcher, "broker"),
        )
    processes["bridge"] = _process(
        command=f"{launcher} run bridge",
        home=home,
        name="bridge",
        disabled=False,
        restart_policy="always",
        depends_on=broker_dep,
        readiness_probe=_readiness_probe(launcher, "bridge"),
    )

    return {
        "version": COMPOSE_SCHEMA_VERSION,
        "log_configuration": {
            "rotation": {
                "max_size_mb": _LOG_MAX_SIZE_MB,
                "max_age_days": _LOG_MAX_AGE_DAYS,
                "max_backups": _LOG_MAX_BACKUPS,
                "compress": _LOG_COMPRESS,
            }
        },
        "processes": processes,
    }


def render_compose(
    *,
    home: str,
    launcher: str,
    broker_managed: bool = True,
) -> str:
    """Render the Process Compose SUBSTRATE project as a YAML string.

    Thin serializer over :func:`build_compose_project` — ``sort_keys=False`` keeps
    the broker-before-bridge ordering the builder emits, which makes the generated
    file readable even though the user never edits it. ``broker_managed`` is
    threaded through unchanged (see :func:`build_compose_project`).
    """
    project = build_compose_project(
        home=home,
        launcher=launcher,
        broker_managed=broker_managed,
    )
    return yaml.safe_dump(project, sort_keys=False)
