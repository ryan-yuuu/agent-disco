# Step-trace redesign: rows, states, and a sealed outcome

Redesign of the live step trace (the visible record of a turn's intermediate
events). Today it renders event *shapes* and discards every payload; this plan
makes it render what the agent actually did, in a grammar borrowed from
opencode and codex, without breaking any invariant ADR-0016/0017/0020 own.

Terminology follows [`CONTEXT.md`](../../CONTEXT.md): **step trace**, **segment**,
**row**, **acting agent**.

## Why

Three inversions in the current renderer, all in `render_step_message()` and one
colour constant:

1. **Payloads are discarded.** `StepEvent` carries `args`, tool-result `text`,
   and handoff `reason`. The trace reads none of them. `reason` and `depth` are
   read by *zero* lines in `src/`. So `❌ \`search_docs\` failed` is the entire
   user-facing story of a failure whose error text was in hand.
2. **Colour carries no signal.** `_V2_ACCENT = 0xE74C3C` stripes every trace
   message, successes included. A clean turn and a failing turn are identical.
3. **The hierarchy is upside down.** The trace is red-boxed and loud; the reply
   is unboxed plain text. The scaffolding outranks the payload.

Plus two correctness bugs the redesign has to fix on the way past:

* **The consult marker lies.** It is emitted at *request* time in the *past
  tense* (`💬 consulted \`peer\``) and never updates. A rejected or faulted
  consult leaves that optimistic line in the human's thread while the `⚠️`/`💥`
  goes only to the audit thread.
* **A block over the cap is unrenderable forever.** `_append()` opens a new
  segment then appends *unconditionally*, so an over-cap block yields a segment
  Discord 400s — swallowed to a WARNING, left `dirty=True`, retried on every
  wake, never rendered. Unreachable at `_V2_CHUNK = 3900`; rendering args and
  error text moves it into reach.

## The grammar

Borrowed from the references, both read at source:

* **One row per action, mutated in place.** Never a "called" line followed by a
  "returned" line. The glyph swaps without the row reflowing — completion is a
  colour change, not a layout event.
* **Completion is a fade.** opencode: `if (props.complete) return theme.textMuted`.
  A row is bright while it happens and dims when it finishes. Their discipline:
  *colour means "something needs you"; everything else is grayscale. Success is
  never green — a successful tool is simply quiet.*
* **One argument promoted to prose, scalar remainder bracketed, non-scalars
  dropped.**

Discord's `-# subtext` (small, dim grey) is the dim register — verified against
Discord's own Components-V2 example payload and its rendered screenshot. It is
used nowhere in the codebase today. Escaping `-#` **is** the attention
mechanism, because Discord offers no per-line colour.

| Event | Rendered |
|---|---|
| tool · pending | `◐ read_file invoices/4417.json` |
| tool · ok | `-# ● read_file invoices/4417.json · 40ms` |
| tool · failed | `❌ read_file invoices/4417.json — no such file` |
| tool · denied | `-# ~~⊘ read_file~~ — superseded by handoff` |
| consult · pending | `◐ consulting conan` |
| consult · ok | `-# ● consulted conan · [view exchange](url)` |
| consult · failed | `❌ conan didn't answer · [view exchange](url)` |
| handoff | `➜ handed off to billing — card expired, billing owns re-runs` |
| seal · ok | `-# 4 tools · 12.3s` |
| seal · faulted | `⚠️ run failed after 4 tools · 12.3s — details below` |

**Denied is struck through, not red** — stolen from opencode, which separates
them deliberately. It matters because `denied` is overloaded: "the caller
refused to dispatch" (worth a look) and "a winning handoff stubbed this sibling"
(routine). Reserving bright for `failed`, where something broke, keeps the
attention budget honest — and fixes the confusing burst a handoff-during-fan-out
produces today.

**The handoff is bright.** It is structural, rare, carries the model's own prose,
and is the last row before the avatar and stripe change.

### The glyph register

Posting the probe surfaced a rule that the HTML mockup could not: **an emoji
renders at ~1.4× and in full colour; a text glyph renders inline and inherits
the line's dimming.** They are two different registers, and the split maps
exactly onto the attention tiers:

