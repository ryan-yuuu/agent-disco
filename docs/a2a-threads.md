# A2A Threads (Unified Audit Channel)

How agent-to-agent (A2A) conversations are projected to Discord, how
operators set up the audit surface, and how agents opt into consulting
or handing off to peers.

> **Note (2026-07):** **handoffs are no longer projected to this audit
> channel.** A handoff transfers conversation control, so it now renders
> inline in the main step stream as `➡️ handed off to <target>` — see
> [ADR-0016](./adr/0016-persistent-v2-step-messages.md). Only `message_agent`
> **consults** are projected here. The handoff-projection references below are
> retained for history but no longer describe current behavior.

## What changed

**Before**: A2A was a first-party `private_chat` tool. An agent's LLM
called `private_chat(target_agent_id=…, content=…)`; the tool ran in the
`calfkit-tools` process, invoked the peer over a bespoke calfkit RPC,
anchored a Discord thread itself, and returned a `<thread_id>` the caller
could pass back to continue the conversation.

**After**: A2A is **native to calfkit**, and the Discord projection is
**owned by the bridge**. There is no `private_chat` tool anymore. Two
capabilities replace it, both declared in agent frontmatter and both
**on by default** (see [`authoring-agents.md`](./authoring-agents.md#8-agent-to-agent-a2a-consult--handoff)):

- **Consult** (`a2a`) — calfkit injects a built-in `message_agent(name,
  message)` tool. The agent's LLM calls a peer, the peer answers, and the
  reply folds back into the tool result. The peer answers on a **fresh
  conversation** — it sees only the message, with no replay of prior A2A
  turns (consults are **stateless**).
- **Handoff** (`handoff`) — the agent transfers the turn to a peer, which
  answers the **original** human. The bridge posts the peer's persona
  because the reply is emitter-stamped by the node that actually replied.

The bridge is no longer the A2A *transport* — the consult or handoff
already happened inside the agent runtime. Instead the bridge **observes**
each mention run's event stream, and renders the `message_agent` calls,
peer replies, and handoffs it sees into a unified Discord audit channel.
Kafka is the system of record; Discord is a human-readable audit log.

## Architecture at a glance

```
human mentions an agent
   │
   ▼
bridge  client.agent(<name>).start(...)  ──►  agent runtime
   │                                             │  LLM calls message_agent(peer, msg)
   │  drains handle.stream()  ◄──────────────────┤  or emits a HandoffRequest
   │  (step events: ToolCallEvent / ToolResultEvent / HandoffEvent)
   │
   ├─ A2ADispatcher.classify(event)
   │    pairs each message_agent ToolCallEvent with its ToolResultEvent
   │    by tool_call_id; recognizes HandoffEvents
   │
   └─ A2AProjector.project(...)
        resolve/create the unified audit channel (lazy, cached)
        anchor ONE thread per human turn (keyed by correlation_id)
        post request (caller persona), reply (peer persona),
        and any reject/handoff/fault notes (system "a2a" persona)
```

The dispatcher is **stateful**: there is no `message_agent` step *kind* —
a consult is a `ToolCallEvent` whose name is `message_agent`, and its
reply is a `ToolResultEvent` whose emitter is the *peer*. The dispatcher
records each `message_agent` `tool_call_id` and routes the matching result
to A2A (reliable because a run's steps share one `correlation_id` → single
partition → request-before-reply order, and the handle stream is
lossless and ordered). Everything else on the stream is live progress,
not A2A.

Nested consults reach the bridge too: steps from the whole run tree
publish to the root caller's inbox, so a B→C consult inside an A→B consult
is observable (it carries the same `correlation_id`, `emitter=C`,
`depth>1`) and renders in the same thread.

## Anchoring and personas

- **One thread per human turn.** The projector keys threads by
  `correlation_id` (one per top-level mention), created lazily on the
  first A2A projection for that turn — that first post is the thread's
  starter message. Every later request / reply / reject / handoff / fault
  for the same turn posts into that thread.
- **Thread name** is shaped `caller→peer: <first ~40 chars>` (Discord caps
  thread names at 100 chars; the `→` is `U+2192`).
- **Personas are a pure function** of the agent name —
  `persona_for(name)` → webhook username = the name, avatar = a
  deterministic [DiceBear](https://www.dicebear.com) image seeded by the
  name (`https://api.dicebear.com/9.x/glass/png?seed=<name>`). There is no
  roster lookup and no configured avatar.
- **Meta notes** (rejections, handoffs, faults) are posted under a system
  `a2a` persona, not attributed to any agent — they are annotations, not a
  peer's own words.

## What humans see in Discord

Open the unified audit channel. The flat scrollback contains one starter
message per human turn that produced A2A activity, each anchoring a thread:

```
[#private-a2a-chats]
─────────────────────────────────────────
[Conan]   please summarize the design doc for...
          ↪ Thread: "conan→scribe: please summarize the design doc..."
                    (2 messages)

[Scribe]  what's the latency budget on the ingest path?
          ↪ Thread: "scribe→librarian: what's the latency budget..."
                    (2 messages)
```

Click a thread to see the exchange in order: the caller's consult
(caller persona), the peer's reply (peer persona), and any system notes.

### Finding it from the conversation

You never have to hunt for the right thread. A consult leaves exactly **one**
line in the caller's own step trace, under the caller's persona, linking
straight into the audit thread:

```
💬 consulted `scribe` — view exchange
```

That marker is the *only* thing a consult contributes to the human's thread.
The peer's own work — its preamble, its tool calls — is **private** and never
renders there: those steps reach the bridge only because calfkit flushes every
hop to the root caller (`emitter=<peer>`, `depth>1`), and the drain drops any
step whose emitter is not the agent currently in control of the turn. A handoff
is different: it *transfers* control, so the new owner's steps keep rendering
inline under its own persona.

When the projection failed there is no thread to link, so the marker reads
`⚠️ couldn't write the audit log` instead — the consult still happened, and
saying nothing would hide the gap from the one person watching.

### Reject and fault rendering

Not every A2A event is a peer speaking, so two cases render as system
`a2a` notes rather than peer posts:

| Case | What you see |
|---|---|
| **Rejected consult** (peer offline / cycle / self) | `⚠️ consult to <peer> was rejected: <reason>` |
| **Faulted peer** (no reply came back) | `⚠️ <peer> did not reply — the consult faulted before a response.` |

(Handoffs are no longer rendered here — see the note at the top of this doc.)

The happy-path consult renders the request under the caller's persona and
the reply under the peer's persona.

## Operator setup

### Environment variables

The A2A projection now runs in the **bridge**, so these are read by the
bridge process (they moved off the tools process in the migration):

| Var | Required | Default | Purpose |
|---|---|---|---|
| `CALFKIT_A2A_CHANNEL_NAME` | no | `private-a2a-chats` | Name of the unified audit channel. Lazy-created in the guild on the first A2A projection if absent. |
| `CALFKIT_A2A_CHANNEL_CATEGORY` | no | unset | If set, the unified channel is placed under this category. Category is lazy-created too. Lock the category's permission overwrites once and the channel + threads inherit them. |
| `DISCORD_GUILD_ID` | recommended | — | The guild that hosts the unified channel. The bridge already uses it for slash-command sync. |

Because the bridge is now the only Discord-touching process, the tools
process no longer needs a Discord token for A2A.

### Bot permissions

On the **unified audit channel** (or its category, with inheritance), the
bridge needs:

| Permission | Why |
|---|---|
| View Channel | The bridge has to see the channel to use it. |
| Manage Webhooks | Persona webhooks are created on demand for the projection. |
| Create Public Threads | Each human turn's A2A activity anchors a public thread. |
| Send Messages in Threads | All request / reply / note projections post into threads. |

On the **guild** (server-wide), `Manage Channels` is required for lazy
creation of the unified channel or category if they don't exist yet. The
invite link `disco init` prints grants it, so a fresh install needs no manual
step. An install invited **before** that bit joined the link 403s here
(`error code: 50013`) on its very first consult — re-run the invite to
re-authorize, or create the channel by hand and grant the four channel
permissions above.

`Manage Channels` is only ever exercised on a **discovery miss**, so once the
channel exists it is dead weight. It is also the broadest permission the bot
holds (it covers editing and deleting *any* channel in the guild), so if you
prefer a tighter standing grant you can revoke it after the first consult —
everything above still works, and A2A only breaks again if the channel is
deleted or `CALFKIT_A2A_CHANNEL_NAME` changes.

The projection is **best-effort**: if a post fails (missing permission,
rate-limit, transient 5xx) the bridge logs and continues — a Discord failure
never faults the human turn. But it is not silent. The first failure logs at
**ERROR** with the remedy named inline; while the outage persists, repeats drop
to DEBUG so one broken channel cannot bury the log under identical tracebacks
(a recovery re-arms the loud line). And because the consult marker in the
caller's own thread renders `⚠️ couldn't write the audit log` whenever there is
no thread to link, an audit gap is visible to the person talking to the agents
— not just to whoever reads the log.

## Lifecycle

There is no explicit thread-close affordance. Threads are managed by
Discord's auto-archive (the channel's default, typically 24 hours of
inactivity). Posting via the API auto-unarchives, so a thread revives the
next time the bridge posts into it.

Because consults are stateless and threads are keyed per human turn, there
is no cross-turn continuation and no `thread_id` for an agent to carry —
the old return-value convention is gone. An agent that wants to consult a
peer again simply calls `message_agent` again; the LLM keeps its own
context in its conversation history.

## Failure modes (operator runbook)

| Symptom | Likely cause | Fix |
|---|---|---|
| A2A activity happens but nothing appears in the audit channel | Bot lacks `Manage Channels` (to create it), or `Create Public Threads` / `Send Messages in Threads` / `Manage Webhooks` (to use it) | The consult marker reads `⚠️ couldn't write the audit log`, and the first failure logs at ERROR naming the fix. `403 error code: 50013` on `create_text_channel` means `Manage Channels` — re-run the invite from `disco init`, which now grants it |
| `⚠️ consult to X was rejected` in a thread | The peer is offline, or the consult is a self/cycle call | Bring the peer online; check the calling agent's `a2a` peer list |
| `⚠️ X did not reply — the consult faulted` | The peer errored mid-consult | Check the peer agent's runner logs for the correlation id |
| Unified channel keeps getting recreated | `CALFKIT_A2A_CHANNEL_NAME` differs between bridge restarts, or the channel keeps getting deleted | Pin the env var; check for moderation rules |
| Audit-render WARNs in the bridge log | Discord rate-limit or transient 5xx | Usually self-healing; investigate if persistent |

## What's not in v1

- Cross-turn consult continuation (consults are stateless — a peer never
  sees prior A2A turns replayed).
- An agent-side `list_threads` / thread-management surface.
- Multi-party consults (a `message_agent` call is always 1:1 caller↔peer;
  nested consults fan out but each hop is still 1:1).
- A per-call A2A timeout knob (the old `CALFKIT_TOOLS_TIMEOUT_SECONDS` is
  removed; native `message_agent` has no per-call deadline). The bridge
  bounds a parked human turn with a client-side `result()` timeout, not a
  per-consult one.
- A handoff-loop guard — calfkit has no cycle backstop for handoffs, so
  keep the declared handoff graph acyclic (an A→B→A handoff ring loops).
