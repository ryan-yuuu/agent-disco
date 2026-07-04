# Plan: Persistent Components-V2 step messages

Status: **Accepted / implemented** · Recorded as
[ADR-0016](../adr/0016-persistent-v2-step-messages.md) · Supersedes decision
**D-1a** in [`step-transcripts-and-live-streaming-plan.md`](./step-transcripts-and-live-streaming-plan.md)

## 1. Intent

Replace the current live-progress mechanism — a single **transient** message per
turn that is edited (debounced) then **deleted** at the end — with **persistent,
per-step Discord messages** rendered as **Components V2** (a **LayoutView**). Each
intermediate step event posts its own message under the emitting agent's persona
and **stays** after the run. The on-demand "N steps" button is **removed**.

Agent memory is unchanged: the structured tool-call transcript replay remains the
sole source of the model's tool context.

## 2. Motivation

- A durable, readable, inline trace of what the agent did — no button click, no
  disappearing message.
- Removes fragile lifecycle state in the current renderer (a restart strands the
  transient message; a late debounced edit can race the delete).
- **Components V2 messages carry no `content`** (Discord forbids `content`/`embeds`
  when the v2 flag is set), so they are naturally excluded from Discord-fetched
  history — killing the double-count problem — while the structured transcript
  replay continues to feed the model.

## 3. Verified facts (discord.py 2.7.1, empirically confirmed)

- **Webhooks send Components V2.** The webhook payload builder auto-sets the
  `components_v2` flag when the view has v2 items
  (`discord/webhook/async_.py:599-603`). Confirmed live: a **LayoutView** sent
  through an app-owned persona webhook is accepted and rendered.
- **Roundtrip marker.** Fetched v2 messages come back with `content == ""` **and**
  `flags.components_v2 == True` (`discord/flags.py:547`, value `32768`). Both are
  reliable exclusion signals.
- **`silent=True`** is supported on the webhook send path (`async_.py:1858`).
- **Character limits (empirically established):**
  - Each **TextDisplay**: **1–4000** chars (empty is invalid).
  - Whole message (sum across **all** containers/text displays): **≤ 4000**. This
    is **per-message, not per-container** (two containers of 3000 each = 6000 was
    rejected; two of 2000 = 4000 accepted).
  - **discord.py does not enforce this client-side** — Discord returns HTTP 400.
    We must enforce ≤ 4000 ourselves before sending.

## 4. Design decisions (locked)

| # | Decision | Rationale |
|---|---|---|
| D1 | Each renderable step event = one persistent message (no edit, no delete). | User requirement; removes lifecycle state. |
| D2 | Rendered as **LayoutView** → **Container**(`accent_colour = discord.Colour(0xE74C3C)`) → **TextDisplay**(s). | User-specified style/colour; verified webhook-compatible + server-accepted. |
| D3 | Posted under the emitting agent's persona: `username = persona.name`, `avatar_url = persona.avatar_url` (resolved per step via **persona_for**`(step.emitter)`), `silent=True`. | Correct attribution after handoffs; no notification spam. |
| D4 | Tool call and tool result are **separate** messages (one per stream event). | Matches "each step event = a message"; gives live call-then-result feedback; avoids buffering state. |
| D5 | Content per event kind — see the complete coverage matrix (§5.1). Tool names rendered **monospace** (`` `read_file` ``), not bold. | Short-form for tools, full prose for agent text; monospace tool names per user. |
| D6 | History fetch **drops** persona-webhook messages where `flags.components_v2` is `True` (belt-and-suspenders with the existing empty-content skip). No text sentinel. | The message *type* is the marker; sidesteps any content-roundtrip fragility. |
| D7 | Keep the transcript write + tool-call replay exactly as-is. | Sole source of the model's structured tool memory now. |
| D8 | Remove the "N steps" button + its persistent view. | Steps are now always visible. |
| D9 | Every emitted step kind is explicitly rendered (§5.1); unknown/future kinds hit a safe no-op fallback. Typing indicator **disabled** (commented out, not deleted) for now. | Completeness + robustness; typing paused per user. |
| D10 | **Handoff** renders in the **main** step stream **only**, as `➡️ handed off to <target>` (bare name, no reason). The **A2ADispatcher** no longer claims `handoff` and the **A2AHandoff** audit projection is **removed**. `message_agent` consults still project to the A2A messaging/audit channel. | A handoff transfers conversation control (the peer replies in your place, ADR-0019) — distinct from a `message_agent` consult (agent keeps control, ADR-0015). The user should see the control transfer inline; the consult trail stays in the A2A channel. |

