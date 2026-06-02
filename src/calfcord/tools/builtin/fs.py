"""Filesystem tools: ``read_file``, ``write_file``, ``edit_file``.

Thin :func:`agent_tool`-decorated wrappers around :class:`FileEditorExecutor`
from ``openhands-tools``. The executor is a module-global singleton whose
``workspace_root`` is the calfcord shared workspace
(:func:`~calfcord.tools.builtin.workspace.get_workspace_root`).
Relative paths in ``path`` arguments are resolved against that root before
being passed to upstream so the LLM can use ``"main.py"`` style addressing
without knowing the container/host filesystem layout.

The three tools mirror Claude Code's surface 1:1:

* ``read_file`` → ``FileEditorAction(command="view", ...)`` — supports
  ``view_range=[start, end]`` for partial views (1-indexed; ``-1`` for "to end").
* ``write_file`` — creates or overwrites a file with the provided content.
  Uses :meth:`pathlib.Path.write_text` directly because the upstream
  ``create`` command refuses to overwrite (by design — they expect
  ``str_replace`` for in-place edits). Parent directories are created
  if missing, matching Claude Code's ``Write`` semantics.
* ``edit_file`` → ``FileEditorAction(command="str_replace", ...)`` —
  exact-string replacement. The upstream tool requires ``old_string`` to
  occur exactly once (otherwise it errors with "Multiple occurrences…").
  We add a ``replace_all`` knob: when true, our wrapper drives upstream in
  a loop replacing one occurrence at a time until ``old_string`` no longer
  appears. This matches Claude Code's ``Edit`` semantics without patching
  upstream.

The upstream tool's ``insert`` and ``undo_edit`` commands are intentionally
not exposed in v1 — they widen the LLM's tool surface beyond Claude Code's
and aren't worth the extra description tokens for our use case.
"""

from __future__ import annotations

import logging
from pathlib import Path

from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool
from openhands.tools.file_editor.definition import FileEditorAction
from openhands.tools.file_editor.impl import FileEditorExecutor

from calfcord.tools.builtin._observation import flatten_observation_text
from calfcord.tools.builtin.workspace import get_workspace_root

logger = logging.getLogger(__name__)

_executor: FileEditorExecutor | None = None


def _get_executor() -> FileEditorExecutor:
    """Return the module-global executor, constructing it on first use.

    Lazy so importing this module doesn't read ``CALFCORD_WORKSPACE_DIR``
    or create the workspace directory — that work happens on the first
    actual tool call. Tests reset the singleton by patching ``_executor``
    directly.
    """
    global _executor
    if _executor is None:
        root = get_workspace_root()
        _executor = FileEditorExecutor(workspace_root=str(root))
    return _executor


def _resolve_path(path: str) -> str:
    """Resolve a user-supplied ``path`` against the workspace root.

    Absolute paths pass through unchanged (the trusted-workspace model
    means the LLM is allowed to address absolute paths on the host).
    Relative paths are joined to the workspace root so ``read_file("foo.py")``
    works without the LLM knowing the host's absolute layout.
    """
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((get_workspace_root() / p).resolve())


async def read_file(
    ctx: ToolContext,
    path: str,
    view_range: list[int] | None = None,
) -> str:
    """Read a file's contents (or list a directory's children).

    Use this to inspect a file before editing it, to spot-check a config,
    or to enumerate what's in a directory. The output includes ``cat -n``
    style line numbers, which you can reference when writing follow-up
    ``edit_file`` calls.

    Args:
        path: Absolute path on the host, or a path relative to the
            calfcord workspace root. Both files and directories are
            supported (directories return a recursive listing up to 2
            levels deep).
        view_range: Optional ``[start, end]`` line numbers (1-indexed) to
            return only part of the file. ``end=-1`` means "to end of
            file". Ignored for directory views.

    Returns:
        The file contents (or directory listing) as a string. On error
        (missing file, no read permission, binary file too large), an
        ``"error: ..."`` style message describing what went wrong.
    """
    _ = ctx  # unused
    resolved = _resolve_path(path)
    action = FileEditorAction(command="view", path=resolved, view_range=view_range)
    obs = _get_executor()(action)
    return flatten_observation_text(obs)


