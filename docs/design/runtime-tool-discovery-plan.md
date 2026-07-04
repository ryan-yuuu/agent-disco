# Runtime tool discovery for agents - rescoped spec & implementation plan

**Status:** Implemented in `feat/runtime-tool-discovery`.
**Scope:** Replace build-time builtin-tool resolution in agent construction with
calfkit runtime `Tools` selectors, and hard-cut MCP grants over to a dedicated
`mcp:` frontmatter field. The tools-host deployment remains the explicit
`ALL_TOOLS` surface and is unchanged.

## 1. Motivation

Today builtin tool names and MCP selectors share one frontmatter field:

```yaml
tools: [terminal, mcp/github, mcp/docs/search]
```

That worked while `tools:` was an explicit baked list, but it cannot cleanly
express the new hybrid runtime-discovery mode. In particular, a single field
cannot distinguish:

- "discover every live builtin tool, plus named MCP grants"
- "no builtin tools, only named MCP grants"

The better model is to reflect the architecture in the schema:

- `tools:` controls builtin calfkit tool nodes.
- `mcp:` controls MCP toolbox grants.

Builtins and MCP already have different trust boundaries and discovery behavior.
The split makes that difference explicit without changing the runtime MCP
mechanism: named MCP grants still resolve through `MCPToolbox` against the live
capability view.

## 2. Frontmatter contract

Canonical syntax after this change:

```yaml
# Discover all live builtin tools, no MCP.
# tools omitted, mcp omitted
```

```yaml
# Discover all live builtin tools, plus all live tools from github.
mcp: [github]
```

```yaml
# Restrict builtins, plus one MCP tool.
tools: [read_file, web_search]
mcp: [github/search_issues]
```

```yaml
# MCP-only agent.
tools: []
mcp: [gmail]
```

Field semantics:

| Frontmatter | Internal value | Runtime selectors |
|---|---|---|
| `tools:` omitted | `tools is None` | `Tools(discover=True)` |
| `tools: []` | `tools == ()` | no builtin selector |
| `tools: [read_file]` | `tools == ("read_file",)` | `Tools(names=["read_file"])` |
| `mcp:` omitted | `mcp == ()` | no MCP selector |
| `mcp: []` | `mcp == ()` | no MCP selector |
| `mcp: [github]` | `mcp == ("github",)` | `MCPToolbox("github")` |
| `mcp: [github/search]` | `mcp == ("github/search",)` | `MCPToolbox("github", include=("search",))` |

Hard cutover:

- `tools:` is builtin-only. Any `mcp/...` entry in `tools:` is invalid.
- `mcp:` is the only supported MCP grant field.
- There is no legacy migration path in code. Existing agents using
  `tools: [mcp/...]` must be edited to the new convention.

Accepted behavioral deltas:

- Typo'd builtin names in hand-edited frontmatter no longer fail at agent boot.
  They degrade at runtime through `Tools(names=[...])`.
- CLI writer/editor paths keep local-registry validation for this PR, so the
  same typo entered through the CLI is still rejected.
- Agent processes stop importing `TOOL_REGISTRY`; the live tools host remains
  the runtime authority for builtins.

## 3. Runtime selector construction

`AgentFactory` builds selectors from two independent fields:

```python
selectors = []

if definition.tools is None:
    selectors.append(Tools(discover=True))
elif definition.tools:
    selectors.append(Tools(names=builtin_names_only))

selectors.extend(mcp_selectors_from(definition.mcp))
```

Important details:

- `Tools(discover=True)` appears only for omitted `tools:`.
- `tools: []` means no builtin tools, even if `mcp:` is present.
- MCP is never wildcard-discovered across all servers. It is named by `mcp:`.
- A bare `mcp: [server]` still means "all live tools currently advertised by
  that named server"; the server's internal tool list remains runtime-discovered.
- The memory guard checks only builtin names. `mcp:` cannot satisfy
  `read_file` / `write_file`.

## 4. Code changes

### `src/calfcord/agents/definition.py`

Add a new field:

```python
mcp: tuple[str, ...] = ()
```

`tools:` becomes builtin-only. Validation changes:

- `tools:` continues preserving `None` vs `()`.
- `tools:` rejects any `mcp/...` token with a clear error telling the operator
  to move MCP grants to `mcp:`.
- `mcp:` validates entries in canonical field form:
  - `server`
  - `server/tool`
