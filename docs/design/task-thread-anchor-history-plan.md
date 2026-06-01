# Plan: include a thread's starter message in fetched history

## Problem

Agents working a `/task` thread lose the original task instruction on every
turn *after* the first.

`/task` opens a thread off the user's own message via Discord's
"Start Thread from Message" API (`gateway.py:454`,
`message.create_thread(...)`). Two Discord facts make the anchor invisible to
our history fetcher:

1. **The starter message's id equals the thread's id.** Confirmed in the
   discord.py docs: *"the thread starter message ID is the same ID as the
   thread."* That is why the observed thread id (`1510795249998364863`) is
   identical to the `/task` message id.
2. **The starter message physically lives in the parent channel, not the
   thread.** `Thread.history()` only enumerates messages posted *inside* the
   thread, so it never yields the starter. discord.py exposes the starter
   separately via the cache-only `Thread.starter_message` property.

Compounding this, `history(before=…)` is **exclusive** ("messages before this
id"). Because `thread.id == starter.id` and every in-thread message has a
larger snowflake, a thread-scoped fetch anchored at `before=thread.id` could
not return the starter even if it were in the thread.

### Where the project loses it

- `normalize_task` sets `source_channel_id = thread_id` (`normalizer.py:183`),
  so all history for the task is thread-scoped (`ingress.py:677`,
  `history.py:463-466`).
- The task text survives **only** as the first turn's `user_prompt`
  (`ingress.py:464`, carried through the router fan-out via `model_copy`).
- On every follow-up turn, `message_history` is rebuilt purely from
  `thread.history()`, which never contains the anchor → the original
  instruction is gone.

This is **agent-agnostic** (not codex-specific): it affects any agent on any
follow-up turn in a message-started thread.

## Goal / non-goals

- **Goal:** the thread's starter message appears in the projected
  `message_history` on the turns where it belongs, for `/task` threads and any
  other message-started thread, with no schema or wire changes.
- **Non-goal (explicitly out of scope):** *pinning* the anchor against the
  per-agent `history_turns` trim so it survives past N thread messages. The
  anchor is the oldest record, so on a thread longer than `history_turns`
  (default 30) it ages out. That is acceptable for v1 and noted below.

## Design

Recover the starter message inside `ChannelHistoryFetcher._do_fetch` and
prepend it as the oldest record. The fetcher is the single place that already
owns "what counts as this channel's history," and a human reading the thread
in Discord *does* see the starter at the top — so this is the semantically
honest layer. No new wire fields, no synthetic records, no schema change.

### Key insight: apply Discord's `before` exclusivity to the anchor too

The anchor must appear in history for **follow-up** turns, but must **not**
duplicate on the **first** turn — where the anchor is simultaneously the
triggering message *and* the `user_prompt`. (For a normal `@agent` message the
triggering message is naturally excluded from history by `before=` exclusivity
and supplied only as `user_prompt`; we must preserve that invariant.)

The two cases are distinguished by `before_message_id`:

| Turn | `before_message_id` | vs. `starter.id` (== thread id) |
|------|---------------------|---------------------------------|
| First (`/task` itself) | `wire.message_id == thread.id` | `starter.id == before` → exclude |
| Any follow-up          | later message id `> thread.id` | `starter.id <  before` → include |

So the prepend is guarded by **`starter.id < before_message_id`** — which is
exactly Discord's exclusive-`before` rule applied to the anchor. The anchor
ends up behaving precisely as if it were an ordinary in-window message; the
only reason it needs manual handling is that it lives in the parent channel so
the API never returns it. This single condition handles first-turn exclusion
and follow-up inclusion with no special-casing of `/task`.

Ordering is automatic: `starter.id == thread.id` is smaller than every
in-thread message id, so inserting at position 0 (oldest) is always correct.

### Interaction with `/clear` (intended behavior)

The starter is prepended **before** the existing clear-marker scan
(`history.py:503-509`). Consequence: a `/clear` posted *inside* the thread
truncates the anchor along with everything above the marker. This is the
correct semantics — `/clear` means "forget the prior conversation," and in a
task thread the task statement is part of that conversation. Documented and
tested so the choice is explicit (the alternative, prepend-after-scan, would
make the anchor a permanent pin that `/clear` cannot remove — rejected as
inconsistent with "clear means clear").

### Duck-typing (mirrors the existing convention)

The "is this a thread?" check duck-types on `parent_id`, exactly as
`normalizer._resolve_channel_id` already does (`normalizer.py:68`, documented
there as "so tests can use simple fakes"). A `discord.Thread` has `parent_id`;
`TextChannel`/`VoiceChannel` expose `category_id`, not `parent_id`. This keeps
test fakes as plain `SimpleNamespace`/`MagicMock` without `spec=discord.Thread`
gymnastics and avoids a hard isinstance dependency.

### Failure modes → graceful degrade to today's behavior (return `None`)

`_thread_starter_message` returns `None` (⇒ unchanged thread-only history) for:

- a non-thread channel (`parent_id` absent);
- a thread with no starter — created standalone, or the starter was deleted
  (`parent.fetch_message` → `discord.NotFound`);
- an uncached parent that cannot be resolved;
- any Discord error, logged like the sibling fetch failures
  (`Forbidden` deduped once per parent channel via `_log_forbidden_once`;
  `HTTPException` at WARN).

The method is **total** (never raises into the invocation path), matching the
fetcher's existing "never raises" contract.

### Cost

Cheap, with a REST fetch as the realistic common path.

`Thread.starter_message` is a pure read of **discord.py's in-memory message
cache** — the `Client.cached_messages` deque, bounded by `max_messages`
(default 1000), populated from `MESSAGE_CREATE` gateway events (our `messages`
intent is on). It is *not* our cache and there is no `/task`-specific caching.
A hit requires the anchor to still be in that global, recency-bounded deque
**and** the process not to have restarted. Since the anchor is only prepended
on **follow-up** turns — which may arrive long after creation, after restarts,
or on busy servers where 1000 messages churn quickly — expect a cache **miss**
in the general case, i.e. one REST `parent.fetch_message(thread.id)`. The
cache hit is opportunistic (a follow-up soon after creation within the same
process lifetime), not the steady state.

The REST cost is still well-contained: the whole result (anchor included) is
stored in the fetcher's existing TTL/LRU cache keyed on
`(source_channel_id, before_message_id, limit)` and is single-flighted, so an
N-way router fan-out for one user turn still costs a single fetch, not N.

### Contract note

When the source is a message-thread and the anchor is in-window, the returned
list may contain one record beyond `limit` (the prepended starter). Callers
already re-trim to their own `history_turns` (`ingress.py:747-748`,
`_publish_ambient`), so this is harmless; documented in the method/docstring.

## Implementation

All changes in `src/calfkit_organization/bridge/history.py`.

### 1. New method `ChannelHistoryFetcher._thread_starter_message`

```python
async def _thread_starter_message(self, channel: Any) -> Any | None:
    """Recover a message-thread's starter message, or ``None``.

    A thread created from a message (Discord's Start-Thread-from-Message —
    how /task threads and manual message-threads are made) keeps that
    starter message in the PARENT channel, not the thread, so
    ``thread.history()`` never yields it. Its id equals the thread id.
    Try discord.py's in-memory message cache first via ``starter_message``
    (the ``max_messages`` deque; usually a miss on later turns / after a
    restart); on a miss, one REST ``parent.fetch_message(thread.id)``.

    Duck-typed on ``parent_id`` (mirrors ``_resolve_channel_id``) so a
    non-thread channel returns ``None`` and tests can use plain fakes.
    Total — every Discord error degrades to ``None`` (today's thread-only
    history), logged like the sibling fetch failures.
    """
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        return None  # not a thread
    # discord.py's in-memory message cache (the max_messages deque,
    # populated from MESSAGE_CREATE). Opportunistic — usually a miss on
    # later turns or after a restart, in which case we fall to REST below.
    starter = getattr(channel, "starter_message", None)
    if starter is not None:
        return starter  # in-memory cache hit — no REST
    parent = getattr(channel, "parent", None)
    if parent is None:
        parent = await self._resolve_channel(parent_id)
    if parent is None or not hasattr(parent, "fetch_message"):
        return None
    try:
        return await parent.fetch_message(channel.id)
    except discord.NotFound:
        return None  # standalone thread or deleted starter — not an error
    except discord.Forbidden:
        self._log_forbidden_once(parent_id)
        return None
    except discord.HTTPException as e:
        logger.warning(
            "channel_id=%d: starter-message fetch failed status=%s: %s",
            channel.id, e.status, e,
        )
        return None
```

### 2. Prepend in `_do_fetch`, before the marker scan

Inside the existing `try:` that builds `ordered` (`history.py:502-509`), right
after `ordered = list(reversed(messages))`:

```python
ordered = list(reversed(messages))
# Recover a message-thread's starter (it lives in the parent channel, so
# thread.history() never yields it) and prepend it as the oldest entry —
# but only when it falls within this fetch's exclusive `before=` window
# (starter.id < before_message_id). That mirrors Discord's `before`
# semantics: on the first /task turn the anchor IS the triggering message
# and the user_prompt, so it stays excluded exactly as a normal trigger
# would; on every later turn it is included. Prepended BEFORE the clear
# scan so a /clear inside the thread truncates the task statement too.
starter = await self._thread_starter_message(channel)
if starter is not None and starter.id < before_message_id:
    ordered.insert(0, starter)
```

The anchor flows through the unchanged `_to_record` → `project_history`, so a
human-authored anchor projects as a `<author>`-prefixed `ModelRequest` just
like any other message.

### 3. Docstrings

- Add a short paragraph to the module docstring (`history.py`) under the
  existing "Why the fetcher uses `source_channel_id`" note, explaining the
  starter-recovery and the `before`-exclusivity reuse.
- Add one line to `_do_fetch`'s docstring noting the starter prepend.

## Tests (`tests/bridge/test_history.py`)

Add a `_FakeThread` helper (a `_FakeChannel` plus `parent_id`,
`starter_message`, and `parent`) and a `_FakeParent` whose `fetch_message` is
an `AsyncMock`. New `TestThreadStarterMessage` class:

1. **Follow-up includes anchor (REST path):** `starter_message=None`,
   `parent.fetch_message` returns the anchor; `before` > thread id → anchor is
   the oldest record; `fetch_message` awaited once.
2. **Cache-hit path (no REST):** `starter_message` set → returned; assert
   `parent.fetch_message` **not** awaited.
3. **First-turn exclusion:** `before_message_id == thread.id (== starter.id)`
   → anchor **absent** (preserves trigger-not-in-history invariant).
4. **Non-thread channel:** plain `_FakeChannel` (no `parent_id`) → no prepend,
   no parent fetch attempted.
5. **Standalone/deleted starter:** `fetch_message` raises `NotFound` → no
   prepend; thread history unchanged.
6. **Forbidden on parent:** `fetch_message` raises `Forbidden` → no prepend;
   `_log_forbidden_once` fires once for the parent id.
7. **`/clear` truncates the anchor:** marker present in thread; anchor
   prepended then dropped by the scan → asserts the intended clear semantics.
8. **Uncached parent fallback:** `channel.parent = None`, `parent_id` set,
   `client.get_channel(parent_id)` returns the parent → REST fetch succeeds.

Existing fetcher and clear-marker tests must remain green (the prepend is a
no-op for non-thread channels and gated by the `before` window).

## Verification

- `uv run pytest tests/bridge/test_history.py`
- `uv run pytest tests/bridge` (regression: ingress/synthesized/clear paths)
- `uv run ruff check` on the changed file.

## Risk

Low. Single file, additive, gated, total (never raises), and degrades to
current behavior on every failure path. No schema, wire, or call-site changes.
```
