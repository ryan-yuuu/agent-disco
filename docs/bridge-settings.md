# How to configure bridge settings

This guide shows you how to change bridge behavior stored in `settings.json`.
Use this file for runtime behavior toggles such as sticky replies. Keep secrets
and process wiring in `config/.env` instead.

## Find the file

On a native install, edit:

```bash
$EDITOR ~/.agent-disco/config/settings.json
```

The installer creates this file with sticky replies enabled:

```json
{
  "sticky_replies": {
    "enabled": true
  }
}
```

If `CALFCORD_HOME` points somewhere else, the bridge reads:

```text
$CALFCORD_HOME/config/settings.json
```

For a one-off path, set `CALFCORD_SETTINGS` to the settings file path before
starting the bridge. In local development with no `CALFCORD_HOME`, the fallback
path is `./settings.json`.

## Disable sticky replies

Set `sticky_replies.enabled` to `false`:

```json
{
  "sticky_replies": {
    "enabled": false
  }
}
```

Then restart the bridge:

```bash
disco bridge restart
```

If you are changing a stopped workspace, the next `disco start` will read the
file.

When sticky replies are disabled, ambient messages without `!name` are ignored.
Explicit mentions such as `!scribe summarize this` still work.

## Re-enable sticky replies

Set `sticky_replies.enabled` back to `true`:

```json
{
  "sticky_replies": {
    "enabled": true
  }
}
```

Then restart the bridge:

```bash
disco bridge restart
```

## How sticky replies behave

With sticky replies enabled, a successful visible agent reply makes that channel
or thread sticky to the responding agent. Later ambient human messages in the
same channel or thread route to that agent without repeating `!name`.

Use `!unstick` in Discord to clear the sticky owner for the current channel or
thread. Addressing another agent explicitly with `!name` bypasses the current
sticky owner.

## Troubleshooting

The settings file must be valid JSON. Boolean values must be JSON booleans
(`true` or `false`), not strings such as `"true"` or `"false"`.

Only documented keys are accepted. If the file has invalid JSON, an invalid
value, or an unknown key, the bridge fails during startup. Check the bridge log:

```bash
disco logs bridge
```

## Reference

| Path or setting | Meaning |
|---|---|
| `CALFCORD_SETTINGS` | Optional path to `settings.json`. Wins over the default path. |
| `$CALFCORD_HOME/config/settings.json` | Default native install location. |
| `./settings.json` | Development fallback when neither `CALFCORD_SETTINGS` nor `CALFCORD_HOME` is set. |
| `sticky_replies.enabled` | `true` by default. When `false`, ambient messages do not route to sticky owners and successful replies do not update sticky owner state. |