| Register | Meaning | Rows |
|---|---|---|
| text glyph + dim (`-#`) | routine, finished | `● ok`, `⊘ denied`, the seal |
| text glyph + bright | routine in flight, or structural | `◐ pending`, `➜ handoff` |
| **emoji** + bright | **needs you** | `❌ failed`, `⚠️ fault` |

Emoji are therefore **rationed to the two attention states** — they are the only
way to get red on a Discord line, and that is precisely what should be spent on
a failure. The `BEFORE` rendering is what happens when the ration is ignored:
six emoji in eight lines, and none of them mean anything.

## Rows are values

`_Segment.rows: list[TraceRow]`, not `list[str]`. Frozen variants + union alias
+ `isinstance`/`assert_never` — the `A2AProjection` idiom already in
`a2a_dispatch.py` / `a2a_project.py`.

```python
RowState = Literal["pending", "ok", "failed", "denied", "interrupted"]

@dataclass(frozen=True, slots=True)
class ToolRow:
    key: str                      # tool_call_id
    name: str
    subject: str                  # the promoted arg
    detail: str                   # bracketed scalars, then the result tail
    state: RowState
    elapsed_ms: int | None = None

TraceRow = ProseRow | ToolRow | ConsultRow | HandoffRow | SealRow

def render_row(row: TraceRow) -> str: ...      # pure, total, exhaustive
```

`body()` folds `render_row` over the rows. Resolving a row replaces
`rows[i]` with a new frozen value.

**Why not keep strings and add an index.** To re-render a row on resolve you
need the call's original name/subject/detail — so that context must be stored
regardless. Storing it *beside* `list[str]` gives you two containers holding one
value, free to drift, with no exhaustiveness check. It is this design built
badly, not a simpler one.

**Why not fold the event log each flush.** Genuinely elegant, and it is what
`_render_tree_blocks` already does (pair by id, order-independent). But a
resolving row changes rendered length, which would retroactively move a segment
boundary that is *already posted*. Segment assignment must freeze at append
time — which collapses it back into this design plus a reservation rule.

`chars` is recomputed rather than incrementally tracked: `n ≤ ~100` rows per
segment, a few appends per second. The O(1) accounting was a micro-optimisation
paid for in correctness.

**The tests stop being brittle.** ~22 tests assert exact rendered strings today,
which is why every cosmetic tweak is a test rewrite. Row values move the state
machine under value assertions, leaving one render test per variant.

## The reservation rule

A posted segment cannot be re-split, so a row that grows on resolve must be
guaranteed to still fit.

For every pending row reserve `_ROW_GROWTH_RESERVE`; the reservation releases as
rows resolve:

```python
def fits(self, row: TraceRow) -> bool:
    pending = self.pending_count + (1 if is_pending(row) else 0)
    return len(self._body_with(row)) + _ROW_GROWTH_RESERVE * pending <= _V2_TEXT_LIMIT
```

The bound holds by construction: a resolved render is the pending render plus a
tail of ` · 40ms` or ` — <detail>`, with detail truncated to `_DETAIL_MAX`, plus
the 3-char `-# ` prefix. **This is an invariant and gets a property test** — for
every row and every state, `len(render(resolved)) - len(render(pending)) ≤
_ROW_GROWTH_RESERVE`.

`_append` also stops appending unconditionally: a row that cannot fit an *empty*
segment is truncated to fit rather than posted and rejected. That closes the
existing dirty-forever bug.

## Row hygiene

Rows are single lines; tool results are not. **A newline in `detail` breaks the
row and, worse, breaks out of `-# ` — subtext is a per-line prefix, so the
continuation renders bright.** That is a visible bug, not a cosmetic one.

`_plain(text)` — pure, in the row module:

