# Threaded Private Chat v1 — Implementation Plan

**Status**: Awaiting approval (drafted 2026-05-23)
**Scope**: v1 — Unified A2A audit channel; every `private_chat` invocation lives inside a Discord thread; caller opts in to continuing a prior thread to give the callee multi-turn context.
**Touches**: tools process (`private_chat` + tools runner) and bridge process (`bridge/egress.py` resolver collapses from per-pair to unified; `bridge/history.py` gains a cache-bypass affordance). Agent processes and the router are unchanged.

## 1. Goals

- Replace flat per-pair audit channels with **one unified A2A channel** that contains a thread per A2A conversation. Eliminates the O(N²) channel-count growth and simplifies operator setup to one channel + one permission overwrite.
- Give the **caller agent control** over whether a given A2A invocation continues a prior conversation (callee sees prior turns) or starts fresh (callee sees only the current message). Decision is per-call, opt-in to continue.
- Make the **callee** stateful across A2A turns when (and only when) the caller opts in, by injecting the thread's prior messages as `message_history` projected to the callee's POV.
- Keep the **caller** stateful through its own LLM context — the caller already has its own conversation history and the `thread_id` it received from prior `private_chat` returns; no extra plumbing on the caller side.
- Preserve the existing **audit invariant**: every A2A exchange has a request projection followed by a response projection, in order, visible to humans.

## 2. Non-goals (deferred)

- Cross-pair thread reuse, thread search, thread-list discovery tools. Caller is solely responsible for remembering thread ids across calls (its own LLM context is the storage layer).
- Explicit thread close / archive control from the agent. Discord's auto-archive (1h–7d, configurable per-thread) handles lifecycle; auto-unarchive on post means continuing an old thread "just works."
- Thread-ownership validation. v1 does not check that a passed `thread_id` belongs to this caller↔target pair. A malicious or confused LLM could (in principle) reference an arbitrary thread id; for v1 we trust the LLM and accept the small data-leak surface. Revisit if it ever fires.
- Migration of existing `a2a-{x}-{y}` per-pair channels. Operators may delete them at their leisure; the new resolver simply ignores them.
- Persistence of thread context in any DB/cache beyond Discord. Discord is the source of truth (same posture as the channel-history feature).
- Per-thread permission overwrites, summarization, vector-recall.

## 3. Architecture overview

```
caller agent LLM
    │  invoke private_chat(target, content, thread_id=None)
    ▼
┌─────────────── calfkit-tools process (private_chat body) ───────────────┐
│                                                                         │
│ resolver.resolve_unified_channel()  ──►  unified A2A channel id         │
│                                                                         │
│ thread_id is None?                                                      │
│   ├─ YES (new thread):                                                  │
│   │     1. persona_sender.send(caller_persona, channel, content)        │
│   │           ─► returns SentMessage(message_id=...)                    │
│   │     2. resolver.create_anchored_thread(channel, msg_id,             │
│   │                                       name=f"{caller}→{target}:..") │
│   │           ─► returns new thread_id                                  │
│   │     3. message_history = []                                         │
│   │                                                                     │
│   └─ NO (continue thread):                                              │
│        1. records = await fetch_thread_history(thread_id,               │
│                                  bypass_cache=True, limit=N)            │
│        2. message_history = project_history(records,                    │
│                                  self_agent_id=target_agent_id)         │
│        3. persona_sender.send(caller_persona, channel,                  │
│                              thread_id=thread_id, content)              │
│                                                                         │
│ result = client.execute_node(                                           │
│              topic=agent.{target}.in,                                   │
│              user_prompt=content,                                       │
│              message_history=message_history,                           │
│              deps={discord, caller_agent_id, phonebook},                │
│              temp_instructions=build_temp_instructions(...))            │
│                                                                         │
│ persona_sender.send(target_persona, channel,                            │
│                    thread_id=thread_id, result.output)                  │
│                                                                         │
│ return f"<thread_id>{thread_id}</thread_id>\n{result.output}"           │
└─────────────────────────────────────────────────────────────────────────┘
            │
            ▼
       caller agent LLM continues with the tool's tagged return value
       in its context; on a follow-up turn, the LLM may pass the same
       thread_id back into private_chat to continue, or omit it to fork.
```

