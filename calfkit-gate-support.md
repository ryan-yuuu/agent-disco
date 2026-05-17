# Calfkit — Gate Support for Node Filtering

A specification for an additive feature on `BaseNodeDef` (and its
subclasses) that lets a node decide, before its `run()` method is invoked,
whether to process or skip an inbound event.

## 1. Summary

Add an optional `gate` parameter to `BaseNodeDef.__init__`. When set, the
gate is invoked in `BaseNodeDef.handler()` after `prepare_context` and
before `run`. If the gate returns `False`, the handler returns early
without invoking `run`, without publishing any action, and without
mutating state. The Kafka offset commits normally — the message is
considered consumed.

The gate is the canonical "is this event for me?" extension point.

## 2. Motivation

In a Kafka-native multi-agent topology where many agents subscribe to the
same channel topic — each with its own consumer group, so each receives
every message — every agent needs to independently decide whether a given
message is intended for it. The decision depends on metadata carried in
`SessionRunContext` (e.g., slash addressing, author identity, thread
membership, fact-scope tags).

Doing this filtering inside `run()` is wrong: by the time `run()` is
called on an `Agent` node, the model has already been invoked. Tokens are
spent before the agent knows the message is not addressed to it.

A gate moves the decision earlier in the handler, gives it the same
context `run()` would see, and short-circuits cleanly. It is the smallest
possible addition that unblocks consumer-group-per-agent designs.

## 3. Public API

### 3.1 New parameter

`BaseNodeDef.__init__` (and therefore all subclasses, including
`BaseAgentNodeDef` / `Agent`, `NodeDef`, `ToolNodeDef`) gains:

```python
gate: GateFunction | None = None
```

Default `None` preserves current behavior — the node always runs.

### 3.2 Type alias

Defined alongside the node types (e.g., in `calfkit/nodes/base.py` or a
new `calfkit/nodes/gate.py`):

```python
from collections.abc import Awaitable, Callable
from calfkit.models.session_context import SessionRunContext

GateFunction = Callable[[SessionRunContext], bool | Awaitable[bool]]
```

The gate accepts the prepared `SessionRunContext` and returns either a
`bool` or an awaitable resolving to a `bool`. Sync and async gates are
both supported.

### 3.3 Usage

```python
def scheduler_gate(ctx: SessionRunContext) -> bool:
    discord = ctx.deps.provided_deps.get("discord")
    if discord is None:
        return False
    if discord["author"].get("agent_id") == "scheduler":
        return False  # self-recognition
    return (
        discord.get("kind") == "slash"
        and discord.get("slash_target") == "scheduler"
    )

scheduler = Agent(
    node_id="scheduler",
    subscribe_topics=["discord.thread.123456789"],
    model_client=...,
    gate=scheduler_gate,
)
```

## 4. Implementation specification

### 4.1 Storage

In `BaseNodeDef.__init__` (currently in `calfkit/nodes/base.py`):

```python
self._gate: GateFunction | None = gate
```

The subclasses `BaseAgentNodeDef`, `NodeDef`, `ToolNodeDef`, and any
others must accept and forward `gate` to `super().__init__()`.

### 4.2 Gate evaluation

Add a private method on `BaseNodeDef`:

```python
async def _evaluate_gate(
    self,
    ctx: SessionRunContext,
    correlation_id: str,
) -> bool:
    """Return True if the node should process this event, False to skip."""
    if self._gate is None:
        return True
    try:
        result = self._gate(ctx)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception:
        logger.exception(
            "[%s] gate raised for node=%s; treating as reject",
            correlation_id[:8],
            self.node_id,
        )
        return False
```

### 4.3 Hooking into the handler

Modify `BaseNodeDef.handler` to call the gate after `prepare_context` and
before `run`:

```python
async def handler(
    self,
    envelope: Envelope,
    correlation_id: Annotated[str, Context()],
    broker: BrokerAnnotation,
) -> Envelope:
    logger.debug("[%s] handler entered node=%s", correlation_id[:8], self.node_id)
    ctx = await self.prepare_context(envelope)

    if not await self._evaluate_gate(ctx, correlation_id):
        logger.debug("[%s] gated out node=%s", correlation_id[:8], self.node_id)
        return envelope

    if self._run_accepts_input and envelope.internal_workflow_state.current_frame.input_args is not None:
        output = await self.run(ctx, *envelope.internal_workflow_state.current_frame.input_args)
    else:
        output = await self.run(ctx)

    logger.debug("[%s] run() returned action=%s node=%s", correlation_id[:8], type(output).__name__, self.node_id)
    return await self._publish_action(output, envelope, correlation_id, broker)
```

