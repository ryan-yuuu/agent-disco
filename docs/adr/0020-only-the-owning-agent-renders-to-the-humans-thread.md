# Only the owning agent renders to the human's thread

**Status:** accepted

> **Terminology note.** This decision stands unchanged, but the concept it calls
> the *owning agent* is now the **acting agent** (`acting_agent`, `is_acting`).
> "Owner" collided with the established *sticky owner* — a routing fact that
> persists across turns — where this is control within a single run. See
> [`CONTEXT.md`](../../CONTEXT.md). Read "owning agent" below as "acting agent".

calfkit publishes *every* hop's steps to the **root** caller's topic
(`nodes/base.py` `_flush_steps` → `stack.root.callback_topic`), so a consulted
peer's own preamble and tool calls arrive on the mention run's stream stamped
`emitter=<peer>`, `depth>1` — indistinguishable, to a naive drain, from the
mention target's own progress. The bridge therefore renders a step to the
human's thread **only when `step.emitter` is the agent currently in control of
the turn**; every other emitter belongs to a consulted peer's private sub-tree
and is dropped (at DEBUG). That control is transferred by a handoff — but only
the *owner's* handoff, never one a peer performs inside its own sub-tree. A
consult contributes exactly one line to the human's thread: a cross-link marker
into the A2A audit thread holding the exchange.

## Consequences

- **The drop is deliberate, not an oversight.** Without this rule the bridge
  spills a private agent-to-agent exchange into the human's conversation — the
  bug this ADR records (a peer's preamble appeared in the caller's thread, and
  the peer's tool calls appeared there under the *caller's* persona, because
  `owning_agent` only advances on handoff). A future reader who deletes the
  emitter guard re-creates it.
- **Auditing and rendering diverge on purpose.** The A2A projector still audits
  *every* consult including nested peer-to-peer ones (the audit channel is the
  system of record for the whole run tree); only the *marker* is gated on
  ownership. The two branches read the same `is_owner` predicate so they cannot
  drift apart.
- **Ownership is load-bearing for silence.** Because a non-owner step is dropped
  invisibly, anything that wrongly advances `owning_agent` blackholes the rest
  of the turn's trace. That is why the transfer is gated on `is_owner` too.
- This enforces [ADR-0011](./0011-native-a2a-and-handoff.md)'s consult/handoff
  distinction (a consult *keeps* control; a handoff *transfers* it) at the
  render boundary, where it had never actually been applied.

## Considered options

- **Filter by `depth`** (drop `depth>1`). Structurally appealing — a handoff is a
  `TailCall` that pushes no frame, so depth distinguishes consult-nesting from
  handoff for free. Rejected: the exact frame-depth semantics of the root hop are
  not pinned by any contract calfcord owns, so the threshold would be a guess
  against an internal detail. `emitter` is already the identity the renderer
  resolves personas from.
- **Project the peer's internal steps into the A2A thread** instead of dropping
  them. Rejected for now: the audit thread is specified as request / reply /
  system notes, and a peer's intermediate reasoning is noise there. Revisit if
  operators ask to audit a peer's working.
