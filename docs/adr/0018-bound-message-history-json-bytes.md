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
tunable via `message_history.max_json_bytes` in bridge `settings.json`. R-A5's
other half stands: there is still no per-agent `history_turns` knob, and the
fetched window is still used as-is up to the budget.

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
  `_drop_until_user_request` and shared with `build_message_history`. It also
  gives replay-delta atomicity for free: a delta contains no user prompt, so
  repair walks past a half delta entirely, with no explicit turn model.
- **Bytes, not tokens.** The failure being prevented is a broker rejection,
  which is measured in bytes; a token bound would need a provider-specific
  tokenizer to prevent a byte-denominated failure. Model context-window
  overflow remains unaddressed (calfkit has a
  `FaultTypes.MODEL_CONTEXT_WINDOW_EXCEEDED` constant that nothing mints).
- **No re-grow after head repair.** Re-admitting the older message repair
  orphaned past (typically the delta's `tool_call`) would win back a little
  context for a settle loop and a subtler invariant. Shedding slightly more than
  strictly necessary at the boundary is the cheaper trade for a safety valve.