The early `return envelope` path causes the FastStream / Kafka subscriber
to commit the offset without publishing anything downstream. No action is
taken; the message is dropped silently.

## 5. Semantics

These are the required behaviors. Each maps to a test case in §6.

| Scenario | Behavior |
|---|---|
| No gate provided (`gate=None`) | `run()` is called; current behavior preserved. |
| Sync gate returns `True` | `run()` is called. |
| Sync gate returns `False` | `run()` is NOT called. Handler returns `envelope` unmodified. No publish. |
| Async gate returns `True` | `run()` is called. |
| Async gate returns `False` | `run()` is NOT called. Handler returns `envelope` unmodified. |
| Gate raises an exception | Exception is logged with `correlation_id` and `node_id`. Treated as reject. `run()` is NOT called. Exception is NOT propagated. |
| Gate returns a non-bool truthy value (e.g., `1`, `"yes"`) | Cast to `bool` via `bool(result)`. `1` and non-empty strings count as accept. |

### 5.1 Properties to preserve

- Gate runs **after** `prepare_context`, so the gate sees the same context the node would, including any `overrides` applied by `prepare_context`.
- Gate runs **before** any `run()` invocation, so no model tokens are spent on rejected messages.
- Gate does not receive `current_frame.input_args`. Gates filter on the event itself, not on per-invocation arguments. Tool nodes that need argument-aware filtering can implement that inside `run()`.
- Gate is registered at node construction time and is not mutable at runtime.

### 5.2 Idempotency

A gate may be called multiple times for the same logical event if Kafka
redelivers (e.g., after a consumer restart before offset commit). Gate
functions must be deterministic — same context in, same answer out — and
must not have side effects. Document this constraint in the README.

## 6. Testing requirements

Add `tests/nodes/test_gate.py` (or wherever node tests live). Use a
minimal `BaseNodeDef` subclass whose `run` is a `MagicMock` so each test
can assert call vs. no-call cheaply.

Required cases:

1. **No gate** — `run()` is called exactly once; return matches the mocked publish path.
2. **Sync gate returning `True`** — `run()` is called.
3. **Sync gate returning `False`** — `run()` is not called; handler returns the original envelope; no publishes hit the broker.
4. **Async gate returning `True`** — `run()` is called.
5. **Async gate returning `False`** — `run()` is not called.
6. **Gate raises `RuntimeError`** — `run()` is not called; the exception is captured by the logger (assert via `caplog`); the handler returns normally.
7. **Gate receives the post-`prepare_context` context** — assert the gate sees `ctx.state` and `ctx.deps` with the values produced by `prepare_context`, including any `overrides` applied from `current_frame.overrides`.
8. **Non-bool truthy/falsy return** — `1` → accept; `0` → reject; `"yes"` → accept; `""` → reject.

Each case should be self-contained with no network I/O. A fake `KafkaBroker` (or the `broker.publish` mock) verifies that no downstream publishes occur on reject.

## 7. Backward compatibility

Strictly additive. Existing callers that do not pass `gate` get the
current behavior. No public signatures change beyond the new optional
parameter. No existing test in Calfkit should require modification.

## 8. Documentation

Update the Calfkit README (or developer docs) with:

- A short section "Gating node invocations" describing the parameter, the signature, and the early-short-circuit semantics.
- One end-to-end example: a node that subscribes to a shared topic and uses a gate to filter only events addressed to it.
- The idempotency note from §5.2.
- A pointer to the test file as the executable contract.

No changelog entry beyond the standard "Added: optional `gate` parameter on `BaseNodeDef`."

## 9. Non-goals

Explicit exclusions to keep this feature minimal:

- **No predicate / composable gate helper library.** Composing gates is an application-level concern. This spec ships the mechanism only.
- **No middleware-style gate chain.** A node has zero or one gate. To compose, write a single gate function that does the composition.
- **No gate-driven routing.** The gate returns a `bool`. It does not return a target topic, a redirect, or an action. Conditional routing belongs in `run()`.
- **No retry-on-reject semantics.** Reject means "skip and commit." It is not a soft failure or a deferred retry.
- **No dynamic gate replacement.** Gates are set at node construction and remain fixed for the node's lifetime.
- **No instrumentation hooks on the gate.** A future spec can add gate-call metrics if needed; v1 relies on the existing log line.
