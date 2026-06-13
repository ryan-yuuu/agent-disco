# Bridge/router clients publish fire-and-forget via `send(reply_to=...)`, never awaiting their own reply inbox

## Context

calfkit 0.10.0 reworked the client caller API (breaking, calf-ai/calfkit-sdk#215):
`invoke_node` → `start`/`send`, `execute_node` → `execute`. The new
`Client.send(..., reply_to=...)` is a one-way publish that registers **no** reply
future, with an optional `reply_to` "return address" the worker delivers the
terminal result to for *someone else* to consume. It guards against a footgun:
`send` raises `ValueError` if `reply_to` equals the calling client's **own** reply
inbox (the reply would hit the client's dispatcher, find no future, and be dropped).

Before this, calfcord's bridge and router fired `invoke_node` and **immediately
cancelled the returned future** at every publish site — because the actual reply
is consumed by a *separate consumer group* (the outbox consumer on
`discord.outbox`, or the fan-out reading the router's `publish_topic`), never by
the originating client. That invoke-then-cancel dance was pure ceremony forced by
the old API, and it relied on the bridge `Client` naming `discord.outbox` as its
own `reply_topic` purely as a mechanism to set agents' `callback_topic`.

## Decision

Adopt the new API and stop awaiting replies the bridge/router never read:

- The bridge `Client` (and the router `Client`) connect with a **private reply
  inbox they never await** — the bridge takes an auto-generated one; the router
  keeps a distinct named one. They never call `start`/`execute`.
- Agent replies are addressed explicitly via `send(reply_to=...)`:
  - **`reply_to="discord.outbox"`** for the bridge's slash ingress and outbox
    retry — the outbox consumer (a separate group) posts each to Discord.
  - **`reply_to=None`** for the bridge's ambient publish and the router's fan-out
    publish — true fire-and-forget; the router's `RoutingDecision` flows forward
    on its `publish_topic` (`routing.decisions`), not back as a callback.
- A2A (`private_chat`) keeps awaiting its reply and uses `execute`.

Decoupling the bridge client's inbox from `discord.outbox` is the load-bearing
part: it is what lets `send(reply_to="discord.outbox")` clear the new guard.

## Consequences

- `discord.outbox` is no longer provisioned as the bridge client's framework reply
  topic; it is provisioned as the **outbox consumer's `subscribe_topics`** via the
  managed `Worker` lifecycle (already the case). A regression test pins that the
  bridge never re-claims it as its own inbox.
- The `_calf.ambient.callback-discard` topic and `router_infra_topics()` are
  **deleted** — with `reply_to=None` there is no terminal callback to route, which
  also removes that topic's plaintext-retention/privacy hazard (it had carried a
  copy of every ambient message's `deps`).
- The reply dispatcher no longer subscribes to `discord.outbox`, retiring the
  "no pending future" WARN logged on every agent reply.

## Considered options

- **Keep `invoke_node` + cancel the future** — not possible; 0.10.0 removed it.
- **Use `start()` + cancel the future** — perpetuates the anti-pattern (a
  registered-then-discarded future and a dispatcher subscription the bridge never
  uses), and `start` has no `reply_topic` override, so it cannot target
  `discord.outbox` anyway. Rejected.
- **Keep `reply_topic="discord.outbox"` on the bridge client** — rejected: the new
  `send` guard rejects `reply_to == own inbox`, so the bridge could not address its
  own outbox topic. Decoupling the inbox is required, not optional.
