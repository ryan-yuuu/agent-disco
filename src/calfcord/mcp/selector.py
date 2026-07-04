"""Parsing + validation for canonical ``mcp:`` grants in agent frontmatter.

Background
----------

Agents declare MCP grants in ``agents/*.md`` frontmatter under a dedicated
``mcp:`` list. Builtin tools remain under ``tools:`` by bare name
(``terminal``, ``web_search``); MCP grants use canonical server paths:

* ``<server>`` — expose *every* tool the named MCP server publishes.
* ``<server>/<tool>`` — expose a single tool of that server.

The agent's LLM sees each tool under the name the MCP server advertises;
tool calls dispatch to the server's calfkit toolbox over the
``mcp_server.<server>`` topic. Resolving a selector against the live
capability view is handled downstream in :mod:`calfcord.mcp.agent_select`;
this module is concerned only with recognizing and decomposing the
selector string itself.

Design choices
--------------

* **Leaf module, zero project imports** — this file imports nothing from
  :mod:`calfcord` or :mod:`calfkit` (only :mod:`re` + stdlib). The agent
  frontmatter parser (``calfcord.agents.definition``) needs to recognize
  legacy ``mcp/...`` entries in ``tools:`` and validate canonical ``mcp:``
  grants *before* anything decides whether to build the
  schema-only MCP catalog. Keeping this module a pure leaf lets that
  parser ``from calfcord.mcp.selector import ...`` without dragging in
  the catalog build (which imports :mod:`calfkit`) or risking an import
  cycle with the ``tools`` package.

* **Regexes redeclared, not imported** — the character-class rules below
  intentionally duplicate the spirit of ``TOOL_NAME_REGEX`` in
  :mod:`calfcord.tools.deploy_filters` rather than importing it. Importing
  from the ``tools`` package would create a coupling (and a potential
  import cycle, since tool modules import back through bridge/agent code)
  for the sake of one shared constant. A few lines of duplication here
  buys this module its leaf status; the regexes are commented so a future
  reader knows the omission is deliberate.

* **Two distinct name grammars** — the *server* segment is constrained to
  ``[a-z0-9_]`` because it must double as a Kafka topic segment
  (``mcp_server.<server>``), an ``mcp.json`` key, and the suffix of the
  server's roster process name (``mcp-<server>``); lowercase-plus-
  underscore is the safe intersection.
  The *tool* segment allows ``[a-zA-Z0-9_-]`` (and a longer bound) to
  match the original MCP tool name as advertised by the upstream server,
  which we do not control and which commonly uses mixed case or hyphens.

* **Strict, message-rich ``ValueError``** — every rejection names the
  offending ``entry`` verbatim so a typo in an ``agents/*.md`` file
  surfaces with the exact bad string, not a generic "invalid grant".
"""

from __future__ import annotations

import re
from typing import NamedTuple

MCP_SELECTOR_PREFIX = "mcp/"
"""The legacy prefix that marks a stale ``tools:`` entry as an MCP grant.

``tools:`` is builtin-only after the hard cutover, so callers use this only to
reject old ``tools: [mcp/...]`` syntax with migration guidance. Canonical
``mcp:`` field entries do not include this prefix.
"""

# Server segment: must double as a Kafka topic segment, an mcp.json key,
# and a roster process-name suffix, so we restrict to lowercase + digits +
# underscore. Redeclared here (rather than imported from
# calfcord.tools.deploy_filters) to keep this module a pure leaf — see the
# module docstring's "Regexes redeclared" note.
_SERVER_NAME_REGEX = re.compile(r"^[a-z0-9_]{1,64}$")

# Tool segment: matches the *original* MCP tool name advertised by an
# upstream server we do not control; allow mixed case + hyphen + a longer
# bound, mirroring ``TOOL_NAME_REGEX`` in calfcord.tools.deploy_filters (also
# redeclared, not imported — same leaf-module rationale).
_TOOL_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def is_valid_server_name(name: str) -> bool:
    """Return whether ``name`` is a legal ``<server>`` selector segment.

    Single source of truth for "is this a valid MCP server name?" — it
    matches against the *same* module-level :data:`_SERVER_NAME_REGEX`
    (``^[a-z0-9_]{1,64}$``) that :func:`parse_mcp_selector` enforces on the
    server segment, so a name accepted here is exactly a name a canonical
    ``mcp:`` grant can reach. Reuses the compiled regex rather than re-spelling
    the pattern, so the grammar cannot drift between the two call sites.

    The intended consumers are the ``mcp.json`` loader and the
    ``disco mcp add`` writer, which key server config by this name: a
    configured server whose name is not a legal server segment would be
    unreachable by any valid ``mcp:`` grant (and could not name a
    Kafka topic segment or roster process). Validating here lets both
    reject that case loudly instead.

    Args:
        name: A candidate server name (typically a ``schemas/`` module
            name). Not a full ``mcp:`` grant — just the bare server
            segment.

    Returns:
        ``True`` if ``name`` matches the server-segment grammar, else
        ``False``.
    """
    return bool(_SERVER_NAME_REGEX.match(name))


