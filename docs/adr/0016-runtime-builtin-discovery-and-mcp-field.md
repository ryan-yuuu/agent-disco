# Runtime builtin discovery and split MCP grants

Agent `tools:` frontmatter is builtin-only and is converted to calfkit runtime
tool selectors: omitted `tools:` discovers the default live builtin tool nodes,
`tools: []` grants no builtins, and explicit names become `Tools(names=[...])`.

Amendment: bridge-hosted Discord history tools are security-sensitive and are
filtered out of omitted-field discovery. They remain ordinary named tool
capabilities and require an explicit `tools:` grant.
MCP grants moved to a separate `mcp:` field (`server` or `server/tool`) because
MCP has a different secrets boundary and trust model: agents must name MCP
servers explicitly, but the server's internal tool list remains runtime
discovered through `MCPToolbox`.

This is a hard cutover. Legacy `tools: [mcp/...]` is rejected instead of
partitioned so the frontmatter schema cannot keep two meanings for one field.
The served builtin boundary is now the tools host's `ALL_TOOLS` plus deploy
filters; per-agent builtin omission is discovery, not an auditable upper bound.

> **Amended by [ADR-0028](0028-mcp-field-discovers-all-servers-by-default.md):**
> the "agents must name MCP servers explicitly" stance no longer holds — `mcp:`
> is now a tri-state that *discovers every server by default*. The `tools:`
> builtin discovery and the `tools:`/`mcp:` split described here are unchanged.
