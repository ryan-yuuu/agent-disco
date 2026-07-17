# Seal the step trace from the stream's terminal, not from finish()

**Status**: accepted

The step trace must end with the run's outcome, but `finish()` cannot know it:
it is called in a `finally` around the stream drain, while the fault only
surfaces afterwards in `_await_terminal` → `handle.result()` → `NodeFaultError`.
A seal written at `finish()` would render `4 tools · 12.3s` on a crashed turn.
Since `RunEvent = RunCompleted | RunFailed | RunStepEvent`, **the terminal
already arrives on the stream the drain is reading**, as its last item, before
the `finally` — `normalize_run_event` returns `None` for it and the drain
discards it. Sealing there needs no control-flow change at all, and stays inside
the §5.1 swappable step-source seam, since `normalize_run_event` is already the
only code that knows calfkit's event types.

## Considered options

- **Move `finish()` after `_await_terminal`, or pass the outcome into it.**
  Rejected: it reorders the `finally` that guarantees the writer is retired on
  every path, to obtain information the drain already had and threw away.
- **Seal from `handle.result()` in `_deliver`.** Rejected: by then
  `StepTraceRenderer` has popped the entry and retired the writer, so the seal
  would have to resurrect per-correlation state that was just deliberately
  torn down.

## Consequences

- Two readers observe the terminal, which reads oddly next to
  `normalize_run_event`'s *"terminal — handled by result() below"*. They cannot
  disagree: `result()` derives its `NodeFaultError` from the same terminal. The
  split of duty is deliberate — **the stream tells the trace *whether* it
  faulted; `result()` remains the sole authority for the notice and its root
  causes.**
- The seal also resolves every still-`pending` row to `interrupted` and resolves
  dangling consults (`dispatcher.dangling()` already returns exactly those, and
  already carries `tool_call_id`), so a fault strands nothing.
- **`finish()` seals defensively.** An unsealed entry is sealed `interrupted`
  with a WARNING, covering a drain that raised, a broken stream, and a calfkit
  contract violation — a permanent `◐` is never the failure mode.
- The fault **notice** deliberately stays on the native-reply path, which
  *"needs only Send Messages and is independent of the failing webhook path"*.
  The trace rides that webhook, so when the webhook is what is broken the trace
  cannot deliver anything. The trace seals itself best-effort; the notice carries
  the detail over the reliable channel.
- A mid-run bridge restart is now the only case that strands a trace, and it is
  unfixable by persistence: the run dies with the bridge, so no restored state
  resurrects the stream.
