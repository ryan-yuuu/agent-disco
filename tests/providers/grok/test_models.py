"""Tests for the xAI Grok model catalog.

The catalog is fetched live from xAI's authenticated ``/language-models`` (falling
back to the OpenAI-compatible ``/models``); both require a credential — verified
empirically, anonymous requests 401. On any failure the resolver degrades to a
small pinned catalog rather than raising, because xAI's OAuth API access is
allowlist-gated and may 403.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from calfcord.providers.grok import models as models_mod
from calfcord.providers.grok.models import GrokModelResolver, grok_supports_reasoning_effort


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_LANGUAGE_MODELS = {
    "models": [
        {
            "id": "grok-4.3",
            "aliases": ["grok-4.3-latest"],
            "input_modalities": ["text", "image"],
            "context_length": 1_000_000,
        },
        {"id": "grok-4.5", "aliases": [], "input_modalities": ["text"]},
        {"id": "grok-build-0.1", "aliases": [], "input_modalities": ["text"]},
    ]
}


class TestReasoningEffortAllowlist:
    @pytest.mark.parametrize("model", ["grok-4.3", "grok-4.5", "grok-4.20-multi-agent-0309", "grok-3-mini"])
    def test_supported_models(self, model: str) -> None:
        assert grok_supports_reasoning_effort(model) is True

    @pytest.mark.parametrize("model", ["grok-4", "grok-4-fast", "grok-code-fast-1", "grok-3", ""])
    def test_unsupported_models(self, model: str) -> None:
        assert grok_supports_reasoning_effort(model) is False

    def test_strips_aggregator_prefix(self) -> None:
        assert grok_supports_reasoning_effort("x-ai/grok-4.5") is True


class TestEnsureLoaded:
    async def test_parses_language_models_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/language-models")
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json=_LANGUAGE_MODELS)

        resolver = GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)

        assert resolver.source == "api"
        assert "grok-4.3" in resolver.selectable_models()
        assert "grok-build-0.1" in resolver.selectable_models()

    async def test_falls_back_to_models_endpoint_when_language_models_404(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/language-models"):
                return httpx.Response(404)
            return httpx.Response(200, json={"data": [{"id": "grok-4.3"}]})

        resolver = GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)

        assert any(p.endswith("/language-models") for p in paths)
        assert any(p.endswith("/models") for p in paths)
        assert resolver.source == "api"
        assert "grok-4.3" in resolver.selectable_models()

    async def test_401_degrades_to_pinned_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        resolver = GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)

        assert resolver.source == "fallback"
        assert resolver.default_slug() in resolver.selectable_models()

    async def test_no_bearer_uses_fallback_without_calling_api(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not hit the API without a credential")

        resolver = GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("", client=client)
        assert resolver.source == "fallback"

    async def test_network_error_degrades_to_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        resolver = GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"

    async def test_is_idempotent(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_LANGUAGE_MODELS)

        resolver = GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)
            await resolver.ensure_loaded("tok", client=client)
        assert calls["n"] == 1


class TestSelection:
    async def test_default_prefers_known_general_model(self) -> None:
        resolver = GrokModelResolver()
        async with _client(lambda r: httpx.Response(200, json=_LANGUAGE_MODELS)) as client:
            await resolver.ensure_loaded("tok", client=client)
        # grok-4.3 is the preferred general default when present.
        assert resolver.default_slug() == "grok-4.3"

    async def test_default_falls_back_to_first_entry(self) -> None:
        payload = {"models": [{"id": "grok-experimental-9"}, {"id": "grok-other"}]}
        resolver = GrokModelResolver()
        async with _client(lambda r: httpx.Response(200, json=payload)) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.default_slug() == "grok-experimental-9"

    def test_default_slug_before_load_uses_pinned_default(self) -> None:
        # Construction must not require a network fetch; a sensible default is
        # always available synchronously.
        resolver = GrokModelResolver()
        assert resolver.default_slug() == "grok-4.3"

    async def test_is_known_matches_id_and_alias(self) -> None:
        resolver = GrokModelResolver()
        async with _client(lambda r: httpx.Response(200, json=_LANGUAGE_MODELS)) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.is_known("grok-4.3") is True
        assert resolver.is_known("grok-4.3-latest") is True  # alias
        assert resolver.is_known("gpt-5") is False


class TestSingleton:
    async def test_prewarm_populates_default_resolver(self) -> None:
        models_mod.reset_default_resolver()
        try:
            async with _client(lambda r: httpx.Response(200, json=_LANGUAGE_MODELS)) as client:
                await models_mod.prewarm_grok_models("tok", client=client)
            assert "grok-4.3" in models_mod.get_default_resolver().selectable_models()
        finally:
            models_mod.reset_default_resolver()
