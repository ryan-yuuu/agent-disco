"""Shell command tool.

Wraps :class:`openhands.tools.terminal.TerminalExecutor` with a single
calfkit ``@agent_tool``. One module-global executor is created on first
use with ``working_dir`` set to the calfcord shared workspace
(:func:`~calfkit_organization.tools.builtin.workspace.get_workspace_root`).

Backend selection:
    * If ``tmux`` is installed on the host, the upstream factory selects
      a tmux-backed persistent session — ``cd``/env-var changes survive
      across calls. The docker image installs ``tmux`` so this is the
      default path for the shipped deploy.
    * Otherwise (e.g. development on macOS without ``brew install tmux``),
      the upstream factory falls back to the ``subprocess`` backend.
      Each call is independent — no state persists.
    * Operators can force a backend via ``CALFCORD_SHELL_BACKEND`` =
      ``tmux`` | ``subprocess`` | ``powershell``.

The wrapper deliberately does NOT expose upstream's ``is_input`` (inject
a keystroke into a running process) or ``reset`` (rebuild the session)
flags in v1 — both are footguns for an LLM, and Claude Code's ``Bash``
tool doesn't have them either. They can be added later via env-driven
opt-in if a real use case emerges.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool
from openhands.tools.terminal.definition import TerminalAction
from openhands.tools.terminal.impl import TerminalExecutor

from calfkit_organization.tools.builtin._observation import flatten_observation_text
from calfkit_organization.tools.builtin.workspace import get_workspace_root

logger = logging.getLogger(__name__)

_BACKEND_ENV = "CALFCORD_SHELL_BACKEND"
_executor: TerminalExecutor | None = None


def _resolve_backend() -> Literal["tmux", "subprocess", "powershell"] | None:
    """Read ``CALFCORD_SHELL_BACKEND`` or return ``None`` (auto-detect).

    Invalid values fall back to auto-detect with a warning rather than
    raising — the LLM caller can't fix a misconfigured operator env, and
    auto-detect always produces a working terminal.
    """
    raw = os.getenv(_BACKEND_ENV)
    if raw is None:
        return None
    raw = raw.strip().lower()
    if raw in ("tmux", "subprocess", "powershell"):
        return raw  # type: ignore[return-value]
    if raw:
        logger.warning(
            "ignoring invalid %s=%r; valid values: tmux|subprocess|powershell",
            _BACKEND_ENV, raw,
        )
    return None


def _get_executor() -> TerminalExecutor:
    """Return the module-global executor, constructing it on first use.

    Lazy so importing this module doesn't try to find tmux at boot —
    in test environments and CI the executor never gets called and we
    don't want startup noise about missing shell binaries.
    """
    global _executor
    if _executor is None:
        _executor = TerminalExecutor(
            working_dir=str(get_workspace_root()),
            terminal_type=_resolve_backend(),
        )
    return _executor


async def shell(
    ctx: ToolContext,
    command: str,
    timeout: float | None = None,
) -> str:
    """Run a shell command on the host where ``calfkit-tools`` is running.

    Use this to inspect the system, run build/test commands, manipulate
    files outside what ``write_file`` and ``edit_file`` can do, or
    invoke any other CLI tool installed on the host. The shell starts
    in the calfcord workspace directory.

    When backed by ``tmux`` (the default in the calfcord docker image),
    the session is persistent — ``cd subdir`` followed by ``pwd`` works
    as expected. Without ``tmux`` each call is independent; ``cd``
    doesn't carry across calls.

    Args:
        command: The command to run. Pipes, redirects, and ``&&`` /
            ``||`` chaining work as in any interactive shell.
        timeout: Hard timeout in seconds. When the command exceeds it
            the tool returns whatever output was produced so far plus a
            ``"command timed out"`` notice. Defaults to upstream's
            no-output-progress timeout (~30s of silence).

    Returns:
        The command's stdout and stderr (interleaved) followed by an
        ``exit code: N`` summary. On infrastructure failure (binary
        missing, terminal session crashed), an ``"error: ..."`` message.
    """
    _ = ctx
    action = TerminalAction(command=command, timeout=timeout, is_input=False, reset=False)
    # Catch broad: both ``_get_executor()`` (lazy first-call init may
    # ``LibTmuxException`` if ``CALFCORD_SHELL_BACKEND=tmux`` is set on
    # a host without ``tmux`` installed) and ``__call__`` (mid-call
    # session crash) can raise. Both are LLM-recoverable per the
    # docstring contract — surface as a string so the calling LLM can
    # adapt rather than triggering the calfkit infra-bug RuntimeError
    # path. ``KeyboardInterrupt``/``SystemExit`` propagate via the
    # ``BaseException`` shortcut.
    try:
        obs = _get_executor()(action)
    except Exception as e:
        logger.warning("shell tool failed command=%r: %s", command, e)
        return f"error: shell execution failed: {e}"
    body = flatten_observation_text(obs)
    # ``flatten_observation_text`` already prefixes "error: " when
    # ``obs.is_error`` is set, so callers see a single discriminator
    # regardless of which layer detected the failure.
    return f"{body}\n\nexit code: {obs.exit_code}"


shell_tool: ToolNodeDef = agent_tool(shell)
