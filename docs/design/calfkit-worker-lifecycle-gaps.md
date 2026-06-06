# calfkit `Worker` lifecycle gaps for embedded deployments

> **Status: CLOSED.** All four gaps were filed against calfkit-sdk — Gap 1 →
> [#165], Gap 2 → [#166], Gap 3 → [#167], Gap 4 → [#168] (companion to existing
> [#159]) — and **shipped in calfkit 0.5.2/0.5.4 ([#175])**. 0.5.4 surfaced the
> capabilities through a cleaner API than the kwargs/`serving()` proposed below:
> Worker decorators `@worker.after_startup` / `@worker.on_shutdown` (Gap 1),
> `await worker.start()` / `await worker.stop()` and `async with worker:` (Gaps
> 2+3 — `start()`/`stop()`/`async with` install no signal handlers; only `run()`
> does), and a documented contract in calfkit's `docs/worker-lifecycle.md` (Gap
> 4). **calfcord has adopted all four** — see
> [`calfkit-0.5.4-lifecycle-adoption.md`](./calfkit-0.5.4-lifecycle-adoption.md)
> for the adoption plan. The proposals below are retained as the original
> upstream feature request and as the rationale for why each gap mattered.
> **Filed against:** calfkit `0.3.6`. **Closed in:** calfkit `0.5.4` ([#175]).

[#165]: https://github.com/calf-ai/calfkit-sdk/issues/165
[#166]: https://github.com/calf-ai/calfkit-sdk/issues/166
[#167]: https://github.com/calf-ai/calfkit-sdk/issues/167
[#168]: https://github.com/calf-ai/calfkit-sdk/issues/168
[#159]: https://github.com/calf-ai/calfkit-sdk/issues/159
[#175]: https://github.com/calf-ai/calfkit-sdk/pull/175
> **Evidence repo:** calfcord @ `8bf1a58` (file:symbol references below).
> **Audience:** calfkit maintainers. This doc is self-contained; no calfcord
> knowledge is required to act on it.

## Summary

`calfkit.worker.Worker.run()` is an all-or-nothing, process-owning call. It
works perfectly for the case it was designed for — "this process exists only to
host these nodes" — but it conflates four separable responsibilities into one
blocking method, with no seams. Every deployment that is *more than* a pure node
host is therefore forced to abandon `run()` and re-assemble its behavior from
lower-level, semi-private parts (`register_handlers()`, `client.broker.start()`,
`client.broker.running`), re-implementing the run loop and signal handling by
hand.

In one downstream (calfcord) this has already produced **three** divergent
run-loop implementations across five deployment processes, because three of the
five don't fit `run()`. This request proposes four additive, backward-compatible
changes so embedders can use the managed path instead of forking it.

## The four responsibilities `run()` conflates

`Worker.run()` today (calfkit `0.3.6`, `calfkit/worker/worker.py`):

```python
async def run(self, **extra_run_args: Any) -> None:
    """Blocking method to run worker as a service until stopped."""
    app = FastStream(
        self._client._connection,
        on_startup=[self._on_startup],     # opens MCP sessions + register_handlers
        on_shutdown=[self._on_shutdown],   # closes MCP sessions
    )
    await app.run(**extra_run_args)         # blocks foreground + owns OS signals
```

It bundles:

1. **Register** subscribers/publishers (`register_handlers`).
2. **Resource lifecycle** — open/close MCP sessions (`_on_startup`/`_on_shutdown`).
3. **Broker lifecycle** — start/stop the FastStream broker.
4. **Process ownership** — block the foreground and install OS signal handling
   (via `FastStream.run()`).

There is no supported way to take (1)+(2)+(3) while keeping (4) for yourself, and
no seam to run caller code *between* "broker started" and "begin serving", or
*before* "broker stops".

## Evidence: three run loops for five processes (resolved)

> **Resolved in calfkit 0.5.4.** This section captures the pre-0.5.4 state that
> motivated the request. With the gaps closed, all five processes now run on the
> managed `Worker` lifecycle: the shared `run_worker_until_signal()` helper and
> the two hand-rolled loops are gone (the four standalone runners call
> `worker.run()`; the bridge composes `async with worker:`), so the
> fragmentation below no longer exists. See
> [`calfkit-0.5.4-lifecycle-adoption.md`](./calfkit-0.5.4-lifecycle-adoption.md).

At the time of filing, calfcord ran five worker-bearing processes and only two
could use `Worker.run()`:

| Process | Run loop (then) | Used `Worker.run()`? | Reason it deviated |
|---|---|---|---|
| tools / mcp / router | shared `run_worker_until_signal()` (spawns `worker.run()` as a task) | ✅ | fit the managed path |
| agents | bespoke loop in `agents/runner.py:_run_worker` | ❌ | had to publish presence events at precise lifecycle points |
| bridge | inline `asyncio.wait` race in `bridge/gateway.py:main` | ❌ | co-ran a second foreground service (Discord) + owned signals + composed shutdown across subsystems |

Two independent teams-of-one arrived at two different hand-rolled loops, and the
shared helper itself only existed because `run()` didn't surface a
"clean-exit-without-signal is a crash" contract. The fragmentation was the
symptom — now removed by the 0.5.4 managed-lifecycle adoption.

---

## Gap 1 — No lifecycle hooks around broker start/stop

**The need.** Embedders frequently must run their own async work at two precise
moments that only the broker lifecycle defines:

- **After the broker producer is live, before/as serving begins.** calfcord
  agents publish a presence/`AgentStateEvent` so peers and the bridge learn the
  agent exists. This *must* run after `broker.start()` — calling the producer
  earlier raises `IncorrectState: You can't use producer here, please connect
  broker first`. (Evidence: `agents/runner.py:_amain`, the eager
  `broker.start()` then `publish_state_event` sequence; the bridge's
  `on_ready` discovery ping, `bridge/gateway.py`.)
- **Before the broker stops, while the producer is still alive.** Agents publish
  an `AgentDepartureEvent` on shutdown so peers/the bridge can deregister them.
  (Evidence: `agents/runner.py:_publish_departures_best_effort`, invoked via an
  `on_shutdown_signal` callback *before* drain.)

**Current workaround.** Abandon `run()`; manually `register_handlers()` →
`broker.start()` → publish startup events → wait for signal → publish departures
→ drain. This re-implements the whole loop to get two callback points.

**The irony.** `Worker.run()` already builds a `FastStream` app, and FastStream
already supports `on_startup`, **`after_startup`**, `on_shutdown`, and
`after_shutdown`. The hooks exist one layer down — they're just not surfaced.

**Proposed API (additive):**

```python
async def run(
    self,
    *,
    after_startup: Sequence[Callable[[], Awaitable[None]]] = (),
    on_shutdown: Sequence[Callable[[], Awaitable[None]]] = (),
    **extra_run_args: Any,
) -> None:
    app = FastStream(
        self._client._connection,
        on_startup=[self._on_startup],
        after_startup=list(after_startup),                      # caller hooks (producer live)
        on_shutdown=[*on_shutdown, self._on_shutdown],          # caller hooks first, then MCP close
    )
    await app.run(**extra_run_args)
```

`after_startup` is guaranteed by FastStream to run after the broker is started,
so producer publishes are safe. Defaulting both to `()` keeps every existing
caller unchanged.

**Impact.** This single change lets calfcord move agents back onto the managed
`run()` path and *delete* its bespoke agent loop. It is the highest-leverage,
lowest-risk fix. Cheapest possible win — it surfaces hooks the Worker already
constructs.

---

## Gap 2 — No embeddable / non-blocking lifecycle API

**The need.** A process may host calfkit nodes *alongside* another long-lived
foreground service that owns the event loop. calfcord's Discord bridge is the
canonical case: its real foreground is the Discord gateway WebSocket
(`discord.Client.start()`), which blocks. The calfkit `Worker` there is used
**only for handler registration** — never run — because the bridge cannot cede
the foreground to `Worker.run()` when Discord already holds it. (Evidence:
`bridge/gateway.py:main` — `worker.register_handlers()` then a manual
`broker.start()`, then `asyncio.wait({gateway_task, stop_task})`.)

The bridge also needs subscriber consumer-groups to **join before** it starts
accepting Discord events and before it publishes a discovery ping — otherwise
replies arriving in the gap are lost (`auto_offset_reset="latest"`). That
ordering requires explicit control over register-vs-start-vs-serve.

**Current workaround.** Drive `client.broker.start()` / `client.broker.running`
directly and re-implement the serve loop. These are lower-level than the public
`Worker` contract (a code comment in the bridge even calls `broker.running`
"public-ish").

**Proposed API (additive):** a managed, non-blocking lifecycle that does
register + resource-open + broker-start on enter and drain + resource-close +
broker-stop on exit, **without** owning the foreground or signals:

```python
@asynccontextmanager
async def serving(self) -> AsyncIterator[None]:
    """Register handlers, open resources, start the broker; reverse on exit.
    Does NOT block the foreground or install signal handlers — the caller
    owns the run loop and shutdown."""
    ...

# Embedder usage:
async with worker.serving():
    await my_other_foreground_service.run_until_signal()
```

(Equivalently, bless `await worker.start()` / `await worker.stop()` as public
methods.) This lets a caller compose the worker as one managed component among
several under its own supervisor.

---

## Gap 3 — Signal handling can't be opted out

**The need.** When a worker is composed under a parent supervisor (Gap 2), the
parent owns SIGINT/SIGTERM so it can drain *all* its subsystems in order. Today
`Worker.run()` (via `FastStream.run()`) installs its own signal handling, which
collides with a caller that also installs handlers — the bridge hits exactly
this and avoids `run()` partly because of it. (Evidence: `bridge/gateway.py`
installs `loop.add_signal_handler(...)` itself and notes the overlap with
FastStream's handlers as a reason it does not call `run()`.)

**Proposed API (additive):**

```python
async def run(self, *, install_signal_handlers: bool = True, ...) -> None: ...
```

When `False`, forward FastStream's equivalent so the caller's supervisor owns
shutdown. Complements Gap 2 for callers who prefer `run()` over `serving()`.

---

## Gap 4 — The embedding contract is unspecified (and a comment has already gone stale)

**The need.** Embedders currently reverse-engineer ordering and idempotency
guarantees from source. Concretely, they need documented answers to:

- When do subscribers actually **join their consumer groups** relative to
  `broker.start()`? (Drives the "register before serving" correctness the bridge
  depends on.)
- Is `register_handlers()` **idempotent**? In `0.3.6` it is (guards on
  `self._prepared`, logs and returns). But a downstream comment asserts the
  opposite — "Calling Worker.run would call register_handlers again (which errors
  on the second call)" (`bridge/gateway.py`). The downstream's correctness
  rationale has **drifted from upstream behavior**, which is precisely what
  happens when the embedding contract isn't written down.
- Is `client.broker.running` a **supported** flag to branch on, or an internal?
  (Downstreams use it to make `broker.start()` idempotent and currently hedge by
  calling it "public-ish".)

**Proposal.** Document the lifecycle ordering and idempotency guarantees, and
either bless `client.broker.running` or expose a supported `worker.is_running` /
lifecycle-state accessor. Low code cost, high clarity payoff — and it prevents
the stale-comment class of bug above.

---

## Suggested sequencing

1. **Gap 1 (hooks)** — smallest change, unblocks the most (agent unification);
   ship first.
2. **Gap 4 (contract docs + `is_running`)** — no/low code; do alongside Gap 1.
3. **Gap 2 (`serving()` / `start()`+`stop()`)** — the structural one; unblocks
   embedding the worker beside a foreign foreground service (the bridge).
4. **Gap 3 (opt-out signals)** — completes Gap 2 for `run()`-preferring callers.

All four are **additive and backward compatible**: existing `Worker(...).run()`
callers are unaffected by any of them.

## What each gap unblocks downstream

- After **Gap 1**, calfcord can fold agents/tools/mcp/router into a single
  generic deployer on the managed `run()` path and delete its bespoke agent loop.
- After **Gaps 2+3**, the Discord bridge becomes just another node host under one
  supervisor, collapsing all three downstream run loops into one and removing all
  reliance on semi-private broker internals.
