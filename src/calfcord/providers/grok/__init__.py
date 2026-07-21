"""xAI Grok providers for Agent Disco.

Two providers share this package:

* ``xai-grok`` — device-code OAuth against a SuperGrok / X Premium+
  subscription (``uv run calfkit-auth grok login``); requests are billed
  against the operator's Grok subscription rather than metered API credits.
* ``xai`` — the standard metered API-key path (``XAI_API_KEY``).

Both route through xAI's OpenAI-compatible Responses API at
``https://api.x.ai/v1``. The device-code flow, refresh, and endpoint
host-pinning are a faithful port of NousResearch/hermes-agent's xAI OAuth
implementation (MIT, © 2025 Nous Research); see :mod:`calfcord.providers.grok.oauth`.
"""

from __future__ import annotations

from calfcord.providers.grok.credentials import GrokNotLoggedInError
from calfcord.providers.grok.factory_hook import (
    GrokApiKeyMissingError,
    build_grok_api_key_client,
    build_grok_subscription_client,
)
from calfcord.providers.grok.models import (
    GrokModelResolver,
    get_default_resolver,
    grok_supports_reasoning_effort,
    prewarm_grok_models,
)
from calfcord.providers.grok.oauth import GrokAuthError

__all__ = [
    "GrokApiKeyMissingError",
    "GrokAuthError",
    "GrokModelResolver",
    "GrokNotLoggedInError",
    "build_grok_api_key_client",
    "build_grok_subscription_client",
    "get_default_resolver",
    "grok_supports_reasoning_effort",
    "prewarm_grok_models",
]