The unified channel itself is resolved/created exactly like the old per-pair channels: on cache miss, lazy-look-up by name; on full miss, create. Just one channel total instead of N*(N-1)/2.

## 4. Design decisions (locked from earlier discussion)

| # | Decision | Rationale |
|---|---|---|
| D1 | Caller agent remembers its own `thread_id`s; no companion `list_threads(target=)` tool, no framework-side mapping. | Keeps storage burden where it naturally belongs (caller's LLM context). Zero new state in tools/bridge process. |
| D2 | On continue, callee receives projected thread history as `message_history`. Caller does NOT — it already has the conversation in its own LLM history. | Asymmetric injection matches what each side actually needs and avoids duplicate context. |
| D3 | Thread lifecycle delegated entirely to Discord (auto-archive + auto-unarchive on post). No explicit close, no archived-thread rejection. | YAGNI for v1. |
| D4 | One thread per "topic" between A↔B; caller decides via continue/new. | Matches how humans use threads. |
| D5 | Default behavior = new thread. Continue is opt-in via the `thread_id` parameter. | Conservative default: if the LLM forgets to opt in, the worst outcome is a stateless A2A (today's behavior), not accidental context leak. |
| D6 | Return surface = `f"<thread_id>{tid}</thread_id>\n{response_text}"` (inline tag in str). | Avoids the breaking signature change to a structured pydantic type. LLM parsing of a single `<thread_id>` tag is reliable enough for v1. |
| D7 | Thread name = `{caller}→{target}: {first ~40 chars of content}`, capped at Discord's 100-char limit; newlines normalized to spaces. | Pair-prefix is load-bearing since all pairs share one channel. Topic-tail makes the thread list scannable for humans. |
| D8 | Thread anchored on the persona's first request message (`message.create_thread(name=...)`), not standalone. | The parent channel becomes a useful flat directory of every conversation's first message; standalone threads would leave the parent empty. |
| D9 | Unified A2A channel — one channel for all pairs, not per-pair. | Collapses ~110 LOC of resolver logic, fixes O(N²) channel-count growth, simplifies permission setup, makes audit subscription one-click. |
| D10 | History fetch reuses `ChannelHistoryFetcher` from `bridge/history.py` with a new `bypass_cache=True` kwarg, and `project_history` verbatim. | Reuses error handling + record shape; cache bypass is necessary because we post the caller's request BEFORE fetching is incorrect (see D11), so the cache is the wrong semantics for the A2A flow. |
| D11 | On continue, fetch FIRST, then post caller request, then `execute_node`. (Opposite order from the new-thread branch, where the request must be posted first so it can anchor the thread.) | If we posted first then fetched, the just-posted request would appear in `message_history` AND as `user_prompt` to `execute_node` — a duplicate. Fetch-first avoids it cleanly. |
| D12 | Thread-history limit reuses the agent's existing `history_turns` config (`AgentDefinition.history_turns`). | Single knob for "how much context this agent wants" across both channel and thread sources. |
| D13 | No thread-ownership validation in v1 (no check that `thread.parent_id == unified_channel_id`). | YAGNI; reconsider if a real bug surfaces. |
| D14 | No backwards-compat for per-pair channels. New resolver only knows the unified channel; existing `a2a-{x}-{y}` channels are inert. | Avoids dual-code-path overhead; operators can delete old channels manually. |

## 5. Data model changes

### 5.1 `WireMessage` — unchanged

`private_chat` continues to forward the caller's `incoming_wire` with `slash_target` and `content` rewritten. No new field needed — the `thread_id` lives at the tool API level, not on the wire (the callee doesn't need to know it's running inside an A2A thread; everything it needs is in `message_history` + `deps`).

### 5.2 `A2AChannelResolver` — collapses to unified-channel resolver

**Before** (current — `bridge/egress.py`):
```python
class A2AChannelResolver:
    def __init__(self, sender, guild_id, *, category_name=None): ...
    async def resolve_or_create(self, agent_a_id, agent_b_id) -> int:
        # canonicalizes pair, looks up "a2a-{x}-{y}", creates if missing
```

**After**:
```python
class A2AChannelResolver:
    def __init__(self, sender, guild_id, *, channel_name, category_name=None):
        # channel_name defaults to "a2a-audit" (constant on the class)
    async def resolve_unified_channel(self) -> int:
        # looks up the one channel; creates if missing
    async def create_anchored_thread(
        self, channel_id: int, anchor_message_id: int, *, name: str
    ) -> int:
        # creates a public thread anchored on the given message
```

The class name stays for diff hygiene; only the internals change. The `category_name` kwarg is unchanged — the unified channel can still live under a category (the category just contains one channel now instead of N).

### 5.3 `PrivateChatInit` signature — adds history fetcher

```python
def init(
    *,
    client: Client,
    persona_sender: DiscordPersonaSender,
    resolver: A2AChannelResolver,
    history_fetcher: ChannelHistoryFetcher,   # NEW
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None: ...
```

The tools runner already has the bot client (via `persona_sender.client`) and can construct the `ChannelHistoryFetcher` directly.

### 5.4 `ChannelHistoryFetcher.fetch` — adds `bypass_cache` kwarg

```python
async def fetch(
    self,
    channel_id: int,
    *,
    limit: int,
    bypass_cache: bool = False,    # NEW
) -> tuple[HistoryRecord, ...]: ...
```

When `bypass_cache=True`: skip the LRU read, do a direct fetch, do not populate the cache. Single-flight is still applied (a no-op for the A2A use case but harmless). All other behavior — Forbidden log dedup, error normalization, record shape — unchanged.

### 5.5 `private_chat` return type — string with inline tag

Signature stays `str`. Format:
```
<thread_id>{integer-thread-id}</thread_id>
{response_text}
```

On error (timeout, unknown target, infra), tag is omitted (error strings stay bare so the LLM doesn't mistake the error for a continuable thread).

## 6. Modified module surfaces

### 6.1 `bridge/egress.py` — `A2AChannelResolver` rewrite

- Drop `_canonical_pair`, `_channel_name`, `_discover` (per-pair), `_create` (per-pair). Cache type collapses from `dict[tuple[str, str], int]` to `int | None`.
- Add `resolve_unified_channel(self) -> int` (cached single int).
- Add `create_anchored_thread(self, channel_id, anchor_message_id, *, name) -> int`.
- Channel name comes from constructor (`channel_name: str`), defaulting to `"a2a-audit"`. Sourced from `CALFKIT_A2A_CHANNEL_NAME` env var at the runner.
- `resolve_unified_channel` is `async` for consistency with the create path even though, after the first call, it's a pure dict lookup.
- `create_anchored_thread` propagates `discord.Forbidden` (no Manage Threads), `discord.NotFound` (message gone — race), `discord.HTTPException` (transient). Logged at WARN with channel/message context.

### 6.2 `bridge/history.py` — fetch bypass kwarg

Single-line behavior change: when `bypass_cache=True`, the read-cache branch and the write-back are skipped. Single-flight registration still happens (no harm). Existing call sites unchanged (default `False`).

### 6.3 `tools/private_chat.py` — body rewrite

Net structure of the new body, in order:

1. Validate caller, target, phonebook, `incoming_wire` (unchanged).
2. Build `caller_persona`, `target_persona` (unchanged).
3. `unified_channel_id = await _resolver.resolve_unified_channel()`.
4. Branch on `thread_id`:
   - **None (new thread)**:
     - `sent = await _persona_sender.send(caller_persona, unified_channel_id, content)` — best-effort retry as today.
     - `name = _build_thread_name(caller_agent_id, target_agent_id, content)` (pure helper; see §6.4).
     - `thread_id = await _resolver.create_anchored_thread(unified_channel_id, sent.id, name=name)`. Not best-effort — if this fails the conversation has no thread and continuation is impossible; log ERROR and raise (same posture as the response-projection invariant today).
     - `message_history = []`.
   - **int (continue)**:
     - `records = await _history_fetcher.fetch(thread_id, limit=target_entry.history_turns, bypass_cache=True)`. On `Forbidden`/`NotFound`: log WARN, return recoverable error string (`"error: thread {id} not accessible; start a new one by omitting thread_id"`).
     - `message_history = project_history(records, self_agent_id=target_agent_id)`.
     - `await _persona_sender.send(caller_persona, unified_channel_id, thread_id=thread_id, content=content)` — best-effort retry as today.
5. `result = await _client.execute_node(...)` with `message_history=message_history` and the existing deps/timeout/temp_instructions. Same exception handling as today.
6. `await _post_projection(target_persona, unified_channel_id, response_text, thread_id=thread_id, ...)` — invariant-bound (raises on failure).
7. Return `f"<thread_id>{thread_id}</thread_id>\n{response_text}"`.

### 6.4 `tools/private_chat.py` — new pure helpers

```python
_THREAD_NAME_MAX = 100  # Discord limit
_THREAD_NAME_CONTENT_MAX = 40  # tunable; topic tail ≤ 40 chars

def _build_thread_name(caller: str, target: str, content: str) -> str:
    """Produce a thread name like 'conan→scribe: please summarize the doc'.
    Strips newlines, truncates content tail, caps total at 100 chars."""
```

Unit-testable as a pure function (no Discord, no IO).

### 6.5 `tools/runner.py` — construct fetcher and pass through

- Read new env var `CALFKIT_A2A_CHANNEL_NAME` (default `"a2a-audit"`).
- Construct `ChannelHistoryFetcher(client=persona_sender.client)`.
- Pass it to `private_chat.init(history_fetcher=fetcher, ...)`.

### 6.6 `_post_projection` — accepts optional `thread_id`

Today `_post_projection(persona, channel_id, content, *, caller, target, correlation_id)` only posts to a flat channel. Add `thread_id: int | None = None` and forward to `persona_sender.send(...)`. All retry / branch-on-side logic unchanged.

## 7. File-by-file changes

| File | Change | LOC delta (approx) |
|---|---|---|
| `src/calfkit_organization/bridge/egress.py` | Rewrite `A2AChannelResolver` to unified-channel + `create_anchored_thread` | -60, +50 (net ≈ -10) |
| `src/calfkit_organization/bridge/history.py` | Add `bypass_cache` kwarg on `ChannelHistoryFetcher.fetch` | +10 |
| `src/calfkit_organization/tools/private_chat.py` | Add `thread_id` param, branch on new/continue, inline-tag return, init signature gains `history_fetcher` | +120, -40 (net ≈ +80) |
| `src/calfkit_organization/tools/runner.py` | New env var read, fetcher construction, init call signature | +25 |
| `src/calfkit_organization/agents/peer_roster.py` | Docstring touch — explain the `<thread_id>` return tag in `build_temp_instructions` so peer-aware agents know to keep it | +10 |
| `agents/*.md` (only ones declaring `tools: [private_chat]`) | Brief note in the agent's persona prompt: "private_chat returns `<thread_id>NNN</thread_id>\n…` — reuse the id if you want continuity, omit to start fresh." | +3/file |
| `tests/bridge/test_egress.py` | Rewrite for unified resolver; add `create_anchored_thread` coverage | ≈ ±20 |
| `tests/bridge/test_history.py` | Add `bypass_cache` cases | +30 |
| `tests/tools/test_private_chat.py` | Extend: new-thread path, continue path, tag in return, fetcher injection, error branches | +150 |
| `tests/tools/test_runner.py` | Update for new env var + fetcher injection | +20 |
| `tests/tools/test_a2a_integration.py` | Update for unified channel + thread structure | ≈ ±30 |
| `docs/` | New `docs/a2a-threads.md` documenting the audit-channel layout, operator setup, and the return-tag convention | new file, ~120 lines |

## 8. Modified `private_chat` flow (detail)

Full sequence with error edges marked:

```
private_chat(ctx, target, content, thread_id=None)
│
├─ Validate (unchanged; recoverable errors return "error: ...")
│   ├─ tool initialized?            else _raise_infra
│   ├─ caller_agent_id known?       else _raise_infra
│   ├─ self-target?                 else return "error: ... cannot privately chat with itself"
│   ├─ phonebook present & valid?   else _raise_infra
│   ├─ target in phonebook?         else return "error: unknown agent ..."
│   └─ incoming_wire valid?         else _raise_infra
│
├─ unified_channel_id = resolve_unified_channel()      [raises on Discord error]
│
├─ thread_id is None?
│   │
│   ├─ YES (new):
│   │   request_msg = send(caller_persona, unified_channel_id, content)
│   │       on best-effort failure: log WARN, accept audit gap, continue
│   │   thread_id = create_anchored_thread(unified_channel_id, request_msg.id, name=...)
│   │       on Forbidden/HTTPException: _raise_infra (no thread = no continuation later)
│   │   message_history = []
│   │
│   └─ NO (continue):
│       records = fetcher.fetch(thread_id, limit=N, bypass_cache=True)
│           on Forbidden/NotFound:  return "error: thread {id} not accessible; ..."
│           on HTTPException:        _raise_infra
│       message_history = project_history(records, self=target_agent_id)
│       send(caller_persona, unified_channel_id, thread_id=thread_id, content)
│           on best-effort failure: log WARN, accept gap, continue
│
├─ execute_node(target_topic, content, message_history, deps, temp_instructions, timeout)
│       on TimeoutError:        return "error: target {} did not reply within {}s"
│       on any other Exception: _raise_infra
│
├─ post_projection(target_persona, unified_channel_id, response, thread_id=thread_id)
│       on persistent failure:  _raise_infra (response-side invariant — no reply without audit)
│
└─ return f"<thread_id>{thread_id}</thread_id>\n{response}"
```

## 9. Configuration changes

| Env var | Status | Default | Notes |
|---|---|---|---|
| `CALFKIT_A2A_CHANNEL_NAME` | **NEW** | `a2a-audit` | The single unified A2A channel. Lazy-created on first miss. |
| `CALFKIT_A2A_CHANNEL_CATEGORY` | unchanged | unset | Optional; if set, the unified channel is placed under this category (instead of in the guild root). Semantics identical to today, just applies to one channel now. |
| `DISCORD_GUILD_ID` | unchanged | required | Already required by the tools runner. |

Bot permissions checklist for operators (add to `docs/a2a-threads.md`):

- `View Channel` + `Manage Webhooks` on the unified A2A channel (existing requirement for persona sender).
- `Manage Channels` on the guild (existing — for lazy channel creation).
- **`Create Public Threads` + `Send Messages in Threads`** on the unified A2A channel (NEW — for anchored thread creation and posting into existing threads).
- `Read Message History` on the unified A2A channel (NEW — for the thread-history fetch on continue).

## 10. Error matrix

| Failure | Side | Behavior | Logged |
|---|---|---|---|
| Unified channel resolution fails (Forbidden/HTTPException) | infra | `_raise_infra` — A2A has no audit home | ERROR |
| Caller request projection fails (transient) | best-effort | log WARN, accept gap, RPC proceeds | WARN |
| Thread anchoring fails (Forbidden/HTTPException) on new-thread branch | infra | `_raise_infra` (no thread = no future continuation) | ERROR |
| Caller passes `thread_id` that doesn't exist (404) | recoverable | return `"error: thread {id} not accessible; start a new one"` | WARN |
| Caller passes `thread_id` we lack `Read Message History` on (403) | recoverable | same error string | WARN |
| Thread history fetch hits 5xx | infra | `_raise_infra` | ERROR |
| `execute_node` timeout | recoverable | return `"error: target {} did not reply within {}s"` | WARN |
| Response projection fails (transient → exhausted) | infra | `_raise_infra` (audit invariant) | ERROR |
| Response projection 403/404 on thread | infra | `_raise_infra` (operator-actionable) | ERROR |

Recoverable error strings deliberately do not include `<thread_id>` tags so the LLM does not try to continue an error.

## 11. Test plan

### 11.1 Unit — `tests/tools/test_private_chat.py` (extend)

- `test_new_thread_default_path` — `thread_id=None` → resolver.resolve_unified_channel called once, persona.send to channel (no thread_id), create_anchored_thread called with msg id + correct name, execute_node called with `message_history=[]`, persona.send to thread for response, return value contains `<thread_id>{id}</thread_id>`.
- `test_continue_thread_path` — `thread_id=<int>` → resolve_unified_channel called, fetcher.fetch called with bypass_cache=True, project_history called with self=target, persona.send to thread (with thread_id), execute_node called with non-empty message_history, response posted to thread, return value contains the **same** `<thread_id>` as input.
- `test_continue_thread_fetcher_forbidden_returns_error_string` — fetcher raises `discord.Forbidden` → tool returns `"error: thread ... not accessible..."`, execute_node never called.
- `test_continue_thread_fetcher_not_found_returns_error_string` — same shape for 404.
- `test_continue_thread_fetcher_5xx_raises_infra` — fetcher raises 5xx → RuntimeError.
- `test_new_thread_anchor_fails_raises_infra` — create_anchored_thread raises → RuntimeError; the already-posted request projection is left in place (audit gap is acceptable, not a regression).
- `test_request_projection_failure_best_effort` — first persona.send raises transient HTTPException repeatedly → tool continues, execute_node still called.
- `test_response_projection_failure_raises_infra` — final projection fails persistently → RuntimeError.
- `test_self_target_returns_error` (unchanged).
- `test_unknown_target_returns_error` (unchanged).
- `test_return_tag_format` — exact-format assertion on `<thread_id>{n}</thread_id>\n{response}`.
- `test_return_tag_omitted_on_error` — error strings have no tag.
- `test_empty_response_substitutes_placeholder` (unchanged — verify still works inside thread).
- `test_thread_id_round_trip` — call with `thread_id=None`, parse `thread_id` out of return, call again with that id, assert fetcher called with same id.

### 11.2 Unit — `tests/bridge/test_egress.py` (rewrite)

- `test_resolve_unified_channel_cache_hit` — second call doesn't re-fetch.
- `test_resolve_unified_channel_creates_when_missing` — full miss → create_text_channel called with the configured name + category.
- `test_resolve_unified_channel_uses_configured_name`.
- `test_create_anchored_thread_calls_message_create_thread` — fetches the channel, builds a partial message ref, calls create_thread with the configured name.
- `test_create_anchored_thread_forbidden_propagates`.
- `test_create_anchored_thread_not_found_propagates` — anchor message gone (race).

### 11.3 Unit — `tests/bridge/test_history.py` (extend)

- `test_bypass_cache_skips_read` — pre-populate cache; fetch with `bypass_cache=True` triggers a Discord fetch.
- `test_bypass_cache_skips_write` — fetch with `bypass_cache=True` does not populate the cache for subsequent default-cache reads.
- `test_bypass_cache_still_uses_single_flight` — two concurrent calls with bypass_cache=True coalesce to one Discord fetch (preserves the existing single-flight invariant).
- `test_default_path_unaffected` — regression assertion that the default `bypass_cache=False` flow is byte-identical.

### 11.4 Unit — `tests/tools/test_runner.py` (extend)

- `test_init_passes_history_fetcher` — verify `private_chat.init` receives a fetcher constructed with the bot client.
- `test_env_var_propagates_unified_channel_name` — `CALFKIT_A2A_CHANNEL_NAME=foo` → resolver constructed with `channel_name="foo"`.
- `test_env_var_default_is_a2a_audit` — unset → resolver constructed with `"a2a-audit"`.

### 11.5 Pure-function — new in `tests/tools/test_private_chat.py`

- `test_build_thread_name_basic` — `("conan", "scribe", "please summarize...")` → `"conan→scribe: please summarize..."`.
- `test_build_thread_name_truncates_long_content`.
- `test_build_thread_name_strips_newlines` — content with `\n` → spaces.
- `test_build_thread_name_caps_at_100_chars`.
- `test_build_thread_name_with_unicode_arrow` — verify `→` byte-length doesn't push past Discord's char limit.

### 11.6 Integration — `tests/tools/test_a2a_integration.py` (update)

- Update fixtures to expect a single unified channel + threads, not per-pair channels.
- Add scenarios: caller A→B (new thread), caller A→B (continue with thread_id), caller A→C (independent new thread under same unified channel).

### 11.7 Regression

- All existing `tests/tools/test_private_chat.py` assertions (self-target, unknown target, infra branches, projection retry, empty content placeholder) must continue to pass under the rewrite.
- `tests/bridge/test_outbox*.py`, `tests/bridge/test_ingress*.py`, `tests/bridge/test_pending_wires.py` — untouched; should be 100% green.

## 12. Performance considerations

- **Discord API calls per `private_chat`**:
  - New thread: 1 channel resolve (cached after first) + 1 persona send + 1 create_anchored_thread + 1 execute_node + 1 persona send = **3 Discord REST calls** (was 2 for the per-pair flat-channel case; +1 for the thread anchor).
  - Continue: 1 channel resolve (cached) + 1 history fetch + 1 persona send + 1 execute_node + 1 persona send = **4 Discord REST calls** (history fetch is the new one).
- **Thread-fetch payload**: `history_turns` messages worth of payload per continue. For agent defaults of 10–20 turns, this is sub-50KB at typical message sizes.
- **No new Kafka traffic**; the wire surface is unchanged.
- **Cache bypass** for thread fetches means no LRU benefit, but also no incorrectness. A2A is not a fan-out pattern (one caller per call), so single-flight has no effect either.

## 13. Backward compatibility / migration

- **Breaking for callers**: `private_chat` return value changes from bare `str` to `<thread_id>...\n{response}`. Mitigation: update each agent's `.md` persona file with a one-line note in the tool guidance, and update `peer_roster.build_temp_instructions` to explain the tag at runtime. Agents adapt within their context window without code changes.
- **Breaking for operators**: per-pair `a2a-{x}-{y}` channels are no longer created or read. Existing ones are inert (resolver never touches them). Operators may delete them at any time; the new unified channel is created lazily on first A2A call.
- **Env var**: `CALFKIT_A2A_CHANNEL_NAME` is new; missing env defaults to `a2a-audit` so dev/prod deployments don't need any change at launch.
- **Permissions**: operators must grant `Create Public Threads` + `Send Messages in Threads` + `Read Message History` on the unified channel. Documented in `docs/a2a-threads.md`.
- **No schema migration**: nothing persisted, nothing to migrate.

## 14. Implementation order

Three phases, additive where possible. Sub-agents can split Phase A (mostly mechanical) and Phase B (more entangled). Use opus + xhigh thinking effort per project convention.

### Phase A — additive scaffolding (no behavior change)

A1. **`bridge/history.py`**: add `bypass_cache` kwarg on `ChannelHistoryFetcher.fetch`. Default `False` preserves all current call sites.
A2. **`bridge/history.py` tests**: §11.3 cases.
A3. **`tools/private_chat.py`**: add pure helper `_build_thread_name`. Not yet wired into any flow.
A4. **`tools/private_chat.py` tests**: §11.5 cases.
A5. Full suite green.

### Phase B — wiring (feature goes live)

B1. **`bridge/egress.py`**: collapse `A2AChannelResolver` to unified, add `create_anchored_thread`. Replace `resolve_or_create(a,b)` with `resolve_unified_channel()`.
B2. **`bridge/egress.py` tests**: §11.2 cases (this REPLACES the old test file's per-pair coverage).
B3. **`tools/private_chat.py`**: add `thread_id` parameter; rewrite body per §6.3 (new vs continue branches); update `_post_projection` to forward `thread_id`; init signature gains `history_fetcher`. Update docstring (LLM-facing) to explain the new behavior and return-tag convention.
B4. **`tools/runner.py`**: read `CALFKIT_A2A_CHANNEL_NAME`, construct `ChannelHistoryFetcher`, pass through `init`.
B5. **`agents/peer_roster.py`**: extend `build_temp_instructions` docstring + body if needed to mention the `<thread_id>` return tag.
B6. **`agents/*.md`**: targeted updates to personas that declare `tools: [private_chat]`. Add a 2-line note: how to use `thread_id` for continuity, what the return tag looks like.
B7. **`tools/private_chat.py` tests**: §11.1 + §11.7 (regression).
B8. **`tools/runner.py` tests**: §11.4.
B9. **`tools/test_a2a_integration.py`**: §11.6 updates.
B10. **`docs/a2a-threads.md`**: write the operator-facing doc (audit layout, permissions, return-tag convention, what humans see in Discord).
B11. Full suite green.

### Phase C — review

C1. Run `/pr-review-toolkit:review-pr all parallel` on the full diff with opus + xhigh agents.
C2. Address all critical + important findings.
C3. Re-run review pass for any introduced changes.

### Parallelization

Phase A is small and serial. Phase B can split into two streams:
- **Stream 1**: bridge/egress.py (B1) + tests (B2) — one sub-agent.
- **Stream 2**: tools/private_chat.py (B3) + tools/runner.py (B4) + tests (B7, B8) + integration tests (B9) — one sub-agent.
- **Stream 3** (serial after streams 1 + 2 merge): peer_roster.py (B5) + agents/*.md (B6) + docs (B10) — main agent.

Each sub-agent must be spawned with the opus model and xhigh thinking effort per project CLAUDE.md.

## 15. Open questions / risk register

| # | Question/risk | Disposition |
|---|---|---|
| Q1 | Does `Message.create_thread` work on webhook-authored messages (the persona's first request)? | discord.py docs say yes — webhook messages are real Discord messages. Phase A includes a smoke test in `test_egress.py` to confirm. If it fails for any reason, fall back to standalone-thread creation (option a from earlier discussion) and accept the empty-parent-channel UX as a v1 compromise. |
| Q2 | What if the LLM passes a `thread_id` it received from a different `private_chat` call (different target)? | Per D13, no v1 validation. Worst case: callee sees an unrelated thread's history. Caught only by human auditors. Acceptable risk for v1. |
| Q3 | What if `history_turns` is large (e.g. 50) and the thread has tens of messages — does the Discord history fetch span pages? | `Thread.history(limit=N)` returns up to N messages in a single REST call for N≤100 (Discord's per-call cap). `history_turns` is bounded well below 100. Single fetch suffices. |
| Q4 | What happens if Discord auto-archives an idle thread and the caller continues it months later? | discord.py auto-unarchives on post via the API. The persona send will succeed. The fetch of an archived thread also works (`Read Message History` permission applies regardless of archive state). |
| Q5 | Does the `_persona_sender.send` `SentMessage` return type carry the message id when posting to the parent channel for anchoring? | Yes — verified in `discord/persona.py:359`. `SentMessage.id` is the discord message id. |
| Q6 | What's the cost of doing a Discord fetch on every continue (no LRU)? | One REST call per continue; rate-limit budget is generous; A2A calls are slow anyway (the agent invocation dominates). Negligible. |
| R1 | RISK: Operator forgets to grant `Create Public Threads` permission on first deploy. | Mitigated by: clear error log (`discord.Forbidden` propagates with channel + name context), permissions section in `docs/a2a-threads.md`, infra-raise so the failure surfaces immediately. |
| R2 | RISK: An agent's `.md` is updated to mention the `<thread_id>` tag but a sibling agent's `.md` is missed → inconsistent behavior. | Phase B6 enumerates all agents with `tools: [private_chat]`. Verified by grep before merge. |

## Self-review checklist (verified before sign-off)

- [ ] All 14 design decisions in §4 are reflected in the code (no drift between plan and implementation).
- [ ] `ChannelHistoryFetcher.fetch(bypass_cache=False)` is byte-identical to current behavior (regression test passes).
- [ ] `A2AChannelResolver` rewrite removes pair-canonicalization code paths entirely (no dead code).
- [ ] `private_chat` returns the tagged format on success and bare error strings on every recoverable failure.
- [ ] `create_anchored_thread` is invoked only on the new-thread branch; the continue branch never creates threads.
- [ ] On continue, `fetch` precedes the caller-request projection — no duplicate of `content` in the callee's view.
- [ ] All `discord.HTTPException`, `Forbidden`, `NotFound` paths have explicit logging with `caller`/`target`/`thread_id`/`correlation_id`.
- [ ] No code path silently swallows an exception.
- [ ] `agents/*.md` updates are limited to agents that actually declare `private_chat` in tools.
- [ ] `docs/a2a-threads.md` covers: audit-channel layout, permissions, env vars, return-tag convention, what an auditor sees in Discord.
- [ ] Full test suite (`uv run pytest`) green; lint (`uv run ruff check`) clean on the diff.
- [ ] Post-implementation: `/pr-review-toolkit:review-pr all parallel` clean of critical/important findings.

## What stays out of v1

- Thread search / discovery from the agent side.
- Explicit thread close / archive / pin from the agent side.
- Per-thread permission overwrites.
- Migration of existing per-pair channels (operators delete at leisure).
- Token-budget-based history cap (uses turn-count via `history_turns`, same as channel history).
- A2A summarization or vector recall for very long threads.
- Multi-party A2A (always 1:1 caller↔target).
- A `list_threads(target=)` companion tool.
