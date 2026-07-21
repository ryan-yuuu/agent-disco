"""Tests for the runtime xAI Grok credential resolver.

Resolves a usable access token for the bearer auth and the CLI: reads the store,
refreshes under a cross-process lock when the JWT is expiring, persists the
rotated tokens, and quarantines a dead grant so the next call fails fast.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from calfcord.providers.grok import credentials, token_store
from calfcord.providers.grok.oauth import GrokAuthError
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    return tmp_path / "auth"


def _jwt(exp: float) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


def _seed(access_token: str, refresh_token: str = "rt-1") -> None:
    token_store.save_credentials(
        GrokCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            token_endpoint="https://auth.x.ai/oauth2/token",
        )
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestResolveCredentials:
    async def test_raises_when_not_logged_in(self) -> None:
        with pytest.raises(credentials.GrokNotLoggedInError):
            await credentials.resolve_credentials()

    async def test_fresh_token_is_returned_without_refresh(self) -> None:
        _seed(_jwt(exp=time.time() + 7200))

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not refresh a fresh token")

        async with _client(handler) as client:
            creds = await credentials.resolve_credentials(client=client)
        assert creds.refresh_token == "rt-1"

    async def test_expiring_token_is_refreshed_and_persisted(self) -> None:
        _seed(_jwt(exp=time.time() + 60))
        new_access = _jwt(exp=time.time() + 7200)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2", "expires_in": 3600})

        async with _client(handler) as client:
            creds = await credentials.resolve_credentials(client=client)

        assert creds.access_token == new_access
        assert creds.refresh_token == "rt-2"
        # Rotation is persisted so the next process sees the new refresh token.
        reloaded = token_store.load_credentials()
        assert reloaded is not None and reloaded.refresh_token == "rt-2"

    async def test_force_refresh_refreshes_a_fresh_token(self) -> None:
        _seed(_jwt(exp=time.time() + 7200))
        new_access = _jwt(exp=time.time() + 7200)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})

        async with _client(handler) as client:
            creds = await credentials.resolve_credentials(client=client, force_refresh=True)
        assert creds.access_token == new_access

    async def test_401_quarantines_and_raises_relogin(self) -> None:
        _seed(_jwt(exp=time.time() + 60))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="invalid_grant")

        async with _client(handler) as client:
            with pytest.raises(GrokAuthError) as exc:
                await credentials.resolve_credentials(client=client)
        assert exc.value.relogin_required is True
        # Dead tokens are cleared so the next call fails fast without a retry.
        assert token_store.load_credentials() is None

    async def test_403_tier_denied_does_not_clear_valid_tokens(self) -> None:
        _seed(_jwt(exp=time.time() + 60))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="not eligible")

        async with _client(handler) as client:
            with pytest.raises(GrokAuthError) as exc:
                await credentials.resolve_credentials(client=client)
        assert exc.value.code == "xai_oauth_tier_denied"
        # The tokens are valid (just not allowlisted) — keep them; the error
        # message steers the operator to XAI_API_KEY instead.
        assert token_store.load_credentials() is not None

    async def test_resolve_access_token_returns_bearer_string(self) -> None:
        token = _jwt(exp=time.time() + 7200)
        _seed(token)
        async with _client(lambda r: httpx.Response(500)) as client:
            assert await credentials.resolve_access_token(client=client) == token


class TestLockTimeout:
    async def test_lock_timeout_surfaces_as_grok_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from filelock import Timeout as LockTimeout

        _seed(_jwt(exp=time.time() + 60))  # expiring -> takes the lock path

        class _FakeLock:
            is_locked = False

            def acquire(self) -> None:
                raise LockTimeout("xai_oauth.lock")

            def release(self) -> None:  # pragma: no cover - never held
                raise AssertionError("release must not run when acquire failed")

        monkeypatch.setattr("calfcord.providers.grok.token_store.credential_lock", lambda *a, **k: _FakeLock())
        with pytest.raises(GrokAuthError) as exc:
            await credentials.resolve_credentials()
        assert exc.value.code == "xai_refresh_lock_timeout"


class TestConcurrentRefresh:
    async def test_two_contending_resolvers_refresh_exactly_once(self) -> None:
        # The security-critical invariant: xAI rotates single-use refresh tokens,
        # so two concurrent resolvers on an expiring token must serialize on the
        # file lock — one refreshes, the other re-reads the rotated token. (A
        # regression guard for the filelock thread_local=False fix: acquiring in
        # a to_thread worker and releasing on the loop thread must actually
        # release.)
        _seed(_jwt(exp=time.time() + 60))
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": _jwt(exp=time.time() + 7200),
                    "refresh_token": f"rt-{calls['n'] + 1}",
                    "expires_in": 3600,
                },
            )

        async with _client(handler) as client:
            results = await asyncio.gather(
                credentials.resolve_credentials(client=client),
                credentials.resolve_credentials(client=client),
            )

        assert calls["n"] == 1  # exactly one network refresh
        # Both callers end up on the same rotated token.
        assert results[0].refresh_token == results[1].refresh_token == "rt-2"
