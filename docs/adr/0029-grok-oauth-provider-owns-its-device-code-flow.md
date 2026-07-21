# The Grok OAuth provider owns its device-code flow, pinned to xAI

The `xai-grok` provider authenticates a SuperGrok / X Premium+ subscription
against xAI's Responses API using an **OAuth 2.0 Device Authorization Grant**
(RFC 8628) that Agent Disco implements itself in
`calfcord/providers/grok/oauth.py` — it does **not** reuse the codex provider's
credential machinery. It ships alongside a plain metered `xai` (API-key)
provider; both route through `https://api.x.ai/v1` and share one
`GrokModelClient`. The auth flow is a faithful port of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT).

Three decisions here are hard to reverse (they fix an on-disk credential format,
provider names, and a wire contract) and surprising without context:

**We own the flow instead of delegating to `openhands-sdk` (as codex does).**
OpenHands' `OpenAISubscriptionAuth` is hard-coded to OpenAI (issuer, client id,
scopes), and there is no xAI vendor. So the device-code request/poll/refresh,
the credential store (`$CALFCORD_HOME/auth/xai_oauth.json`, 0600), and the
runtime resolver are ours, built on `httpx` (already a dep) plus `filelock`
(newly added) for the cross-process refresh lock.

**Device-code, not authorization-code-with-PKCE-loopback.** xAI's OIDC server
supports both, and a community plugin uses the loopback variant. Device-code
needs no local callback server or graphical browser, so it works over SSH / on
the headless hosts Agent Disco's bridge and agents actually run on.

**No prompt-fingerprint or `originator` impersonation.** codex must impersonate
the Codex CLI (verbatim fingerprinted system prompt + `originator: codex_cli_rs`)
because its Cloudflare-fenced `chatgpt.com/backend-api/codex` endpoint validates
both. xAI accepts the OAuth bearer on its **standard public** `/v1/responses`
endpoint — the same one API-key users hit — so we send the agent's real system
prompt unchanged. Verified against hermes-agent's xAI path, whose `codex_cli_rs`
header spoof is hostname-gated to `chatgpt.com` and never touches `api.x.ai`.

## Considered options

- **Extend `openhands-sdk` with an xAI vendor and reuse the codex path.**
  Rejected for now: it couples our release to an upstream PR landing, and the
  codex path's impersonation machinery (prompt catalog, fingerprint headers) is
  exactly what Grok does *not* need. We keep the door open — if OpenHands adds
  xAI, the resolver can adopt it.
- **Only an API-key `xai` provider (no OAuth).** Rejected: the subscription path
  is the whole point (billing against SuperGrok, not metered credits). We ship
  both because the API-key path is nearly free and is the documented fallback
  when OAuth is allowlist-gated.
- **Chat Completions instead of the Responses API.** Rejected: Grok's reasoning
  models and hermes-agent both use `/v1/responses`; matching it keeps reasoning /
  tool-calling behavior aligned and reuses the existing Responses client.

## Consequences

- **Allowlist-gated, by design.** xAI can 403 OAuth API access for an otherwise
  valid subscription. The refresh path classifies 403 as `xai_oauth_tier_denied`
  (keep the valid tokens, steer to `XAI_API_KEY`) distinctly from 400/401
  (clear the dead grant, prompt re-login). The `xai` provider is the escape hatch.
- **Single-use refresh tokens force cross-process locking.** xAI rotates the
  refresh token on every refresh, so the resolver serializes the
  read-refresh-write critical section with a `filelock` + double-checked re-read;
  the shared auth dir is assumed to be a real (bind-mounted) filesystem, the same
  assumption codex already makes.
- **Endpoints are host-pinned to `x.ai`.** The discovered `token_endpoint` and
  any `XAI_BASE_URL` override are refused unless HTTPS on `x.ai`/`*.x.ai`, so a
  one-time MITM at discovery (or a tampered env var) can't exfiltrate the bearer.
- **The model catalog is fetched live, with a pinned fallback.** Both providers
  resolve their default from `GET /language-models` (auth-required — anonymous
  requests 401) and degrade to a pinned list on any failure rather than blocking
  startup.