## 5. Data flow (upstream unchanged)

`MentionHandler.handle` drains `handle.stream()` →
**normalize_run_event** → **StepEvent** → **A2ADispatcher.classify** → **message_agent**
consults project to the A2A audit channel; **handoffs and all other steps** call
**progress.on_step**. The change is inside **on_step** (post a v2 message instead of
accumulate/edit), **finish** (becomes a no-op), and the dispatcher (no longer claims
handoffs).

Three concerns stay cleanly separated:
- **Display (humans):** persistent v2 step messages — *this plan*.
- **Memory (model):** transcript write in the reply poster + replay in the history
  provider — *unchanged*.
- **History fetch** excludes v2 messages, so display never pollutes memory.

### 5.1 Event-type coverage (complete)

calfkit's stream is `RunEvent = RunCompleted | RunFailed | RunStepEvent`
(`calfkit/client/events.py:64`). **RunStepEvent** is the only intermediate union and
has exactly **four emitted** members (`calfkit/models/step.py:173`); a fifth,
**AgentThinkingEvent** (`agent_thinking`), is *defined but never emitted/surfaced in
v1 and not re-exported* (`step.py:165-170`), so it cannot currently be streamed.
**normalize_run_event** maps events → **StepEvent**; the **A2ADispatcher** then removes
only **message_agent** *consult* traffic (a Peer message — the agent keeps control and
answers you itself, ADR-0015) to the audit channel. **Handoffs** (transfer of
conversation control — the peer replies in your place, ADR-0019) now flow through to
the progress renderer.

| calfkit event (fields) | kind | A2A-claimed? | Reaches progress? | Rendered as |
|---|---|---|---|---|
| **AgentMessageEvent** (`parts`) | `agent_message` | no | yes | full prose, chunked ≤ 4000 (empty → nothing) |
| **ToolCallEvent** (`name`, `args`) | `tool_call` | only `name == "message_agent"` | yes (non-consult) | `🔧 ` + monospace name + ` called` |
| **ToolResultEvent** (`name`, `parts`, `is_error`) | `tool_result` | only if it matches an open consult | yes (non-consult) | `✅ ` / `❌ ` + monospace name + ` returned`/` errored` |
| **HandoffEvent** (`target`, `reason`) | `handoff` | **no** (dispatcher no longer claims it) | **yes** | `➡️ handed off to ` + monospace **bare** target (leading `/` stripped); `reason` ignored |
| **AgentThinkingEvent** (`parts`) | `agent_thinking` | — | **not emitted in v1** | documented extension point only (no speculative code) |
| **RunCompleted** / **RunFailed** | — | — | no (terminal) | `normalize_run_event` → `None`; reply poster / fault path |

**Robustness guarantee.** The renderer switches on `step.kind` with an explicit branch
for each progress-reachable kind (`agent_message`, `tool_call`, `tool_result`,
`handoff`) plus a **default fallback** (log once, emit nothing) so an unexpected or
future kind can never crash the drain or corrupt output. If a later calfkit version
emits **agent_thinking**, the change is localized: one map branch in
**normalize_run_event** + one render branch — no other files.

## 6. Changes per file

### 6.1 `bridge/progress.py` — rewrite (net deletion)
- Delete the lifecycle: **ProgressEntry**, the `_entries` dict, the debounce task,
  **_edit**, **_delete**, **_cancel_debounce**, **_schedule_debounced_edit**, and
  the accumulate-then-post **_post**.
- New **on_step**`(step, req)`: resolve `persona = persona_for(step.emitter)`; render
  the step to a list of message bodies (§5.1); for each body, best-effort post one v2
  message under the persona into `req.thread_id` (else the parent channel).
- The typing fire is **commented out** (left in place, not deleted). The
  **TypingNotifier** wiring in the gateway stays but goes dormant — a one-line
  re-enable later.
