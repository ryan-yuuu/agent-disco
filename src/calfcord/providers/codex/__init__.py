"""ChatGPT subscription auth provider for Codex models.

Authenticates against OpenAI's Codex OAuth (the same client used by the
official ``codex`` CLI) and routes inference through
``https://chatgpt.com/backend-api/codex`` so requests are billed against
the operator's ChatGPT Plus/Pro subscription rather than API credits.

Agents opt in by setting ``provider: openai-codex`` in their frontmatter.
The runner must have valid cached credentials at startup; obtain them via
``uv run calfkit-auth codex login``.

Composition:
  - OAuth login/refresh flows: delegated to ``openhands-sdk`` (already a
    transitive dependency; its ``OpenAISubscriptionAuth`` handles PKCE,
    browser callback, device-code, and credential persistence).
  - Runtime token refresh: a per-request ``httpx.Auth`` (``_CodexBearerAuth``)
    injects a fresh OAuth bearer on every ``httpx.send()``, refreshing via
    OpenHands' ``OpenAISubscriptionAuth`` when expired — so refresh-on-expiry
    happens transparently with no background task. See ``model_client.py`` for
    why authlib's ``AsyncOAuth2Client`` does not work here (it hooks
    ``request()``, which the OpenAI SDK bypasses via ``send()``).
  - Codex CLI system prompt: fetched verbatim from ``openai/codex`` on
    process startup (with ETag-conditional refresh against an on-disk
    cache) so the ``instructions`` field of every request matches what
    the official Codex CLI sends. OpenAI's Codex backend explicitly
    fingerprints this field — see ``prompts.py`` and openai/codex#4433.
    The runner calls :func:`prewarm_codex_prompts` before constructing
    any model client.
  - Calfkit integration: ``CodexSubscriptionModelClient`` subclasses
    ``calfkit.providers.OpenAIResponsesModelClient`` and overrides
    ``_map_messages`` to substitute the verbatim per-model Codex prompt
    as ``instructions`` and smuggle the agent's real system prompt into
    a leading synthetic user message as ``input_text``.
"""

import os

# The OpenHands SDK prints a multi-line startup banner to stderr the moment
# ``openhands.sdk`` is imported (``openhands.sdk.__init__`` calls
# ``_print_banner``). ``model_client`` below imports ``openhands.sdk.llm.auth``
# at module load, so that banner would fire on any process that touches the
# Codex provider — the ``disco init`` wizard and the agent runner alike, dirtying
# their output. Suppress it before that first import runs. ``setdefault`` (not a
# hard assignment) leaves an operator override intact: exporting
# ``OPENHANDS_SUPPRESS_BANNER=0`` still shows the banner.
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

# NB: these imports intentionally follow the banner-suppress line above.
from calfcord.providers.codex.factory_hook import (
    CodexNotLoggedInError,
    build_codex_subscription_client,
)
from calfcord.providers.codex.model_client import (
    CodexModelNotSupportedError,
)
from calfcord.providers.codex.prompts import (
    CodexModel,
    CodexModelError,
    CodexPromptsUnavailableError,
    DeprecatedCodexModelError,
    UnknownCodexModelError,
    prewarm_codex_prompts,
)

__all__ = [
    "CodexModel",
    "CodexModelError",
    "CodexModelNotSupportedError",
    "CodexNotLoggedInError",
    "CodexPromptsUnavailableError",
    "DeprecatedCodexModelError",
    "UnknownCodexModelError",
    "build_codex_subscription_client",
    "prewarm_codex_prompts",
]
