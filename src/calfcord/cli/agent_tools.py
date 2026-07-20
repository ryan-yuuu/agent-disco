"""``disco agent tools [<name>]`` — interactive editor for an agent's tools.

Picks an agent ``.md`` (by name, or via a prompt over the install's agents
dir), shows a multi-select checkbox of every builtin tool, pre-checked from the
agent's current ``tools:`` declaration plus MCP rows pre-checked from
``mcp:``, and writes the operator's selection back through the
validated-atomic :func:`calfcord.agents.md_writer.update_tool_grants`.

It reads the RAW declaration, not the loader's expansion: it calls
:func:`calfcord.agents.definition.parse_agent_md` directly (not the loader's
default-resolving path) so it can distinguish ``tools:`` *omitted* (``None`` →
implicitly "all builtins") from ``tools: []`` (explicitly none). The
implicit-all case is converted into checked builtin rows in the UI; if every
builtin remains selected, the writer omits ``tools:`` to preserve discovery.
MCP rows use ``mcp/<server>`` UI values, but persist as canonical ``mcp:``
entries.

Tool edits take effect on the next ``disco calfkit-agent`` boot — the node
bakes its tool list at construction time (see the onboarding plan's "tools are
baked into the node at boot" finding), so the command tells the operator to
restart rather than implying a live reload.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from calfcord.agents.definition import parse_agent_md
from calfcord.agents.md_writer import update_tool_grants
from calfcord.cli._agents import MCP_DISCOVER_ROW, _split_tool_selection, detect_agents
from calfcord.cli._fields import truncate
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli.tui import render

# The longest tool summary shown in a checkbox row. Long enough to tell two tools
# apart, short enough that "<pointer> <mark> <name> — <summary>" stays on ONE row
# of a conventional 80-column terminal (the registry's longest name is 12 chars,
# leaving ~57; 50 keeps headroom for narrower panes and the mcp/ rows).
#
# A row that WRAPS is worse than one that is clipped: the widget renders
# continuation lines flush against the panel's left edge, so a wrapped row reads
# as a new row and the list stops being scannable. Found by driving the real
# command under a pty — no renderable-level test could see it.
_SUMMARY_LEN = 50


def first_line(desc: str | None) -> str:
    """Return a one-line, human-readable summary of a tool ``desc``.

    Tool descriptions are multi-line docstrings whose first line is wrapped in
    a ``<summary>...</summary>`` tag and sprinkled with reStructuredText
    double-backtick inline-literal markup; neither renders usefully in a
    single-line checkbox label. We take the first non-empty line, drop the
    summary wrapper, collapse the double-backtick markup to plain text, and clip
    the result to :data:`_SUMMARY_LEN`.

    Clipping is this module's call, not the widget's: only here do we know the
    summary is a *hint* while the tool NAME is what the operator is choosing. The
    widget must never silently cut a label it was handed — so it wraps — which is
    exactly why the label handed to it has to arrive short. The trailing ``…``
    keeps the clip visible; ``disco agent show`` has the full text.
    """
    if not desc:
        return ""
    for raw in desc.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = line.removeprefix("<summary>").removesuffix("</summary>").strip()
        # ``\`\`x\`\``` (RST inline literal) -> ``x``: drop the double-backtick
        # markup so the label reads as plain prose. ``truncate`` is the shared
        # clipper the edit menu and ``agent list`` already use, so the three
        # surfaces cannot clip differently.
        return truncate(line.replace("``", ""), _SUMMARY_LEN)
    return ""


def _resolve_agent(prompter: Prompter, *, agents_dir: Path, name: str | None) -> Path | None:
    """Resolve the agent ``.md`` to edit, or ``None`` after printing why not.

    ``name`` given: require ``agents_dir/<name>.md`` to exist (an explicit
    request for a missing agent is an error, not a fallback to the picker).
    ``name`` omitted: list the detected agents and prompt; an empty dir is an
    error too — there is nothing to edit. Returning ``None`` (rather than
    raising) lets :func:`run` map every "can't proceed" case to exit code 1
    with a single, already-printed message.
    """
    if name is not None:
        md_path = agents_dir / f"{name}.md"
        if not md_path.is_file():
            print(f"error: no agent {name!r} in {agents_dir} (expected {md_path})")
            return None
        return md_path

    agents = detect_agents(agents_dir)
    if not agents:
        print(f"no agents in {agents_dir}")
        return None
    chosen = prompter.select(
        "Which agent's tools do you want to edit?",
        [Choice(a, a) for a in agents],
    )
    return agents_dir / f"{chosen}.md"


def _build_choices(
    current: set[str],
    *,
    mcp_servers: list[str] | None = None,
    live_tools: dict[str, list[str]] | None = None,
) -> list[Choice]:
    """Build the checkbox :class:`Choice` rows: builtins, MCP, then kept.

    Builtins enumerate the schema-only :data:`calfcord.tools.TOOL_REGISTRY`
    seam (no transport, no secrets), checked iff in ``current``.

    A leading MCP-discover row (:data:`MCP_DISCOVER_ROW`) represents
    ``mcp: true`` — discover every live server; it is checked iff discover is
    the agent's current MCP state, and ticking it opts into discover mode.

    Below it, MCP rows merge two sources: ``mcp_servers`` (this host's mcp.json
    names) and ``live_tools`` (the broker's capability view — which also surfaces
    servers OTHER hosts run). Each server gets an ``mcp/<server>`` "all
    tools" row; each live-advertised tool gets an ``mcp/<server>/<tool>``
    row. Named rows are pre-checked only when already in ``current``.

    Anything in ``current`` the rows above did not cover (a server that's
    gone from config and view, a builtin this host doesn't carry) is
    appended as a pre-checked "kept" row: unchecking is an explicit operator
    decision, and an edit that only touched builtins can never silently drop
    a selector the editor failed to enumerate.
    """
    from calfcord.tools import TOOL_REGISTRY

    choices: list[Choice] = []
    for name in sorted(TOOL_REGISTRY):
        summary = first_line(TOOL_REGISTRY[name].tool_schema.description)
        label = f"{name} — {summary}" if summary else name
        choices.append(Choice(name, label, name in current))

    choices.append(
        Choice(
            MCP_DISCOVER_ROW,
            f"{MCP_DISCOVER_ROW} — discover every live MCP server (default)",
            MCP_DISCOVER_ROW in current,
        )
    )

    mcp_servers = mcp_servers or []
    live_tools = live_tools or {}
    for server in sorted(set(mcp_servers) | set(live_tools)):
        all_row = f"mcp/{server}"
        choices.append(
            Choice(all_row, f"{all_row} — all tools from MCP server '{server}'", all_row in current)
        )
        for tool in sorted(live_tools.get(server, [])):
            value = f"mcp/{server}/{tool}"
            choices.append(Choice(value, f"{value} — (live)", value in current))

    offered = {c.value for c in choices}
    for kept in sorted(current - offered):
        choices.append(Choice(kept, f"{kept} — (kept: configured on this agent)", True))
    return choices


def _default_mcp_servers() -> list[str]:
    """mcp.json server names; tolerant — a broken config degrades to none
    (the strict readers surface the error; the editor must still open)."""
    from calfcord.mcp.config import McpConfigError, list_server_names, resolve_config_path

    try:
        return list_server_names(resolve_config_path())
    except McpConfigError:
        return []


def _default_live_tools() -> dict[str, list[str]]:
    """Per-server tool names from the live capability view.

    An *unreachable* view (broker down, workspace closed) prints a one-line
    note and degrades to server-level rows — otherwise it would be
    indistinguishable from "no server advertises anything", and a wrong
    ``CALF_HOST_URL`` would silently hide every per-tool row.
    """
    import os

    from calfcord.mcp.capability_read import snapshot_capability_tools

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    live = snapshot_capability_tools(server_urls)
    if live is None:
        print(
            f"note: live MCP tool view unavailable (broker {server_urls} unreachable?) "
            "— showing server-level mcp/ rows only."
        )
        return {}
    return live


def run(
    prompter: Prompter,
    *,
    agents_dir: Path,
    name: str | None,
    mcp_servers_fn: Callable[[], list[str]] | None = None,
    live_tools_fn: Callable[[], dict[str, list[str]]] | None = None,
) -> int:
    """Run the interactive tool editor and return an exit code.

    Resolves the agent, reads its RAW ``tools:`` declaration, shows the
    pre-checked multi-select, and writes the selection back. Returns 1 (with an
    already-printed ``error:`` line) when no agent can be resolved or the read /
    write fails; 0 after a successful write. Per the CLI error-handling
    convention, operator-recoverable problems (a malformed ``.md``, an
    unwritable file) print an actionable message and return non-zero rather than
    letting a raw traceback escape. All prompting goes through the injected
    :class:`Prompter`, so the flow is testable without a TTY.
    """
    render.header("disco agent tools", subtitle="Choose which tools this agent can use.")
    md_path = _resolve_agent(prompter, agents_dir=agents_dir, name=name)
    if md_path is None:
        return 1
    agent_name = md_path.stem

    try:
        raw = parse_agent_md(md_path)
    except (ValueError, OSError) as e:
        # Malformed/empty frontmatter, a name≠stem mismatch, or an unreadable
        # file — operator-recoverable, so report it instead of crashing.
        print(f"error: cannot read agent {agent_name!r}: {e}")
        return 1

    from calfcord.tools import TOOL_REGISTRY

    # ``tools:`` omitted means "all builtins" — pre-check exactly the
    # builtins, matching the loader's default expansion.
    current = set(raw.tools) if raw.tools is not None else set(TOOL_REGISTRY)
    # ``mcp`` is tri-state: ``True`` pre-checks the discover row; a named tuple
    # pre-checks its ``mcp/<server>`` rows; ``False`` pre-checks nothing.
    if raw.mcp is True:
        current.add(MCP_DISCOVER_ROW)
    elif raw.mcp:
        current.update(f"mcp/{entry}" for entry in raw.mcp)

    mcp_servers = (mcp_servers_fn or _default_mcp_servers)()
    live_tools = (live_tools_fn or _default_live_tools)()
    choices = _build_choices(current, mcp_servers=mcp_servers, live_tools=live_tools)

    selected = prompter.checkbox(
        f"Tools for {agent_name}",
        choices,
    )

    try:
        grants = _split_tool_selection(selected, set(TOOL_REGISTRY))
        update_tool_grants(md_path, tools=grants.tools, mcp=grants.mcp)
    except (ValueError, OSError) as e:
        # The write path: a read-only dir, ENOSPC, a concurrent delete, or a
        # token the writer rejects. Report and exit non-zero; the on-disk file
        # is left untouched by ``update_tools`` on failure.
        print(f"error: failed to update {agent_name!r}: {e}")
        return 1

    print(f"Updated {agent_name}: {len(selected)} tool(s). Restart `disco calfkit-agent` to apply.")
    return 0
