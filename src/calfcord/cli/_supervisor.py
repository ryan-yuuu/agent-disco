"""Shared CLI probe for the install-scoped supervisor (Process Compose) binary.

Both the ``init`` and ``agent create`` live-finishes must know whether the
process-compose binary the supervisor needs is resolvable — and, when it isn't, the
*actionable reason*. Kept in one place, imported by both flows, so the two can't drift
on the subtle "``resolve_pc_binary`` signals 'missing' by raising ``RuntimeError``"
contract, and so neither flow has to reach into the other (``init`` already imports
``agent_create`` at top level).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os
    from collections.abc import Awaitable, Callable

# The singleton tools-host roster slot (pidfile ``state/run/tools.pid``); the same
# slot name ``disco tools start`` targets. One constant so the cold-open launch paths
# (`disco init`, `disco start`, `disco agent create`'s start-now) can't drift from each
# other on which slot they bring up.
_TOOLS_SLOT = "tools"


def supervisor_unavailable_reason(pc_binary_fn: Callable[[], str]) -> str | None:
    """``None`` if the supervisor binary resolves; else the actionable reason it doesn't.

    ``resolve_pc_binary`` raises an actionable :class:`RuntimeError` naming the fix
    (re-run the installer / set ``$CALFCORD_PROCESS_COMPOSE_BIN``). Surface that text as
    a *value* rather than collapse it to a bool, so a caller's manual degrade can say
    WHY. The catch stays narrow on purpose: a missing binary is a documented domain
    signal, but an ``OSError`` (e.g. from an injected or future resolver that does real
    filesystem I/O) is a real fault that must propagate, not be laundered into a benign
    "unavailable" degrade.
    """
    try:
        pc_binary_fn()
    except RuntimeError as exc:
        return str(exc)
    return None


def default_pc_binary() -> str:
    """Resolve the process-compose binary via the supervisor's own resolver.

    Imported lazily so importing this module never pulls the supervisor package (the
    import-light invariant); the dev-mode path degrades before this is ever called.
    """
    from calfcord.supervisor.lifecycle import resolve_pc_binary

    return resolve_pc_binary()


async def start_tools_host(
    home: str | os.PathLike[str],
    *,
    launcher: str,
    tools_start_fn: Callable[..., Awaitable[int]] | None = None,
) -> int:
    """Bring the singleton tools host (all builtin tools) online; **warn-and-continue**.

    The tools host is identity-agnostic infrastructure EVERY tool-using agent depends
    on — it serves every builtin ``ToolNodeDef`` regardless of which tools any one
    agent selected — so every cold-open path (``disco init`` inline, and ``disco start`` /
    ``disco agent create`` via :func:`open_workspace`) brings it up as part of opening the
    workspace, never gated on an agent's tool list. Shared here so those paths can't drift
    on either the ``name="tools"`` slot or the failure wording.

    The outcome is **advisory**: a tools-host *spawn* fault never escapes here — it is
    degraded, not raised (the lazy ``component`` import below sits OUTSIDE the guard on
    purpose, so an ``ImportError`` — a real install defect — still surfaces rather than
    masquerading as a missing tools host). ``component_start`` signals its
    *expected* failures with a non-zero RETURN (workspace down, broker unreachable,
    spawn crash) — already printing the specific cause — but a raw ``OSError`` (a
    lockfile/rundir ``PermissionError``, ``ENOSPC`` while spawning) can still escape it.
    Both are degraded here to the same warning + non-zero code, so a tools-host fault
    never crashes the caller: without this guard a raise would skip the agent in
    ``disco init`` and fail an otherwise-open workspace in ``disco start`` — the exact
    guarantee this helper exists to provide. On a raise the cause is NAMED, not swallowed
    (the same advisory-degrade idiom as :func:`init._await_presence`). ``tools_start_fn``
    is the test seam, defaulting to the same
    :func:`~calfcord.supervisor.component.component_start` that ``disco tools start`` runs.
    """
    if tools_start_fn is None:
        from calfcord.supervisor import component

        tools_start_fn = component.component_start
    try:
        rc = await tools_start_fn(home, name=_TOOLS_SLOT, launcher=launcher)
    except Exception as exc:
        # Advisory: degrade ANY spawn fault, never crash the caller. ``except Exception``
        # (not bare) lets ``CancelledError`` / ``KeyboardInterrupt`` (both ``BaseException``)
        # propagate — only a real fault is degraded to a warning + non-zero code.
        print(f"  the tools host couldn't be started ({exc!r}).")
        rc = 1
    if rc != 0:
        print("  the tools host isn't up — agents can chat, but tool calls will hang until it is up.")
        print("  Bring it up with `disco tools start` (see `disco logs tools` for why it failed).")
    return rc


async def open_workspace(
    home: str | os.PathLike[str],
    *,
    server_urls: str,
    launcher: str,
    start_fn: Callable[..., Awaitable[int]] | None = None,
    tools_start_fn: Callable[..., Awaitable[int]] | None = None,
) -> int:
    """Open the workspace: the substrate (broker + bridge), then the tools host.

    The ONE definition of "open the workspace", shared by the cold-open paths that must
    all bring up the same infrastructure — ``disco start`` and ``disco agent create``'s
    start-now — so a first tool call can never land before the host that serves it is up.
    (``disco init`` runs the same substrate→tools-host sequence inline, keeping its own
    step-by-step onboarding narration + the tools-host outcome it needs for the finish
    banner.)

    A substrate failure short-circuits and returns its non-zero code before the tools
    host is spawned (never spawn a host against a workspace that never opened). The
    tools-host start is advisory (:func:`start_tools_host`, warn-and-continue), so its
    outcome never changes the returned substrate code — a workspace whose substrate
    opened is open. ``start_fn`` defaults (lazily) to :func:`lifecycle.start`;
    ``start_fn``/``tools_start_fn`` are the test seams.
    """
    if start_fn is None:
        from calfcord.supervisor import lifecycle

        start_fn = lifecycle.start
    rc = await start_fn(home, server_urls=server_urls, launcher=launcher)
    if rc != 0:
        return rc
    await start_tools_host(home, launcher=launcher, tools_start_fn=tools_start_fn)
    return rc