async def write_file(ctx: ToolContext, path: str, content: str) -> str:
    """Create a new file or overwrite an existing one with ``content``.

    Use this for new files, or when you want to rewrite a file end-to-end.
    For targeted single-region edits, prefer ``edit_file``.

    Args:
        path: Absolute path on the host, or a path relative to the
            calfcord workspace root. Missing parent directories are
            created.
        content: The complete new file content. Existing content (if
            any) is replaced.

    Returns:
        A short confirmation message including the path, or an
        ``"error: ..."`` message on failure.
    """
    _ = ctx
    resolved = _resolve_path(path)
    # Use ``Path.write_text`` directly rather than the upstream
    # ``FileEditorAction(command="create")``: the upstream command
    # explicitly refuses to overwrite existing files (it's positioned as
    # "create new" only). Claude Code's ``Write`` overwrites, so we
    # match that semantic here. Trade-off: we skip openhands' file-
    # history tracking — acceptable because git is the system of record
    # for change history in the calfcord workspace anyway.
    p = Path(resolved)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Pin UTF-8 explicitly. ``Path.write_text`` would otherwise pick
        # ``locale.getpreferredencoding(False)`` which is non-UTF-8 on
        # some Windows hosts and inside ``LANG=C`` containers — silent
        # corruption of non-ASCII content. The shipped docker image
        # sets ``C.UTF-8`` so this matters mostly for native dev hosts.
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"error: cannot write {resolved}: {e}"
    return f"Wrote {len(content)} characters to {resolved}"


async def edit_file(
    ctx: ToolContext,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace exact text in a file.

    By default, ``old_string`` must occur exactly once in the file — if
    it appears multiple times the tool errors so you can supply more
    surrounding context to disambiguate. Set ``replace_all=True`` to
    replace every occurrence in one call.

    Args:
        path: Absolute path on the host, or a path relative to the
            calfcord workspace root.
        old_string: The exact substring to find. Whitespace and indentation
            must match — copy from a recent ``read_file`` if unsure.
        new_string: The replacement text. May be empty (deletion).
        replace_all: When ``True``, replace every occurrence of
            ``old_string``. When ``False`` (default), require exactly one
            occurrence and error otherwise.

    Returns:
        A short confirmation message with a diff snippet, or an
        ``"error: ..."`` message on failure (file missing, no match,
        ambiguous match when ``replace_all=False``).
    """
    _ = ctx
    resolved = _resolve_path(path)
    if not replace_all:
        action = FileEditorAction(
            command="str_replace", path=resolved, old_str=old_string, new_str=new_string
        )
        obs = _get_executor()(action)
        return flatten_observation_text(obs)

    # replace_all path: openhands' ``str_replace`` rejects multi-match
    # inputs as ambiguous (by design — that's the safety net for the
    # single-shot edit case). To honor ``replace_all=True`` we bypass it
    # and do the substitution in pure Python.
    #
    # Trade-off: skipping the upstream executor means no file-history
    # tracking and no charset auto-detection (we pin UTF-8 below). Git
    # is the system of record for change history in the calfcord
    # workspace, so the missing history is acceptable. The
    # ``exists()``/``is_dir()`` checks are TOCTOU race-y vs. the write,
    # which is also acceptable under the v1 trusted-workspace model
    # (no concurrent operator-on-host editing during agent runs).
    p = Path(resolved)
    if not p.exists():
        return f"error: file not found at {resolved}"
    if p.is_dir():
        return f"error: edit_file does not support directories: {resolved}"
    try:
        original = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"error: cannot read {resolved}: {e}"
    if old_string not in original:
        return (
            f"error: old_string did not appear verbatim in {resolved}; "
            "check whitespace/indentation and re-read the file if needed"
        )
    count = original.count(old_string)
    new_content = original.replace(old_string, new_string)
    try:
        p.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return f"error: cannot write {resolved}: {e}"
    logger.info("edit_file replace_all path=%s replacements=%d", resolved, count)
    return f"Edited {resolved}: replaced {count} occurrence(s) of old_string."


# Calfkit's ``agent_tool`` wraps the bare function into a ``ToolNodeDef``.
# Applied as a regular call (not the ``@agent_tool`` decorator form) so the
# bare functions stay directly importable and unit-testable under their
# real names. Matches the pattern used by ``private_chat``.
read_file_tool: ToolNodeDef = agent_tool(read_file)
write_file_tool: ToolNodeDef = agent_tool(write_file)
edit_file_tool: ToolNodeDef = agent_tool(edit_file)
