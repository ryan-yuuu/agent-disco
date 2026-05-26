"""Filesystem search tools: ``grep`` and ``glob``.

Both wrap the corresponding executors from ``openhands-tools``. Each
executor is a module-global singleton constructed with
``working_dir=`` the calfcord shared workspace
(:func:`~calfkit_organization.tools.builtin.workspace.get_workspace_root`)
so the LLM can pass ``"src/foo.py"`` style relative paths and have them
resolve against the project root.

``grep`` shells out to ``ripgrep`` when available, falling back to system
``grep`` and finally to a pure-Python ``os.walk`` walker. ``glob`` uses
:func:`glob.iglob`. Both cap results at 100 files and return a
``truncated`` flag so the LLM can narrow its query.

Result format is a plain string ready to feed back to the LLM:

    Found <N> match(es) in <root>:
    /absolute/path/to/file1
    /absolute/path/to/file2
    ...
    (truncated to 100 results — narrow the pattern to see more)

When zero matches: a single line saying so. This is friendlier than an
empty string, which Discord webhooks reject anyway.
"""

from __future__ import annotations

import logging
from pathlib import Path

from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool
from openhands.tools.glob.definition import GlobAction
from openhands.tools.glob.impl import GlobExecutor
from openhands.tools.grep.definition import GrepAction
from openhands.tools.grep.impl import GrepExecutor

from calfkit_organization.tools.builtin._observation import flatten_observation_text
from calfkit_organization.tools.builtin.workspace import get_workspace_root

logger = logging.getLogger(__name__)

_grep_executor: GrepExecutor | None = None
_glob_executor: GlobExecutor | None = None


def _get_grep_executor() -> GrepExecutor:
    """Lazy module-global GrepExecutor; constructed on first use."""
    global _grep_executor
    if _grep_executor is None:
        _grep_executor = GrepExecutor(working_dir=str(get_workspace_root()))
    return _grep_executor


def _get_glob_executor() -> GlobExecutor:
    """Lazy module-global GlobExecutor; constructed on first use."""
    global _glob_executor
    if _glob_executor is None:
        _glob_executor = GlobExecutor(working_dir=str(get_workspace_root()))
    return _glob_executor


def _resolve_search_path(path: str | None) -> str:
    """Map a user-supplied search path to an absolute path.

    ``None`` falls back to the workspace root. Relative paths resolve
    against the workspace root for consistency with the fs tools.
    Absolute paths pass through.
    """
    if path is None:
        return str(get_workspace_root())
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((get_workspace_root() / p).resolve())


def _format_files(
    label: str, files: list[str], search_path: str, truncated: bool
) -> str:
    """Render a file list as the user-visible tool output.

    Each entry in ``files`` is a path to a file that matched — for grep
    that means "contains the pattern", for glob "matches the name". The
    label ("grep" / "glob") is used in the leading sentence; we phrase
    it as "N file(s)" rather than "N matches" so the LLM doesn't
    mistake a count of files for a count of in-file occurrences.
    """
    if not files:
        return f"No files matched {label} in {search_path}."
    body = "\n".join(files)
    trailer = (
        "\n(truncated to 100 results — narrow the pattern to see more)"
        if truncated
        else ""
    )
    return (
        f"Found {len(files)} file(s) matched by {label} in {search_path}:\n"
        f"{body}{trailer}"
    )


async def grep(
    ctx: ToolContext,
    pattern: str,
    path: str | None = None,
    include: str | None = None,
) -> str:
    """Search file contents with a regular expression.

    Returns paths of files whose contents match ``pattern``. Use this
    when you know what string or symbol to look for but don't know
    which files contain it. For finding files by name (not content),
    use ``glob``.

    Args:
        pattern: Regular expression to search for. Full regex syntax
            (e.g. ``"def \\w+"``, ``"TODO\\s*:.*"``).
        path: Directory to search in. Absolute, or relative to the
            calfcord workspace root. Defaults to the workspace root.
        include: Optional file pattern filter (e.g. ``"*.py"``,
            ``"*.{ts,tsx}"``). When set, only matching filenames are
            searched.

    Returns:
        A formatted list of matching file paths, sorted by modification
        time (newest first). Capped at 100 results — the output flags
        truncation so you can narrow the pattern.
    """
    _ = ctx
    search_path = _resolve_search_path(path)
    action = GrepAction(pattern=pattern, path=search_path, include=include)
    obs = _get_grep_executor()(action)
    # Without ``is_error`` propagation, a permission-denied / bad-path
    # observation comes back with ``matches=[]`` indistinguishable from
    # a genuine no-hit — a silent false negative for the LLM. Surface
    # the upstream error message so the caller can adapt (try a
    # different path, narrower include, etc.).
    if obs.is_error:
        return flatten_observation_text(obs)
    return _format_files("grep", obs.matches, search_path, obs.truncated)


async def glob(ctx: ToolContext, pattern: str, path: str | None = None) -> str:
    """Find files by name pattern.

    Returns paths matching a glob expression. Use this when you know
    the filename or extension you want but don't know what's inside the
    files. For searching file *contents*, use ``grep``.

    Args:
        pattern: Glob pattern (e.g. ``"**/*.py"``, ``"src/**/*.ts"``,
            ``"**/test_*.py"``).
        path: Directory to search in. Absolute, or relative to the
            calfcord workspace root. Defaults to the workspace root.

    Returns:
        A formatted list of matching file paths, sorted by
        modification time (newest first). Capped at 100 results.
    """
    _ = ctx
    search_path = _resolve_search_path(path)
    action = GlobAction(pattern=pattern, path=search_path)
    obs = _get_glob_executor()(action)
    if obs.is_error:
        return flatten_observation_text(obs)
    return _format_files("glob", obs.files, search_path, obs.truncated)


grep_tool: ToolNodeDef = agent_tool(grep)
glob_tool: ToolNodeDef = agent_tool(glob)
