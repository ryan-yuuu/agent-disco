# Persistent Components-V2 step messages replace the transient progress + reply toggle

**Status**: accepted (reverses decision **D-1a** in
[`docs/design/step-transcripts-and-live-streaming-plan.md`](../design/step-transcripts-and-live-streaming-plan.md);
see also [`docs/design/live-step-messages-v2-plan.md`](../design/live-step-messages-v2-plan.md))

## Context

D-1a chose a *transient* live-progress message (posted, debounced-edited, then
**deleted** on the terminal hop) plus an on-demand "⤵ N steps" button on the
final reply that opened the turn's step transcript as an ephemeral message. The
transient message avoided polluting channel history, so no history-pollution
filter and no two-writer lock were needed.

## Decision

Each intermediate step event is now posted as its own **persistent, inline
Components-V2 message** (a red container of text displays) under the emitting
agent's persona — `🔧 \`tool\` called` / `✅ \`tool\` returned` short lines, the
full `agent_message` prose (chunked to the 4000-char v2 cap), and a
`➡️ handed off to \`peer\`` note for handoffs. Nothing is edited or deleted, and
the "⤵ N steps" button and its persistent view are removed.

The double-count problem this creates — persistent step messages being re-fetched
into the next turn's history — is solved by the message *type*: a Components-V2
message carries no `content` (Discord forbids it under the `components_v2` flag),
so the history fetcher drops the bridge's own v2 messages
(`ChannelHistoryFetcher._is_v2_step_message`). The model's tool memory continues
to ride the separate structured transcript replay, unchanged.

Handoffs, which transfer conversation control (ADR-0011) — distinct from a
`message_agent` consult where the caller keeps control (also ADR-0011) — are no
longer claimed by the A2A dispatcher; they render inline in the main step stream
instead of the A2A audit channel.

## Considered options

- **Keep D-1a (edit-in-place transient message + toggle).** Rejected: a durable,
  readable inline trace is wanted, and the edit/delete lifecycle carries fragile
  state (a mid-run restart strands the message; a late edit races the delete).
- **Persist step messages as plain text.** Rejected: plain-text step messages
  re-enter history and double-count the agent's tool activity; excluding them
  would need a content sentinel whose survival through Discord is fragile.
- **Move message persistence off Discord (SQLite/MySQL).** Rejected as out of
  scope: Discord remains the source of truth.

## Consequences

- A durable, inline step trace; no transient message to strand on a mid-run restart.
- Discord's Components-V2 cap (4000 chars per message, per TextDisplay **and** per
  whole message — verified against the live API) bounds each message; long agent
  prose is chunked across several messages.
- The exclusion invariant: **persona-webhook Components-V2 messages are
  display-only and are never part of history.** A future feature that posts v2
  messages meant to be part of history would need to revisit
  `ChannelHistoryFetcher`.
- Handoffs no longer appear in the A2A audit channel.
- `Read Message History` is still required (history is still fetched from Discord).
- The transcript store (`transcripts.py`) is retained — it now backs only
  tool-call replay and `/thinking-effort` overrides, not an expand button.
