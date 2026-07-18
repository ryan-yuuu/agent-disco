# Nested-consult affordance in the A2A audit thread

**Status:** Proposed
**Date:** 2026-07-17
**Related:** [ADR-0026](../../adr/0026-the-a2a-thread-records-the-consulted-sub-tree.md) · [ADR-0020](../../adr/0020-only-the-owning-agent-renders-to-the-humans-thread.md) · [ADR-0017](../../adr/0017-aggregated-step-messages-throttled-edits.md) · [ADR-0025](../../adr/0025-seal-the-step-trace-from-the-streams-terminal.md) · PR #124

## Problem

Since #124 (ADR-0026) an A2A audit thread records the whole consulted sub-tree:
every consulted agent's own preamble and tool calls render into the turn's single
thread under that agent's persona. That surfaced a confusion.

When `marketing` consults `sol`, and `sol` in turn consults `terra`, the thread —
named after only the first hop, `marketing→sol` — fills with `terra`'s messages
with nothing that reads as "sol is consulting terra." A real occurrence:
`marketing→sol` review, sol delegated the repo inspection to terra, and terra's
~40 tool-call steps appeared under terra's persona with no announcement. To a
reader it looks like terra materialised out of nowhere.

### Why it happens today

The thread *does* contain sol's request to terra, but not in a form that reads as
a consult. In the drain loop (`mention_handler._drain_and_deliver`), **every**
consult projection — nested ones included — is rendered by:

```python
url = await self._a2a.project(projection)     # A2ARequest → _emit() posts projection.message
if is_acting:
    await self._render_consult(projection, url, human_dest)   # human-thread row: acting agent only
```

For a nested consult `is_acting` is `False` (the emitter, sol, is not the turn's
acting agent, marketing), so:

- `project()` posts sol's **raw prompt text** (the `message` arg of
  `message_agent(name="terra", message="…")`) as a standalone `[sol] <prompt>`
  message. This reads as "sol talking," not "sol consulting terra."
- `_render_consult` — the thing that draws the resolving `◐ consulting X` row —
  is **skipped**, because it is gated on `is_acting` and (correctly) only writes
  the *human's* thread.

So a nested consult has no structured "X is consulting Y" affordance anywhere.

## Goal

