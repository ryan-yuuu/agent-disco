# Sticky Replies Implementation Plan

Status: planned

## Goal

Add sticky replies so a user can explicitly route a Discord message with `!name`, then continue the conversation without repeating `!name`. Ambient human messages in a sticky conversation route to the owner of the last successful visible agent reply in that channel or thread. `!unstick` clears that routing until a later explicit `!name` produces another successful agent reply.

This feature belongs in the Discord bridge. The bridge is the singleton Discord gateway, owns Discord I/O, receives human messages, starts agent runs, and posts terminal agent replies through persona webhooks.

## Decisions

- Sticky ownership is scoped by Discord `source_channel_id`.
- A thread has its own sticky owner, separate from its parent channel.
- Sticky ownership is active bridge-local state, not inferred from Discord history at send time.
- Sticky ownership is persisted in the existing bridge-local SQLite database.
- `!unstick` clears the sticky owner and posts one short bridge-authored confirmation.
- `!unstick <text>` also clears the sticky owner, but does not route the trailing text to an agent.
- Explicit `!name` always bypasses the current sticky owner.
- A failed explicit `!name` invocation does not clear or replace the existing owner.
- Sticky owner changes only after a successful visible terminal agent reply.
- `settings.json` is bridge-side install config, seeded with sticky replies enabled by default.
- `unstick` is a reserved agent name because it collides with a Discord routing command.

## Configuration

Add a bridge-side settings file:

```json
{
  "sticky_replies": {
    "enabled": true
  }
}
```

Path resolution:

1. `CALFCORD_SETTINGS`, if set.
2. `$CALFCORD_HOME/config/settings.json`, for native installs.
3. `./settings.json`, for development runs.

The installer seeds `$CALFCORD_HOME/config/settings.json` once, next to `.env` and `mcp.json`, with mode `0600`. Existing files are never clobbered.

The bridge loads settings at startup. Invalid JSON or invalid schema should fail bridge startup with an operator-actionable error. A missing settings file defaults to sticky replies enabled, matching the seeded native default.

When sticky replies are disabled:

- explicit `!name` routing still works;
- ambient messages are ignored;
- sticky owner rows remain in SQLite and are ignored until the feature is re-enabled;
- successful replies do not update sticky owner state.

## SQLite Model

Extend the existing bridge-local SQLite schema in `TranscriptStore`:

```sql
CREATE TABLE IF NOT EXISTS sticky_conversations (
  conversation_key TEXT PRIMARY KEY,
  owner_agent_id   TEXT NOT NULL,
  updated_at       INTEGER NOT NULL
);
```

Field meanings:

- `conversation_key`: `str(req.source_channel_id)`.
- `owner_agent_id`: the actual terminal responder, `result.emitter_node_id or target`.
- `updated_at`: epoch seconds, for diagnostics and future cleanup.

No row means the conversation is not sticky. `!unstick` deletes the row.

## Python Interface

Add a narrow store protocol for the bridge and handler to depend on:

```python
@dataclass(frozen=True, slots=True)
class StickyConversation:
    conversation_key: str
    owner_agent_id: str
    updated_at: int


class StickyStoreLike(Protocol):
    enabled: bool

    async def get_owner(self, conversation_key: str) -> str | None: ...
    async def set_owner(self, conversation_key: str, owner_agent_id: str) -> None: ...
    async def clear_owner(self, conversation_key: str) -> None: ...
```

Implement these methods on `TranscriptStore` and `NullTranscriptStore`. The null implementation returns `None` and no-ops writes so the bridge still runs if the SQLite store cannot open.

## Ingress Routing

Update `DiscordIngressGateway._on_message`:

1. Keep existing filters for DMs, wrong guilds, pre-ready messages, and bot-authored non-webhook messages.
2. Ignore ambient webhook-authored messages so agent persona replies cannot trigger sticky routing loops.
3. Normalize the message before routing decisions that need `source_channel_id`.
4. Detect `!unstick` as a standalone leading command token, case-insensitive, with optional trailing text.
5. On `!unstick`, clear `str(wire.source_channel_id or wire.channel_id)`, post a short confirmation such as `Sticky replies cleared for this thread.`, and stop.
6. Extract explicit `!name` mentions.
7. If explicit mentions exist, spawn the handler exactly as today.
8. If no explicit mentions and sticky replies are enabled, look up sticky owner by conversation key.
9. If no owner exists, ignore the ambient message.
10. If owner exists, synthesize `mention_ids=(owner,)`, mark the request as sticky-routed, and spawn the handler.

