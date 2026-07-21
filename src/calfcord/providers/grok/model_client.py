"""Calfkit Responses model client for xAI Grok.

One client class serves both providers:

* ``xai-grok`` (OAuth) — wraps an :class:`httpx.AsyncClient` whose ``auth`` is
  :class:`_GrokBearerAuth`, injecting a freshly-resolved OAuth bearer on every
  request. The bearer must be set at ``httpx.send()`` time (the path the OpenAI
  SDK uses); a request-level interceptor would be bypassed — the same constraint
  documented for the codex client.
* ``xai`` (API key) — passes ``XAI_API_KEY`` straight through, no custom transport.

Unlike codex, there is **no prompt-fingerprint impersonation**: xAI accepts the
OAuth bearer on its standard public ``/v1/responses`` endpoint, so we send the
agent's real system prompt unchanged. (Verified against hermes-agent's xAI path,
which likewise sends no ``originator``/fingerprint on ``api.x.ai``.)

Construction bypasses calfkit's ``OpenAIResponsesModelClient.__init__`` (which
builds an ``OpenAIProvider`` with no ``http_client`` hook) and calls the vendored
``OpenAIResponsesModel`` initializer directly, mirroring the codex client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from calfkit._vendor.pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from calfkit._vendor.pydantic_ai.providers.openai import OpenAIProvider
from calfkit.providers import OpenAIResponsesModelClient

from calfcord.providers.grok import credentials
from calfcord.providers.grok.models import grok_supports_reasoning_effort

logger = logging.getLogger(__name__)

# OpenAIProvider requires a non-empty api_key even when a custom http_client
# overrides the Authorization header on every request (OAuth path).
_API_KEY_PLACEHOLDER = "placeholder-overridden-by-bearer-auth"


class _GrokBearerAuth(httpx.Auth):
    """Injects a fresh xAI OAuth bearer on every request, refreshing as needed.

    ``async_auth_flow`` runs on every ``httpx.send()`` regardless of how the
    OpenAI SDK dispatches, so token rotation is transparent. The in-process
    :class:`asyncio.Lock` collapses concurrent requests onto a single refresh;
    the cross-process file lock lives inside :func:`credentials.resolve_access_token`.
    """

    requires_request_body = False
    requires_response_body = False

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        async with self._lock:
            token = await credentials.resolve_access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def _without_reasoning_effort(model_settings: Any) -> Any:
    """Return ``model_settings`` minus ``openai_reasoning_effort`` (copy, if present)."""
    if not model_settings or "openai_reasoning_effort" not in model_settings:
        return model_settings
    stripped = dict(model_settings)
    stripped.pop("openai_reasoning_effort", None)
    return stripped


class GrokModelClient(OpenAIResponsesModelClient):
    """xAI Grok Responses client, used by both the OAuth and API-key providers."""

    def __init__(
        self,
        *,
        model_name: str,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
    ) -> None:
        # ``store=False``: xAI persists nothing server-side, so stored reasoning
        # ids would 404 on the next turn — don't send them back either.
        settings = OpenAIResponsesModelSettings(  # type: ignore[typeddict-item]
            extra_body={"store": False},
            openai_send_reasoning_ids=False,
        )
        provider_kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key or _API_KEY_PLACEHOLDER}
        if http_client is not None:
            provider_kwargs["http_client"] = http_client
        provider = OpenAIProvider(**provider_kwargs)

        self.model_settings = settings
        OpenAIResponsesModel.__init__(self, model_name, provider=provider, settings=settings)

    @classmethod
    def for_oauth(cls, *, model_name: str, base_url: str) -> GrokModelClient:
        """Build an OAuth-backed client that owns its bearer-refreshing transport.

        Keeps the private :class:`_GrokBearerAuth` an implementation detail of this
        module (callers pass only the model + base URL), mirroring how the codex
        client owns its own auth.
        """
        return cls(model_name=model_name, base_url=base_url, http_client=httpx.AsyncClient(auth=_GrokBearerAuth()))

    async def _responses_create(
        self,
        messages: Any,
        stream: bool,
        model_settings: Any,
        model_request_parameters: Any,
    ) -> Any:
        """Drop ``reasoning.effort`` for Grok models that reject it.

        xAI 400s ``reasoning.effort`` on non-reasoning families (grok-4,
        grok-4-fast, grok-code-fast-1, grok-3, grok-4.20-0309-*); those models
        reason natively, so we simply omit the dial rather than error. Faithful
        to hermes-agent's ``grok_supports_reasoning_effort`` transport gate.
        """
        if not grok_supports_reasoning_effort(self.model_name):
            model_settings = _without_reasoning_effort(model_settings)
        return await super()._responses_create(
            messages=messages,
            stream=stream,
            model_settings=model_settings,
            model_request_parameters=model_request_parameters,
        )
