# Runtime builtin discovery and split MCP grants

Agent `tools:` frontmatter is builtin-only and is converted to calfkit runtime
`Tools` selectors: omitted `tools:` discovers every live builtin tool node,
`tools: []` grants no builtins, and explicit names become `Tools(names=[...])`.
MCP grants moved to a separate `mcp:` field (`server` or `server/tool`) because
MCP has a different secrets boundary and trust model: agents must name MCP
servers explicitly, but the server's internal tool list remains runtime
discovered through `MCPToolbox`.

This is a hard cutover. Legacy `tools: [mcp/...]` is rejected instead of
partitioned so the frontmatter schema cannot keep two meanings for one field.
The served builtin boundary is now the tools host's `ALL_TOOLS` plus deploy
filters; per-agent builtin omission is discovery, not an auditable upper bound.
