"""Tests for the xAI Grok Responses model client.

Both providers use :class:`GrokModelClient`: the OAuth path wraps an
``httpx.AsyncClient`` whose auth injects a fresh bearer per request (xAI's SDK
dispatches via ``send()``, bypassing request-level interceptors — same reason as
codex); the API-key path passes ``XAI_API_KEY`` straight through. The client
strips ``reasoning.effort`` for models that don't accept it, so an operator's
``thinking_effort`` never 400s a non-reasoning Grok model.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

from calfcord.providers.grok import token_store
from calfcord.providers.grok.credentials import GrokNotLoggedInError
from calfcord.providers.grok.model_client import GrokModelClient, _GrokBearerAuth
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    return tmp_path / "auth"


def _jwt(exp: float) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


def _seed_fresh() -> str:
    token = _jwt(exp=time.time() + 7200)
    token_store.save_credentials(
        GrokCredentials(access_token=token, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token")
    )
    return token


class TestBearerAuth:
    async def test_injects_bearer_from_store(self) -> None:
        token = _seed_fresh()
        auth = _GrokBearerAuth()
        request = httpx.Request("POST", "https://api.x.ai/v1/responses")
        flow = auth.async_auth_flow(request)
        prepared = await flow.__anext__()
        assert prepared.headers["Authorization"] == f"Bearer {token}"
        with pytest.raises(StopAsyncIteration):
            await flow.__anext__()

    async def test_raises_when_not_logged_in(self) -> None:
        auth = _GrokBearerAuth()
        flow = auth.async_auth_flow(httpx.Request("POST", "https://api.x.ai/v1/responses"))
        with pytest.raises(GrokNotLoggedInError):
            await flow.__anext__()


class TestConstruction:
    def test_api_key_client_sets_store_false_and_no_reasoning_ids(self) -> None:
        client = GrokModelClient(model_name="grok-4.3", base_url="https://api.x.ai/v1", api_key="xai-key")
        assert client.model_name == "grok-4.3"
        assert client.model_settings["extra_body"] == {"store": False}
        assert client.model_settings["openai_send_reasoning_ids"] is False

    def test_oauth_client_accepts_bearer_http_client(self) -> None:
        http_client = httpx.AsyncClient(auth=_GrokBearerAuth())
        client = GrokModelClient(model_name="grok-4.5", base_url="https://api.x.ai/v1", http_client=http_client)
        assert client.model_name == "grok-4.5"


class TestReasoningEffortStripping:
    async def test_strips_effort_for_non_reasoning_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def fake_super(
            self: Any,
            messages: Any,
            stream: Any,
            model_settings: Any,
            model_request_parameters: Any,
        ) -> str:
            captured["settings"] = model_settings
            return "resp"

        monkeypatch.setattr(OpenAIResponsesModel, "_responses_create", fake_super)
        client = GrokModelClient(model_name="grok-4-fast", base_url="https://api.x.ai/v1", api_key="k")
        await client._responses_create(
            messages=[], stream=False, model_settings={"openai_reasoning_effort": "high"}, model_request_parameters=None
        )
        assert "openai_reasoning_effort" not in captured["settings"]

    async def test_keeps_effort_for_reasoning_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def fake_super(
            self: Any,
            messages: Any,
            stream: Any,
            model_settings: Any,
            model_request_parameters: Any,
        ) -> str:
            captured["settings"] = model_settings
            return "resp"

        monkeypatch.setattr(OpenAIResponsesModel, "_responses_create", fake_super)
        client = GrokModelClient(model_name="grok-4.3", base_url="https://api.x.ai/v1", api_key="k")
        await client._responses_create(
            messages=[], stream=False, model_settings={"openai_reasoning_effort": "high"}, model_request_parameters=None
        )
        assert captured["settings"]["openai_reasoning_effort"] == "high"
