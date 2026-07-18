# A nested consult is announced by a resolving row in the caller's trace

**Status:** accepted

[ADR-0026](0026-the-a2a-thread-records-the-consulted-sub-tree.md) made a consulted
agent's own work render into the turn's audit thread. That surfaced a gap: when a
consulted agent consults a *peer* (B→C inside A→B), the peer's trace box appeared in
a thread named only for the first hop (`A→B`), with nothing that read as "B is
consulting C" — the peer's work looked like it materialised from nowhere. The
nested request *was* in the thread, but only as a bare `[B] <prompt>` message
(`A2AProjector.project`), which reads as B talking, not as a consult.

**We announce a nested consult with the same resolving `ConsultRow` the human's
thread already uses for a top-level consult (`◐ consulting C` → `● consulted C`),
rendered into B's own trace inside the audit thread** — driven by the projector's
own `StepTraceRenderer` via `project_consult`/`project_consult_result`. The standalone
prompt message is **suppressed**; a bounded preview of the ask is folded onto the row
instead, and the row carries no cross-link because the exchange is inline right below.

## Considered options

- **A system `a2a` note** (`↪ B consulted C`) before the peer's box. Smallest change;
  rejected because it posts inline via `project` while the peer's box is written by the
  throttled step writer, so the two race — the note can land *after* the box it
  announces, which is the exact confusion this fixes. The row avoids this by construction
  (below).
- **A persona-provenance label** on the peer's messages (`C ⟵ B`). Per-message, so no
  race; rejected because it mutates the pure `persona_for` identity model and shows *who*
  without showing *that a consult happened* or resolving with its outcome.
- **Keep the raw prompt message and add the row.** Full fidelity; rejected because the
  bare `[B] <prompt>` line is the very thing that read as unannounced peer talk — the row
  plus a folded preview conveys the ask without it.

## Consequences

- **Ordering is reliable, unlike a projection.** The row and the peer's trace box are
  appended to the *same* per-correlation entry on the *same* renderer, flushed by that
  entry's single writer task, which never posts a later segment before an earlier one
  lands. So `◐ consulting C` is guaranteed to post before C's box — the two-writer race
  ADR-0026 accepts for inline projections does not apply to the announcement.
- **The request is suppressed but the outcome is not.** A nested request routes to
  `project_consult` (a row) instead of `project` (a message); its reply/reject/fault still
  route to `project` *and* resolve the row. The asymmetry is deliberate: the prompt is
  redundant with the row and the peer's visible work, while the peer's reply and any
  system note are the audit's record of what came back.
- **`inline` — an explicit flag — decides the row's tail, not the presence of a preview.**
  A nested row is `inline`: its exchange is right here, so it shows a glimpse of the ask (or
  a bare marker when the ask was blank) and never links out. A top-level row is not inline:
  it links to its audit thread, or renders `⚠️ couldn't write the audit log` when that
  render failed. Precedence: inline (preview, else nothing), else link, else gap. An earlier
  draft inferred the kind from a *non-empty preview* instead, so it rendered the audit-gap
  marker for a blank-prompt nested consult — a false alarm pointing operators at a
  nonexistent permission problem. The flag makes the kind a field, not a guess.
- **The human's thread is untouched.** Only the `is_acting` consult still routes to
  `_render_consult`; ADR-0020's boundary is unchanged, so a nested consult never leaks a
  marker into the human's conversation.
- **The preview is a display budget, not a safety one.** It is truncated to ~60 chars for
  the row, then re-hygienised and hard-bounded by `ConsultRow` like every other
  model-controlled field, so a hostile prompt can neither break the row nor blow the
  growth reserve.
- **The nested request's full prompt is no longer preserved anywhere.** Suppressing the
  standalone message means the audit thread keeps only a ~60-char preview of what a nested
  caller asked — the peer's *answer* and *steps* are still recorded in full, so the
  exchange stays legible, but the request side loses verbatim fidelity. Accepted
  deliberately — the bare `[caller] <prompt>` line it replaced was the confusion this ADR
  removes. A top-level consult still keeps its full prompt (the thread's starter message).
