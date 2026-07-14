# Aggregate live step messages via throttled in-place edits

**Status**: accepted (partially supersedes
[ADR-0016](0016-persistent-v2-step-messages.md): the one-message-per-step posting
model; 0016's Components-V2 message type and its history-exclusion invariant
stand unchanged)

ADR-0016's one-persistent-message-per-step model floods a channel on tool-heavy
turns. Intermediate steps are now **aggregated into one growing Components-V2
message per turn**, edited in place as steps stream in — split into a new message
only when the persona changes (webhook edits cannot change `username`/`avatar`,
so a handoff starts a fresh message under the new agent's persona) or when the
4000-char v2 cap would overflow (rollover, full trace preserved). Messages
persist at end of turn; nothing is deleted.

Edits are paced by a **leading-edge throttle** (min 1s between edits, immediate
when idle), implemented as a dirty-flag + per-run writer task so the stream drain
never awaits Discord. This is the pattern streaming-LLM Discord frontends
converge on (llmcord's `EDIT_DELAY_SECONDS = 1`; Discord webhook bucket
~5 req/2s; Discord offers no streaming API).

## Considered options

- **Keep ADR-0016 (message per step).** Rejected: channel flooding is the
  problem being solved.
- **Trailing debounce (the pre-ADR-0016 renderer).** Rejected: delays every
  update by the full window even on a quiet stream; the old design's fatal
  edit-vs-delete race does not return here because the aggregate is never
  deleted.
- **Tail-window elision inside a single message.** Rejected: silently discards
  trace; rollover keeps the full trace at ~1 message per 4000 chars.

## Consequences

- A turn's visible trace is one message in the common case; handoffs and long
  turns produce a few. Steps render with no artificial latency when the stream
  is quiet; bursts coalesce into ≤1 edit/sec.
- A mid-run bridge restart strands a partial (persistent) trace — cosmetically
  identical to a shorter turn; no lifecycle state to recover.
- The history-exclusion invariant is untouched: edited v2 messages still carry
  no `content`, so `ChannelHistoryFetcher` keeps dropping them from model
  history.
- See [`docs/design/aggregated-step-messages-plan.md`](../design/aggregated-step-messages-plan.md)
  for the implementation plan.
