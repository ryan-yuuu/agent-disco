"""Tests for the xAI Grok device-code OAuth flow.

The flow is a faithful port of NousResearch/hermes-agent's ``_xai_oauth_*``
functions (MIT). These tests pin the wire contract (endpoints, grant types,
field validation, error classification) and the security host-pinning that
stops a MITM'd ``token_endpoint`` from ever receiving a refresh token.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest

from calfcord.providers.grok import oauth


def _jwt(exp: float | None = None, **claims: Any) -> str:
    """Build an unsigned JWT-shaped token carrying ``exp`` (+ arbitrary claims)."""
    payload = dict(claims)
    if exp is not None:
        payload["exp"] = exp

    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://auth.x.ai")


_DISCOVERY = {
    "issuer": "https://auth.x.ai",
    "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
    "token_endpoint": "https://auth.x.ai/oauth2/token",
    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
}

_DEVICE_CODE_OK = {
    "device_code": "dev-123",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://accounts.x.ai/device",
    "verification_uri_complete": "https://accounts.x.ai/device?code=ABCD-EFGH",
    "expires_in": 600,
    "interval": 5,
}

_DEVICE_TOKENS = {
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "id_token": "id-1",
    "expires_in": 3600,
    "token_type": "Bearer",
}


class TestConstants:
    def test_pins_the_public_grok_cli_oauth_client(self) -> None:
        assert oauth.XAI_OAUTH_CLIENT_ID == "b1a00492-073a-47ea-816f-4c329264a828"
        assert oauth.XAI_OAUTH_ISSUER == "https://auth.x.ai"
        assert oauth.XAI_OAUTH_DEVICE_CODE_URL == "https://auth.x.ai/oauth2/device/code"
        assert oauth.DEFAULT_XAI_BASE_URL == "https://api.x.ai/v1"

    def test_scope_requests_offline_access_and_api_access(self) -> None:
        # offline_access -> a refresh token; api:access + grok-cli:access ->
        # the bearer is accepted by the inference API.
        for scope in ("openid", "offline_access", "grok-cli:access", "api:access"):
            assert scope in oauth.XAI_OAUTH_SCOPE.split()


class TestValidateOauthEndpoint:
    def test_accepts_https_on_x_ai_apex(self) -> None:
        url = "https://auth.x.ai/oauth2/token"
        assert oauth.validate_oauth_endpoint(url, field="token_endpoint") == url

    def test_accepts_x_ai_subdomain(self) -> None:
        url = "https://oauth2.x.ai/token"
        assert oauth.validate_oauth_endpoint(url, field="token_endpoint") == url

    def test_rejects_non_https(self) -> None:
        with pytest.raises(oauth.GrokAuthError):
            oauth.validate_oauth_endpoint("http://auth.x.ai/oauth2/token", field="token_endpoint")

    def test_rejects_foreign_host(self) -> None:
        with pytest.raises(oauth.GrokAuthError):
            oauth.validate_oauth_endpoint("https://attacker.example/token", field="token_endpoint")

    def test_rejects_lookalike_suffix_host(self) -> None:
        # ``auth.x.ai.evil.com`` must not pass the ``.x.ai`` suffix check.
        with pytest.raises(oauth.GrokAuthError):
            oauth.validate_oauth_endpoint("https://auth.x.ai.evil.com/token", field="token_endpoint")


class TestValidateInferenceBaseUrl:
    def test_accepts_default_api_host(self) -> None:
        assert oauth.validate_inference_base_url(
            "https://api.x.ai/v1", fallback="https://api.x.ai/v1"
        ) == "https://api.x.ai/v1"

    def test_empty_returns_fallback(self) -> None:
        assert oauth.validate_inference_base_url("", fallback="https://api.x.ai/v1") == "https://api.x.ai/v1"

    def test_foreign_host_falls_back_rather_than_leaking_bearer(self) -> None:
        assert oauth.validate_inference_base_url(
            "https://attacker.example/v1", fallback="https://api.x.ai/v1"
        ) == "https://api.x.ai/v1"

    def test_non_https_falls_back(self) -> None:
        assert oauth.validate_inference_base_url(
            "http://api.x.ai/v1", fallback="https://api.x.ai/v1"
        ) == "https://api.x.ai/v1"


class TestExpiryHelpers:
    def test_decode_jwt_exp_returns_claim(self) -> None:
        assert oauth.decode_jwt_exp(_jwt(exp=1234567890)) == 1234567890

    def test_decode_jwt_exp_returns_none_for_opaque_token(self) -> None:
        assert oauth.decode_jwt_exp("not-a-jwt") is None

    def test_expiring_when_exp_within_skew(self) -> None:
        token = _jwt(exp=time.time() + 100)
        assert oauth.access_token_is_expiring(token, skew_seconds=300) is True

    def test_not_expiring_when_far_in_future(self) -> None:
        token = _jwt(exp=time.time() + 10_000)
        assert oauth.access_token_is_expiring(token, skew_seconds=300) is False

    def test_opaque_token_is_not_expiring(self) -> None:
        # No exp claim to reason about -> don't force a refresh on every call.
        assert oauth.access_token_is_expiring("opaque", skew_seconds=300) is False

    def test_skew_is_a_fraction_of_lifetime_with_iat(self) -> None:
        now = time.time()
        token = _jwt(exp=now + 900, iat=now)  # 15-min lifetime
        assert oauth.proactive_refresh_skew_seconds(token) == 180  # 20% of 900

    def test_skew_capped_at_full_window_for_long_token(self) -> None:
        now = time.time()
        token = _jwt(exp=now + 10 * 3600, iat=now)  # 10h lifetime; 20% exceeds the cap
        assert oauth.proactive_refresh_skew_seconds(token) == oauth.REFRESH_SKEW_SECONDS

    def test_skew_floored_at_120_for_tiny_token(self) -> None:
        now = time.time()
        token = _jwt(exp=now + 300, iat=now)  # 20% = 60 -> floored to 120
        assert oauth.proactive_refresh_skew_seconds(token) == 120

    def test_skew_without_iat_uses_fixed_lead(self) -> None:
        token = _jwt(exp=time.time() + 3 * 3600)  # no iat claim
        assert oauth.proactive_refresh_skew_seconds(token) == 300

    def test_fresh_token_does_not_read_as_expiring(self) -> None:
        # Core invariant: a just-issued token must NOT be "expiring", or every
        # resolution would refresh and thrash single-use refresh tokens.
        now = time.time()
        token = _jwt(exp=now + 900, iat=now)
        assert oauth.access_token_is_expiring(token, oauth.proactive_refresh_skew_seconds(token)) is False

    def test_long_token_refreshes_ahead_of_expiry(self) -> None:
        # The wide window is not dead: a 2h-lifetime token with 20min left is
        # inside its ~24min (0.2*7200) refresh window.
        now = time.time()
        token = _jwt(exp=now + 20 * 60, iat=now - 100 * 60)  # lifetime 2h, 20min remaining
        skew = oauth.proactive_refresh_skew_seconds(token)
        assert skew == 1440
        assert oauth.access_token_is_expiring(token, skew) is True


class TestDiscovery:
    async def test_returns_validated_endpoints(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_DISCOVERY)

        async with _client(handler) as client:
            endpoints = await oauth.discover_endpoints(client)
        assert endpoints["token_endpoint"] == "https://auth.x.ai/oauth2/token"
        assert endpoints["authorization_endpoint"] == "https://auth.x.ai/oauth2/authorize"

    async def test_missing_endpoint_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"issuer": "https://auth.x.ai"})

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.discover_endpoints(client)

    async def test_foreign_token_endpoint_is_rejected(self) -> None:
        poisoned = dict(_DISCOVERY, token_endpoint="https://attacker.example/token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=poisoned)

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.discover_endpoints(client)


class TestRequestDeviceCode:
    async def test_posts_client_id_and_scope(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(200, json=_DEVICE_CODE_OK)

        async with _client(handler) as client:
            payload = await oauth.request_device_code(client)
        assert payload["device_code"] == "dev-123"
        assert seen["url"] == oauth.XAI_OAUTH_DEVICE_CODE_URL
        assert f"client_id={oauth.XAI_OAUTH_CLIENT_ID}" in seen["body"]
        assert "grok-cli" in seen["body"]  # url-encoded scope present

    async def test_missing_fields_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"device_code": "x", "user_code": "y"})

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.request_device_code(client)

    async def test_non_200_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad")

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.request_device_code(client)

    async def test_non_numeric_expires_in_raises_grok_auth_error(self) -> None:
        # A present-but-non-integer numeric field must fail as GrokAuthError here,
        # not as a bare ValueError from the poll loop's later int() calls.
        bad = dict(_DEVICE_CODE_OK, expires_in="soon")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=bad)

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                await oauth.request_device_code(client)
        assert exc.value.code == "device_code_invalid"


class TestPollDeviceToken:
    async def test_polls_until_authorized(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json=_DEVICE_TOKENS)

        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        async with _client(handler) as client:
            payload = await oauth.poll_device_token(
                client,
                token_endpoint="https://auth.x.ai/oauth2/token",
                device_code="dev-123",
                expires_in=600,
                interval=5,
                sleep=fake_sleep,
            )
        assert payload["access_token"] == "at-1"
        assert calls["n"] == 3
        assert slept == [5, 5]  # slept once per pending response

    async def test_slow_down_increases_interval(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(400, json={"error": "slow_down"})
            return httpx.Response(200, json=_DEVICE_TOKENS)

        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        async with _client(handler) as client:
            await oauth.poll_device_token(
                client,
                token_endpoint="https://auth.x.ai/oauth2/token",
                device_code="dev-123",
                expires_in=600,
                interval=5,
                sleep=fake_sleep,
            )
        assert slept == [6]  # interval bumped from 5 -> 6 on slow_down

    async def test_missing_refresh_token_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "at-1"})

        async def fake_sleep(seconds: float) -> None:
            return None

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="dev-123",
                    expires_in=600,
                    interval=1,
                    sleep=fake_sleep,
                )

    async def test_hard_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "access_denied"})

        async def fake_sleep(seconds: float) -> None:
            return None

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="dev-123",
                    expires_in=600,
                    interval=1,
                    sleep=fake_sleep,
                )

    async def test_deadline_exceeded_raises_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "authorization_pending"})

        async def fake_sleep(seconds: float) -> None:
            return None

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                # expires_in=0 -> deadline already passed, loop never enters.
                await oauth.poll_device_token(
                    client,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="dev-123",
                    expires_in=0,
                    interval=1,
                    sleep=fake_sleep,
                )
            assert exc.value.code == "device_code_timeout"


class TestRefreshTokens:
    async def test_rotates_refresh_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            assert f"client_id={oauth.XAI_OAUTH_CLIENT_ID}" in body
            return httpx.Response(
                200,
                json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600},
            )

        async with _client(handler) as client:
            updated = await oauth.refresh_tokens(
                client, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token"
            )
        assert updated["access_token"] == "at-2"
        assert updated["refresh_token"] == "rt-2"

    async def test_keeps_old_refresh_token_when_omitted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})

        async with _client(handler) as client:
            updated = await oauth.refresh_tokens(
                client, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token"
            )
        assert updated["refresh_token"] == "rt-1"

    async def test_403_is_tier_denied_not_relogin(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="not eligible")

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                await oauth.refresh_tokens(
                    client, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token"
                )
        assert exc.value.code == "xai_oauth_tier_denied"
        assert exc.value.relogin_required is False

    async def test_401_requires_relogin(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="invalid_grant")

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                await oauth.refresh_tokens(
                    client, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token"
                )
        assert exc.value.relogin_required is True

    async def test_missing_access_token_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"refresh_token": "rt-2"})

        async with _client(handler) as client:
            with pytest.raises(oauth.GrokAuthError):
                await oauth.refresh_tokens(
                    client, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token"
                )

    async def test_empty_refresh_token_requires_relogin(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(oauth.GrokAuthError) as exc:
                await oauth.refresh_tokens(
                    client, refresh_token="  ", token_endpoint="https://auth.x.ai/oauth2/token"
                )
        assert exc.value.relogin_required is True


class TestDeviceCodeLogin:
    async def test_end_to_end_returns_credentials(self) -> None:
        access = _jwt(exp=time.time() + 3600)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("openid-configuration"):
                return httpx.Response(200, json=_DISCOVERY)
            if path.endswith("/device/code"):
                return httpx.Response(
                    200,
                    json={
                        "device_code": "dev-123",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": "https://accounts.x.ai/device",
                        "verification_uri_complete": "https://accounts.x.ai/device?code=ABCD-EFGH",
                        "expires_in": 600,
                        "interval": 1,
                    },
                )
            if path.endswith("/oauth2/token"):
                return httpx.Response(
                    200,
                    json={"access_token": access, "refresh_token": "rt-1", "expires_in": 3600},
                )
            return httpx.Response(404)

        printed: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            return None

        async with _client(handler) as client:
            creds = await oauth.device_code_login(
                client=client,
                open_browser=False,
                printer=printed.append,
                sleep=fake_sleep,
            )

        assert creds["access_token"] == access
        assert creds["refresh_token"] == "rt-1"
        assert creds["token_endpoint"] == "https://auth.x.ai/oauth2/token"
        assert creds["base_url"] == "https://api.x.ai/v1"
        # The user is shown the verification URL + code.
        assert any("ABCD-EFGH" in line for line in printed)
