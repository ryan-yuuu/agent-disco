# xAI Grok auth

This guide shows you how to bring an xAI **Grok**-backed agent online. Agent
Disco reaches Grok two ways, both through xAI's OpenAI-compatible Responses API
at `https://api.x.ai/v1`:

- **`provider: xai`** — the metered API, authenticated with an `XAI_API_KEY`.
- **`provider: xai-grok`** — device-code OAuth against a **SuperGrok** or
  **X Premium+** subscription, so requests bill against that subscription
  instead of API credits (the Grok analogue of `openai-codex`).

If you have an API key, use `xai` — set `XAI_API_KEY` (see below) and skip to
[Declare a Grok-backed agent](#declare-a-grok-backed-agent). To bill against a
subscription instead, use `xai-grok` and follow the login steps first.

For *why* the OAuth path is built the way it is (device-code, self-owned flow, no
CLI impersonation, the allowlist trade-off), see
[ADR-0029](./adr/0029-grok-oauth-provider-owns-its-device-code-flow.md).

## Prerequisites

- For `xai`: an `XAI_API_KEY` (get one at <https://console.x.ai>).
- For `xai-grok`: an active **SuperGrok** or **X Premium+** subscription that xAI
  has enabled for OAuth API access (see [If login is refused](#if-login-is-refused-http-403)).
- Egress from the host to `auth.x.ai` (OAuth + refresh) and `api.x.ai`
  (inference + model catalog).

No local browser is needed — the device-code flow prints a URL and a one-time
code you open on any device, so it works over SSH and on headless hosts.

## Log in (the `xai-grok` OAuth path)

Do this on the **host**, before starting any container:

```bash
# Prints a URL + code; open it, sign in, approve.
uv run calfkit-auth grok login

# Confirm it took.
uv run calfkit-auth grok status
# → Logged in. Credential dir: ~/.agent-disco/auth
#   Base URL: https://api.x.ai/v1
#   Access token expires: 2026-07-20T05:00:00+00:00
```

Credentials are written to `~/.agent-disco/auth/xai_oauth.json` (mode 0600),
beside the codex store in the shared auth dir. To relocate it, set
`CALFCORD_AUTH_DIR`. For containers, bind-mount that dir into the agent the same
way the shipped `docker-compose.yml` does for codex — the host login is the
source of truth, and token rotation inside a container writes back through it.

## Set `XAI_API_KEY` (the `xai` path)

Put the key in the install's `.env` (or the host environment):

```bash
XAI_API_KEY=xai-...
```

That is all the `xai` provider needs — there is nothing to log in to.

## Declare a Grok-backed agent

In `agents/<name>.md`, set `provider:` to `xai-grok` (subscription) or `xai`
(API key); everything else is identical:

```markdown
---
name: grok_demo
description: Demonstration agent backed by a Grok subscription.
provider: xai-grok
model: grok-4.3
thinking_effort: medium
---

You are a helpful assistant.
```

Then start it like any other teammate:

```bash
disco agent start grok_demo
```

If an `xai-grok` agent reports `GrokNotLoggedInError`, the host hasn't logged in
— run `uv run calfkit-auth grok login` and retry. If an `xai` agent reports
`GrokApiKeyMissingError`, set `XAI_API_KEY`.

You can omit `model:` — Agent Disco then fetches the live catalog and picks its
default (`grok-4.3`). To pin a model, see [Choosing a model](#choosing-a-model).

## If login is refused (HTTP 403)

xAI gates OAuth API access behind a server-side allowlist, so a valid SuperGrok /
Premium+ login can still be refused with `403 tier denied`. Re-logging in won't
change that. Instead, switch the agent to the API-key path: set `XAI_API_KEY` and
change its frontmatter to `provider: xai`. Agent Disco reports this case
distinctly (it does **not** tell you to re-login on a 403).

## Choosing a model

The model list is fetched live from your account (`GET /language-models`,
falling back to `/models`) — both require your credential; anonymous requests are
rejected. List what's available:

```bash
uv run calfkit-auth grok models
# Grok models (api):
#   grok-4.3 (default)
#   grok-4.5
#   ...
```

If the line reads `(fallback)` instead of `(api)`, the catalog couldn't be
fetched (allowlist 403, missing credential, or offline) and Agent Disco is using
a small pinned list (default `grok-4.3`) — the provider still runs, and
configured models are not validated in that state.

Set `model:` to any id the command lists. Reasoning models (`grok-4.3`,
`grok-4.5`, `grok-4.20-multi-agent`, `grok-3-mini`) honor `thinking_effort`; on
other Grok families the effort dial is omitted automatically (they reason
natively and would otherwise 400), so setting `thinking_effort` on them is
harmless.

## Command reference

All run on the host:

| Command | Effect |
|---|---|
| `calfkit-auth grok login [--no-browser] [--force]` | Device-code login. `--no-browser` prints the URL; `--force` re-runs even if cached credentials are valid. |
| `calfkit-auth grok logout` | Delete cached credentials. |
| `calfkit-auth grok status` | Show login state and access-token expiry. |
| `calfkit-auth grok refresh` | Force a token refresh now. |
| `calfkit-auth grok models` | List available models (live catalog, or pinned fallback). |

## See also

- [ADR-0029](./adr/0029-grok-oauth-provider-owns-its-device-code-flow.md) — why
  the OAuth flow is self-owned, device-code, and impersonation-free, and the
  allowlist / token-rotation consequences.
- [docs/codex-auth.md](./codex-auth.md) — the sibling ChatGPT/Codex subscription
  provider this one mirrors.

## Attribution

The device-code flow, refresh handling, and endpoint host-pinning are a faithful
port of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
(MIT, © 2025 Nous Research). OIDC endpoints and the public `grok-cli` client id
come from xAI's discovery document at
`https://auth.x.ai/.well-known/openid-configuration`.
