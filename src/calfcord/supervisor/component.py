"""Generic component lifecycle: a named SINGLETON roster process clocking in/out.

The tools host is a single roster slot — unlike agents (of which a host runs many,
and which can collide org-wide), a component is exactly one process per role per
host. Since Phase 2 the roster lives OFF Process Compose (PC cannot hot-add a
process), so a component is SPAWNED as a detached process (a pidfile under
``state/run/<name>.pid``, no auto-respawn) via the shared
:mod:`calfcord.supervisor._workspace` primitives, exactly like an agent — minus the
agent-only pieces:

* **No broker-wide duplicate guard.** A singleton cannot duplicate on one host —
  there is one slot — so there is no org-wide probe. Any *cross-host* policy is
  owned by the CLI dispatch that names the slot (:mod:`calfcord.cli.main`).
* **The workspace (substrate) must be up.** A component clocks into the running
  office; the workspace check (broker + bridge on Process Compose) gates every verb
  with the shared not-running hint, exactly like the agent roster.

This is the DRY base ``tools start|stop`` builds on (it is the only component role —
the router was removed in the calfkit 0.12 migration). Like the rest of
:mod:`calfcord.supervisor` it is import-light so it stays importable from the CLI
entry point.
"""

from __future__ import annotations

import os

from calfcord.health.check import BrokerProbe
from calfcord.supervisor import _slot_ops, _workspace
from calfcord.supervisor._workspace import (
    BROKER_UNREACHABLE_HINT,
    WORKSPACE_NOT_RUNNING_HINT,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient

# The one shared broker-gate refusal, aliased like the not-running hint so every
# lifecycle surface speaks with one voice.
_BROKER_UNREACHABLE_HINT = BROKER_UNREACHABLE_HINT

# The single hint shown when an op needs a running workspace and there isn't one;
# the one shared :data:`_workspace.WORKSPACE_NOT_RUNNING_HINT` (Fix #14), aliased
# for the call sites below so every lifecycle surface speaks the same one voice.
_NOT_RUNNING_HINT = WORKSPACE_NOT_RUNNING_HINT

# A per-home client resolver alias kept for the call sites + the test that pins the
# default wiring (``test_component._resolve_client``); the body is the one shared
# :func:`_workspace.resolve_client` (Fix #14 consolidation).
_resolve_client = resolve_client

# A workspace-readiness alias kept for the call sites below; the body is the one
# shared :func:`_workspace.workspace_is_up` (Fix #14 consolidation).
_workspace_is_up = workspace_is_up


def _component_argv(launcher: str, name: str) -> list[str]:
    """The argv that spawns component ``name`` — the SAME command the PC slot ran
    (``<launcher> run <name>``, e.g. ``... run tools``)."""
    return [launcher, "run", name]


async def component_start(
    home: str | os.PathLike[str],
    *,
    name: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Bring the singleton component ``name`` up (spawn its detached process).

    Returns a POSIX exit code. Workspace check first: if the supervisor REST is
    unreachable the SUBSTRATE is down, so print the not-running hint and return
    ``1`` before a doomed spawn. Then the broker gate — the same reachability probe
    ``lifecycle.start`` uses (the old PC slot's ``depends_on: broker healthy``,
    re-imposed off PC; ``broker_probe`` is the test seam, defaulting to the
    effective ``CALF_HOST_URL``).

    **Start of an already-running component is a restart (behavior #2).** A
    component's node bakes its config at construction, so re-running ``start`` on a
    slot that is already live locally re-applies an edited config (``tools set``
    etc.) by terminating and re-spawning it (print ``<name> restarted``). Otherwise
    spawn the component fresh. Both run under the slot-mutation locks (no
    double-spawn from concurrent starts; no interleave with a ``disco stop``
    sweep), and the spawn must survive its confirmation window — a crash-on-boot
    prints an honest failure naming the log path and returns ``1``; success says
    ``<name> started`` (presence is the callers' watchers' job).

    ``launcher`` defaults to the home-derived shim; ``client`` is injected for
    testing.
    """
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Legacy-workspace guard (fail-open): an old-main supervisor still runs the
    # roster (this singleton included) as PC slots — spawning beside it would
    # double the component.
    if await _workspace.legacy_pc_roster(client):
        print(_workspace.LEGACY_WORKSPACE_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    return await _slot_ops.start_slot(home, name, _component_argv(launcher, name), label=name)


async def component_stop(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take the singleton component ``name`` offline (terminate its process).

    Workspace check first (the not-running hint + return ``1`` if the office isn't
    open); otherwise terminate the process the pidfile names and clear it — under
    the same slot-mutation locks the spawn verbs take, so a stop racing a start's
    confirmation window never unlinks the fresh pidfile mid-confirm. A busy slot
    is reported honestly and skipped (benign). A live component is ``stopped``; a
    slot with no live process here reports ``is not running here``.

    ``client`` is injected for testing.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    return await _slot_ops.stop_slot(home, name, label=name)


async def component_restart(
    home: str | os.PathLike[str],
    *,
    name: str,
    launcher: str | None = None,
    client: ProcessComposeClient | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Reload the singleton component ``name`` after a config edit (terminate + re-spawn).

    The node bakes its config at construction, so a restart is how a ``tools set`` /
    ``tools edit`` takes effect on a running singleton. Workspace check first (the
    not-running hint + return ``1`` if the office isn't open), then the broker gate
    (killing a healthy singleton to spawn a doomed one during a broker bounce would
    be worse than refusing). The terminate + spawn runs under the slot-mutation
    locks and the spawn must survive its confirmation window — a crash-on-boot
    reports the log path and returns ``1``; otherwise ``<name> restarted``, return
    ``0`` (a stopped slot restarting back up is the correct, expected effect).

    ``launcher`` defaults to the home-derived shim; ``client`` is injected for
    testing.
    """
    home = os.fspath(home)
    launcher = launcher or _workspace.launcher_for(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Legacy-workspace guard (fail-open), like every spawn verb.
    if await _workspace.legacy_pc_roster(client):
        print(_workspace.LEGACY_WORKSPACE_HINT)
        return 1

    if not await _workspace.broker_gate(None, broker_probe):
        print(_BROKER_UNREACHABLE_HINT)
        return 1

    return await _slot_ops.restart_slot(home, name, _component_argv(launcher, name), label=name)
