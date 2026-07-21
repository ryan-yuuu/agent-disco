"""AgentFactory entry points for the ``xai-grok`` and ``xai`` providers.

Kept thin and lazily imported by ``agents/factory.py`` so a deployment that uses
neither provider never pulls in the xAI OAuth / model-client machinery.

Both resolve an unset ``model:`` to the live catalog default (falling back to a
pinned slug when the catalog hasn't loaded), so agents need not hard-code a Grok
model that xAI may later retire.
"""

from __future__ import annotations

import os

from calfcord.providers.grok.credentials import GrokNotLoggedInError
from calfcord.providers.grok.model_client import GrokModelClient
from calfcord.providers.grok.models import get_default_resolver
from calfcord.providers.grok.oauth import DEFAULT_XAI_BASE_URL, validate_inference_base_url
from calfcord.providers.grok.token_store import load_credentials

__all__ = [
    "GrokApiKeyMissingError",
    "GrokNotLoggedInError",
    "build_grok_api_key_client",
    "build_grok_subscription_client",
]


class GrokApiKeyMissingError(RuntimeError):
    """Raised when a ``provider: xai`` agent runs without ``XAI_API_KEY`` set."""


def _resolved_base_url() -> str:
    """Inference base URL, honoring (and host-pinning) an ``XAI_BASE_URL`` override."""
    return validate_inference_base_url(os.getenv("XAI_BASE_URL", ""), fallback=DEFAULT_XAI_BASE_URL)


def _resolved_model(model_name: str | None) -> str:
    return model_name or get_default_resolver().default_slug()


def build_grok_subscription_client(model_name: str | None) -> GrokModelClient:
    """Construct an OAuth-backed Grok client (``provider: xai-grok``).

    ``model_name=None`` selects the catalog default. Fails fast with a login hint
    when no credentials are cached, so the operator sees it at runner bootstrap
    rather than on the first Discord message.
    """
    if load_credentials() is None:
        raise GrokNotLoggedInError()
    return GrokModelClient.for_oauth(
        model_name=_resolved_model(model_name),
        base_url=_resolved_base_url(),
    )


def build_grok_api_key_client(model_name: str | None) -> GrokModelClient:
    """Construct an API-key-backed Grok client (``provider: xai``).

    Reads ``XAI_API_KEY`` explicitly (never the OpenAI SDK's ``OPENAI_API_KEY``
    fallback, which would silently send the wrong key to xAI).
    """
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        raise GrokApiKeyMissingError(
            "provider 'xai' requires XAI_API_KEY. Set it (obtain one at "
            "https://console.x.ai), or use 'xai-grok' with: uv run calfkit-auth grok login"
        )
    return GrokModelClient(
        model_name=_resolved_model(model_name),
        base_url=_resolved_base_url(),
        api_key=api_key,
    )
