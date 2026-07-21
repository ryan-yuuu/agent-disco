"""Edge-branch coverage for the grok provider modules."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

from calfcord.providers.grok import credentials, models, oauth, token_store
from calfcord.providers.grok.model_client import GrokModelClient
from calfcord.providers.grok.oauth import GrokAuthError
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _jwt(exp: float) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


class TestOauthEdges:
    def test_decode_jwt_exp_handles_bad_base64(self) -> None:
        assert oauth.decode_jwt_exp("aaa.!!!not-base64!!!.ccc") is None

    def test_decode_jwt_exp_handles_non_numeric_exp(self) -> None:
        def b64(obj: dict[str, Any]) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        token = f"{b64({'alg': 'none'})}.{b64({'exp': 'soon'})}.sig"
        assert oauth.decode_jwt_exp(token) is None

    def test_decode_jwt_exp_rejects_oversized_integer(self) -> None:
        # An arbitrary-precision JSON int (corrupt/tampered token) must not raise
        # OverflowError from float() — that would escape every caller and crash boot.
        assert oauth.decode_jwt_exp(_jwt(exp=10**400)) is None

    def test_decode_jwt_exp_rejects_non_finite(self) -> None:
        assert oauth.decode_jwt_exp(_jwt(exp=float("inf"))) is None
        assert oauth.decode_jwt_exp(_jwt(exp=float("nan"))) is None

    def test_skew_and_expiry_tolerate_oversized_exp(self) -> None:
        token = _jwt(exp=10**400)
        # No OverflowError; degrade to the safe defaults.
        assert oauth.proactive_refresh_skew_seconds(token) == oauth.REFRESH_SKEW_SECONDS
        assert oauth.access_token_is_expiring(token, 300) is False

    def test_proactive_skew_full_for_opaque_token(self) -> None:
        assert oauth.proactive_refresh_skew_seconds("opaque") == oauth.REFRESH_SKEW_SECONDS

    def test_proactive_skew_full_for_expired_token(self) -> None:
        assert oauth.proactive_refresh_skew_seconds(_jwt(exp=time.time() - 10)) == oauth.REFRESH_SKEW_SECONDS

    def test_proactive_skew_without_iat_is_fixed_lead(self) -> None:
        # No iat to size the window -> a modest fixed lead (not the full hour, or
        # a ~50-min token would refresh on every resolution).
        assert oauth.proactive_refresh_skew_seconds(_jwt(exp=time.time() + 50 * 60)) == 300

    async def test_refresh_network_error_is_grok_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.refresh_tokens(
                    client, refresh_token="rt", token_endpoint="https://auth.x.ai/oauth2/token"
                )

    async def test_request_device_code_network_error_is_grok_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.request_device_code(client)

    async def test_poll_network_error_is_grok_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async def fake_sleep(_s: float) -> None:
            return None

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="d",
                    expires_in=600,
                    interval=1,
                    sleep=fake_sleep,
                )

    async def test_refresh_non_json_200_is_grok_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>", headers={"content-type": "application/json"})

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.refresh_tokens(
                    client, refresh_token="rt", token_endpoint="https://auth.x.ai/oauth2/token"
                )

    async def test_refresh_non_dict_200_body_is_grok_auth_error(self) -> None:
        # A valid-JSON but non-dict 200 (e.g. a proxy returning ["blocked"]) must
        # not AttributeError on the boot/per-request refresh hot path.
        async with _client(lambda r: httpx.Response(200, json=["blocked"])) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                await oauth.refresh_tokens(
                    client, refresh_token="rt", token_endpoint="https://auth.x.ai/oauth2/token"
                )
        assert exc.value.code == "xai_refresh_invalid_response"

    async def test_poll_non_200_non_dict_body_raises(self) -> None:
        async def fake_sleep(_s: float) -> None:
            return None

        async with _client(lambda r: httpx.Response(400, json=["x"])) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="d",
                    expires_in=600,
                    interval=1,
                    sleep=fake_sleep,
                )

    async def test_discovery_non_dict_body_raises(self) -> None:
        # A well-formed-JSON but wrong top-level type (a list) must not AttributeError.
        async with _client(lambda r: httpx.Response(200, json=[1, 2, 3])) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.discover_endpoints(client)

    async def test_request_device_code_null_body_raises(self) -> None:
        async with _client(lambda r: httpx.Response(200, json=None)) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.request_device_code(client)

    async def test_poll_200_null_body_raises(self) -> None:
        async def fake_sleep(_s: float) -> None:
            return None

        async with _client(lambda r: httpx.Response(200, json=None)) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="d",
                    expires_in=600,
                    interval=1,
                    sleep=fake_sleep,
                )

    async def test_discovery_non_200_raises(self) -> None:
        async with _client(lambda r: httpx.Response(503)) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.discover_endpoints(client)

    async def test_discovery_network_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.discover_endpoints(client)

    async def test_refresh_generic_5xx_is_not_relogin(self) -> None:
        async with _client(lambda r: httpx.Response(500, text="boom")) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                await oauth.refresh_tokens(
                    client, refresh_token="rt", token_endpoint="https://auth.x.ai/oauth2/token"
                )
        assert exc.value.relogin_required is False

    async def test_device_login_opens_browser_via_injected_opener(self) -> None:
        opened: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
                        "token_endpoint": "https://auth.x.ai/oauth2/token",
                    },
                )
            if path.endswith("/device/code"):
                return httpx.Response(
                    200,
                    json={
                        "device_code": "d",
                        "user_code": "U",
                        "verification_uri": "https://accounts.x.ai/device",
                        "verification_uri_complete": "https://accounts.x.ai/device?c=U",
                        "expires_in": 600,
                        "interval": 1,
                    },
                )
            return httpx.Response(200, json={"access_token": _jwt(time.time() + 3600), "refresh_token": "rt"})

        async def fake_sleep(_s: float) -> None:
            return None

        async with _client(handler) as client:
            await oauth.device_code_login(
                client=client,
                open_browser=True,
                browser_opener=lambda url: bool(opened.append(url)) or True,
                printer=lambda _line: None,
                sleep=fake_sleep,
            )
        assert opened and opened[0].endswith("c=U")

    async def test_poll_non_json_error_raises(self) -> None:
        async def fake_sleep(_s: float) -> None:
            return None

        async with _client(lambda r: httpx.Response(500, text="<html>oops")) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="d",
                    expires_in=600,
                    interval=1,
                    sleep=fake_sleep,
                )


class TestModelsEdges:
    def test_fallback_slugs_default_first(self) -> None:
        assert models.fallback_model_slugs()[0] == "grok-4.3"

    def test_grok_model_matches_and_reasoning_property(self) -> None:
        model = models.GrokModel(id="grok-4.3", aliases=("grok-4.3-latest",))
        assert model.matches("GROK-4.3-LATEST") is True
        assert model.matches("other") is False
        assert model.supports_reasoning_effort is True

    async def test_parses_bare_list_and_skips_bad_entries(self) -> None:
        payload = ["not-a-dict", {"no_id": 1}, {"id": "grok-4.3", "context_length": 900}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        resolver = models.GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.selectable_models() == ["grok-4.3"]

    async def test_scalar_models_value_does_not_crash(self) -> None:
        # A 200 body with a truthy non-list at "models" must NOT raise TypeError —
        # ensure_loaded is contracted to never raise (the runner boots on it).
        resolver = models.GrokModelResolver()
        async with _client(lambda r: httpx.Response(200, json={"models": 5})) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"

    async def test_scalar_alias_and_modality_fields_are_ignored(self) -> None:
        payload = {"models": [{"id": "grok-4.3", "aliases": 7, "input_modalities": 9}]}
        resolver = models.GrokModelResolver()
        async with _client(lambda r: httpx.Response(200, json=payload)) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "api"
        assert resolver.selectable_models() == ["grok-4.3"]

    async def test_unexpected_fetch_error_degrades_to_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr("calfcord.providers.grok.models._fetch_catalog", boom)
        resolver = models.GrokModelResolver()
        async with _client(lambda r: httpx.Response(200)) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"

    async def test_unexpected_payload_shape_degrades_to_fallback(self) -> None:
        # A scalar body (neither list nor {"models"/"data": [...]}) yields no
        # models -> both endpoints exhausted -> pinned fallback.
        resolver = models.GrokModelResolver()
        async with _client(lambda r: httpx.Response(200, json=5)) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"

    async def test_invalid_json_body_degrades_to_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<not json>", headers={"content-type": "application/json"})

        resolver = models.GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"


class TestModelsFallbackReason:
    async def test_403_records_fallback_status(self) -> None:
        resolver = models.GrokModelResolver()
        async with _client(lambda r: httpx.Response(403, json={"error": "not eligible"})) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"
        assert resolver.fallback_status == 403

    async def test_network_failure_leaves_status_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        resolver = models.GrokModelResolver()
        async with _client(handler) as client:
            await resolver.ensure_loaded("tok", client=client)
        assert resolver.source == "fallback"
        assert resolver.fallback_status is None


class TestCredentialsEdges:
    async def test_persist_failure_after_rotation_raises_actionable_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_store.save_credentials(
            GrokCredentials(
                access_token=_jwt(exp=time.time() + 60),
                refresh_token="rt-1",
                token_endpoint="https://auth.x.ai/oauth2/token",
            )
        )

        async def fake_refresh(_client: Any, *, refresh_token: str, token_endpoint: str) -> dict[str, Any]:
            return {
                "access_token": _jwt(exp=time.time() + 7200),
                "refresh_token": "rt-2",
                "id_token": "",
                "expires_in": 3600,
                "token_type": "Bearer",
                "last_refresh": "2026-07-20T00:00:00Z",
            }

        def boom(_creds: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("calfcord.providers.grok.oauth.refresh_tokens", fake_refresh)
        monkeypatch.setattr("calfcord.providers.grok.token_store.save_credentials", boom)
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(GrokAuthError) as exc:
                await credentials.resolve_credentials(client=client)
        assert exc.value.code == "xai_refresh_persist_failed"
        assert exc.value.relogin_required is True

    async def test_quarantine_failure_preserves_original_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        token_store.save_credentials(
            GrokCredentials(
                access_token=_jwt(exp=time.time() + 60),
                refresh_token="rt-1",
                token_endpoint="https://auth.x.ai/oauth2/token",
            )
        )

        async def fake_refresh(_client: Any, *, refresh_token: str, token_endpoint: str) -> dict[str, Any]:
            raise GrokAuthError("revoked", code="xai_refresh_failed", relogin_required=True)

        def boom(**_kw: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("calfcord.providers.grok.oauth.refresh_tokens", fake_refresh)
        monkeypatch.setattr("calfcord.providers.grok.token_store.quarantine_credentials", boom)
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(GrokAuthError) as exc:
                await credentials.resolve_credentials(client=client)
        # The disk failure must not mask the actionable auth error.
        assert exc.value.code == "xai_refresh_failed"
        assert exc.value.relogin_required is True

    async def test_refresh_creates_and_closes_own_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        token_store.save_credentials(
            GrokCredentials(
                access_token=_jwt(exp=time.time() + 60),
                refresh_token="rt-1",
                token_endpoint="https://auth.x.ai/oauth2/token",
            )
        )
        new_access = _jwt(exp=time.time() + 7200)

        async def fake_refresh(_client: Any, *, refresh_token: str, token_endpoint: str) -> dict[str, Any]:
            return {
                "access_token": new_access,
                "refresh_token": "rt-2",
                "id_token": "",
                "expires_in": 3600,
                "token_type": "Bearer",
                "last_refresh": "2026-07-20T00:00:00Z",
            }

        monkeypatch.setattr("calfcord.providers.grok.oauth.refresh_tokens", fake_refresh)
        # client=None -> _refresh owns and closes its own AsyncClient (no network,
        # since refresh_tokens is stubbed).
        creds = await credentials.resolve_credentials()
        assert creds.access_token == new_access


class TestModelClientEdges:
    async def test_none_model_settings_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        await client._responses_create(messages=[], stream=False, model_settings=None, model_request_parameters=None)
        assert captured["settings"] is None
