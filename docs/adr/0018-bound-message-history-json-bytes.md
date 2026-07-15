# Bound outgoing message history to a JSON byte budget

**Status:** accepted — supersedes requirement R-A5 of
[`docs/design/calfkit-012-implementation-plan.md`](../design/calfkit-012-implementation-plan.md)
in part.

R-A5 decided the bridge would add **no** global token/byte cap on
`message_history`, on the grounds that the per-return
`REPLAY_TOOL_RETURN_MAX_CHARS` truncation already bounded "the one realistic
envelope blow-up". That reasoning does not hold: the per-return cap does not
compose into a total — N replayed tool returns at the cap grow without limit,
across a window of up to ~100 fetched records. With aiokafka's default
`max_request_size` of 1 MiB (which neither calfkit nor agent-disco overrides)
an oversized envelope raises `MessageSizeTooLargeError` from inside the
producer, uncaught on the normal publish path — calfkit only catches it on the
fault-report path.

We therefore bound the serialized history at the bridge, in
`DiscordHistoryProvider` (`bridge/history.py`), defaulting to 800 000 bytes and
tunable via `message_history.max_json_bytes` in bridge `settings.json` (floored
at 10 000 — see below). R-A5's other half stands: there is still no per-agent
`history_turns` knob, and the fetched window is still used as-is up to the
budget.

## Consequences

- **The budget bounds the history term only, not the envelope.** The envelope
  also carries the prompt, `deps` and headers; the default leaves ~250 KB of
  headroom for those. This is a floor under the common blow-up, not a guarantee
  the envelope fits. An envelope-level bound belongs in calfkit (it owns the
  broker and the 1 MiB fact) and is filed upstream rather than worked around
  here.
- **The head invariant is stricter than "not assistant-first".** Dropping oldest
  can cut inside a replay delta and strand a tool-return `ModelRequest` at the
  head — a `tool_result` with no `tool_use`, which providers reject. The rule is
  "first message is a `ModelRequest` carrying a `UserPromptPart`", extracted as
  `_drop_until_user_request` and shared with `build_message_history`.
- **Replay-delta atomicity falls out of that rule, but rests on where user
  prompts sit.** A delta's only user-prompt `ModelRequest` is its *first* message
  (calfkit commits the staged prompt at `initial_len` — `nodes/agent.py` — and
  `_turn_delta` slices from there, `bridge/reply_poster.py`), so a cut either
  keeps a delta whole or is walked past it entirely. No explicit turn model
  needed. This holds only while user prompts appear at delta *boundaries*: a
  mid-delta user prompt would become a legal-looking head with its `tool_use`
  dropped, and 400 the provider. Worth re-checking if calfkit ever stages a
  prompt mid-turn.
- **Bytes, not tokens.** The failure being prevented is a broker rejection,
  which is measured in bytes; a token bound would need a provider-specific
  tokenizer to prevent a byte-denominated failure. Model context-window
  overflow remains unaddressed (calfkit has a
  `FaultTypes.MODEL_CONTEXT_WINDOW_EXCEEDED` constant that nothing mints).
- **No re-grow after head repair.** Re-admitting the older message repair
  orphaned past (typically the delta's `tool_call`) would win back a little
  context for a settle loop and a subtler invariant. Shedding slightly more than
  strictly necessary at the boundary is the cheaper trade for a safety valve.
- **The budget is floored at 10 000 bytes, not at 1.** A single minimal message
  serializes to ~200 bytes, so any budget in the low hundreds empties every
  history and makes every agent amnesiac — one warning per turn, indefinitely.
  `> 0` accepts that entire class. The floor turns it into a startup
  `SettingsConfigError` instead, which is the failure mode an operator can act
  on.
- **Dropped context is logged, not surfaced to the user, and not marked for the
  model.** A Discord notice on every trim would fire on any long channel, where
  shedding is expected and harmless. A marker in-band (the way
  `REPLAY_TOOL_RETURN_MAX_CHARS` marks a truncated tool return) would have to be
  a `UserPromptPart` to survive head repair — the bridge putting words in a
  user's mouth — and would be incoherent while the ~100-message fetch window
  goes unmarked. Accepted cost: a model given a trimmed history may assert
  "you never mentioned X". Revisit if that shows up in practice.