- **finish**`(correlation_id)` → **no-op** (kept to satisfy the **ProgressRenderer**
  protocol and the handler's `finally`, minimizing handler churn).
- Keep the best-effort wrapper (swallow `NotFound`/`Forbidden`/`DiscordException`;
  never let a step post crash the drain or affect the reply).
- Holds the **_StepView**(**LayoutView**) builder: one **Container**(accent) with one
  **TextDisplay** per body.

### 6.2 `bridge/steps_render.py` — new renderer, remove live-tail
- Add **render_step_message**`(step) -> list[str]`, switching on `step.kind` (§5.1):
  - `tool_call` → one body: `🔧 ` + the tool name in **monospace** (backticks) + ` called`.
  - `tool_result` → one body: `✅ `/`❌ ` + monospace name + ` returned`/` errored`.
  - `agent_message` → chunk the full text to ≤ `_V2_CHUNK` chars → one entry per
    message (split on paragraph/line boundaries; hard-split a single over-long line);
    empty/whitespace → `[]`.
  - `handoff` → one body: `➡️ handed off to ` + the **target** agent name in monospace,
    with any leading `/` stripped (bare name). The `reason` is **not** rendered.
  - **default** → log once + return `[]` (never crash on an unknown/future kind).
- Remove: **render_step_line**, **_progress_content**, **_tail_window**,
  `_PROGRESS_DEBOUNCE_SECONDS`, the `_LIVE_*` caps, `_HIDDEN_STEPS_MARKER`.
- Keep: **_render_tree_blocks** (still used to count steps for the transcript write).
- New constants: `_V2_ACCENT = discord.Colour(0xE74C3C)`, `_V2_MAX_CHARS = 4000`,
  `_V2_CHUNK = 3900` (headroom).

### 6.3 `discord/persona.py` — add v2 send path, remove edit/delete
- Add **send_components**`(persona, channel_id, view, *, thread_id) -> SentMessage`:
  `webhook.send(view=view, username=persona.name, avatar_url=<avatar or MISSING>,
  thread=<thread or MISSING>, wait=True, silent=True)`. **No `content`/`embeds`**.
- Remove **edit_message** and **delete_message** (now dead).
- Keep **send** (used by the reply poster). Drop its now-unused `extra_buttons`
  parameter and the button-view branch (see 6.5).

### 6.4 `bridge/history.py` — exclude v2 step messages (correctness-critical)
- In the fetch→record path (**_do_fetch**/**_to_record**), skip messages where
  `getattr(msg.flags, "components_v2", False)` is `True` and the message is a persona
  webhook post — so neither **build_message_history** nor **_build_replay_hydration**
  ever sees them.
- The existing empty-content skip in **build_message_history** remains as a second
  line of defence.
- Document the invariant: *persona-webhook Components-V2 messages are display-only
  step traces and are never part of history.*

### 6.5 Remove the "N steps" button
- Delete `bridge/steps_toggle.py`.
- `bridge/reply_poster.py`: remove the **build_toggle_button** import and the
  `extra_buttons` construction in **post_reply** and **post_chunked**; drop the
  `extra_buttons` threading through **_send_with_one_retry_on_outage**. **Keep**
  **_render_step_count**, the `write_transcript` gate, and **_write_transcript**.
- `bridge/gateway.py`: remove the **StepsToggleView** import and the
  `add_view(StepsToggleView(...))` registration in `_on_ready`.
- `bridge/steps_render.py`: remove **_pluralize_steps** (button label only).

### 6.6 Config / settings
- No new settings. The transcript store (`DISCORD_TRANSCRIPT_DB_PATH`,
  `DISCORD_TRANSCRIPT_RETENTION_DAYS`) stays — it still backs tool-call replay and
  `/thinking-effort` overrides; only its "expand toggle" role is retired.
- `Read Message History` permission is **still required** — we still fetch history
  from Discord (persistence stays on Discord).

### 6.7 `bridge/a2a_dispatch.py` (+ `a2a_project.py`) — route handoffs to the step stream
- Remove the `handoff` branch from **A2ADispatcher.classify** so it returns `None` for
  a handoff → the step falls through to **progress.on_step** and renders inline
  (`➡️ handed off to <target>`, bare name, no reason).
- **Decided: handoffs are main-channel only.** Delete the now-dead **A2AHandoff**
  projection — the `isinstance(projection, A2AHandoff)` branch in **a2a_project.py** and
  the **A2AHandoff** dataclass — and narrow `A2AProjection` to
  `A2ARequest | A2AReply | A2AReject`. (Grep for other **A2AHandoff** references, e.g.
  tests / mention_handler imports.)
- **message_agent** consults/replies are **unchanged** — still projected to the A2A
  messaging/audit channel (**A2ARequest** / **A2AReply** / **A2AReject**).

## 7. TDD test plan (tests first, per `/test-driven-development`)

- **render_step_message** (pure): tool call (monospace name); tool result ok/error;
  short agent text (1 chunk); agent text > 4000 (N chunks, each ≤ limit, boundary-split,
  nothing lost); empty agent text → `[]`; every emitted body is 1–4000 chars; a
  `handoff` step → `➡️ handed off to <target>` (bare name — leading `/` stripped, no
  reason); an **unknown/synthetic kind → `[]`** (fallback never raises).
- **a2a_dispatch**: a `handoff` step is **not** classified as A2A (returns `None`, so
  it reaches progress); `message_agent` consult/reply classification is unchanged.
- **ProgressRenderer** (fake persona sender): one v2 message per renderable step;
  correct persona per emitter; thread routing; `silent=True`; agent-text chunk → N
  messages; non-renderable step → no send; send failure swallowed (drain survives);
  **finish** performs no delete; **no typing fired** (commented out).
- **persona.send_components**: builds a **LayoutView**, calls **webhook.send** with
  view + username + avatar_url + thread + `wait=True` + `silent=True`; returns
  **SentMessage**; avatar MISSING fallback when `persona.avatar_url is None`. (Delete
  the **edit_message**/**delete_message** tests.)
- **history exclusion**: a fetched v2 persona message is dropped from records and the
  built history; a normal persona reply (`components_v2 == False`) is kept;
  **_build_replay_hydration** never sees v2 messages.
- **reply_poster**: no toggle button attached; transcript still written when steps
  are present.
- **gateway**: **StepsToggleView** no longer registered (replace the existing
  registration test); **ProgressRenderer** still wired with the persona sender.
- Close coverage with `/pytest-coverage`; Ruff-clean all changed files.

## 8. Failure semantics & edge cases

- **Best-effort posting.** A `429`/`5xx`/`NotFound` on a step message is swallowed
  (that line just doesn't appear); the terminal reply is independent and unaffected.
- **Rate limits.** N tool calls → ~2N messages; `silent=True` avoids pings, and
  discord.py backs off webhook 429s. The drain awaits each send, so a sustained
  rate-limit can *delay* the reply (drain must finish first). Acceptable under
  best-effort; a fire-and-forget-with-ordering optimization is noted as future work.
- **Empty TextDisplay is invalid** (min 1): the renderer never emits an empty body.
- **Thread routing** matches today: post into `req.thread_id` when the wire
  originated in a thread, else the parent channel.
- **Restart mid-run:** already-posted step messages persist (intended). No stranded
  transient message anymore — a net improvement.

## 9. ADR (to author alongside the change)

New ADR — *"Persistent Components-V2 step messages; Discord fetch excludes them;
transcript replay remains the memory source."* Reverses **D-1a** (transient progress
+ reply toggle). Record: context; decision; consequences (durable trace, 4000-char
cap, the v2-flag exclusion invariant, button removed, handoffs now rendered inline in
the main stream instead of the A2A audit channel, `Read Message History` still
required); alternatives considered and rejected (keep edit-in-place; dedicated
per-turn thread; zero-width content sentinel; move persistence to MySQL). Follow
`.agents/skills/grill-with-docs/ADR-FORMAT.md`.

## 10. Docs to update (`/diataxis-docs-writer`)

- `step-transcripts-and-live-streaming-plan.md` — mark the live-streaming and toggle
  sections superseded.
- `docs/architecture.md` — update the live-progress + steps description.
- `docs/configuration.md` — retire the "⤵ expand toggle" wording for the transcript
  store (keep the env vars; it still serves replay + overrides).
- README / any user-facing mention of the steps button.

## 11. Sequencing (each milestone independently landable)

1. **render_step_message** + tests (pure, no Discord).
2. **persona.send_components** + tests.
3. **ProgressRenderer** rewrite + the **A2ADispatcher** handoff change + tests.
4. **history.py** exclusion + tests (correctness-critical).
5. N-steps button removal + test updates (could be its own PR).
6. Gateway wire-through + an integration test (mention → step messages posted →
   excluded from next-turn history).
7. ADR + docs.

## 12. Out of scope

- Moving persistence off Discord (MySQL) — dropped.
- Combining call + result into one message — chose separate.
- Fire-and-forget ordered posting for throughput — future.
- A feature flag to disable steps — not requested (always on today).

## 13. Resolved decisions (previously open)

- **Typing indicator:** **disabled** for now — the fire call is commented out (not
  deleted); the **TypingNotifier** wiring stays dormant for a one-line re-enable.
- **Tool-name formatting in the message:** **monospace** (backticks) — e.g.
  `` 🔧 `read_file` called ``.
- **Event coverage:** all four emitted step kinds are accounted for (§5.1), with a
  safe fallback for unknown/future kinds; **agent_thinking** is a documented,
  code-free extension point (not emitted in v1).
