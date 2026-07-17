# Step-trace rows are values, rendered at flush

**Status**: accepted (partially supersedes
[ADR-0017](0017-aggregated-step-messages-throttled-edits.md): its append-only
model of pre-rendered blocks. 0017's aggregation, leading-edge throttle, and
rollover-not-elision all stand unchanged)

A segment held `list[str]` of pre-rendered, append-only text, so a tool call and
its result had to be two separate lines — the first could never be revised. Both
reference TUIs (opencode, codex) instead render **one row that mutates in
place**, which halves the line count and lets a row carry its result. A segment
now holds `list[TraceRow]` — frozen variants + union alias + `isinstance`/
`assert_never`, the same idiom as `A2AProjection` — and `body()` folds a pure
`render_row` over them; resolving a row replaces it with a new value. This is
affordable because `_flush` already re-renders and re-sends the *whole* body on
every edit, so revising an earlier row costs exactly what appending one costs.

## Considered options

- **Keep `list[str]`, add a `tool_call_id → index` map.** Rejected: re-rendering
  a row on resolve needs the call's original name/subject/detail, so that context
  must be stored anyway — beside the strings, as a second container holding one
  value, free to drift, with no exhaustiveness check. It is this decision
  implemented badly, not a smaller one.
- **Store the `StepEvent`s and fold the whole body each flush** (what
  `_render_tree_blocks` does for the transcript: pair by id, order-independent).
  Rejected: a resolving row changes rendered length, which would retroactively
  move a segment boundary that is *already posted*. Segment assignment must
  freeze at append time, which collapses this back into the chosen design plus a
  reservation rule.

## Consequences

- **A posted segment cannot be re-split, so growth must be reserved up front.**
  `fits()` reserves `_ROW_GROWTH_RESERVE` per *pending* row, released as rows
  resolve. The bound holds by construction (a resolved render is the pending
  render plus a bounded tail) and is pinned by a property test rather than left
  to inspection.
- `chars` is recomputed instead of tracked incrementally. `n ≤ ~100` rows per
  segment at a few appends per second — the O(1) accounting was a
  micro-optimisation paid for in correctness.
- Rows resolve out of order and across segments (calfkit fans out parallel calls,
  each folding on its own hop). `_flush` already iterates every segment and each
  holds its own `message_id`, so an older segment just goes dirty. Rows stay in
  call order; chronology is untouched.
- Tests assert on row values and state transitions, with one render test per
  variant, instead of on exact rendered strings — the reason every cosmetic tweak
  previously rewrote ~22 tests.
- A result with no matching call row **appends** rather than raising, upholding
  the drain's contract that the render path can never fault the turn.