- `mcp: []` normalizes to `()`.
- malformed `mcp:` entries aggregate into one validation error, matching the
  current `tools:` MCP selector behavior.

### `src/calfcord/mcp/selector.py`

Replace the agent-frontmatter parser surface with canonical `mcp:` values:

- `github` -> `("github", None)`
- `github/search` -> `("github", "search")`

The `mcp/...` prefix can disappear from authoring-facing helpers. If any
lower-level tests still need the old prefixed parser, delete or rewrite them
rather than keeping a compatibility layer.

### `src/calfcord/mcp/agent_select.py`

Replace `selectors_from_entries()` with a canonical helper that accepts
`mcp:` entries directly. Merge semantics stay the same: a bare server subsumes
explicit tool picks, explicit picks dedupe into sorted `include`, and servers
sort deterministically.

### `src/calfcord/agents/factory.py`

Already partially changed to runtime selectors. Rescope the selector builder:

- builtin names come from `definition.tools`.
- MCP selectors come from `definition.mcp`.
- omitted `tools:` + non-empty `mcp:` now correctly yields
  `[Tools(discover=True), MCPToolbox(...)]`.

Update logging to show both surfaces, e.g.:

- `discover:*`
- `tools:['read_file']`
- `mcp:github`

### `src/calfcord/agents/loader.py`

No meaningful new work beyond the already-completed change: omitted `tools:`
stays `None`. `mcp:` is parsed by `AgentDefinition`.

### `src/calfcord/agents/md_writer.py`

This grows from "rewrite one tools list" into "rewrite tool grants":

- allow `tools=None` to remove the `tools:` key, expressing builtin discovery;
- write `tools: []` for explicit no builtins;
- validate builtin names against `TOOL_REGISTRY` on CLI write paths;
- validate canonical `mcp:` entries syntactically;
- remove `mcp:` when empty for canonical output;
- reject any `mcp/...` token passed to a builtin-tools writer path.

Recommended API shape:

```python
update_tool_grants(path, *, tools: Sequence[str] | None, mcp: Sequence[str] = ())
```

Keep `update_tools()` only if it remains a builtin-only convenience wrapper.
It should reject `mcp/...` tokens rather than partitioning them.

### `src/calfcord/cli/_agents.py`

`pick_tools()` currently returns one flat list containing builtin names and
`mcp/...` rows. Change it to return a structured selection:

```python
@dataclass(frozen=True)
class ToolGrantSelection:
    tools: list[str] | None   # None means discover builtins
    mcp: list[str]            # canonical entries: github, github/search
```

Rules:

- all builtin rows selected -> `tools=None`
- a builtin subset selected -> `tools=[...]`
- no builtin rows selected -> `tools=[]`
- selected `mcp/<server>` row -> `mcp=["server"]`
- selected `mcp/<server>/<tool>` row -> `mcp=["server/tool"]`

`write_agent()` writes both fields. New files omit `tools:` when `tools is None`
and omit `mcp:` when empty.

### `src/calfcord/cli/agent_tools.py`

The checkbox UI can stay a single "Tools" editor, but internally it should
round-trip two fields:

- pre-check builtins from `raw.tools`, treating omitted as all builtins;
- pre-check MCP rows from `raw.mcp`;
- on save, write canonical split fields.

No-op editing of a discover-mode agent with MCP grants should preserve:

```yaml
mcp: [github]
```

and keep `tools:` omitted.

### `src/calfcord/cli/agent_lifecycle.py`

`agent set --tools` remains builtin-only. If the comma-separated value includes
`mcp/...`, reject it with a clear error telling the operator to edit `mcp:`
or use the interactive tools editor once it supports the split write.

Adding a first-class `--mcp` flag is useful but not required for this PR. If
added, it touches `FIELDS`, argparse, tests, and docs; defer unless we want a
larger CLI surface change.

### Inspect/render surfaces

Update these so users can see both fields:

- `src/calfcord/cli/_fields.py`
- `src/calfcord/cli/agent_inspect.py`
- related tests

Recommendation: keep one human "Tools" row that summarizes builtins and MCP,
but include both `tools` and `mcp` separately in JSON output.

### Docs and ADR

Update:

- `agents/agent.template.md`
- `docs/authoring-agents.md`
- `docs/mcp-tools.md`
- `docs/distributed-deployment.md`
- `docs/security.md`
- `docs/architecture.md`
- `src/calfcord/tools/__init__.py` docstring
- `src/calfcord/mcp/__init__.py`, `agent_select.py`, `selector.py`,
  `capability_read.py`, `config.py`, and `runner.py` docstrings/comments
