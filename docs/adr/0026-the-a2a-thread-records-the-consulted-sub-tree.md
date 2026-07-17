# The A2A thread records the consulted sub-tree, not just the exchange

**Status:** accepted

calfkit flushes every hop's steps to the *root* caller, so a consulted agent's own
preamble and tool calls arrive on the mention run's stream stamped `emitter=<peer>`,
`depth>1`. [ADR-0020](0020-only-the-owning-agent-renders-to-the-humans-thread.md) keeps
them out of the human's thread — correctly — but then **dropped them at DEBUG**, so they
rendered nowhere. The codebase disagreed with itself about whether that was right:
`docs/a2a-threads.md` described a thread as holding "the caller's consult, the peer's
reply, and any system notes" (mock-up: "(2 messages)"), while the drain loop called the
same channel "the system of record for the whole run tree" and its own drop-branch
comment called peer work "the audit channel's business" — a promise nothing kept.

**We resolve it in favour of completeness: an A2A thread records every consulted agent's
own work** — the consulted sub-trees, not literally the whole run tree, which would
duplicate the caller. It renders through the same `StepTraceRenderer` the human's thread uses, so the
ADR-0017 aggregation and throttle apply unchanged. The trigger was a real incident: a
consulted agent died after 23 tool calls and never replied, and the audit thread showed a
request and a shrug — the 23 tool calls, and where it stopped, existed only in a log file.

## Considered options

- **One Discord message per peer step.** Smallest change; rejected because it re-creates
  the exact flood [ADR-0017](0017-aggregated-step-messages-throttled-edits.md) exists to
  fix (the incident's consulted agent made 23 tool calls in 90 s — roughly twice that in
  steps, since each call also emits a result — and the caller 37).
- **Fold the peer's trace into its reply.** One message, no throttling; rejected because a
  faulted peer never *sends* a reply — the trace would vanish precisely in the case that
  motivated this.
- **Keep the exchange-only thread and render peer steps only when the consult faults.** A
  defensible middle; rejected as a special case that makes the channel's contents depend
  on the outcome, so a reader can't know what a thread is meant to contain.

## Consequences

- **ADR-0020's rule survives; one of its rejected options does not.** Its `is_acting`
  predicate already drew exactly the boundary this needs — the mention target's own work
  goes to the human's thread, everything else is inside a consulted sub-tree — so there is
  no new routing predicate, and 0020's "and is dropped" merely becomes "and is routed to
  the A2A thread". What 0024 *does* overturn is 0020's explicit "Project the peer's
  internal steps … Rejected for now", whose own condition ("revisit if operators ask") is
  what triggered this. That reversal is annotated in 0020 rather than hidden here.
- **The caller's own work is still not in the thread.** It appears only as the *author of
  the consult*. "The whole run tree" — the phrase both the drain loop and 0020 used — is
  imprecise, and both were corrected to "the consulted sub-trees": taken literally it would
  render the caller twice, once per surface.
- **A step never creates a thread, so one failure mode survives.** If the consult's own
  projection failed there is no thread to render into, and the working trace is dropped at
  DEBUG — in neither surface, exactly as before this ADR. A step cannot supply the
  `caller→peer` thread name, and inventing an unnamed thread would be worse than the gap
  the `⚠️ couldn't write the audit log` marker already reports.
- **Ordering within a thread is best-effort.** The step aggregate is flushed by a throttled
  writer task while projections (`A2ARequest`/`A2AReply`/system notes) are awaited inline —
  two writers now target one thread for the first time, so a peer's step and a projection
  can race. Accepted deliberately: every message is *attributed* correctly, and only the
  interleaving is uncertain. Serialising them (flushing before each projection post) was
  considered and dropped as not worth the coupling.
- **Attribution is sound by construction, not by care.** `message_agent` validates its
  target by exact-match against live node names before dispatch, so any peer that actually
  replies was addressed by its real node id — its steps (`persona_for(step.emitter)`) and
  its reply (`persona_for(projection.peer)`) therefore resolve to the same persona. A
  mismatched name is rejected pre-dispatch and never becomes a reply.
- **The A2A channel's single webhook becomes a contention point.** Every thread in the
  channel shares one webhook, while the throttle is per-correlation — so concurrent
  consults can exceed the per-webhook budget the 1 s interval was sized against. It
  degrades rather than breaks (discord.py backs off; every progress call is best-effort and
  retries on the next wake), and is recorded in the failure-modes runbook rather than
  engineered around. `min_edit_interval` is a per-instance constructor argument if it bites.
- **The projector now has a per-turn lifecycle.** It must be finished per correlation — and
  *after* the terminal reply is delivered, because the fault path synthesizes its "did not
  reply" notes then and needs the turn's thread to still be mapped. This also retires the
  previously-unbounded `correlation_id → thread_id` map, which nothing evicted.
- **No feature flag.** The channel is already opt-in via operator setup; a sub-flag would
  keep the rejected exchange-only mode alive as a supported configuration.
