"""Wiring tests: the xai / xai-grok providers are recognized end to end.

Covers the three coordinated call sites — the ``Provider`` literal, the factory
dispatch + catalog-default handling, and the thinking-effort mapping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.factory import (
    _PROVIDER_DEFAULT_MODELS,
    _default_model_client_factory,
    resolve_provider,
)
from calfcord.agents.thinking import build_model_settings
from calfcord.providers.grok.factory_hook import GrokApiKeyMissingError, GrokNotLoggedInError


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def _definition(provider: str) -> AgentDefinition:
    return AgentDefinition(name="grokbot", description="d", provider=provider, system_prompt="p")


class TestProviderLiteral:
    @pytest.mark.parametrize("provider", ["xai", "xai-grok"])
    def test_definition_accepts_provider(self, provider: str) -> None:
        assert _definition(provider).provider == provider

    @pytest.mark.parametrize("provider", ["xai", "xai-grok"])
    def test_resolve_provider_recognizes_it(self, provider: str) -> None:
        assert resolve_provider(_definition(provider)) == provider

    @pytest.mark.parametrize("provider", ["xai", "xai-grok"])
    def test_catalog_resolved_default_is_none(self, provider: str) -> None:
        # Both defer the default model to the live catalog (like openai-codex).
        assert _PROVIDER_DEFAULT_MODELS[provider] is None


class TestFactoryDispatch:
    def test_xai_grok_dispatches_to_subscription_builder(self) -> None:
        # Not logged in -> the subscription builder's fail-fast error proves dispatch.
        with pytest.raises(GrokNotLoggedInError):
            _default_model_client_factory("xai-grok", None)

    def test_xai_dispatches_to_api_key_builder(self) -> None:
        with pytest.raises(GrokApiKeyMissingError):
            _default_model_client_factory("xai", None)


class TestThinkingMapping:
    @pytest.mark.parametrize("provider", ["xai", "xai-grok"])
    def test_effort_maps_to_openai_reasoning_effort(self, provider: str) -> None:
        settings = build_model_settings(provider, "high")
        assert settings is not None
        assert "openai_reasoning_effort" in settings

    @pytest.mark.parametrize("provider", ["xai", "xai-grok"])
    def test_none_effort_returns_none(self, provider: str) -> None:
        assert build_model_settings(provider, None) is None
