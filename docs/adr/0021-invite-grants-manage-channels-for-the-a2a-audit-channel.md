# The invite grants Manage Channels so the A2A audit channel can create itself

**Status:** accepted

The A2A audit channel is lazily created on the first consult
(`bridge/egress.py` `_create` → `guild.create_text_channel`), which Discord gates
behind **Manage Channels** — a bit the canonical invite mask never granted. The
invite gave every permission needed to *use* that channel (Manage Webhooks,
Create Public Threads, Send Messages in Threads) and none to *bring it into
existence*, so every fresh install's first agent-to-agent consult 403'd
(`error code: 50013`) and, the projection being best-effort, the audit log
silently never appeared. `INVITE_PERMISSIONS` now includes it
(`309774601216` → `309774601232`).

## Consequences

- **This is a broad grant.** Manage Channels is server-wide: it covers editing
  and deleting *any* channel in the guild, for a bot `docs/security.md` already
  warns can run code on the host. It buys exactly one thing, and Discord offers
  no narrower "create a single channel" permission.
- **It is exercised only on a discovery miss**, so it is dead weight once the
  channel exists. `docs/a2a-threads.md` tells operators they may revoke it after
  the first consult; A2A then only breaks again if the channel is deleted or
  `CALFKIT_A2A_CHANNEL_NAME` changes.
- **Existing installs are not fixed by this.** A permission can only be granted
  by a human, so an install invited before this change keeps 403ing until an
  admin re-authorizes via the `disco init` invite link.

## Considered options

- **Require operators to create the channel by hand.** Rejected: it makes
  agent-to-agent messaging — on by default — silently broken out of the box, and
  the failure surfaces only in a log.
- **Degrade to the conversation's own channel** when the audit channel can't be
  created, using permissions the bot already holds. Rejected: Discord threads
  hang off a starter message, so the consult would become visible in the human's
  channel — the opposite of the privacy the audit channel exists to provide.
  Kept on the table as a fallback if the standing grant proves unacceptable.
