"""Per-agent task tracker: ``todo_view`` and ``todo_write``.

Wraps :class:`openhands.tools.task_tracker.TaskTrackerExecutor` with one
executor per ``caller_agent_id``. Lists are kept in memory and wiped on
``calfkit-tools`` restart — Claude Code's TodoWrite has the same
"conversation-scoped" lifetime, so this matches LLM expectations.

The upstream tool models a task as ``{title: str, notes: str, status:
"todo"|"in_progress"|"done"}``. We accept the same shape from the LLM
via a list of dicts and validate via :class:`TaskItem`.

Two operations:

* ``todo_view`` — return the agent's current list. Use this before
  writing to see what's already there.
* ``todo_write`` — replace the agent's list. Pass the complete new
  list; partial updates aren't supported (this is intentional — the
  LLM should think of the task list as a snapshot it owns).
"""

from __future__ import annotations

import logging
import threading

from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool
from openhands.tools.task_tracker.definition import (
    TaskItem,
    TaskTrackerAction,
    TaskTrackerExecutor,
)
from pydantic import ValidationError

from calfcord.tools.builtin._observation import flatten_observation_text

logger = logging.getLogger(__name__)

# One executor per agent_id. The lock guards the dict; each executor is
# itself accessed only by serial calls from one agent at a time (calfkit
# dispatches one tool call per agent at a time on the tools worker), so
# the executor's internal mutation does not need its own lock.
_executors: dict[str, TaskTrackerExecutor] = {}
_executors_lock = threading.Lock()


def _executor_for(agent_id: str) -> TaskTrackerExecutor:
    """Return the per-agent executor, constructing it on first use.

    The dict-and-lock pattern is the standard idempotent-init shape.
    Holding the lock across construction is fine because executor init
    is in-memory only (we don't pass ``save_dir``, so no disk I/O).
    """
    with _executors_lock:
        ex = _executors.get(agent_id)
        if ex is None:
            ex = TaskTrackerExecutor(save_dir=None)
            _executors[agent_id] = ex
        return ex


def _require_agent_id(ctx: ToolContext) -> str:
    """Return the calling agent's id, or raise ``RuntimeError`` if it's missing.

    ``ctx.agent_name`` is populated by calfkit from the ``x-calf-emitter``
    Kafka header. A missing value means calfkit's dispatch was bypassed
    — an infrastructure bug. The project convention (see
    :mod:`calfcord.tools.builtin.private_chat`) is to raise
    ``RuntimeError`` on infra bugs so operators see the failure rather
    than silently sharing one task list across "different" callers.
    """
    if not ctx.agent_name:
        logger.error(
            "todos tool invoked without agent_name; calfkit dispatch was bypassed"
        )
        raise RuntimeError(
            "todos tool invoked without agent_name; the calfkit-tools runner "
            "must populate ctx.agent_name from the x-calf-emitter header"
        )
    return ctx.agent_name


async def todo_view(ctx: ToolContext) -> str:
    """Show the current task list for the calling agent.

    Use this before writing — the LLM should see what's already there so
    its update is informed rather than starting from scratch. Each agent
    has its own list; you cannot see other agents' lists.

    Returns:
        A markdown rendering of the task list, with status icons. When
        empty, a one-line note suggesting ``todo_write``.
    """
    agent_id = _require_agent_id(ctx)
    obs = _executor_for(agent_id)(TaskTrackerAction(command="view"))
    return flatten_observation_text(obs)


async def todo_write(ctx: ToolContext, todos: list[dict]) -> str:
    """Replace the calling agent's task list with ``todos``.

    Pass the complete new list — items not present in ``todos`` are
    removed. To update one item, view the list first, modify the
    relevant entry, and write the full list back.

    Args:
        todos: A list of task dicts. Each dict has:

            * ``title`` (str, required) — a short summary.
            * ``notes`` (str, optional) — additional context.
            * ``status`` (str, optional) — one of ``"todo"``,
              ``"in_progress"``, ``"done"``. Defaults to ``"todo"``.

    Returns:
        A confirmation message including the new task count, or an
        ``"error: ..."`` message if any item failed validation (bad
        status, missing title, etc.).
    """
    agent_id = _require_agent_id(ctx)
    try:
        task_list = [TaskItem.model_validate(t) for t in todos]
    except ValidationError as e:
        return f"error: invalid todo item(s): {e}"
    obs = _executor_for(agent_id)(
        TaskTrackerAction(command="plan", task_list=task_list)
    )
    return flatten_observation_text(obs)


def _reset_for_tests() -> None:
    """Clear the per-agent executor registry. Test-only."""
    with _executors_lock:
        _executors.clear()


todo_view_tool: ToolNodeDef = agent_tool(todo_view)
todo_write_tool: ToolNodeDef = agent_tool(todo_write)
