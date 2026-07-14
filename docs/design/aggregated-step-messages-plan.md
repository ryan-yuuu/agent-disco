# Aggregated live step messages — implementation plan

**Status**: in progress. Follow-up to [ADR-0016](../adr/0016-persistent-v2-step-messages.md)
(persistent per-step Components-V2 messages); decision recorded in
[ADR-0017](../adr/0017-aggregated-step-messages-throttled-edits.md).

## Problem

ADR-0016 posts every intermediate run step as its **own** persistent Components-V2
message. A tool-heavy turn floods the channel with dozens of short messages
(`🔧 called` / `✅ returned`), drowning the conversation. We want the same durable
inline trace, but **aggregated: one growing message per turn** (with well-defined
exceptions below) that is edited in place as steps stream in.

## Decisions (settled with the user)

1. **Persona segmentation.** A Discord webhook message's `username`/`avatar` are
   frozen at creation — an edit cannot change them. So the aggregate is split into
   **segments**: one message per contiguous run of steps sharing a persona. A
   handoff (or any persona change, e.g. a peer's preamble mid-consult) closes the
   current segment and starts a new message under the new persona. Only the
   **latest** segment ever receives new content; order stays chronological.
2. **Rollover on overflow.** Discord caps a Components-V2 message at 4000 chars
   (whole message *and* per TextDisplay). When appending a block would exceed the
   cap, close the segment and start a new message (same persona). The full trace
   is preserved — no elision.
3. **Leading-edge throttle, not trailing debounce** (see "Writer task" below).
   Minimum interval between Discord edits: **1.0s** (llmcord's value; Discord's
   webhook bucket is ~5 req/2s). A step arriving while the writer is idle renders
   **immediately** — no fixed wait on every event.
4. **`agent_message` prose renders in full**, verbatim (chunked only by the
   existing 3900-char `_V2_CHUNK` renderer). No compaction, no truncation.
5. **End-of-turn: the message(s) persist.** No delete. This dissolves ADR-0016's
   main objection to edit-in-place (the edit-vs-delete race and the stranded
   transient message on mid-run restart — a stranded aggregate is just a shorter
   persistent trace).

## What deliberately does NOT change

- **`StepEvent` seam** (`bridge/step_events.py`) and the
  `MentionHandler` ⇄ `ProgressRenderer` protocol
  (`on_step(step, req, *, owning_agent)` + `finish(correlation_id)`), including
  per-step persona semantics: owning agent for `tool_call`/`tool_result`,
  emitter for `agent_message`/`handoff` (#96).
- **Per-step render bodies** (`render_step_message` in `steps_render.py`) — the
  same `🔧/✅/❌/🚫/➡️` lines and full prose chunks; they become *blocks* joined
  with `\n` inside a segment instead of one message each.
- **Components-V2 + history exclusion** (ADR-0016's second half): edited v2
  messages still carry no `content`, so
  `ChannelHistoryFetcher._is_v2_step_message` keeps excluding them from model
  history with zero changes.
- **A2A audit channel** (`a2a_project.py`): separate surface, already
  thread-contained; untouched.
- **Transcript store / tool-call replay**: independent of the display surface.

## Architecture: dirty-flag + per-run writer task

`on_step` must never await Discord (today a slow send back-pressures the stream
drain and the A2A projections behind it). Split responsibilities:

- **`on_step` (producer, no I/O):** render the step to blocks; pick the persona;
  append each block to the entry's latest segment — starting a new segment on
  persona change or cap overflow — mark the segment dirty; set the entry's wake
  event.
- **Writer task (consumer, one per in-flight correlation):**

  ```
  loop:
      await wake; clear wake
      flush every dirty segment (post if no message_id yet, else edit) — best-effort
      if finished: break
      wait min-interval (interruptible only by finish)
  ```

  Idle writer + new step ⇒ immediate flush (leading edge). Steps landing during
  the interval wait coalesce into the next flush (trailing edge). `finish()` sets
  `finished` + `wake` and awaits the task ⇒ guaranteed final flush, prompt even
  mid-interval.

- **Failure semantics (unchanged in spirit — best-effort, never faults the run):**
  - Failed **post** (segment has no `message_id`): segment stays dirty → retried
    on every subsequent wake and the final flush, so a segment can't be lost.
  - Failed **edit**: dropped (not re-marked dirty — avoids hot-looping on a
    deleted message). The next append re-marks dirty and the next flush carries
    the full body anyway; every edit renders the segment's complete current
    state, so a dropped edit self-heals.
  - `finish()` pops the entry; a restart mid-run strands only a
    persistent partial trace (cosmetically fine).

## File-by-file changes

| File | Change |
| --- | --- |
| `src/calfcord/discord/persona.py` | Add `edit_components(channel_id, message_id, view, *, thread_id)` → `webhook.edit_message(message_id, view=..., thread=...)`. |
| `src/calfcord/bridge/progress.py` | Rewrite: `_Segment` / `_Entry` state, segment selection (persona/cap), writer task, throttled flush, `finish` = signal + await final flush. Keep `_build_step_view`, `_best_effort_progress`, dormant typing notifier. |
| `src/calfcord/bridge/steps_render.py` | No behavior change. `progress` imports `_V2_TEXT_LIMIT` for the cap check. |
| `src/calfcord/bridge/mention_handler.py` | None (protocol unchanged). |
| `src/calfcord/bridge/history.py`, `gateway.py` | None. |
| `tests/discord/test_persona.py` | Add `edit_components` cases (edit call shape, thread routing, not-started guard). |
| `tests/bridge/test_progress.py` | Rewrite for the new lifecycle (details below). |
| `tests/bridge/test_steps.py`, `test_history.py`, `test_gateway_replies.py` | Unchanged; must stay green. |
| `docs/adr/0017-…md`, this plan | New. ADR-0016 gets a "partially superseded" pointer. |

## Test plan (TDD order)

1. **`edit_components`** (new tests, then code): calls `webhook.edit_message`
   with the message id, `view=`, `thread=`; returns/raises like `send_components`;
   RuntimeError when not started.
2. **Aggregation:** two renderable steps, same persona → one `send_components`
   then one `edit_components`; edited body is both blocks joined by `\n`.
3. **Leading edge:** a step posted while the writer is idle flushes without
   waiting the interval (test with a large interval: first step still posts).
4. **Coalescing:** with a large interval, steps 2..n arriving after the first
   flush produce **zero** interim edits; `finish()` produces exactly **one**
   final edit containing all blocks.
5. **Persona segmentation:** handoff step (emitter=old) lands in the old
   segment; the next step (owning=new) opens a new message under the new persona.
6. **Rollover:** blocks sized near the 4000 cap force a second message; both
   bodies correct, same persona.
7. **Multi-chunk `agent_message`:** each ≥3900-char chunk lands in its own
   segment/message, in order.
8. **Thread routing:** thread-originated req → posts/edits carry
   `thread_id`, webhook host stays the parent channel.
9. **Failure paths:** Forbidden/NotFound/RateLimited on send and on edit are
   swallowed; failed post retries on next wake (then succeeds, with full body);
   failed edit does not retry until new content arrives; nothing escapes
   `on_step`/`finish`.
10. **No-op paths:** whitespace-only step → no entry, no writer, no posts;
    `finish` on unseen correlation → no-op.
11. **Determinism:** `ProgressRenderer(min_edit_interval=...)` injectable; tests
    use `0` (immediate cadence) or a large value (coalescing assertions) — no
    real sleeps, no flaky timing.

## Risks / edge cases

- **`asyncio.Event` wake/finish race**: finish sets both `finished` and `wake`;
  the writer re-checks `finished` after every flush, and the interval wait is
  `wait_for(finished.wait(), interval)` — interruptible by finish only, so
  spacing between edits is always enforced.
- **Mutation during await**: `on_step` appends synchronously (single event
  loop); the flush snapshots a segment's body before awaiting; appends during an
  await re-set dirty and are carried by the next flush. Flush iterates
  `list(entry.segments)`.
- **discord.py `Webhook.edit_message` + LayoutView**: supported ≥2.6 (pinned
  ≥2.7.1); verify at implementation time against the installed package; v2
  edits must not pass `content`/`embeds`.
- **Two flushes in one wake** (rollover mid-burst: old segment edit + new
  segment post) — bounded, still ≪ the webhook bucket.
