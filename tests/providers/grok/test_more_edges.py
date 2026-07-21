"""Remaining reachable-branch coverage: CLI display paths, store failures."""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from calfcord.providers.grok import cli, credentials, token_store
from calfcord.providers.grok.oauth import GrokAuthError
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _token(exp: float) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


def _seed(exp: float) -> None:
    token_store.save_credentials(
        GrokCredentials(access_token=_token(exp), refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token")
    )


class TestStatusDisplay:
    def test_status_reports_expired_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(exp=time.time() - 100)
        assert cli._cmd_status(_ns()) == 0
        assert "expired" in capsys.readouterr().out

    def test_status_tolerates_opaque_access_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A non-JWT access token has no exp to display; status still succeeds.
        token_store.save_credentials(
            GrokCredentials(
                access_token="opaque-token", refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token"
            )
        )
        assert cli._cmd_status(_ns()) == 0
        assert "Logged in" in capsys.readouterr().out

    def test_status_reports_quarantine_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(exp=time.time() + 7200)
        token_store.quarantine_credentials(code="xai_refresh_failed", message="revoked")
        assert cli._cmd_status(_ns()) == 1
        assert "last error" in capsys.readouterr().out


class TestLoginAndRefreshErrorPaths:
    async def test_login_reports_unusable_cache_then_relogins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(exp=time.time() + 7200)

        async def bad_resolve(**_kw: object) -> object:
            raise GrokAuthError("tier gate", code="xai_oauth_tier_denied")

        async def fake_login(**_kw: object) -> dict[str, Any]:
            return {
                "access_token": _token(time.time() + 7200),
                "refresh_token": "rt-2",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            }

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_credentials", bad_resolve)
        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", fake_login)
        assert await cli._cmd_login(_ns(force=False, no_browser=True)) == 0

    async def test_refresh_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(exp=time.time() + 7200)

        async def boom(**_kw: object) -> object:
            raise GrokAuthError("network down", code="xai_refresh_failed")

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_credentials", boom)
        assert await cli._cmd_refresh(_ns()) == 1

    async def test_refresh_reports_oserror_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A lock-acquisition timeout (OSError) must print "Refresh failed", not a
        # traceback — the handler catches (GrokAuthError, OSError).
        _seed(exp=time.time() + 7200)

        async def boom(**_kw: object) -> object:
            raise OSError("lock timeout")

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_credentials", boom)
        assert await cli._cmd_refresh(_ns()) == 1

    async def test_login_probe_tolerates_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(exp=time.time() + 7200)

        async def boom(**_kw: object) -> object:
            raise OSError("lock timeout")

        async def fake_login(**_kw: object) -> dict[str, Any]:
            return {
                "access_token": _token(time.time() + 7200),
                "refresh_token": "rt-2",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            }

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_credentials", boom)
        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", fake_login)
        # OSError in the cache probe must fall through to a fresh login, not crash.
        assert await cli._cmd_login(_ns(force=False, no_browser=True)) == 0


class TestCredentialsEdges:
    def test_not_logged_in_error_accepts_custom_message(self) -> None:
        err = credentials.GrokNotLoggedInError("custom message")
        assert "custom message" in str(err)
        assert err.relogin_required is True

    async def test_double_checked_skip_when_peer_refreshed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(exp=time.time() + 60)
        calls = {"n": 0}

        def expiring(_token: str, _skew: int = 0) -> bool:
            # Expiring on the pre-lock check, fresh on the post-lock re-read
            # (as if another process refreshed while we waited for the lock).
            calls["n"] += 1
            return calls["n"] == 1

        monkeypatch.setattr("calfcord.providers.grok.credentials.oauth.access_token_is_expiring", expiring)
        creds = await credentials.resolve_credentials()
        assert creds.refresh_token == "rt-1"  # untouched — no refresh happened


class TestTokenStoreFailures:
    def test_load_returns_none_on_incomplete_shape(self) -> None:
        # access/refresh present (passes the emptiness guard) but token_endpoint
        # missing -> GrokCredentials(...) TypeErrors -> treated as logged out.
        path = token_store.credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"access_token": "a", "refresh_token": "b"}), encoding="utf-8")
        assert token_store.load_credentials() is None

    def test_atomic_write_cleans_up_temp_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_src: str, _dst: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("calfcord.providers.grok.token_store.os.replace", boom)
        with pytest.raises(OSError):
            token_store.save_credentials(
                GrokCredentials(access_token="a", refresh_token="b", token_endpoint="https://auth.x.ai/oauth2/token")
            )
        leftovers = list(token_store.credentials_path().parent.glob("*.tmp"))
        assert leftovers == []