- `src/calfcord/agents/factory.py` and `definition.py` docstrings

Do not update historical design docs unless they are serving as live guidance.
They can retain old context if clearly superseded by this plan and the ADR.

Add an ADR for:

- runtime builtin discovery;
- split builtin/MCP frontmatter fields;
- hard cutover from `tools: [mcp/...]` to `mcp:`;
- trust-model change: `tools:` omitted is no longer an auditable per-agent
  upper bound; the tools host's `ALL_TOOLS` + deploy filters are the served
  builtin boundary, and `mcp:` remains explicit per-agent external-tool grant.

## 5. Test impact

Already-completed / still valid:

- factory selector tests for `Tools(discover=True)`, `Tools(names=[...])`,
  empty `tools: []`, and memory guard retargeting;
- loader tests expecting omitted `tools:` to stay `None`.

New or changed tests:

- `tests/agents/test_definition.py`
  - `mcp:` field accepts `github` and `github/search`;
  - malformed `mcp:` entries aggregate;
  - `tools: [mcp/github]` is rejected with a move-to-`mcp:` message;
  - unknown bare builtin names still parse.
- `tests/agents/test_factory.py`
  - omitted `tools:` + `mcp: [github]` yields discover builtins plus
    `MCPToolbox("github")`;
  - `tools: []` + `mcp: [github]` yields MCP-only;
  - memory guard ignores `mcp:`.
- `tests/agents/test_md_writer.py`
  - can remove `tools:` by writing `tools=None`;
  - can write / remove `mcp:`;
  - rejects `mcp/...` values in builtin-only writer paths;
  - preserves atomic validate-before-write.
- `tests/cli/test_agent_tools.py`
  - no-op edit of omitted `tools:` writes omitted, not full list;
  - selected MCP rows write `mcp:`.
- `tests/cli/test_init.py` and `tests/cli/test_agent_create.py`
  - all builtins selected writes no `tools:` line;
  - MCP selections write `mcp:`;
  - subset and empty builtin behavior unchanged.
- `tests/cli/test_agent_inspect.py`, `tests/cli/test_fields.py`,
  `tests/cli/test_agent_lifecycle.py`
  - inspect/render/set behavior for split fields.
- `tests/mcp/test_selector.py` and `tests/mcp/test_agent_select.py`
  - canonical `mcp:` parser / selector helper.
- Repository examples/templates
  - `agents/agent.template.md` documents `tools:` as builtin-only and `mcp:`
    as canonical MCP grant syntax;
  - committed agent `.md` files contain no legacy `tools: [mcp/...]` entries.

## 6. Scope impact

This does meaningfully expand the work, but not the runtime architecture.

The earlier plan touched mainly:

- factory/loader/definition;
- factory/loader tests;
- writer/editor fixes to avoid pinning discover agents;
- docs/ADR.

The split-field plan adds:

- one new schema field and validation path;
- one small MCP parser/helper path;
- structured writer/editor return values instead of one flat list;
- inspect/render updates so `mcp:` is visible;
- additional CLI tests around canonical output.

Estimate:

- Core runtime: small increase, about 2-3 additional implementation spots.
- CLI/write surfaces: moderate increase, because the flat selected list must
  become structured and atomically written as two fields.
- Docs/tests: moderate increase, because the public authoring contract changes.

This is still the better design. The extra work is mostly schema hygiene and
round-tripping, and it avoids a worse long-term ambiguity in the `tools:` field.

## 7. Sequencing

1. Red: update definition/factory tests for `mcp:`.
2. Green: add `AgentDefinition.mcp`, canonical MCP parser/helper, and factory
   combination logic.
3. Red: update writer/editor/create/init tests for split output.
4. Green: implement structured tool-grant writing and split-field CLI output.
5. Update inspect/render tests and code.
6. Update docs/docstrings and add ADR.
7. Run focused tests, coverage on touched modules, ruff, then full pytest.

## 8. Risks / non-goals

- Legacy flat MCP syntax is not accepted after this cutover.
- No `MCPToolbox(discover=True)` or all-server wildcard is introduced.
- No live capability-view validation for builtin writer rows in this PR; the
  CLI remains registry-backed for B1.
- Adding a dedicated `disco agent set --mcp` flag is deferred unless explicitly
  pulled into scope.
