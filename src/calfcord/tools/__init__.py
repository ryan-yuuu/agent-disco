"""The calfcord tool surface.

The runnable tool node — the process that actually executes the tool's Python
body — lives in a separate ``calfkit-tools`` deployment. That process serves
:data:`TOOL_REGISTRY`; agents carry runtime selectors and resolve live builtin
tools from the capability view rather than importing this registry at build
time.

Composition over discovery
--------------------------

The surface is an **explicit list** (:data:`ALL_TOOLS`), not a filesystem
walk. Every tool is vendored from the ``calfkit-tools`` package (hermes
shell/files/web/todo + an SSRF-safe ``web_fetch``); agent-to-agent messaging is
no longer a tool at all — the calfkit 0.12 migration moved A2A onto calfkit's
native handoff/messaging dispatch. The hermes nodes are imported by name rather
than spread from ``HERMES_NODES`` on purpose: this list is the security boundary
— tools like ``terminal`` and ``execute_code`` run arbitrary code on the
tools host, so what agents can reach must be a reviewable, local decision,
never an artifact of which package version happens to be installed. The
drift-guard in ``tests/tools/test_registry.py`` fails CI if this list and
the package's published set diverge.

Deploy-time narrowing (``CALFCORD_TOOLS_INCLUDE``, per-host tool subsets) and
aliasing (``CALFCORD_TOOLS_ALIAS``, multi-host rename) are applied by
:func:`~calfcord.tools.deploy_filters.apply_deploy_filters` — a pure
transform over :data:`ALL_TOOLS`.
"""

from __future__ import annotations

from calfkit.nodes.tool import ToolNodeDef
from calfkit_tools.hermes.node import (
    execute_code,
    patch,
    process,
    read_file,
    search_files,
    terminal,
    todo,
    web_extract,
    web_search,
    write_file,
)
from calfkit_tools.web_fetch.node import web_fetch

from calfcord.tools.deploy_filters import apply_deploy_filters

ALL_TOOLS: tuple[ToolNodeDef, ...] = (
    # hermes (vendored ``calfkit-tools``) — shell + process management,
    # files, search, todo, code execution, web search/extract.
    terminal,
    process,
    read_file,
    write_file,
    patch,
    search_files,
    todo,
    execute_code,
    web_search,
    web_extract,
    # SSRF-safe URL fetch (vendored, separate ``calfkit-tools`` subpackage).
    web_fetch,
)
"""The complete, auditable tool surface this deployment can expose."""

TOOL_REGISTRY: dict[str, ToolNodeDef] = apply_deploy_filters(ALL_TOOLS)
"""Tool name → :class:`ToolNodeDef`, after applying the deploy-time
``CALFCORD_TOOLS_INCLUDE`` / ``CALFCORD_TOOLS_ALIAS`` transforms. Order
follows :data:`ALL_TOOLS` so boot logs are reproducible."""


def default_builtin_tool_names() -> frozenset[str]:
    """Names granted by an omitted ``tools:`` frontmatter field.

    The tools-host registry is only half of the default surface: the bridge also
    advertises read-only Discord tools against its authenticated client. Those
    names are not in :data:`TOOL_REGISTRY` (the tools host has no bot token), but
    they are ordinary live function tools and ride the same omitted-field
    discovery path. CLI create/edit UIs use this set so "accept defaults" and
    runtime discovery cannot drift.
    """
    from calfcord.tools.discord import DISCORD_TOOL_NAMES

    return frozenset(TOOL_REGISTRY) | DISCORD_TOOL_NAMES
