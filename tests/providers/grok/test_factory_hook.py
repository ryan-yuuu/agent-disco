"""Tests for the AgentFactory entry points for the two xAI Grok providers."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from calfcord.providers.grok import models as models_mod
from calfcord.providers.grok import token_store
from calfcord.providers.grok.factory_hook import (
    GrokApiKeyMissingError,
    GrokNotLoggedInError,
    build_grok_api_key_client,
    build_grok_subscription_client,
)
from calfcord.providers.grok.model_client import GrokModelClient
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_BASE_URL", raising=False)
    models_mod.reset_default_resolver()


def _seed() -> None:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none'})}.{b64({'exp': time.time() + 7200})}.sig"
    token_store.save_credentials(
        GrokCredentials(access_token=token, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token")
    )


class TestSubscriptionClient:
    def test_raises_when_not_logged_in(self) -> None:
        with pytest.raises(GrokNotLoggedInError):
            build_grok_subscription_client(model_name="grok-4.3")

    def test_uses_configured_model(self) -> None:
        _seed()
        client = build_grok_subscription_client(model_name="grok-4.5")
        assert isinstance(client, GrokModelClient)
        assert client.model_name == "grok-4.5"

    def test_none_model_resolves_catalog_default(self) -> None:
        _seed()
        client = build_grok_subscription_client(model_name=None)
        # Resolver unloaded -> pinned default.
        assert client.model_name == "grok-4.3"


class TestApiKeyClient:
    def test_raises_when_key_missing(self) -> None:
        with pytest.raises(GrokApiKeyMissingError):
            build_grok_api_key_client(model_name="grok-4.3")

    def test_builds_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "xai-secret")
        client = build_grok_api_key_client(model_name="grok-4.3")
        assert isinstance(client, GrokModelClient)
        assert client.model_name == "grok-4.3"

    def test_none_model_resolves_catalog_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "xai-secret")
        client = build_grok_api_key_client(model_name=None)
        assert client.model_name == "grok-4.3"
