# A2A turn threads use explicit consult branches

**Status:** accepted

A root run can issue several `message_agent` calls in parallel. All calls share the
root `correlation_id`, so the A2A audit projector intentionally places the whole
consulted sub-tree in one Discord thread. The old presentation named that thread
after the first projected pair and rendered later requests as unlabelled caller
messages. A thread called `marketing→grok` could therefore also contain a request
to `sol`, with no visible addressee. Correct transport pairing by `tool_call_id`
did not make the audit UI understandable.

We keep **one thread per human turn**, but represent every top-level consultation
as an explicit branch:

- `correlation_id` owns the shared Discord thread;
- `tool_call_id` owns one editable Components V2 consult card;
- the card states the complete route (`caller → peer`), a bounded prompt, and its
  lifecycle state;
- reply, rejection, failure, and interruption edit that original card in place;
- a successful peer reply remains a separate persona-authored message prefixed
  `↩ peer → caller · response`;
- no standalone completion or fault message is added;
- the thread title uses the root agent and triggering human subject, never the
  first peer;
- a nested consultation remains a compact resolving row in the consulting
  agent's trace, but now states `caller → peer` explicitly.

Before a reply is posted, the audit trace's single writer is asked to flush its
current dirty segments. This preserves chronological reading without inventing
contiguous branch groups: genuinely parallel agent work may still interleave.

## Why not one thread per pair or call?

Native consults are stateless: every `message_agent` call starts a fresh peer
conversation. A persistent pair thread would imply shared conversational state
that does not exist, while a thread per invocation would fragment one fan-out
decision across several audit surfaces. A turn-level thread keeps the causal run
together; explicit route cards supply the missing provenance.

## Consequences

- Two concurrent calls to the same peer still receive distinct cards because the
  key is `tool_call_id`, not `(caller, peer)`.
- The first consult card anchors the thread in the unified A2A channel.
- Card edits are best-effort. If an edit fails, the substantive routed reply still
  posts; a stale `Consulting` card is preferable to losing the answer.
- Pending cards resolve to `No response` during abnormal finish so the audit never
  permanently claims they are running.
- Ordinary peer step events are not labelled with a branch. Their current schema
  carries `correlation_id`, `frame_id`, `depth`, and `emitter`, but no parent
  consult `tool_call_id`; assigning a concurrent same-peer step to a branch would
  be guesswork. Exact branch chips require upstream lineage metadata and are out
  of scope for this decision.