def is_mcp_selector(entry: str) -> bool:
    """Return ``True`` if ``entry`` uses the legacy ``mcp/`` prefix.

    This is a cheap prefix check only. After the hard cutover, callers use it
    to reject old ``tools: [mcp/...]`` entries with migration guidance.
    """
    return entry.startswith(MCP_SELECTOR_PREFIX)


class McpSelector(NamedTuple):
    """A parsed canonical ``mcp:`` grant: a server plus an optional single tool.

    ``tool is None`` means "all tools of ``server``" (the bare ``<server>``
    form); a concrete ``tool`` selects exactly one (``<server>/<tool>``).
    Prefer reading that distinction through :attr:`selects_all_tools` over
    re-checking ``tool is None`` at each call site.

    As a :class:`~typing.NamedTuple` it stays backward compatible with the
    previous ``(server, tool)`` return: positional unpacking
    (``server, tool = parse_mcp_selector(...)``) and tuple equality
    (``== ("gmail", "search")``) both still hold.
    """

    server: str
    tool: str | None

    @property
    def selects_all_tools(self) -> bool:
        """Whether this selector expands to every tool of :attr:`server`."""
        return self.tool is None


def parse_mcp_selector(entry: str) -> McpSelector:
    """Decompose a canonical MCP grant into ``(server, tool_or_none)``.

    Examples::

        parse_mcp_selector("gmail")         -> ("gmail", None)
        parse_mcp_selector("gmail/search")  -> ("gmail", "search")

    A ``None`` tool means "all tools of this server" (the bare-server
    form); a non-``None`` tool selects exactly one.

    Args:
        entry: The raw ``mcp:`` list entry.

    Returns:
        An :class:`McpSelector` (a ``(server, tool)`` NamedTuple) where
        ``tool`` is ``None`` for the bare-server form and the original tool
        name otherwise. It unpacks and compares as the plain ``(server,
        tool)`` tuple it replaced, so existing callers are unaffected.

    Raises:
        ValueError: When ``entry`` splits into a segment count other than 1
            or 2, has an empty server or tool segment, or has a server/tool
            segment that violates the respective name grammar. The message
            always names ``entry`` verbatim so the offending frontmatter line
            is unambiguous.
    """
    segments = entry.split("/")
    if len(segments) not in (1, 2):
        raise ValueError(
            f"MCP grant {entry!r} must be '<server>' or "
            f"'<server>/<tool>', got {len(segments)} '/'-separated "
            f"segment(s)"
        )

    server = segments[0]
    tool = segments[1] if len(segments) == 2 else None

    if not server:
        raise ValueError(f"MCP grant {entry!r} has an empty server segment")
    if not _SERVER_NAME_REGEX.match(server):
        raise ValueError(
            f"MCP grant {entry!r} has invalid server name {server!r}; "
            f"must match {_SERVER_NAME_REGEX.pattern}"
        )

    if tool is not None:
        if not tool:
            raise ValueError(f"MCP grant {entry!r} has an empty tool segment")
        if not _TOOL_NAME_REGEX.match(tool):
            raise ValueError(
                f"MCP grant {entry!r} has invalid tool name {tool!r}; "
                f"must match {_TOOL_NAME_REGEX.pattern}"
            )

    return McpSelector(server, tool)


def validate_mcp_selector(entry: str) -> None:
    """Raise :class:`ValueError` if ``entry`` is not a well-formed selector.

    Thin wrapper over :func:`parse_mcp_selector` that discards the parsed
    result — for call sites (e.g. the frontmatter validator) that care
    only about *whether* a selector is structurally valid, not about its
    decomposed parts. Keeping this as a named function makes those call
    sites read as the assertion they are and avoids a stray
    ``parse_mcp_selector(entry)``-with-unused-result lint smell.
    """
    parse_mcp_selector(entry)
