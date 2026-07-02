# Discord setup

One-time, about 5 minutes. You'll create a Discord app, grab two values
(the bot token and application ID), enable two intents, and invite the bot to
your server. `disco init` takes it from there — it verifies the token, waits
for the invite, and **discovers your server and channel for you**, so these two
values are the only Discord IDs you ever paste.

**Before you start:** you need a Discord server you own (or have **Manage
Server** on).

## 1. Create the app

Grab two values to hand to `disco init` when it asks (it writes them to
`.env` for you):

1. Create the app:
   - Open the [Developer Portal](https://discord.com/developers/applications).
   - Click **New Application**.
   - Name it, then click **Create**.
2. On **General Information**, copy the **Application ID** — this is
   `DISCORD_APPLICATION_ID`.
3. Get the bot token — this is `DISCORD_BOT_TOKEN`:
   - Open the **Bot** tab.
   - Click **Reset Token**.
   - Click **Copy**.

   There's no separate "create bot" step — every new app already has a bot user,
   so this tab is where it lives. Discord shows the token **only once**, so copy
   it right away; if you lose it, just **Reset Token** again. Treat it like a
   password; `init` verifies it on the spot when you paste it.

## 2. Enable two intents

Still on the **Bot** tab, under **Privileged Gateway Intents**, switch on
**both** and click **Save Changes**:

- ✅ Message Content Intent
- ✅ Server Members Intent *(not enforced yet — enabling it now future-proofs your setup)*

> ⚠️ **Most-missed step.** Skip **Message Content** and the bridge won't start —
> it exits with `PrivilegedIntentsRequired`. (Server Members isn't required yet.)

## 3. Invite the bot

You don't build the invite link yourself — `disco init` does. When you reach the
invite step, the wizard prints a ready-made link and tries to open it in your
browser. Your only job here: **pick your server and click Authorize.** If no
browser tab appears, copy the link the wizard printed and open it yourself.

The link grants the channel permissions Agent Disco needs to operate: View
Channel, Send Messages, Embed Links, Read Message History, Manage Webhooks,
Create Public Threads, and Send Messages in Threads. (The two privileged
*intents* from step 2 are a separate Bot-tab toggle the link can't set.) Invite
it **only to servers you trust** — agents can run code on the host (see
[`security.md`](./security.md)).

## 4. The wizard takes it from here

Discord setup is done — back in `disco init`, the wizard detects the moment
the bot joins, picks up your server, confirms the bot can actually post, brings
your agent online, and waits until it sees the first reply. When it finishes,
confirm in any channel the bot can see:

```
@assistant hello
```

A reply appears under the agent's persona. You're connected. (`@assistant` is the
default starter agent — use whatever name you gave yours in `init`.)

---

## Advanced: override what `init` auto-discovers

`disco init` discovers your server automatically, so you don't need these.
They're here for cases where you want to set a value explicitly — e.g. pin
slash-command sync to one guild, or unlock owner-only commands. Turn on
**Developer Mode** (Discord → User Settings → Advanced), right-click to **Copy
ID**, and set the key in `~/.calfcord/config/.env`:

| `.env` key | Copy ID from | What it does |
|---|---|---|
| `DISCORD_GUILD_ID` | your server | Slash commands appear instantly (otherwise ~1 h). |
| `DISCORD_OWNER_USER_ID` | yourself | Unlocks owner-only commands (`/clear`, `/thinking-effort`). |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bridge exits with `PrivilegedIntentsRequired` | Enable the **Message Content** intent (step 2). |
| Bot is online but never replies | Confirm Message Content intent (step 2); check it can **View Channel** + **Send Messages** in that channel. |
| Agent can't post / `Forbidden` on a webhook | Bot needs **Manage Webhooks** in that channel. |
| `/task` does nothing | The invite grants **Create Public Threads** server-wide, but a channel-level permission override can still deny it in a specific channel — check that channel's permission overrides. |
| "typing…" indicator never shows | The bot **user** needs **Send Messages** (and **Send Messages in Threads** for `/task` threads) in that channel — this is separate from Manage Webhooks, and a channel override can deny it. Typing is cosmetic, so it fails silently; the first denial is logged at WARNING. |
| Slash commands don't appear | Set `DISCORD_GUILD_ID` for instant sync (global takes ~1 h). |
