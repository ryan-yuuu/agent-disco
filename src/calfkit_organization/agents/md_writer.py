"""Atomic mutation of a single field in an ``agents/<name>.md`` frontmatter.

The Discord ``/thinking-effort`` slash command persists its value into the
agent's declarative ``.md`` file rather than a parallel state file, so
``agents/<name>.md`` is the single source of truth for an agent's declared
defaults. This module owns that write — load the file with
``python-frontmatter``, mutate the metadata, validate the mutated state
**in memory**, dump to disk atomically.

Validate-before-write
---------------------
Validation runs on a synthetic :class:`AgentDefinition` built from the
mutated metadata before any disk write. If validation fails the existing
file is untouched. This is what keeps the on-disk file and the
:class:`AgentRegistry`'s in-memory entry from diverging when an operator
has hand-edited the ``.md`` between boot and the slash invocation: either
both succeed or neither does. The function returns the validated
in-memory definition rather than re-parsing from disk, so a post-write
read error (transient OS, concurrent edit) can't break the invariant
either.

Atomicity on disk uses the same tmp-file + fsync + ``os.replace`` +
parent-dir fsync sequence as
:class:`calfkit_organization.agents.state.AgentStateStore`. Mirrored here
rather than abstracted because a one-call-site abstraction would add
indirection without saving meaningful lines — extract a shared helper if
a third atomic-write call site appears.

Frontmatter round-trip caveat: ``python-frontmatter`` ultimately dumps
through PyYAML's ``safe_dump``, which alphabetizes keys (PyYAML defaults
to ``sort_keys=True`` and the library does not override it) and does not
preserve comments. Operators should avoid putting load-bearing comments
in agent frontmatter.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import frontmatter
import yaml

from calfkit_organization.agents.definition import AgentDefinition, ThinkingEffort

logger = logging.getLogger(__name__)


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
    try:
        post = frontmatter.load(md_path)
    except yaml.YAMLError as e:
        raise ValueError(f"{md_path}: existing frontmatter is malformed YAML: {e}") from e

    post.metadata["thinking_effort"] = value

    # Validate the mutated state in memory FIRST. parse_agent_md does an
    # equivalent construction; mirror it here so the disk write is gated
    # on a successful AgentDefinition build.
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
    logger.info("rewrote thinking_effort=%s in %s", value, md_path)

    # Return the validated in-memory definition rather than re-parsing
    # from disk: the disk content is byte-for-byte what produced
    # ``validated`` above, and a re-parse exception here would leave the
    # caller's registry copy stale relative to disk — the very desync
    # the validate-before-write design is meant to prevent.
    return validated


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via tmp-file + fsync + atomic rename.

    Caller is expected to have verified ``path`` exists (and therefore
    its parent does) — no defensive ``mkdir`` here.
    """
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
