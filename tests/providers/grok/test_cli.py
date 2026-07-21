"""Tests for the ``calfkit-auth grok`` CLI and the unified auth entry point."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from calfcord.providers import auth_cli
from calfcord.providers.grok import cli, token_store
from calfcord.providers.grok.oauth import GrokAuthError
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    return tmp_path / "auth"


def _login_payload(exp: float) -> dict[str, Any]:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return {
        "access_token": f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig",
        "refresh_token": "rt-1",
        "id_token": "",
        "expires_in": 3600,
        "token_type": "Bearer",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
        "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
        "base_url": "https://api.x.ai/v1",
        "last_refresh": "2026-07-20T00:00:00Z",
    }


def _seed_fresh() -> None:
    payload = _login_payload(exp=time.time() + 7200)
    token_store.save_credentials(GrokCredentials.from_login(payload))


class TestParserRegistration:
    @pytest.mark.parametrize("command", ["login", "logout", "status", "refresh", "models"])
    def test_grok_subcommands_parse(self, command: str) -> None:
        parser = auth_cli._build_parser()
        args = parser.parse_args(["grok", command])
        assert args.provider == "grok"
        assert args.command == command

    def test_unified_parser_still_has_codex(self) -> None:
        parser = auth_cli._build_parser()
        args = parser.parse_args(["codex", "status"])
        assert args.provider == "codex"


class TestStatus:
    def test_not_logged_in_returns_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._cmd_status(_ns()) == 1
        assert "grok login" in capsys.readouterr().out

    def test_logged_in_reports_expiry(self, capsys: pytest.CaptureFixture[str]) -> None:
        _seed_fresh()
        assert cli._cmd_status(_ns()) == 0
        assert "Logged in" in capsys.readouterr().out


class TestLogout:
    def test_removes_credentials(self, capsys: pytest.CaptureFixture[str]) -> None:
        _seed_fresh()
        assert cli._cmd_logout(_ns()) == 0
        assert token_store.load_credentials() is None

    def test_noop_when_absent(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._cmd_logout(_ns()) == 0
        assert "Not logged in" in capsys.readouterr().err


class TestLogin:
    async def test_fresh_login_persists_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_login(**_kw: object) -> dict[str, Any]:
            return _login_payload(exp=time.time() + 7200)

        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", fake_login)
        assert await cli._cmd_login(_ns(force=False, no_browser=True)) == 0
        creds = token_store.load_credentials()
        assert creds is not None and creds.refresh_token == "rt-1"

    async def test_already_logged_in_skips_device_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_fresh()

        async def fail_login(**_kw: object) -> dict[str, Any]:
            raise AssertionError("device flow must not run when already logged in")

        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", fail_login)
        assert await cli._cmd_login(_ns(force=False, no_browser=True)) == 0

    async def test_login_failure_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(**_kw: object) -> dict[str, Any]:
            raise GrokAuthError("device timeout", code="device_code_timeout")

        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", boom)
        assert await cli._cmd_login(_ns(force=True, no_browser=True)) == 1


def _ns(**kwargs: object) -> Any:
    import argparse

    return argparse.Namespace(**kwargs)