The handler's existing roster check remains authoritative. If the sticky owner is offline, the ambient turn should not clear ownership. The recommended user-facing notice is:

```text
This conversation is sticky to `!<owner>`, but that agent is offline. Use `!unstick` or address another agent with `!name`.
```

Add an explicit request-origin field to `MentionRequest`, such as `route_kind: Literal["explicit", "sticky"]`, so the handler can choose the sticky-specific offline notice without guessing from the mention list.

## Ownership Updates

Update `MentionHandler._deliver` so sticky owner writes happen only after visible terminal reply delivery:

- `post_reply` returns `ok`: update owner to final responder.
- retry succeeds: update owner to the retry result's final responder.
- `post_chunked` returns `True`: update owner to final responder.
- `post_reply` returns `dropped`: do not update.
- `post_chunked` returns `False`: do not update.
- `result.output` is empty and no Discord message is posted: do not update.
- run fault or retry fault: do not update.

This preserves the rule that ownership follows what users can see in the Discord channel.

## Reserved Agent Name

Add `unstick` to the reserved agent-name checks in `calfcord.agents.identifier`. The error should name the command collision, not a process slot collision:

```text
'unstick' is reserved for the Discord !unstick routing command — pick another agent name
```

This blocks creation, rename, and manually-authored `agents/unstick.md` files through the existing validation chokepoint.

## Testing Plan

Write tests before implementation.

Settings:

- missing settings file defaults to sticky replies enabled;
- valid settings file loads;
- invalid JSON fails with a clear error;
- invalid schema fails with a clear error;
- path resolution honors `CALFCORD_SETTINGS`, then `CALFCORD_HOME`, then dev fallback.

Installer:

- seeds `config/settings.json` once;
- never clobbers an existing settings file;
- writes mode `0600`.

SQLite:

- `set_owner` then `get_owner`;
- `set_owner` upserts a new owner;
- `clear_owner` deletes the row idempotently;
- owner persists across store reopen;
- null store methods no-op.

Gateway:

- ambient message is ignored when disabled;
- ambient message routes to sticky owner when enabled;
- explicit `!name` bypasses sticky owner;
- `!unstick` clears owner, posts confirmation, and invokes no agent;
- `!unstick <text>` clears owner, posts confirmation, and invokes no agent;
- ambient webhook-authored message invokes no agent;
- explicit webhook-authored `!name` keeps existing behavior.

Handler:

- successful explicit reply updates owner to emitter;
- handoff result updates owner to handoff responder;
- retry success updates owner to retry responder;
- chunk fallback success updates owner;
- dropped reply does not update;
- full chunk failure does not update;
- run fault does not update;
- empty reply does not update;
- failed explicit `!name` leaves prior owner unchanged.

Reserved name:

- `unstick` is rejected by `AgentDefinition`;
- `disco agent create` reprompts on `unstick`;
- `disco agent rename` rejects `unstick`.

## Implementation Order

1. Add settings model, loader, path resolver, and tests.
2. Seed `settings.json` in the installer and update installer tests.
3. Extend `TranscriptStore` schema and add sticky store methods with tests.
4. Add `unstick` to reserved agent names and update tests.
5. Add sticky command parsing and ambient sticky routing in the gateway with tests.
6. Inject sticky store/settings into the gateway and handler wiring.
7. Update handler delivery success paths to persist sticky owner with tests.
8. Update user docs after behavior is implemented, especially configuration and usage docs.

## ADR Check

This does not need a new ADR. The choice to keep sticky state in the bridge-local SQLite database follows existing bridge ownership: the bridge is the singleton Discord I/O process, and the existing transcript store already holds bridge-local runtime state. The decision is documented here and can be revisited if the bridge stops being singleton.
