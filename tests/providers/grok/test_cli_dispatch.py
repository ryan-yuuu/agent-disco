"""Dispatch-routing coverage for the grok CLI and the unified auth entry point."""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from calfcord.providers import auth_cli
from calfcord.providers.grok import cli, token_store
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _seed_fresh() -> None:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none'})}.{b64({'exp': time.time() + 7200})}.sig"
    token_store.save_credentials(
        GrokCredentials(access_token=token, refresh_token="rt-1", token_endpoint="https://auth.x.ai/oauth2/token")
    )


class TestGrokDispatch:
    def test_status_route(self) -> None:
        assert cli.dispatch(_ns(command="status")) == 1

    def test_logout_route(self) -> None:
        assert cli.dispatch(_ns(command="logout")) == 0

    def test_models_route_lists_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.dispatch(_ns(command="models")) == 0
        assert "grok-4.3" in capsys.readouterr().out

    def test_refresh_route_not_logged_in(self) -> None:
        assert cli.dispatch(_ns(command="refresh")) == 1

    def test_login_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_login(**_kw: object) -> dict[str, Any]:
            def b64(obj: dict[str, Any]) -> str:
                return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

            token = f"{b64({'alg': 'none'})}.{b64({'exp': time.time() + 7200})}.sig"
            return {
                "access_token": token,
                "refresh_token": "rt-1",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
                "base_url": "https://api.x.ai/v1",
            }

        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", fake_login)
        assert cli.dispatch(_ns(command="login", force=True, no_browser=True)) == 0
        assert token_store.load_credentials() is not None

    def test_refresh_route_forces_refresh_when_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_fresh()

        async def fake_resolve(**kwargs: object) -> object:
            assert kwargs.get("force_refresh") is True
            return token_store.load_credentials()

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_credentials", fake_resolve)
        assert cli.dispatch(_ns(command="refresh")) == 0

    def test_unknown_command_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown grok command"):
            cli.dispatch(_ns(command="bogus"))


class TestStandaloneMain:
    def test_grok_main_status(self) -> None:
        assert cli.main(["grok", "status"]) == 1


class TestUnifiedEntry:
    def test_routes_to_grok(self) -> None:
        assert auth_cli.main(["grok", "status"]) == 1

    def test_routes_to_codex(self) -> None:
        # codex status returns 1 when not logged in; asserting it dispatches.
        assert auth_cli.main(["codex", "status"]) == 1
