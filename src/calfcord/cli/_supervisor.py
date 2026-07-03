"""Shared CLI probe for the install-scoped supervisor (Process Compose) binary.

Both the ``init`` and ``agent create`` live-finishes must know whether the
process-compose binary the supervisor needs is resolvable — and, when it isn't, the
*actionable reason* — so they can degrade to manual next-steps that NAME the fix
rather than silently swallow it (§12.6). Kept in one place, imported by both flows,
so the two can't drift on the subtle "``resolve_pc_binary`` signals 'missing' by
raising ``RuntimeError``" contract, and so neither flow has to reach into the other
(``init`` already imports ``agent_create`` at top level).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def supervisor_unavailable_reason(pc_binary_fn: Callable[[], str]) -> str | None:
    """``None`` if the supervisor binary resolves; else the actionable reason it doesn't.

    ``resolve_pc_binary`` raises an actionable :class:`RuntimeError` naming the fix
    (re-run the installer / set ``$CALFCORD_PROCESS_COMPOSE_BIN``). Surface that text as
    a *value* rather than collapse it to a bool, so a caller's manual degrade can say
    WHY. The catch stays narrow on purpose: a missing binary is a documented domain
    signal, but an ``OSError`` (e.g. a permissions fault on the bin dir) is a real fault
    that must propagate, not be laundered into a benign "unavailable" degrade.
    """
    try:
        pc_binary_fn()
    except RuntimeError as exc:
        return str(exc)
    return None


def default_pc_binary() -> str:
    """Resolve the process-compose binary via the supervisor's own resolver.

    Imported lazily so importing this module never pulls the supervisor package — the
    import-light invariant the dev-mode path (which degrades before this is called)
    relies on.
    """
    from calfcord.supervisor.lifecycle import resolve_pc_binary

    return resolve_pc_binary()