1. collapse all whitespace runs (including newlines) to single spaces, strip;
2. escape Discord markdown that would break out (`*`, `_`, `~`, `` ` ``, `|`,
   and a leading `#`/`>`/`-`);
3. truncate to `_DETAIL_MAX` with an ellipsis.

Rows carry **no inline code**. opencode renders tool names as plain text — no
bold, no colour — and dropping backticks also drops the unverified question of
whether `-#` composes with inline code.

## Argument summarisation

opencode's rule, ported. Its subject is hand-picked per tool from a closed
allowlist; ours cannot be, since tools are arbitrary. So:

* **Subject** — the first present key of `("path", "file_path", "filepath",
  "file", "url", "query", "pattern", "command", "cmd", "name")`. Absent → no
  subject.
* **Detail** — the remaining **scalars only** (`str | int | float | bool`) as
  `[k=v, ...]`. Non-scalars are dropped, so a nested object can never bloat a
  row.

Degrades exactly like opencode's `generic` fallback: `⚙ toolname [k=v]`.

## The seal

`finish()` **cannot** know the outcome: it is called in a `finally` around the
drain, while the fault surfaces later in `_await_terminal` → `handle.result()`.
A footer written at `finish()` would render `4 tools · 12.3s` on a crashed turn.

The seam already exists. `RunEvent = RunCompleted | RunFailed | RunStepEvent` —
**the terminal arrives on the stream the drain is already reading**, as its last
item, before the `finally`. `normalize_run_event` returns `None` for it and the
drain does `continue  # terminal — handled by result() below`.

So sealing on the terminal needs **no control-flow change** — no reordering of
`finish()` and `_deliver`. The two readers cannot disagree: `result()` derives
its `NodeFaultError` from that same terminal. The stream tells the trace
*whether* it faulted; `result()` stays the authority for the notice.

`seal(correlation_id, *, faulted)` does three things, all lookups on the one row
index:

1. resolve every still-`pending` row to `interrupted`;
2. resolve dangling consults — `dispatcher.dangling()` already returns exactly
   these, and already carries `tool_call_id`;
3. append the `SealRow`.

**`finish()` seals defensively.** If it finds an unsealed entry it seals as
`interrupted` and logs a WARNING. That covers a drain that raised, a broken
stream, and a calfkit contract violation — turning a permanent `◐` into an
honest trace.

## What stays out of the trace

The fault **notice** does not move into the container. It deliberately rides the
native-reply path because it *"needs only Send Messages and is independent of the
failing webhook path"*. The trace rides the webhook — so when the webhook is the
broken thing (401 bad token, 403 missing Manage Webhooks), the trace delivers
nothing. The split is not redundancy: **the trace seals itself best-effort; the
notice carries detail over the reliable channel.** The seal's `— details below`
is the connective tissue missing today.

`_agent_error_text` needs no redesign. One alignment: render its cause lines as
`-#` so machine detail dims and the header stays bright.

## Accent

**Accent = agent identity, always.** Not state. Red stops being meaningless by
being gone; the fault is carried by the seal row and the notice. This matches
the references, where colour is identity (opencode's agent-coloured `▣` and
owner-coloured rail) and status is glyph and brightness.

`persona_for` derives a DiceBear avatar from the agent name; `accent_for(name)`
sits beside it as the same kind of pure derivation. It stays **off** `Persona` —
ADR-0012 deliberately keeps that a minimal webhook identity (name + avatar), and
the accent is a trace-rendering concern with one caller.

Since a persona change already opens a new segment, each message's stripe is
naturally one agent.

> **A handoff does not reliably read as a colour change.** A curated palette
> collides by design — `aksel` and `billing` both land on green today — so
> roughly one handoff in `len(_ACCENTS)` keeps the same stripe. That is fine,
> and the reason is worth stating: the persona change already swaps the webhook's
> **name and avatar**, so identity never rests on colour. The stripe reinforces
> it; it does not carry it. (An earlier revision of this doc claimed otherwise.)

> **`hash()` is per-process randomised.** `hash(name) % n` would give an agent a
> different colour on every bridge restart. Use `zlib.crc32` (or hashlib) to
> index a small curated palette — hand-picked so every hue is legible on both
> Discord themes, rather than free-hue HSL that can land muddy.

## Module layout

| Module | Holds |
|---|---|
| `bridge/trace_rows.py` *(new)* | Row variants, `RowState`, `render_row`, `_plain`, arg summarisation. Pure: no I/O, no time, no state. |
| `bridge/trace.py` *(was `progress.py`)* | `StepTraceRenderer`, `_Segment`, `_Entry`, the writer loop. Owns the clock and the row index. |
| `bridge/transcript_tree.py` *(was `steps_render.py`)* | `_render_tree_blocks` only — the persisted transcript's step count. |

`steps_render.py`'s docstring opens *"Two render surfaces share this module"*.
They shared `_fence_safe`/`_fenced`; rows are markdown, not code blocks, so the
overlap goes to zero and the split is natural. The tree renderer is untouched —
it guards a separate, byte-stable surface, and its 13 tests stand.

**Time lives in `trace.py`**, never in `trace_rows.py`, which stays pure. The
renderer stamps `time.monotonic()` on append and computes `elapsed_ms` on
resolve; rows carry it as data. The clock is injectable, as `min_edit_interval`
already is.

## Edge cases

| Case | Handling |
|---|---|
| **Parallel fan-out** | calfkit fans out parallel calls, each folding on its own hop, so results arrive in **completion order** with unbounded steps between. Keying by `tool_call_id` handles it; rows stay in call order and resolve out of order, as both references do. |
| **Result in an older segment** | `_flush` already iterates every segment and each holds its own `message_id`, so an older segment simply goes dirty and is edited. Chronology is untouched — the row does not move. |
| **Call crossing a persona boundary** | Cannot happen. A handoff stubs every pending sibling as an adjacent denied pair before transferring, and a consult opens no new segment. Cap rollover mid-flight is the only way a call and its result land in different messages, and it is rare. |
| **Orphan result** (no matching call row) | **Append a resolved row; never raise.** The drain's contract is that *"the render path can't fault the turn"*. The tree renderer already renders orphan returns defensively *"so an orphan return is never silently dropped"*; the live renderer matches it. |
| **Duplicate `tool_call_id`** | Does not occur in well-formed calfkit output. Last resolve wins, as the tree renderer already does. |
| **Multi-line / markdown in a result** | `_plain` — see Row hygiene. Mandatory, not cosmetic. |
| **Agent message over the cap** | Still `_chunk_text`-ed into several `ProseRow`s. Unkeyed, never mutated. |
| **Terminal never arrives** | `finish()` seals as `interrupted` + WARNING. |
| **Mid-run bridge restart** | The only stranding left — a frozen `◐`, no seal. Unfixable: the run dies with the bridge, so no amount of persistence resurrects the stream. Discord's own message timestamp (the *post* time, never updated by edits) already dates the trace, so a strand reads as obviously stale. |
| **Nested consults** | Unchanged. Non-acting emitters are still dropped by `emitter`, never by `depth` — ADR-0020 rejected depth filtering because the root hop's frame-depth semantics are not pinned by any contract calfcord owns. |

## Storage

**No new persistence, and the trace does not ride `state/transcripts.sqlite3`.**

* The durable artifact *is* the Discord message. Nothing is deleted;
  `ChannelHistoryFetcher` excludes v2 messages by flag. Discord is the store,
  and it is write-only from the bridge's side.
* The new state — the row index, call timestamps, a start time — is intra-run
  and has **no reader after `finish()`**. Persisting it would write rows only
  ever read back inside one process lifetime.
* The precedent is set: `A2AProjector._threads` is a `correlation_id → thread_id`
  dict held in memory, the same class of per-correlation display state.
* The store that exists is justified by a **cross-turn reader** — next-turn
  replay hydration needs last turn's `delta_json`. The trace has no equivalent.
* Process affinity is architectural: the bridge is a hard singleton, and the one
  process draining a run is the only one that could render it.

`<t:UNIX:R>` was considered for a self-updating "started N ago" and **cut** —
Discord's native message timestamp already dates the trace, so it was redundant,
and cutting it removes a dependency on undocumented behaviour.

## Invariants preserved

Checkable list — a change that breaks one of these is a non-starter:

* **No `content` on trace messages** (ADR-0016). The history-exclusion invariant
  is the `components_v2` flag, and `tests/bridge/test_history.py:731` pins it.
* **One `TextDisplay` per message.** Text is cheap (4000 chars); components are
  expensive (40 per message, counting every nested node — a `Section` costs 5).
* **A persona change opens a new message.** A webhook edit cannot change
  `username`/`avatar_url`. Not a choice — a Discord limit.
* **Rollover, never elision** (ADR-0017). The full trace survives.
* **Best-effort always.** A failed post stays dirty and retries; a failed edit
  drops without retry and heals on the next append, because every edit
  re-renders the whole body. `CancelledError` stays uncaught.
* **Only the acting agent renders** (ADR-0020). Ownership is load-bearing for
  silence.
* **Nothing is ever deleted.**
* **Traces never ping** (`silent=True`).
* **The render path can never fault the turn.**

## Settled against the live API

Probed by posting the real rows through `send_components` into a scratch
channel — the actual path, not a hand-rolled webhook call. **All three pass; no
fallback is needed.**

| Question | Result |
|---|---|
| Does `~~strike~~` compose inside `-#`? | **Yes**, and it scopes correctly — `⊘ search_docs` strikes, `— superseded by handoff` does not. The denied row keeps its strike. |
| Does a masked link compose inside `-#`? | **Yes** — the link renders blue *and* stays small and dim. The consult row keeps its `[view exchange]` and stays quiet. This was the one with a bad fallback (a routine consult shouting at full brightness), so it was the load-bearing probe. |
| Do consecutive `-#` rows stack tightly? | **Yes, but modestly** — ~33px line pitch against ~38px plain, about 13%. |
| Do the glyphs render? | `◐ ● ⊘ ➜` all render as text glyphs. `❌ ⚠️` render as full-size colour emoji — see *The glyph register*. |

> **Correction worth keeping.** The plan previously called `-#` stacking "the
> whole density argument". It is not. The compression comes from collapsing
> call+result (8 rows → 5, ~37%); subtext adds ~13% on top. `-#` earns its place
> through **hierarchy**, not density — dimming is what makes the reply the
> brightest thing on screen. A future reader tempted to drop `-#` for tightness
> alone would be optimising the wrong axis.

Two unknowns from the original four are gone for good: `<t:R>` is cut
(redundant against Discord's message timestamp), and `-#` + inline code is moot
because rows carry no backticks. *(The masked-link half of that question was
**not** moot and was probed above — an earlier revision wrongly waved it
through.)* The 4000-char cap is already verified against the live API.

The probe script is throwaway and deliberately uncommitted; re-derive it from
this table if the questions ever need re-asking.

## Status: implemented

All of the below landed under TDD on `worktree-step-trace-redesign`.

| Module | Role |
|---|---|
| `bridge/trace_rows.py` *(new)* | Row variants, `render_row`, `_plain`, `_summarise_args`, `_duration`, prose chunking. Pure. |
| `bridge/trace.py` *(was `progress.py`)* | `StepTraceRenderer`, `_Segment` on rows, the reservation, the seal, the writer loop. |
| `bridge/transcript_tree.py` *(was `steps_render.py`)* | `_render_tree_blocks` only — the persisted transcript's step count. |
| `bridge/persona_resolve.py` | `accent_for` beside `persona_for`. |
| `bridge/step_events.py` | `normalize_terminal` — the seal's seam. |

Three corrections the build forced on the design, each recorded above where it
applies:

* **The consult link had to move into the *pending* render.** `TestGrowthReserve`
  failed on first run — a link appearing only on resolve is unbudgeted growth
  that breaks the reservation. The fix is also better UX.
* **A handoff does not reliably read as a colour change** — the palette collides
  by design, and identity rests on name + avatar.
* **The seal needed a third outcome.** `interrupted` is not `faulted`: when the
  stream ends without a terminal the bridge does not know the run failed, and
  there may be no notice to point at.

Deferred, and deliberately not in scope:

- **A `Separator` above the seal.** In the probe the seal (`4 tools · 12.3s`)
  sits at the same weight as a tool row and reads slightly unanchored. A
  `Separator` inside the `Container` is affordable — the budget is 40 components
  and this design spends 2 — but it splits the segment into two `TextDisplay`s,
  which complicates `body()` and the reservation for a purely cosmetic gain.
  Revisit once the grammar is live.
- **Human tool labels.** opencode renders `Read`, not `read_file`, because it
  hand-picks a label per tool from a closed allowlist. Ours are arbitrary MCP
  names, so `read_file invoices/4417.json` runs together slightly. Legible in the
  probe; a per-tool label map would be a guess against tools we do not own.
