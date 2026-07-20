# The `mcp:` field discovers every server by default

The agent `mcp:` frontmatter field is a tri-state, mirroring `a2a`/`handoff`:
omitted (or `mcp: true`) **discovers every live MCP server on the network** and
binds its tools per turn; `mcp: false` opts out; a list (`[server]` /
`[server/tool]`) is a named grant. This reverses the "agents must name MCP
servers explicitly" stance of [ADR-0016](0016-runtime-builtin-discovery-and-mcp-field.md),
which it otherwise amends rather than supersedes (builtin discovery and the
`tools:`/`mcp:` split are unchanged).

Two things made the reversal the right call. calfkit 0.13 replaced the
identity-only `MCPToolbox` selector with a `Toolboxes` family selector that has a
first-class `discover=True` mode (the same discover-XOR-named rail as
`Tools`/`Messaging`/`Handoff`), so "discover every server" is now a supported,
per-turn-resolved primitive rather than something calfcord would have to fake.
And `a2a`/`handoff` — the other cross-node capabilities — already default to
discover, so opt-in MCP was the odd one out; making all three discover-by-default
removes a papercut where a new agent silently reached no MCP tools.

## Considered options

- **Keep MCP opt-in (ADR-0016 as written).** Rejected: it is inconsistent with
  the `a2a`/`handoff` defaults and makes the common case (an agent that should
  use the org's MCP tools) require boilerplate that is easy to forget.
- **A `tools`-style `None`/`()`/list encoding.** Rejected: `bool | tuple` reads
  naturally in YAML (`mcp: false`), and matching `a2a`/`handoff` keeps the three
  discover-capable fields shaped identically.

## Consequences

The discover default is broad: an agent that omits `mcp:` binds **all** networked
MCP tools each turn (gmail, github, …), whose trust boundary is the MCP server,
not the agent. An agent that takes input from untrusted users must set
`mcp: false` or a narrow named list — the same caution `tools:`-omission already
carries for builtins. The interactive tool editor (`disco agent tools`) surfaces
the state with an explicit "discover every live MCP server" row so editing a
discover agent cannot silently flip it off.
