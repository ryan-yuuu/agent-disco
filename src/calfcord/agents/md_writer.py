"""Atomic mutation of frontmatter fields in an ``agents/<name>.md`` file.

The Discord ``/thinking-effort`` slash command and the ``disco agent
tools`` editor both persist their values into the agent's declarative
``.md`` file rather than a parallel state file, so ``agents/<name>.md`` is
the single source of truth for an agent's declared defaults. This module
owns that write — load the file with ``python-frontmatter``, mutate the
metadata, validate the mutated state **in memory**, dump to disk
atomically.

Every public mutator routes through the one :func:`_update_fields` path so
there is a single validate-before-write implementation: a per-field mutator
would be a place for the atomicity/validation invariant below to drift.
:func:`update_thinking_effort` and :func:`update_tools` differ only in the
``updates`` dict they pass (and the token pre-validation :func:`update_tools`
layers on top before delegating).

Validate-before-write
---------------------
Validation runs on a synthetic :class:`AgentDefinition` built from the
mutated metadata before any disk write. If validation fails the existing
file is untouched. This is what keeps the on-disk file and any validated
in-memory :class:`AgentDefinition` a CLI caller has cached from diverging
when an operator has hand-edited the ``.md`` between boot and the slash
invocation: either both succeed or neither does. The function returns the
validated in-memory definition rather than re-parsing from disk, so a
post-write read error (transient OS, concurrent edit) can't break the
invariant either.

Atomicity on disk uses a tmp-file + fsync + ``os.replace`` + parent-dir
fsync sequence. This is now the only such atomic-write call site; it is
kept inline rather than abstracted — extract a shared helper if a second
atomic-write call site appears.

Frontmatter round-trip caveat: ``python-frontmatter`` ultimately dumps
through PyYAML's ``safe_dump``, which alphabetizes keys (PyYAML defaults
to ``sort_keys=True`` and the library does not override it) and does not
preserve comments. Operators should avoid putting load-bearing comments
in agent frontmatter.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter
import yaml

from calfcord.agents.definition import AgentDefinition

if TYPE_CHECKING:
    from calfcord.agents.definition import ThinkingEffort

logger = logging.getLogger(__name__)


def _validate_and_write(md_path: Path, post: frontmatter.Post) -> AgentDefinition:
    """Validate ``post``'s mutated state in memory, then atomically rewrite ``md_path``.

    The shared tail every mutator converges on once it has produced the final
    :class:`frontmatter.Post` (metadata overlaid, or body replaced): build a
    synthetic :class:`AgentDefinition` from the post's metadata + stripped body
    **before** any disk write so a bad value raises with the file untouched, dump
    the post, normalize the trailing newline, and atomically replace. Factored out
    so :func:`_update_fields` and :func:`update_system_prompt` can't drift on the
    validate-before-write invariant — their only difference is how they mutate the
    post (metadata overlay vs. body replacement) before handing it here.

    Returns the validated in-memory definition rather than re-parsing from disk:
    the bytes just written are what produced it, and a re-parse exception here
    would leave a caller's cached in-memory definition stale relative to disk —
    the very desync the validate-before-write design prevents.
    """
    candidate_metadata = dict(post.metadata)
    candidate_metadata["system_prompt"] = post.content.strip()
    candidate_metadata["source_path"] = md_path
    validated = AgentDefinition(**candidate_metadata)

    payload = frontmatter.dumps(post)
    # Ensure a trailing newline — frontmatter.dumps may omit it depending
    # on the body's own trailing whitespace, and POSIX text files
    # conventionally end with one.
    if not payload.endswith("\n"):
        payload += "\n"

    _atomic_write_text(md_path, payload)
    return validated


def _update_fields(md_path: Path, updates: dict[str, object]) -> AgentDefinition:
    """Apply ``updates`` to ``md_path``'s frontmatter, validating before write.

    The single validate-before-write path every mutator delegates to: load
    the file, overlay ``updates`` onto its metadata, build and validate a
    synthetic :class:`AgentDefinition` from the mutated metadata **in
    memory**, and only then atomically rewrite the file. A bad value raises
    before any disk write, so the on-disk file (and any in-memory definition a
    caller has cached) is left untouched — the desync-prevention invariant the
    module docstring describes. The returned definition is the validated
    in-memory object, not a re-parse, so a transient post-write read error
    can't break that invariant either.

    ``updates`` carries already-coerced field values (e.g. an explicit
    ``list`` for ``tools``); semantic pre-validation that needs richer error
    messages than pydantic gives (the bad-token report in
    :func:`update_tools`) belongs in the caller, before delegating here.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: the existing ``.md`` is unparseable YAML or the mutated
            metadata fails :class:`AgentDefinition` validation. The on-disk
            file is unchanged.
        OSError: a filesystem error during the atomic write (e.g.
            permission denied, no space). The on-disk file is unchanged.
    """
    try:
        post = frontmatter.load(md_path)
    except yaml.YAMLError as e:
        raise ValueError(f"{md_path}: existing frontmatter is malformed YAML: {e}") from e

    post.metadata.update(updates)

    validated = _validate_and_write(md_path, post)
    logger.info("rewrote %s fields=%s in %s", "/".join(updates), list(updates), md_path)
    return validated


def update_system_prompt(md_path: Path, body: str) -> AgentDefinition:
    """Rewrite the system-prompt **body** of ``md_path``, validating before write.

    The system prompt is the Markdown body (``post.content``), not a frontmatter
    metadata field, so it cannot ride :func:`_update_fields`'s metadata-overlay
    path — it sets ``post.content`` directly instead. Everything else mirrors
    :func:`_update_fields` exactly: build and validate a synthetic
    :class:`AgentDefinition` from the mutated state **in memory** (the
    ``system_prompt`` validator rejects an empty/whitespace-only body, so a bad
    value raises before any disk write), then atomically rewrite. The on-disk
    file is left untouched on any failure, keeping a caller's cached in-memory
    definition from diverging from disk — the same desync-prevention invariant
    the rest of the module upholds. The returned definition is the validated in-memory object,
    not a re-parse, so a transient post-write read error can't break it either.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: the existing ``.md`` is unparseable YAML, the new ``body`` is
            empty/whitespace-only, or the mutated metadata otherwise fails
            :class:`AgentDefinition` validation. The on-disk file is unchanged.
        OSError: a filesystem error during the atomic write (e.g. permission
            denied, no space). The on-disk file is unchanged.
    """
    try:
        post = frontmatter.load(md_path)
    except yaml.YAMLError as e:
        raise ValueError(f"{md_path}: existing frontmatter is malformed YAML: {e}") from e

    post.content = body

    validated = _validate_and_write(md_path, post)
    logger.info("rewrote system_prompt in %s", md_path)
    return validated


def update_thinking_effort(md_path: Path, value: ThinkingEffort) -> AgentDefinition:
    """Rewrite the ``thinking_effort`` frontmatter field in ``md_path``.

    Validates the post-mutation state in memory before touching disk so a
    validation failure leaves the on-disk file untouched and the caller's
    in-memory view is consistent with what's persisted.

    Returns the validated :class:`AgentDefinition` so callers can swap
    their cached copy without a second filesystem round-trip.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: the existing ``.md`` is unparseable YAML, fails
            :class:`AgentDefinition` validation, or the new value would
            produce an invalid definition. The on-disk file is unchanged.
        OSError: a filesystem error during the atomic write (e.g.
            permission denied, no space). The on-disk file is unchanged.
    """
    return _update_fields(md_path, {"thinking_effort": value})


def update_tool_grants(
    md_path: Path,
    *,
    tools: Sequence[str] | None,
    mcp: bool | Sequence[str] = True,
) -> AgentDefinition:
    """Rewrite builtin ``tools:`` and the tri-state MCP ``mcp:`` grant atomically.

    ``tools=None`` removes the ``tools:`` key, expressing runtime discovery of
    all live builtin tools. ``tools=[]`` writes an explicit empty builtin list.
    The ``mcp`` tri-state (``True`` / ``False`` / a named grant list) is written
    by :func:`apply_mcp_metadata`, which owns the canonical on-disk form.
    """
    from calfcord.tools import TOOL_REGISTRY

    if tools is not None:
        _validate_builtin_tools(tools, TOOL_REGISTRY)
    _validate_mcp_grants(mcp)

    try:
        post = frontmatter.load(md_path)
    except yaml.YAMLError as e:
        raise ValueError(f"{md_path}: existing frontmatter is malformed YAML: {e}") from e

    if tools is None:
        post.metadata.pop("tools", None)
    else:
        post.metadata["tools"] = list(tools)

    apply_mcp_metadata(post.metadata, mcp)

    validated = _validate_and_write(md_path, post)
    logger.info("rewrote tool grants in %s", md_path)
    return validated


def apply_mcp_metadata(metadata: MutableMapping[str, Any], mcp: bool | Sequence[str]) -> None:
    """Write the tri-state ``mcp`` field into a frontmatter ``metadata`` mapping.

    The single source of truth for the canonical on-disk form of each state,
    shared by the update (:func:`update_tool_grants`) and create
    (:func:`calfcord.cli._agents.write_agent`) paths so a change to how a state
    serializes cannot silently diverge between them — a real risk here because
    the poles carry security weight (an omitted key now means discover-all, not
    off):

    * ``True`` — remove the ``mcp:`` key. An omitted key parses back to ``True``
      (discover every live MCP server), so this is discover's canonical form.
      ``pop`` is a harmless no-op on the create path's fresh mapping.
    * ``False`` / ``[]`` — write ``mcp: false`` (explicit opt-out). It must NOT
      be an omitted key, which would mean discover.
    * a non-empty sequence — write ``mcp: [<grants>]`` (a named grant list).
    """
    if mcp is True:
        metadata.pop("mcp", None)
    elif mcp:
        metadata["mcp"] = list(mcp)
    else:  # False or []
        metadata["mcp"] = False


def update_tools(md_path: Path, tools: Sequence[str]) -> AgentDefinition:
    """Rewrite only the builtin ``tools`` frontmatter list in ``md_path``.

    Every token is validated *before* the shared write path runs, so an
    unknown builtin or legacy ``mcp/...`` token raises a precise
    :class:`ValueError` (naming the offending token) with the on-disk file
    untouched, rather than surfacing as a generic pydantic error:

    * an ``mcp/...`` token is rejected because ``tools:`` is builtin-only;
    * a *builtin* token must be a key of
      :data:`calfcord.tools.TOOL_REGISTRY`.

    Existing ``mcp:`` grants are preserved. Use :func:`update_tool_grants` when
    a caller needs to rewrite both fields or remove ``tools:`` to express
    builtin discovery.

    The ``TOOL_REGISTRY`` import is deferred to here (rather than module
    scope) so :func:`update_thinking_effort`'s path stays light: importing
    ``TOOL_REGISTRY`` eagerly composes the tool registry (importing the
    vendored ``calfkit-tools`` nodes), which the thinking-effort slash
    command has no reason to pay for.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: an unknown builtin token, a legacy ``mcp/...`` token,
            or a post-mutation :class:`AgentDefinition` validation failure.
            The on-disk file is unchanged.
        OSError: a filesystem error during the atomic write. The on-disk
            file is unchanged.
    """
    from calfcord.tools import TOOL_REGISTRY

    _validate_builtin_tools(tools, TOOL_REGISTRY)

    return _update_fields(md_path, {"tools": list(tools)})


def _validate_builtin_tools(tools: Sequence[str], registry: dict[str, object]) -> None:
    from calfcord.mcp.selector import is_mcp_selector

    for token in tools:
        if not isinstance(token, str):
            raise ValueError(f"invalid tool {token!r}: expected a string")
        if is_mcp_selector(token):
            raise ValueError(f"invalid tool {token!r}: tools: is builtin-only; move MCP grants to mcp:")
        if token not in registry:
            valid = ", ".join(sorted(registry)) or "(none registered)"
            raise ValueError(f"unknown tool {token!r}; expected a builtin ({valid})")


def _validate_mcp_grants(mcp: bool | Sequence[str]) -> None:
    from calfcord.mcp.selector import validate_mcp_selector

    if isinstance(mcp, bool):
        # The ``True``/``False`` tri-state poles carry no grant tokens to check.
        return
    for token in mcp:
        if not isinstance(token, str):
            raise ValueError(f"invalid MCP grant {token!r}: expected a string")
        validate_mcp_selector(token)


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via tmp-file + fsync + atomic rename.

    Caller is expected to have verified ``path`` exists (and therefore
    its parent does) — no defensive ``mkdir`` here.
    """
    # mkstemp creates the tmp file 0o600, and os.replace adopts the tmp file's
    # mode — so without this an existing 0o644 agent .md would silently become
    # 0o600 on every rewrite. Capture the target's mode now to restore it after.
    original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
        if original_mode is not None:
            os.chmod(path, original_mode)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if os.name == "posix":
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # The rename is already durable on most filesystems even if
            # we can't fsync the parent. Don't fail the caller's commit;
            # we just lose the strong-durability guarantee on power loss.
            logger.warning(
                "parent-dir fsync failed for %s; rename is committed but durability "
                "may be weaker on power loss",
                path,
                exc_info=True,
            )
