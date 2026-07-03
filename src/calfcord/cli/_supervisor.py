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
# slot name ``disco tools start`` manages. One constant so the init/start launch
# paths can't drift from the CLI on which slot they bring up.
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
    agent selected — so both ``disco init`` and ``disco start`` bring it up as part of
    opening the workspace, never gated on an agent's tool list. Shared here so the two
    launch paths can't drift on either the ``name="tools"`` slot or the failure wording.

    The return code is **advisory**: a non-zero is returned, not raised. A substrate
    that is already up is a usable workspace even if the tools host lags, so callers
    keep going; ``component_start`` has already printed the actionable failure + log
    path, and the consequence line below tells the operator what a missing tools host
    means and how to fix it. ``tools_start_fn`` is the test seam, defaulting to the same
    :func:`~calfcord.supervisor.component.component_start` that ``disco tools start`` runs.
    """
    if tools_start_fn is None:
        from calfcord.supervisor import component

        tools_start_fn = component.component_start
    rc = await tools_start_fn(home, name=_TOOLS_SLOT, launcher=launcher)
    if rc != 0:
        print("  the tools host didn't start — agents can chat, but tool calls will hang until it is up.")
        print("  Bring it up with `disco tools start` (see `disco logs tools` for why it failed).")
    return rc
