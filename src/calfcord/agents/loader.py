"""Load all :class:`AgentDefinition`s from a directory of Markdown files.

Each ``<name>.md`` file in the directory is parsed via
:func:`parse_agent_md`. Three classes of file are skipped: hidden files
(``.``-prefixed), non-``.md`` files, and ``*.template.md`` reference
templates (e.g. ``agents/agent.template.md``). The last documents the
frontmatter schema for operators and is never a live agent; it is excluded
by name so it does not have to satisfy ``parse_agent_md``'s
``stem == name`` check (it would otherwise abort the whole load).

An omitted ``tools:`` line stays the ``None`` sentinel end to end: the agent
factory maps ``None`` to calfkit's ``Tools(discover=True)`` selector, so the
agent binds every live tool node at runtime rather than a build-time snapshot.
The loader therefore does no tool normalization. See
:attr:`AgentDefinition.tools` for the explicit / implicit semantics.

The filesystem itself prevents duplicate ``agent_id`` (one ``.md`` file per
name); the slash command is always ``/<agent_id>``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from calfcord.agents.definition import AgentDefinition, parse_agent_md

logger = logging.getLogger(__name__)


def _load_one(path: Path) -> AgentDefinition:
    """Parse one agent .md file into a live :class:`AgentDefinition`.

    Single source of truth for turning one file into a definition. Both the
    directory scan (:func:`load_agents_dir`) and explicit file targeting
    (:func:`load_agent_targets`) route through here, so a given file yields an
    identical definition regardless of how it was selected. Tool resolution is
    deferred to the factory and the runtime capability view, so nothing is
    normalized here — an omitted ``tools:`` stays ``None`` (see
    :attr:`AgentDefinition.tools`).
    """
    return parse_agent_md(path)


def load_agents_dir(path: Path) -> list[AgentDefinition]:
    """Scan ``path`` for ``*.md`` files and parse each into an :class:`AgentDefinition`.

    Returns the definitions sorted by ``agent_id`` for deterministic ordering.
    Dot-prefixed files and ``*.template.md`` reference templates are skipped.
    An agent whose frontmatter omits ``tools:`` keeps the ``None`` sentinel;
    the factory maps it to runtime tool discovery (see
    :attr:`AgentDefinition.tools`).

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        NotADirectoryError: if ``path`` is not a directory.
        ValueError: if any individual file fails to parse or validate.
    """
    if not path.exists():
        raise FileNotFoundError(f"agents directory does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"agents path is not a directory: {path}")

    md_files = sorted(
        p for p in path.glob("*.md") if not p.name.startswith(".") and not p.name.endswith(".template.md")
    )
    definitions = [_load_one(p) for p in md_files]
    logger.info("loaded %d agent definition(s) from %s", len(definitions), path)
    return definitions


def load_agent_targets(targets: list[Path]) -> list[AgentDefinition]:
    """Resolve a mix of file and directory paths into ``AgentDefinition``s.

    Each target is classified by the filesystem:

    * **directory** — scanned via :func:`load_agents_dir` (skips dotfiles
      and ``*.template.md``).
    * **regular file** — loaded literally via :func:`_load_one`. Explicitly
      naming a file BYPASSES the directory skip filters: pointing at
      ``agents/foo.template.md`` is an unambiguous request to run it, so it
      is parsed rather than silently dropped (``parse_agent_md`` still
      validates frontmatter, including its ``stem == name`` check).

    The combined set is de-duplicated by ``agent_id``: targeting the same
    agent twice (e.g. a file plus the directory that contains it) is a hard
    error, not silent last-wins — two live agents sharing an ``agent_id``
    would collide on slash command, state-file path, and Kafka identity.

    Returns definitions sorted by ``agent_id`` for deterministic ordering.

    Raises:
        FileNotFoundError: if any target path does not exist.
        ValueError: if a target is neither a file nor a directory, if any
            file fails to parse/validate, or if two targets resolve to the
            same ``agent_id``.
    """
    definitions: list[AgentDefinition] = []
    # agent_id -> source paths that produced it, in encounter order. Used
    # both to detect cross-target collisions and to build a helpful error.
    provenance: dict[str, list[Path]] = {}

    for target in targets:
        if not target.exists():
            raise FileNotFoundError(f"agent target does not exist: {target}")
        if target.is_dir():
            for definition in load_agents_dir(target):
                definitions.append(definition)
                provenance.setdefault(definition.agent_id, []).append(target)
        elif target.is_file():
            definition = _load_one(target)
            definitions.append(definition)
            provenance.setdefault(definition.agent_id, []).append(target)
        else:
            raise ValueError(f"agent target is neither a file nor a directory: {target}")

    duplicates = {aid: paths for aid, paths in provenance.items() if len(paths) > 1}
    if duplicates:
        lines = "\n".join(f"  - {aid}: {', '.join(str(p) for p in paths)}" for aid, paths in sorted(duplicates.items()))
        raise ValueError(f"duplicate agent_id across --target paths:\n{lines}")

    definitions.sort(key=lambda d: d.agent_id)
    logger.info("loaded %d agent definition(s) from %d target(s)", len(definitions), len(targets))
    return definitions
