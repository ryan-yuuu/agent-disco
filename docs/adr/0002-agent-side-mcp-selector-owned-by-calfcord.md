# Agents resolve MCP tools via a calfcord-owned selector, never via MCPToolbox

**Status: superseded.** calfkit 0.9.1 shipped the public by-name constructor
this ADR was waiting for (`MCPToolboxRef`,
[calfkit-sdk#212](https://github.com/calf-ai/calfkit-sdk/issues/212)), and the
hand-rolled `McpToolSelector` was deleted
([#41](https://github.com/ryan-yuuu/calfcord/issues/41)). What this ADR
defends survives the swap: agents still never construct an `MCPToolbox`
(`MCPToolboxRef` is identity-only — no connection params, no secrets), the
import-isolation test still pins the boundary, resolution stays non-strict
(the upstream default, never overridden), MCP stays an explicit per-agent
grant outside the builtins default, and the phonebook still strips `mcp/`
selectors. The per-server merge of frontmatter entries remains calfcord-owned
in `calfcord.mcp.agent_select.selectors_from_entries`. The original record
follows.

Agents turn `mcp/...` frontmatter entries into calfcord's own `McpToolSelector`
(`calfcord/mcp/agent_select.py`) — a ~10-line implementation of calfkit's
public `ToolSelector` protocol over `resolve_capability` — instead of using the
SDK's obvious surface, `Agent(tools=[toolbox])` / `MCPToolbox.select()`.
Constructing an `MCPToolbox` requires `connection_params`, i.e. `mcp.json` and
the secrets inside it, and on a distributed deploy agent hosts deliberately
have neither: only `mcp-<server>` processes read that file. The selector is a
pure capability-view lookup, so the secrets boundary holds with zero schema or
config shipped to agent hosts (pinned by `tests/mcp/test_import_isolation.py`).

## Consequences

- Resolution is **non-strict by design**: an agent boots and answers normally
  when its MCP servers are down; the affected tools drop out of that turn with
  a calfkit WARNING. Declaring `mcp/github` must not hold the agent hostage to
  the github server's uptime ("nothing runs that you didn't start").
- MCP tools never ride the "`tools:` omitted → all builtins" default — they
  are always an explicit per-agent grant, and the phonebook strips `mcp/`
  selectors (MCP tools are not A2A peers).
- The selector duplicates calfkit's private `_ScopedSelector`. This is a
  tracked stopgap, **not** a preference: replace it with the upstream by-name
  constructor when [calfkit-sdk#212](https://github.com/calf-ai/calfkit-sdk/issues/212)
  ships (calfcord tracking issue:
  [#41](https://github.com/ryan-yuuu/calfcord/issues/41)).

Do not "fix" agent wiring to use `MCPToolbox` directly — that re-couples agent
hosts to `mcp.json`. Design: `docs/design/mcp-reintroduction.md` §D4.