Give a nested consult the same clear, resolving affordance a top-level consult
already has — placed where a nested consult actually lives (the consulting
agent's trace inside the A2A thread) — without leaking anything into the human's
thread and without a new visual language.

Non-goals: changing top-level consult rendering; changing the human's thread;
per-consult sub-threads (Discord has no nested threads, and ADR-0026 deliberately
keeps one thread per human turn).

## Design

### Core idea

Reuse the product's existing consult affordance — the resolving `ConsultRow`
(`◐ consulting X` → `● consulted X`) — and render it into the **consulting
agent's trace box inside the A2A thread**, driven by the A2A projector's *own*
`StepTraceRenderer` instance (the same one that already renders each consulted
agent's steps via `project_step`).

This works because that renderer already exposes exactly the two entry points
needed, and is keyed by `correlation_id`:

- `on_consult(key, peer, thread_url, dest, *, correlation_id, persona_name)` —
  opens a keyed pending `ConsultRow` under a given persona.
- `on_consult_result(key, *, state, note, correlation_id)` — resolves it in place.

Driving these under `persona_name=<caller>` (sol) appends the row into sol's
existing trace segment (sol's tool rows share that persona), so it flows inline;
when terra replies, the row resolves in place by `tool_call_id`.

### Why ordering is reliable

The row and terra's trace box are appended to the **same `_Entry`** (root
`correlation_id`) on the **same renderer**, flushed by that entry's **single
writer task**. `_flush` never posts a later segment before an earlier one lands,
so sol's segment (carrying the row) is guaranteed to post before terra's box.
This is the decisive advantage over a lighter "system note" approach, which would
post inline via `project()` and race the throttled step writer for order — the
known cosmetic mis-ordering caveat in `a2a-threads.md`.

### Rendered result

```
┌ sol
│ -# ● read_file CONTEXT.md · 90ms
│ ◐ consulting terra · "review the auth changes in src/…"      ← opens (present tense)
└
┌ terra
│ -# ● read_file src/main.py · 120ms
│ … 40 tool calls …
└   ⚠️ run failed after 40 tools · 3m        (seal, only if terra faults — ADR-0025)
┌ sol  (continues)
│ -# ● consulted terra · "review the auth changes in src/…"    ← resolves in place
└
[terra]  <terra's reply to sol>
```

Reject / fault reuse the row's existing states: `terra didn't answer` (failed),
`⊘ terra — <reason>` (denied). A dangling nested fault (terra faulted with no
reply) is already caught by the seal, which resolves every still-pending row to
interrupted — so the row can never freeze mid-`◐`.

### Decisions

1. **Form: resolving row in the caller's trace** (chosen over a system note or
   persona-provenance label). Most consistent with existing design language and
   the only option with guaranteed ordering.
2. **Request text: fold a truncated preview onto the row** (Option C), e.g.
   `◐ consulting terra · "review the auth changes in src/…"` (≈60 chars of the
   caller's `message` arg). The standalone raw-prompt message is **suppressed**
   for nested consults — it is the very "sol preamble then terra" line that
   confused readers. This keeps a glimpse of the ask without the noisy message.
   - The **peer's reply / reject / fault projections are kept** (terra's answer
     and any system note still post) — only the nested *request* projection is
     replaced by the row. This mirrors how a top-level consult shows the peer's
     reply.
   - The **top-level** request is unchanged: it remains the thread's starter/
     anchor message (and names the thread), so it is never suppressed.
3. **In-thread row carries no cross-link.** A top-level consult row links out to
   the audit thread; a nested consult's exchange is inline right below, so the
   row shows the preview where the link would be, and shows neither a link nor
   the `⚠️ couldn't write the audit log` audit-gap marker (which `thread_url is
   None` currently renders). The human-thread consult row is unchanged and stays
   content-free per ADR-0020 (preview appears only on the nested A2A row).

## Change surface

Small and contained; three modules.

1. **`bridge/a2a_project.py`** — two thin methods on `A2AProjector`:
   - `project_consult(req: A2ARequest)` — look up the turn's thread the same way
     `project_step` does (`self._threads.get(correlation_id)` + `self._channel_id`;
     no thread ⇒ drop, best-effort), then call
     `self._steps.on_consult(key=req.tool_call_id, peer=req.peer,
     thread_url=None, dest=…, correlation_id=req.correlation_id,
     persona_name=req.caller)`, passing a truncated preview of `req.message`.
   - `project_consult_result(proj: A2AReply | A2AReject | A2AFailed)` — call
     `self._steps.on_consult_result(key=proj.tool_call_id, state=…, note=…,
     correlation_id=proj.correlation_id)`, mapping reply→ok, reject→denied,
     failed→failed.

2. **`bridge/mention_handler.py`** — in the consult branch of
   `_drain_and_deliver`, split acting vs. nested:
   - `is_acting` (unchanged): `project()` + `_render_consult(…, human_dest)`.
   - `not is_acting` (new): drive `project_consult` on the `A2ARequest` and
     `project_consult_result` on the reply/reject/fail — and do **not** call the
     old `project()` for the nested *request* (suppression); still call
     `project()` for the nested reply/reject/fault so the peer's answer and notes
     post.

3. **`bridge/trace_rows.py`** — `ConsultRow` gains a `request_preview: str = ""`
   field; `_render_consult` renders `— "<preview>"` in place of the trailing
   `· <link>` when a preview is present (the nested/in-thread case), across all
   states. Width stays stable across states (fixed-length preview), preserving
   the growth-reservation invariant that `fits()` relies on.
   - `on_consult` in `trace.py` gains an optional `request_preview: str = ""`
     parameter, threaded into the `ConsultRow`. The existing human-thread caller
     passes nothing, so its rows are unchanged.

## Edge cases

- **No thread yet** (the turn's first projection render failed): drop the row,
  same best-effort contract as `project_step`.
- **Reject / fault**: row resolves to denied / failed; existing system note still
  posts.
- **Dangling nested fault** (no reply at all): the seal resolves the pending row
  to interrupted (`⊘ terra — never replied`); the existing `project_fault` note
  still posts.
- **Depth > 2** (terra consults someone): uniform — each consulted agent's
  consults render as rows in *its own* trace segment, separated by persona.
- **Human thread**: untouched. The `is_acting` gate still routes only the acting
  agent's consult to the human thread, so nothing leaks.
- **Preview hygiene**: `request_preview` is the model's unvalidated `message`
  arg; it is passed through the same `_plain`/`_hygienise` path `ConsultRow`
  already applies to `peer`, and truncated.

## Testing

TDD, mutation-testing each guard (as #124 did):

- Nested request opens a pending `ConsultRow` under the caller's persona in the
  A2A thread, carrying a truncated preview; no standalone raw-prompt message.
- Reply resolves the row to ok by `tool_call_id`; reject→denied; fault→failed.
- Ordering: the caller's segment (with the row) posts before the peer's box.
- No-thread ⇒ dropped, no crash.
- Dangling fault ⇒ seal resolves the row to interrupted.
- Human thread receives no nested-consult row (is_acting preserved).
- Depth > 2 renders a row per consulting agent.
- `_render_consult` renders the preview variant (no link, no audit-gap) and the
  linked variant unchanged when no preview.

## Docs / ADR impact

- `docs/a2a-threads.md`: update the "What humans see" mock-up and the nested-
  consult paragraph; note the request-preview and that the nested row carries no
  cross-link.
- A short ADR extending ADR-0026 (the nested consult is announced by a resolving
  row in the caller's trace, request text folded onto the row, standalone nested
  request message suppressed), if the decision warrants one per the ADR-FORMAT
  guidance.
