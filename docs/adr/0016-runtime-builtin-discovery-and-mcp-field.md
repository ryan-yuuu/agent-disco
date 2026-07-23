# Runtime builtin discovery and split MCP grants

Agent `tools:` frontmatter is builtin-only and is converted to calfkit runtime
tool selectors: omitted `tools:` discovers every live function-tool node
(`Tools(discover=True)`), `tools: []` grants no builtins, and explicit names
become `Tools(names=[...])`.

MCP grants moved to a separate `mcp:` field (`server` or `server/tool`) because
MCP has a different secrets boundary and trust model: agents must name MCP
servers explicitly, but the server's internal tool list remains runtime
discovered through `MCPToolbox`.

This is a hard cutover. Legacy `tools: [mcp/...]` is rejected instead of
partitioned so the frontmatter schema cannot keep two meanings for one field.
The served builtin boundary is the tools host's `ALL_TOOLS` plus deploy filters,
plus bridge-hosted Discord read tools advertised on the same function-tool
plane; per-agent builtin omission is discovery, not an auditable upper bound.

> **Amended by [ADR-0028](0028-mcp-field-discovers-all-servers-by-default.md):**
> the "agents must name MCP servers explicitly" stance no longer holds — `mcp:`
> is now a tri-state that *discovers every server by default*. The `tools:`
> builtin discovery and the `tools:`/`mcp:` split described here are unchanged.
>
> **Amendment (Discord reads in default discovery):** bridge-hosted
> `discord_list_channels` / `discord_read_messages` are no longer filtered out of
> omitted-`tools:` discovery and no longer require an explicit grant. They remain
> ordinary named tool capabilities (still bridge-hosted because only the bridge
> holds the bot token) and are included in create/edit default checkbox sets.
> Operators who want a narrower agent must list `tools:` explicitly without those
> names. Existing agents with omitted `tools:` gain Discord read access on
> upgrade.
