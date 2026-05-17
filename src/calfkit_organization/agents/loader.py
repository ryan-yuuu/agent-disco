"""Load all :class:`AgentDefinition`s from a directory of Markdown files.

Each ``<name>.md`` file in the directory is parsed via
:func:`parse_agent_md`. Hidden files (``.``-prefixed) and non-``.md``
files are ignored.

Cross-agent uniqueness of ``slash`` and ``display_name`` is the
:class:`AgentRegistry`'s concern, not the loader's. The filesystem itself
prevents duplicate ``agent_id`` (one ``.md`` file per name).
"""

from __future__ import annotations

import logging
from pathlib import Path

from calfkit_organization.agents.definition import AgentDefinition, parse_agent_md

logger = logging.getLogger(__name__)


def load_agents_dir(path: Path) -> list[AgentDefinition]:
    """Scan ``path`` for ``*.md`` files and parse each into an :class:`AgentDefinition`.

    Returns the definitions sorted by ``agent_id`` for deterministic ordering.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        NotADirectoryError: if ``path`` is not a directory.
        ValueError: if any individual file fails to parse or validate.
    """
    if not path.exists():
        raise FileNotFoundError(f"agents directory does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"agents path is not a directory: {path}")

    md_files = sorted(p for p in path.glob("*.md") if not p.name.startswith("."))
    definitions = [parse_agent_md(p) for p in md_files]
    logger.info("loaded %d agent definition(s) from %s", len(definitions), path)
    return definitions
